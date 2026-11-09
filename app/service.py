"""Persistent, read-only public dashboard API and scheduled source updates."""
import asyncio
import csv
import html
import io
import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, closing, contextmanager, nullcontext
from datetime import date, datetime, time as day_time, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from scripts.fetch_toss import TossClient, audit
from app.kind import KIND_URL, kind_form, last_trades, parse_declared, parse_trades
from app.klines import aggregate, history_url, latest_days, nxt_minute, nxt_series, parse_daily, parse_minutes
from app.prices import chart_prices, nxt_after_price, live_prices, KST, LIVE_URL

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.getenv("DASHBOARD_DATA_DIR", ROOT / "runtime"))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "dashboard.sqlite3"
STOCKS = [
    {"code": "A000660", "symbol": "000660", "name": "SK 海力士", "english": "SK hynix", "color": "#EA5B0C", "company": "000660"},
    {"code": "A005930", "symbol": "005930", "name": "三星电子", "english": "Samsung Electronics", "color": "#1428A0", "company": "005930"},
]
LOCK = threading.Lock()
PRICE_LOCK = threading.Lock()
PRICE_NEXT_CHECK = None
LIVE_LOCK = threading.Lock()
LIVE_NEXT_CHECK = None
LIVE_INTERVAL = max(1, float(os.getenv("LIVE_UPDATE_SECONDS", "3")))
LIVE_FAILURES = 0
LIVE_OFF_HOURS = float(os.getenv("LIVE_OFF_HOURS_SECONDS", "300"))
# NXT trades 08:00-20:00 KST, the widest session; a margin covers both ends.
LIVE_WINDOW = (day_time(7, 50), day_time(20, 10))
FLOW_LOCK = threading.Lock()
BEIJING = timezone(timedelta(hours=8))
FLOW_WINDOW = (day_time(14, 30), day_time(14, 45))
KLINE_HISTORY_START = date(1990, 1, 1)
KLINE_FULL_DAYS = {}
NXT_KEEP_DAYS = 30
KLINE_PERIODS = {"minute": "minute", "five": "minute5", "day": "day", "week": "day", "month": "day"}
LOG = logging.getLogger("dashboard")
TRANSLATION_RETRY_AT = 0
TASKS = []


def stamp():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connection():
    db = sqlite3.connect(DB, timeout=15)
    db.row_factory = sqlite3.Row
    # WAL stays consistent without an fsync per commit; quotes commit every second.
    db.execute("PRAGMA synchronous=NORMAL")
    try:
        yield db
        db.commit()
    finally:
        db.close()


