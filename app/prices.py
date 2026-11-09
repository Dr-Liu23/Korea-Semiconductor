"""Keep verified KRX regular-session closes separate from after-hours quotes."""
import math
import re
from datetime import datetime, time, timedelta, timezone

KST = timezone(timedelta(hours=9))
LIVE_URL = "https://polling.finance.naver.com/api/realtime/domestic/stock/000660,005930"


def live_prices(symbol, payload, now):
    """Latest source snapshots; closePrice here is a live quote, NOT a fixed close."""
    items = [item for item in payload.get("datas", []) if item.get("itemCode") == symbol]
    if len(items) != 1:
        raise ValueError("missing or duplicate live instrument")
    item = items[0]
    exchange = item.get("stockExchangeType") or {}
    if exchange.get("code") != "KS" or exchange.get("zoneId") != "Asia/Seoul":
        raise ValueError("unexpected live exchange")
    fetched = now.astimezone(timezone.utc).isoformat()

    def snapshot(data, market, price_key, session):
        at = datetime.fromisoformat(data["localTradedAt"])
        # The source stamps its response time, so allow for small clock differences.
        if at.tzinfo is None or at > now + timedelta(seconds=5):
            raise ValueError("invalid live timestamp")
        price = positive_price(data.get(price_key))
        change = str(data.get("compareToPreviousClosePrice", "")).replace(",", "")
        if not re.fullmatch(r"-?\d+", change):
            raise ValueError("invalid live comparison base")
        previous = positive_price(price - int(change))
        return {"date": at.astimezone(KST).date().isoformat(), "price": price,
                "previousClose": previous, "change": int(change), "changePercent": int(change) / previous * 100,
                "asOf": at.astimezone(KST).isoformat(), "fetchedAt": fetched,
                "exchange": market, "session": session,
                "marketStatus": data.get("marketStatus", data.get("overMarketStatus", "UNKNOWN")),
                "tradeStop": (data.get("tradeStopType") or {}).get("name"),
                "sourceDelayTime": exchange.get("delayTime"), "source": "Naver latest quote", "sourceUrl": LIVE_URL}

    session = {"regularMarket": "REGULAR_MARKET", "afterMarket": "AFTER_MARKET",
               "preMarket": "PRE_MARKET"}.get(item.get("marketSessionType"), "UNKNOWN")
    quotes = [snapshot(item, "KRX", "closePrice", session)]
    nxt = item.get("overMarketPriceInfo")
    if nxt:
        quotes.append(snapshot(nxt, "NXT", "overPrice", nxt.get("tradingSessionType", "UNKNOWN")))
    return quotes


def positive_price(value):
    if isinstance(value, str):
        if not re.fullmatch(r"\d+(,\d{3})*", value):
            raise ValueError("invalid price format")
        value = int(value.replace(",", ""))
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0 or int(value) != value:
        raise ValueError("missing or invalid whole-won price")
    return int(value)


def compact_time(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{14}", value):
        raise ValueError("invalid chart timestamp")
    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=KST)


def chart_prices(symbol, chart, now):
    if chart.get("code") != symbol or chart.get("stockExchangeType") != "KRX":
        raise ValueError("chart is not the requested KRX instrument")
    url = f"https://api.stock.naver.com/chart/domestic/item/{symbol}?periodType=day"
    fetched = now.astimezone(timezone.utc).isoformat()
    regular, after = {}, None
    all_points = []
    for group in ("lastPriceInfos", "priceInfos"):
        for point in chart.get(group, []):
            at = compact_time(point.get("localDateTime"))
            if at > now:
                continue
            all_points.append((at, point))
            if at.time() == time(15, 30):
                price = positive_price(point.get("currentPrice"))
                day_code = at.strftime("%Y%m%d")
                if day_code == chart.get("tradeBaseAt"):
                    opening = compact_time(chart["openTime"])
                    if opening.time() != time(9):
                        continue  # A special late-opening day needs its own schedule.
                elif day_code == chart.get("lastTradeBaseAt"):
                    # Previous-session charts can be truncated at the start.
                    # Cross-check the closing point against the explicit base.
                    if price != positive_price(chart.get("lastClosePrice")):
                        continue
                else:
                    continue
                # The closing-auction snapshot has a single price. Do not use an
                # earlier minute or an intraday average when the snapshot is absent.
                if any(positive_price(point.get(k)) != price for k in ("openPrice", "highPrice", "lowPrice")):
                    raise ValueError("ambiguous 15:30 closing snapshot")
                day = at.date().isoformat()
                record = {"date": day, "close": price, "asOf": at.isoformat(), "exchange": "KRX",
                          "session": "REGULAR_CLOSE", "source": "Naver KRX 15:30 snapshot",
                          "sourceUrl": url, "fetchedAt": fetched}
                if at.strftime("%Y%m%d") == chart.get("tradeBaseAt"):
                    record["previousClose"] = positive_price(chart.get("lastClosePrice"))
                regular[day] = record
    for at, point in all_points:
        if at.date().isoformat() in regular and time(15, 30) < at.time() <= time(20):
            quote = {"date": at.date().isoformat(), "price": positive_price(point.get("currentPrice")),
                     "asOf": at.isoformat(), "exchange": "KRX", "session": "AFTER_MARKET",
                     "granularity": "1min", "source": "Naver KRX minute chart", "sourceUrl": url,
                     "fetchedAt": fetched}
            if after is None or quote["asOf"] > after["asOf"]:
                after = quote
    if not regular:
        raise ValueError("no verified 15:30 regular close in chart")
    return list(regular.values()), after


def nxt_after_price(symbol, payload, now):
    areas = payload.get("result", {}).get("areas", [])
    candidates = [item for area in areas if area.get("name") == "SERVICE_ITEM"
                  for item in area.get("datas", []) if item.get("cd") == symbol]
    if len(candidates) != 1:
        raise ValueError("missing or duplicate NXT instrument")
    info = candidates[0].get("nxtOverMarketPriceInfo") or {}
    if info.get("tradingSessionType") != "AFTER_MARKET":
        return None
    at = datetime.fromisoformat(info["localTradedAt"])
    if at.tzinfo is None:
        raise ValueError("NXT timestamp has no timezone")
    local = at.astimezone(KST)
    if at > now or not time(15, 30) <= local.time() <= time(20):
        return None
    return {"date": local.date().isoformat(), "price": positive_price(info.get("overPrice")),
            "asOf": local.isoformat(), "exchange": "NXT", "session": "AFTER_MARKET", "granularity": "quote",
            "marketStatus": info.get("overMarketStatus"), "source": "Naver NXT after-market quote",
            "sourceUrl": f"https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{symbol}",
            "fetchedAt": now.astimezone(timezone.utc).isoformat()}