def initialize():
    with connection() as db:
        db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS flows(code TEXT,date TEXT,updated TEXT,payload TEXT,PRIMARY KEY(code,date));
        CREATE TABLE IF NOT EXISTS documents(code TEXT,id TEXT,kind TEXT,date TEXT,payload TEXT,PRIMARY KEY(code,id,kind));
        CREATE TABLE IF NOT EXISTS translations(original TEXT PRIMARY KEY,zh TEXT);
        CREATE TABLE IF NOT EXISTS translation_usage(day TEXT PRIMARY KEY,characters INTEGER);
        CREATE TABLE IF NOT EXISTS status(module TEXT PRIMARY KEY,success TEXT,attempt TEXT,error TEXT);
        CREATE TABLE IF NOT EXISTS regular_closes(code TEXT,date TEXT,payload TEXT,PRIMARY KEY(code,date));
        CREATE TABLE IF NOT EXISTS market_quotes(code TEXT,market TEXT,payload TEXT,PRIMARY KEY(code,market));
        CREATE TABLE IF NOT EXISTS live_quotes(code TEXT,market TEXT,payload TEXT,PRIMARY KEY(code,market));
        CREATE TABLE IF NOT EXISTS nxt_minutes(code TEXT,at TEXT,close INTEGER,PRIMARY KEY(code,at));
        CREATE TABLE IF NOT EXISTS repurchase_progress(code TEXT,declared TEXT,payload TEXT,PRIMARY KEY(code,declared));
        CREATE TABLE IF NOT EXISTS kline_bars(code TEXT,period TEXT,at TEXT,open INTEGER,high INTEGER,low INTEGER,close INTEGER,volume INTEGER,PRIMARY KEY(code,period,at));
        """)
    for stock in STOCKS:
        file = ROOT / "data/live" / (stock["code"] + ".json")
        if file.exists():
            payload = json.loads(file.read_text(encoding="utf-8"))
            audit(payload["rows"])
            put_flows(stock["code"], payload["rows"], seed=True)
    price_seed = ROOT / "data/regular-price-seed.json"
    if price_seed.exists():
        with connection() as db:
            for row in json.loads(price_seed.read_text(encoding="utf-8")):
                db.execute("INSERT OR IGNORE INTO regular_closes VALUES(?,?,?)", (row["code"], row["date"], json.dumps(row, ensure_ascii=False)))
    overrides = ROOT / "data/title-overrides.json"
    if overrides.exists():
        with connection() as db:
            for original, zh in json.loads(overrides.read_text(encoding="utf-8")).items():
                db.execute("INSERT OR REPLACE INTO translations VALUES(?,?)", (original, zh))
                db.execute("UPDATE documents SET payload=json_set(payload,'$.titleZh',?) WHERE json_extract(payload,'$.originalTitle')=?", (zh, original))


def put_flows(code, rows, *, seed=False):
    # Bundled initial data must never overwrite a later source correction,
    # including corrections which reuse the same upstream updatedAt timestamp.
    conflict = "DO NOTHING" if seed else "DO UPDATE SET updated=excluded.updated,payload=excluded.payload WHERE excluded.updated>=flows.updated"
    with connection() as db:
        db.executemany("INSERT INTO flows VALUES(?,?,?,?) ON CONFLICT(code,date) " + conflict,
        [(code, r["baseDate"], r["updatedAt"], json.dumps(r, ensure_ascii=False)) for r in rows])


def module_status(module, error=None, db=None):
    now = stamp()
    with nullcontext(db) if db else connection() as db:
        if error:
            db.execute("""INSERT INTO status VALUES(?,NULL,?,?) ON CONFLICT(module)
            DO UPDATE SET attempt=excluded.attempt,error=excluded.error""", (module, now, str(error)[:250]))
        else:
            db.execute("""INSERT INTO status VALUES(?,?,?,NULL) ON CONFLICT(module)
            DO UPDATE SET success=excluded.success,attempt=excluded.attempt,error=NULL""", (module, now, now))


def read_json(url, headers=None, encoding="utf-8"):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; KoreaDashboard/0.1)", **(headers or {})})
    with urlopen(request, timeout=15) as response:
        data = response.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("source response exceeds size limit")
        return json.loads(data.decode(encoding))


def save_market_quote(code, quote, db=None):
    if quote:
        with nullcontext(db) if db else connection() as db:
            db.execute("""INSERT INTO market_quotes VALUES(?,?,?) ON CONFLICT(code,market)
            DO UPDATE SET payload=excluded.payload
            WHERE json_extract(excluded.payload,'$.asOf')>=json_extract(market_quotes.payload,'$.asOf')""",
                       (code, quote["exchange"], json.dumps(quote, ensure_ascii=False)))


def save_regular_prices(code, rows):
    with connection() as db:
        for row in rows:
            cached = db.execute("SELECT payload FROM regular_closes WHERE code=? AND date=?", (code, row["date"])).fetchone()
            # A previous-day chart does not repeat that day's comparison base.
            merged = {**(json.loads(cached[0]) if cached else {}), **row}
            db.execute("INSERT OR REPLACE INTO regular_closes VALUES(?,?,?)", (code, row["date"], json.dumps(merged, ensure_ascii=False)))


def refresh_prices(stock):
    code, symbol = stock["code"], stock["symbol"]
    try:
        chart = read_json(f"https://api.stock.naver.com/chart/domestic/item/{symbol}?periodType=day")
        regular, after = chart_prices(symbol, chart, datetime.now(timezone.utc))
        save_regular_prices(code, regular)
        save_market_quote(code, after)
        module_status(code + ":prices")
    except Exception as error:
        LOG.exception("KRX price refresh failed: %s", code)
        module_status(code + ":prices", error)
    try:
        payload = read_json(f"https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{symbol}", encoding="euc-kr")
        save_market_quote(code, nxt_after_price(symbol, payload, datetime.now(timezone.utc)))
        module_status(code + ":after_nxt")
    except Exception as error:
        LOG.exception("NXT after-market refresh failed: %s", code)
        module_status(code + ":after_nxt", error)


def price_interval():
    return max(30, int(os.getenv("PRICE_UPDATE_SECONDS", "60")))


def refresh_live_prices():
    global LIVE_FAILURES
    if not LIVE_LOCK.acquire(blocking=False):
        return
    failed = False
    try:
        payload = read_json(LIVE_URL)
        now = datetime.now(timezone.utc)
        # One transaction per cycle: this runs every second during trading hours.
        with connection() as db:
            for stock in STOCKS:
                try:
                    quotes = live_prices(stock["symbol"], payload, now)
                    for quote in quotes:
                        db.execute("""INSERT INTO live_quotes VALUES(?,?,?) ON CONFLICT(code,market)
                        DO UPDATE SET payload=excluded.payload
                        WHERE json_extract(excluded.payload,'$.asOf')>=json_extract(live_quotes.payload,'$.asOf')""",
                                   (stock["code"], quote["exchange"], json.dumps(quote, ensure_ascii=False)))
                    for quote in quotes:
                        if quote["session"] == "AFTER_MARKET":
                            save_market_quote(stock["code"], {**quote, "granularity": "quote"}, db)
                        minute = nxt_minute(quote)
                        if minute:
                            db.execute("INSERT OR REPLACE INTO nxt_minutes VALUES(?,?,?)", (stock["code"], minute, quote["price"]))
                    module_status(stock["code"] + ":live", db=db)
                except Exception as error:
                    failed = True
                    LOG.exception("live quote parse failed: %s", stock["code"])
                    module_status(stock["code"] + ":live", error, db)
    except Exception as error:
        failed = True
        LOG.exception("live quote request failed")
        for stock in STOCKS:
            module_status(stock["code"] + ":live", error)
    finally:
        LIVE_FAILURES = LIVE_FAILURES + 1 if failed else 0
        LIVE_LOCK.release()


def in_trading_window(now):
    # Weekdays only; exchange holidays are not modelled and keep the fast cadence.
    local = now.astimezone(KST)
    return local.weekday() < 5 and LIVE_WINDOW[0] <= local.time() <= LIVE_WINDOW[1]


def live_interval(now=None):
    if in_trading_window(now or datetime.now(timezone.utc)):
        return LIVE_INTERVAL
    return max(LIVE_INTERVAL, LIVE_OFF_HOURS)


def live_delay(now):
    # Wake exactly when the fast window opens instead of oversleeping into it.
    local = now.astimezone(KST)
    day = local.date()
    while day.weekday() >= 5 or datetime.combine(day, LIVE_WINDOW[0], KST) <= local:
        day += timedelta(days=1)
    return min(live_interval(local), (datetime.combine(day, LIVE_WINDOW[0], KST) - local).total_seconds())


def live_backoff():
    # We poll faster than the source's suggested interval, so failures must slow us down.
    return min(60, LIVE_INTERVAL * 2 ** min(LIVE_FAILURES, 8)) if LIVE_FAILURES else 0


def live_polling(status):
    rows = [s for s in status if s["module"].endswith(":live")]
    return {"intervalSeconds": live_interval(), "inProgress": LIVE_LOCK.locked(), "nextCheckAt": LIVE_NEXT_CHECK,
            "revision": max((s["attempt"] or "" for s in rows), default=""),
            "lastSuccess": max((s["success"] or "" for s in rows), default="")}


async def live_updater():
    global LIVE_NEXT_CHECK
    try:
        while True:
            started = time.monotonic()
            now = datetime.fromtimestamp(time.time(), timezone.utc)
            LIVE_NEXT_CHECK = (now + timedelta(seconds=live_delay(now))).isoformat()
            try:
                await asyncio.to_thread(refresh_live_prices)
            except Exception:
                LOG.exception("scheduled live quote refresh failed")
            delay = max(0.1, max(live_delay(now), live_backoff()) - (time.monotonic() - started))
            LIVE_NEXT_CHECK = datetime.fromtimestamp(time.time() + delay, timezone.utc).isoformat()
            await asyncio.sleep(delay)
    finally:
        LIVE_NEXT_CHECK = None


def save_bars(code, period, bars, replace):
    with connection() as db:
        if replace:
            db.execute("DELETE FROM kline_bars WHERE code=? AND period=?", (code, period))
        db.executemany("INSERT OR REPLACE INTO kline_bars VALUES(?,?,?,?,?,?,?,?)", [(code, period, *bar) for bar in bars])


def refresh_klines(stock):
    code, symbol = stock["code"], stock["symbol"]
    now = datetime.now(KST)
    today = now.date()
    # The source rewrites history after splits, so reload all of it once a day.
    full = KLINE_FULL_DAYS.get(code) != today
    daily = parse_daily(read_json(history_url(symbol, "day", KLINE_HISTORY_START if full else today - timedelta(days=40), today)), today)
    if full:
        with connection() as db:
            stored = db.execute("SELECT count(*) FROM kline_bars WHERE code=? AND period='day'", (code,)).fetchone()[0]
        if len(daily) < stored * 0.8:
            raise ValueError("daily history shrank unexpectedly")
    save_bars(code, "day", daily, replace=full)
    if full:
        KLINE_FULL_DAYS[code] = today
    latest = date.fromisoformat(daily[-1][0])
    for period, start in (("minute", latest), ("minute5", latest - timedelta(days=14))):
        bars = parse_minutes(read_json(history_url(symbol, period, start, latest)), now)
        if bars:  # Nothing yet for a session that has just opened.
            save_bars(code, period, bars, replace=True)


def refresh_market_klines():
    with connection() as db:
        db.execute("DELETE FROM nxt_minutes WHERE at<?", ((datetime.now(KST).date() - timedelta(days=NXT_KEEP_DAYS)).isoformat(),))
    for stock in STOCKS:
        try:
            refresh_klines(stock)
            module_status(stock["code"] + ":kline")
        except Exception as error:
            LOG.exception("K-line refresh failed: %s", stock["code"])
            module_status(stock["code"] + ":kline", error)


def kline_interval(now=None):
    return max(30, int(os.getenv("KLINE_UPDATE_SECONDS", "60"))) if in_trading_window(now or datetime.now(timezone.utc)) else 1800


async def kline_updater():
    while True:
        started = time.monotonic()
        try:
            await asyncio.to_thread(refresh_market_klines)
        except Exception:
            LOG.exception("scheduled K-line refresh failed")
        await asyncio.sleep(max(1, kline_interval() - (time.monotonic() - started)))


def kind_interval():
    return max(3600, int(os.getenv("KIND_UPDATE_SECONDS", "21600")))


def repurchase_batches():
    return json.loads((ROOT / "data/repurchases.json").read_text(encoding="utf-8"))


def read_kind(form):
    request = Request(KIND_URL, data=urlencode(form).encode(), headers={
        "User-Agent": "Mozilla/5.0 (compatible; KoreaDashboard/0.1)", "Referer": KIND_URL + "?method=loadInitPage", "X-Requested-With": "XMLHttpRequest"})
    with urlopen(request, timeout=30) as response:
        data = response.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("source response exceeds size limit")
        return data.decode("utf-8")


def refresh_repurchase(stock):
    code = stock["code"]
    expected = {batch["announcedAt"] for batch in repurchase_batches() if batch["code"] == code}
    if not expected:
        return
    today, start = datetime.now(KST).date(), min(expected)
    declared = parse_declared(read_kind(kind_form(code, "decl", start, today.isoformat())), code)
    # A hundred rows per page; the newest trade of a batch that finished long ago stays stored.
    trades = parse_trades(read_kind(kind_form(code, "trd", max(start, (today - timedelta(days=120)).isoformat()), today.isoformat())), code)
    latest = last_trades(declared, trades)
    with connection() as db:
        saved = {row[0]: json.loads(row[1]) for row in db.execute("SELECT declared,payload FROM repurchase_progress WHERE code=?", (code,))}
        for batch in declared:
            prior = saved.get(batch["declaredOn"], {})
            # A cumulative total only grows; a drop means a misread page or a restatement to check by hand.
            if batch["tradedKrw"] < prior.get("tradedKrw", 0):
                raise ValueError(f"KIND cumulative amount fell: {batch['declaredOn']}")
            batch.update(lastTradeOn=latest[batch["declaredOn"]] or prior.get("lastTradeOn"), fetchedAt=stamp())
        db.executemany("INSERT OR REPLACE INTO repurchase_progress VALUES(?,?,?)",
                       [(code, batch["declaredOn"], json.dumps(batch, ensure_ascii=False)) for batch in declared])
    # Batches are kept by hand, so a new or mistyped one must show up as a warning.
    found = {batch["declaredOn"] for batch in declared}
    if found != expected:
        raise ValueError(f"KIND and repurchases.json disagree: {sorted(found ^ expected)}")


def refresh_market_repurchases():
    for stock in STOCKS:
        try:
            refresh_repurchase(stock)
            module_status(stock["code"] + ":repurchase")
        except Exception as error:
            LOG.exception("repurchase refresh failed: %s", stock["code"])
            module_status(stock["code"] + ":repurchase", error)


async def repurchase_updater():
    while True:
        started = time.monotonic()
        try:
            await asyncio.to_thread(refresh_market_repurchases)
        except Exception:
            LOG.exception("scheduled repurchase refresh failed")
        await asyncio.sleep(max(1, kind_interval() - (time.monotonic() - started)))


def repurchases_with_progress():
    batches = repurchase_batches()
    with connection() as db:
        saved = {(row[0], row[1]): json.loads(row[2]) for row in db.execute("SELECT code,declared,payload FROM repurchase_progress")}
    today = datetime.now(KST).date().isoformat()
    for batch in batches:
        found = saved.get((batch["code"], batch["announcedAt"]))
        if not found:
            continue
        # By amount: the declared share count comes from the price on the decision day.
        progress = round(min(100, found["tradedKrw"] / batch["amountKrw"] * 100), 2)
        if batch["status"] == "执行中":
            batch["status"] = "购入完成" if progress >= 100 else "执行期结束" if found["periodEnd"] < today else "执行中"
        batch.update(purchasedKrw=found["tradedKrw"], progress=progress, purchasedShares=found["tradedShares"], plannedShares=found["plannedShares"],
                     asOf=found["lastTradeOn"] or batch["asOf"], kindFetchedAt=found["fetchedAt"], kindUrl=KIND_URL + "?method=loadInitPage")
    return batches


def refresh_market_prices():
    # Price collection must continue even while news/translation is slow.
    if not PRICE_LOCK.acquire(blocking=False):
        return
    try:
        for stock in STOCKS:
            refresh_prices(stock)
    finally:
        PRICE_LOCK.release()


def price_polling(status):
    prices = [s for s in status if s["module"].endswith((":prices", ":after_nxt"))]
    return {"intervalSeconds": price_interval(), "inProgress": PRICE_LOCK.locked(),
            "nextCheckAt": PRICE_NEXT_CHECK,
            "revision": max((s["attempt"] or "" for s in prices), default=""),
            "lastSuccess": max((s["success"] or "" for s in prices), default="")}


async def price_updater():
    global PRICE_NEXT_CHECK
    try:
        while True:
            started = time.monotonic()
            due = time.time() + price_interval()
            PRICE_NEXT_CHECK = datetime.fromtimestamp(due, timezone.utc).isoformat()
            try:
                await asyncio.to_thread(refresh_market_prices)
            except Exception:
                LOG.exception("scheduled price refresh failed")
            # Schedule start-to-start; a slow request never overlaps the next run.
            delay = max(1, price_interval() - (time.monotonic() - started))
            PRICE_NEXT_CHECK = datetime.fromtimestamp(time.time() + delay, timezone.utc).isoformat()
            await asyncio.sleep(delay)
    finally:
        PRICE_NEXT_CHECK = None


def translate(title):
    global TRANSLATION_RETRY_AT
    with connection() as db:
        row = db.execute("SELECT zh FROM translations WHERE original=?", (title,)).fetchone()
        if row:
            return row[0]
    # Stay below MyMemory's anonymous 5,000-character daily allowance. Do not
    # retry a failing provider for every headline or restart the quota on deploy.
    if time.monotonic() < TRANSLATION_RETRY_AT or len(title.encode("utf-8")) > 500:
        return None
    today = stamp()[:10]
    with connection() as db:
        used = db.execute("SELECT characters FROM translation_usage WHERE day=?", (today,)).fetchone()
        if (used[0] if used else 0) + len(title) > 4500:
            return None
        db.execute("INSERT INTO translation_usage VALUES(?,?) ON CONFLICT(day) DO UPDATE SET characters=characters+excluded.characters", (today, len(title)))
    # Only public headlines are sent, never article text or personal data.
    language = "ko" if re.search("[\uac00-\ud7a3]", title) else "en"
    query = urlencode({"q": title, "langpair": language + "|zh"})
    try:
        result = read_json("https://api.mymemory.translated.net/get?" + query)
        zh = (result.get("responseData", {}).get("translatedText") or "").strip()
        if result.get("responseStatus") != 200 or zh.upper().startswith("MYMEMORY WARNING") or result.get("quotaFinished"):
            raise ValueError("translation unavailable or quota exhausted")
        if not zh:
            # A new segment can have no match yet; keep other headlines moving.
            return None
        zh = html.unescape(zh)
        with connection() as db:
            db.execute("INSERT OR REPLACE INTO translations VALUES(?,?)", (title, zh))
        return zh
    except Exception:
        LOG.warning("title translation unavailable")
        TRANSLATION_RETRY_AT = time.monotonic() + 1800
        return None


def source_api(client, path):
    data = read_json("https://wts-info-api.tossinvest.com" + path, {"Cookie": "deviceId=" + client.device})
    if "result" not in data:
        raise ValueError("source response has no result")
    return data["result"]


def safe_link(url):
    if isinstance(url, str) and len(url) < 3000 and urlsplit(url).scheme in {"https", "http"} and urlsplit(url).hostname:
        return url
    return None


def refresh_documents(client, stock, kind):
    code, company = stock["code"], stock["company"]
    path = (f"/api/v2/news/companies/{company}?size=12&number=1&orderBy=latest" if kind == "news"
            else f"/api/v1/stock-detail/companies/{company}/filings?size=30&number=1")
    rows = source_api(client, path)["body"]
    for item in rows:
        with connection() as db:
            cached = db.execute("SELECT payload FROM documents WHERE code=? AND id=? AND kind=?", (code, item["id"], kind)).fetchone()
        previous = json.loads(cached[0]) if cached else None
        if previous and previous.get("originalTitle") == item["title"] and previous.get("titleZh"):
            continue
        link, publisher = None, None
        if kind == "news":
            detail = source_api(client, "/api/v2/news/" + quote(item["id"], safe=""))
            localized = detail.get("kr") or detail.get("en") or {}
            link = safe_link(localized.get("linkUrl"))
            publisher = item.get("source", {}).get("name", "媒体报道")
            # Only keep headlines and source links, not copyrighted article bodies.
            if not link:
                continue
        else:
            report = item.get("reportId", "")
            if report.startswith("DART:") and "-" in report:
                report_number = report.rsplit("-", 1)[-1]
                if report_number.isdigit() and len(report_number) == 14:
                    link = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + report_number
            publisher = "DART 公告" if link else "公司 IR 公告索引"
            link = link or ("https://www.skhynix.com/ir/UI-FR-IR12_T3/" if code == "A000660" else "https://www.samsung.com/global/ir/reports-disclosures/public-disclosure/")
        record = {"id": item["id"], "code": code, "kind": kind, "originalTitle": item["title"],
                  "titleZh": translate(item["title"]), "publishedAt": item["createdAt"] + ("+09:00" if "+" not in item["createdAt"] and not item["createdAt"].endswith("Z") else ""),
                  "publisher": publisher, "url": link, "indexedLink": publisher == "公司 IR 公告索引"}
        with connection() as db:
            db.execute("INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)", (code, item["id"], kind, record["publishedAt"], json.dumps(record, ensure_ascii=False)))
        time.sleep(0.15)


def flow_history_days(code, today):
    with connection() as db:
        count, latest = db.execute("SELECT count(*),max(date) FROM flows WHERE code=?", (code,)).fetchone()
    if count < 120:
        return 120
    # Five days catch source revisions. Calendar days since the newest row always
    # cover the trading days missed during an outage, so history cannot silently gap.
    return 5 + max(0, (today - date.fromisoformat(latest)).days)


def refresh_flows(client):
    # A fetch already running saves the same rows, so it counts as success.
    if not FLOW_LOCK.acquire(blocking=False):
        return True
    try:
        ok = True
        for stock in STOCKS:
            code = stock["code"]
            try:
                payload = client.history(code, days=flow_history_days(code, datetime.now(KST).date()))
                put_flows(code, payload["rows"])
                module_status(code + ":flows")
            except Exception as error:
                LOG.exception("flow refresh failed: %s", code)
                module_status(code + ":flows", error)
                ok = False
        return ok
    finally:
        FLOW_LOCK.release()


def refresh_all():
    if not LOCK.acquire(blocking=False):
        return
    try:
        client = TossClient(DATA / "state")
        refresh_flows(client)
        # Translate current news for both companies before historical filings.
        for kind in ("news", "filings"):
            for stock in STOCKS:
                code = stock["code"]
                try:
                    refresh_documents(client, stock, kind)
                    module_status(code + ":" + kind)
                except Exception as error:
                    LOG.exception("document refresh failed: %s %s", code, kind)
                    module_status(code + ":" + kind, error)
        # Online SQLite backup is consistent even while public requests read WAL.
        backup = DATA / "backups"
        backup.mkdir(exist_ok=True)
        with connection() as db, closing(sqlite3.connect(backup / ("dashboard-" + stamp()[:10] + ".sqlite3"))) as target:
            db.backup(target)
        backups = sorted(backup.glob("dashboard-*.sqlite3"))
        for file in backups[:-14]:
            file.unlink()
    finally:
        LOCK.release()


def next_update_delay(delay):
    with connection() as db:
        pending = sum(not json.loads(row[0]).get("titleZh") for stock in STOCKS for row in db.execute(
            "SELECT payload FROM documents WHERE code=? AND kind='news' ORDER BY date DESC LIMIT 12", (stock["code"],)))
        usage = db.execute("SELECT characters FROM translation_usage WHERE day=?", (stamp()[:10],)).fetchone()
    if pending and (usage[0] if usage else 0) < 4000 and time.monotonic() >= TRANSLATION_RETRY_AT:
        return min(delay, 120)
    return delay


async def updater():
    while True:
        try:
            await asyncio.to_thread(refresh_all)
        except Exception:
            LOG.exception("scheduled refresh failed")
        delay = int(os.getenv("UPDATE_SECONDS", "1800"))
        try:
            # An early retry is optional; a failure here must not end the updater.
            delay = await asyncio.to_thread(next_update_delay, delay)
        except Exception:
            LOG.exception("update delay check failed")
        await asyncio.sleep(delay)


def flow_window_delay(now):
    # Seconds to the next whole minute in the window, both ends included; weekdays only.
    local = now.astimezone(BEIJING)
    minute = local.replace(second=0, microsecond=0) + timedelta(minutes=1)
    if minute.weekday() < 5 and FLOW_WINDOW[0] <= minute.time() <= FLOW_WINDOW[1]:
        return (minute - local).total_seconds()
    day = local.date()
    while day.weekday() >= 5 or datetime.combine(day, FLOW_WINDOW[0], BEIJING) <= local:
        day += timedelta(days=1)
    return (datetime.combine(day, FLOW_WINDOW[0], BEIJING) - local).total_seconds()


def refresh_window_flows():
    return refresh_flows(TossClient(DATA / "state"))


async def flow_updater():
    # Separate from updater so slow news translation cannot hold up these fetches.
    failures = 0
    while True:
        # Measured after each fetch, so a slow one skips a slot instead of overlapping.
        delay = flow_window_delay(datetime.now(timezone.utc))
        # Toss must not be retried every minute once it starts rejecting requests.
        await asyncio.sleep(max(delay, min(480, 60 * 2 ** failures)) if failures else delay)
        try:
            ok = await asyncio.to_thread(refresh_window_flows)
        except Exception:
            LOG.exception("scheduled flow window refresh failed")
            ok = False
        failures = 0 if ok else failures + 1


@asynccontextmanager
async def lifespan(app):
    initialize()
    if os.getenv("DISABLE_UPDATER") != "1":
        TASKS[:] = [asyncio.create_task(job(), name=job.__name__) for job in (updater, price_updater, live_updater, kline_updater, flow_updater, repurchase_updater)]
    try:
        yield
    finally:
        for task in TASKS:
            task.cancel()
        await asyncio.gather(*TASKS, return_exceptions=True)
        TASKS.clear()


app = FastAPI(title="韩国半导体观察", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)


@app.middleware("http")
async def headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    path = request.url.path
    response.headers["Cache-Control"] = "public, max-age=300" if path.startswith("/assets/") else "public, max-age=15" if path.startswith("/api/kline/") else "no-cache"
    return response


def stale_tolerance(module):
    if module.endswith(":live"):
        return max(live_interval() * 4, 30)
    if module.endswith((":prices", ":after_nxt")):
        return max(price_interval() * 3, 180)
    if module.endswith(":repurchase"):
        return kind_interval() * 3
    return max(int(os.getenv("UPDATE_SECONDS", "1800")) * 3, 7200)


@app.get("/api/health")
def health():
    with connection() as db:
        rows = db.execute("SELECT count(*) FROM flows").fetchone()[0]
        status = db.execute("SELECT module,success,error FROM status ORDER BY module").fetchall()
    now = datetime.now(timezone.utc)
    # At one request per second a single failed live poll is noise; only a lasting gap counts.
    stale = [s["module"] for s in status if (s["error"] and not s["module"].endswith(":live")) or not s["success"]
             or (now - datetime.fromisoformat(s["success"])).total_seconds() > stale_tolerance(s["module"])]
    # Only a dead task fails the check: restarting cannot fix an unavailable source.
    dead = [task.get_name() for task in TASKS if task.done()]
    return JSONResponse({"ok": not dead, "deadTasks": dead, "dataOk": not stale, "staleModules": stale,
                         "storedTradingDays": rows, "refreshInProgress": LOCK.locked(),
                         "priceRefreshInProgress": PRICE_LOCK.locked()}, status_code=503 if dead else 200)


@app.get("/api/price-status")
def price_status():
    with connection() as db:
        status = [dict(row) for row in db.execute("SELECT * FROM status WHERE module LIKE '%:prices' OR module LIKE '%:after_nxt'")]
    return price_polling(status)


@app.get("/api/quotes")
def quotes():
    with connection() as db:
        status = [dict(row) for row in db.execute("SELECT * FROM status WHERE module LIKE '%:live'")]
        stocks = [{"code": stock["code"], "liveQuotes": [json.loads(r[0]) for r in db.execute(
            "SELECT payload FROM live_quotes WHERE code=? ORDER BY market", (stock["code"],))],
            "afterHours": [json.loads(r[0]) for r in db.execute("SELECT payload FROM market_quotes WHERE code=? ORDER BY market", (stock["code"],))]} for stock in STOCKS]
    return {"stocks": stocks, "status": status, "livePolling": live_polling(status), "servedAt": stamp()}


@app.get("/api/dashboard")
def dashboard():
    stocks = []
    with connection() as db:
        status = [dict(row) for row in db.execute("SELECT * FROM status")]
        for config in STOCKS:
            rows = [json.loads(r[0]) for r in db.execute("SELECT payload FROM flows WHERE code=? ORDER BY date", (config["code"],))]
            regular = [json.loads(r[0]) for r in db.execute("SELECT payload FROM regular_closes WHERE code=? ORDER BY date", (config["code"],))]
            by_date = {r["date"]: r["close"] for r in regular}
            rows = [{**row, "regularClose": by_date.get(row["baseDate"])} for row in rows]
            after = [json.loads(r[0]) for r in db.execute("SELECT payload FROM market_quotes WHERE code=? ORDER BY market", (config["code"],))]
            stocks.append({**config, "rows": rows, "latest": rows[-1] if rows else None,
                           "regularClose": regular[-1] if regular else None, "afterHours": after,
                           "liveQuotes": [json.loads(r[0]) for r in db.execute("SELECT payload FROM live_quotes WHERE code=? ORDER BY market", (config["code"],))]})
        # Keep filings visible even when a busy news day produces many headlines.
        documents = []
        for config in STOCKS:
            for kind in ("news", "filings"):
                documents.extend(json.loads(r[0]) for r in db.execute(
                    "SELECT payload FROM documents WHERE code=? AND kind=? ORDER BY date DESC LIMIT 30", (config["code"], kind)))
        documents.sort(key=lambda item: item["publishedAt"], reverse=True)
    return {"stocks": stocks, "documents": documents, "repurchases": repurchases_with_progress(), "status": status, "kindUpdateSeconds": kind_interval(),
            "servedAt": stamp(), "refreshInProgress": LOCK.locked(), "updateSeconds": int(os.getenv("UPDATE_SECONDS", "1800")),
            "pricePolling": price_polling(status), "livePolling": live_polling(status)}


def session_previous_close(db, code, day):
    # A live quote's comparison base only applies to the trading day it belongs to.
    live = db.execute("SELECT payload FROM live_quotes WHERE code=? AND market='KRX'", (code,)).fetchone()
    if live and json.loads(live[0]).get("date") == day:
        return json.loads(live[0]).get("previousClose")
    closing = db.execute("SELECT payload FROM regular_closes WHERE code=? AND date=?", (code, day)).fetchone()
    return json.loads(closing[0]).get("previousClose") if closing else None


@app.get("/api/kline/{code}/{period}")
def kline(code: str, period: str, limit: int = Query(default=0, ge=0, le=100000)):
    if code not in {s["code"] for s in STOCKS} or period not in KLINE_PERIODS:
        raise HTTPException(404)
    with connection() as db:
        bars = [list(row) for row in db.execute("SELECT at,open,high,low,close,volume FROM kline_bars WHERE code=? AND period=? ORDER BY at", (code, KLINE_PERIODS[period]))]
        if period in ("week", "month"):
            bars = aggregate(bars, period)
        elif period == "five":
            bars = latest_days(bars, 5)
        day = bars[-1][0][:10] if bars else None
        previous = session_previous_close(db, code, day) if period == "minute" and day else None
        extra = {}
        if period in ("minute", "five"):
            first = day if period == "minute" else bars[0][0][:10] if bars else None
            rows = db.execute("SELECT at,close FROM nxt_minutes WHERE code=? AND at>=? AND at<? ORDER BY at", (code, first, (day or "") + "~")).fetchall() if first else []
            extra["nxt"] = nxt_series(rows, period)
        status = db.execute("SELECT success FROM status WHERE module=?", (code + ":kline",)).fetchone()
    # Built directly: daily history has thousands of rows and needs no field conversion.
    return Response(json.dumps({"code": code, "period": period, "bars": bars[-limit:] if limit else bars, "tradingDay": day,
                                "previousClose": previous, "updatedAt": status[0] if status else None, **extra},
                               ensure_ascii=False, separators=(",", ":")), media_type="application/json")


@app.get("/api/export/{code}.csv")
def export(code: str, start: str = Query(default=""), end: str = Query(default="")):
    if code not in {s["code"] for s in STOCKS}:
        raise HTTPException(404)
    fields = ["baseDate", "regularClose", "netIndividualsBuyVolume", "netForeignerBuyVolume", "netInstitutionBuyVolume", "netOtherCorporationBuyVolume", "updatedAt"]
    with connection() as db:
        rows = [json.loads(r[0]) for r in db.execute("SELECT payload FROM flows WHERE code=? AND date>=? AND date<=? ORDER BY date", (code, start or "0000", end or "9999"))]
        closing = {r[0]: json.loads(r[1])["close"] for r in db.execute("SELECT date,payload FROM regular_closes WHERE code=?", (code,))}
        rows = [{**row, "regularClose": closing.get(row["baseDate"])} for row in rows]
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["日期", "KRX常规收盘价(KRW,未核实则空)", "个人净买入(股)", "外资净买入(股)", "机构净买入(股)", "其他法人净买入(股)", "资金流更新时间"])
    for row in rows:
        writer.writerow([row.get(f) for f in fields])
    return Response("\ufeff" + stream.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{code}-fund-flow.csv"'})


app.mount("/assets", StaticFiles(directory=ROOT / "web/assets"), name="assets")


@app.get("/")
def index():
    return FileResponse(ROOT / "web/index.html")
