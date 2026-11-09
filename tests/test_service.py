"""Verify persistence, stale-data behavior, and the public HTTP contract offline."""
import csv
import asyncio
import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

os.environ["DISABLE_UPDATER"] = "1"
from fastapi.testclient import TestClient
from app import service


class ServiceTests(unittest.TestCase):
    def setUp(self):
        service.LIVE_FAILURES = 0
        service.KLINE_FULL_DAYS.clear()
        self.directory = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(service, "DATA", Path(self.directory.name))
        self.db_patch = patch.object(service, "DB", Path(self.directory.name) / "test.sqlite3")
        self.data_patch.start()
        self.db_patch.start()
        self.client = TestClient(service.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.db_patch.stop()
        self.data_patch.stop()
        self.directory.cleanup()

    def test_seed_is_idempotent_and_old_data_cannot_overwrite_revision(self):
        before = self.client.get("/api/dashboard").json()["stocks"][0]
        row = {**before["latest"], "close": 1234567}
        service.put_flows(before["code"], [row])
        service.initialize()  # Re-seeding at the next deployment must retain corrections.
        after = self.client.get("/api/dashboard").json()["stocks"][0]
        self.assertEqual(len(after["rows"]), len(before["rows"]))
        self.assertEqual(after["latest"]["close"], 1234567)
        service.put_flows(before["code"], [{**row, "close": 1, "updatedAt": "2000-01-01T00:00:00+09:00"}])
        self.assertEqual(self.client.get("/api/dashboard").json()["stocks"][0]["latest"]["close"], 1234567)

    def test_failure_preserves_last_success_and_data(self):
        service.module_status("A000660:flows")
        saved = self.client.get("/api/dashboard").json()
        service.module_status("A000660:flows", "upstream unavailable")
        result = self.client.get("/api/dashboard").json()
        self.assertEqual(result["status"][0]["success"], saved["status"][0]["success"])
        self.assertEqual(result["status"][0]["error"], "upstream unavailable")
        self.assertEqual(result["stocks"], saved["stocks"])

    def test_price_collection_runs_while_content_refresh_is_busy(self):
        def collect(stock):
            service.module_status(stock["code"] + ":prices")
        # A slow news/translation job holds the content lock, never the price lock.
        with service.LOCK, patch.object(service, "refresh_prices", side_effect=collect) as request:
            service.refresh_market_prices()
            self.assertEqual(request.call_count, 2)
            self.assertTrue(service.LOCK.locked())
        polling = self.client.get("/api/price-status").json()
        self.assertTrue(polling["lastSuccess"])
        self.assertTrue(polling["revision"])
        self.assertFalse(polling["inProgress"])
        self.assertEqual(polling["intervalSeconds"], 60)
        self.assertEqual(self.client.get("/api/dashboard").json()["pricePolling"], polling)
        self.assertFalse(service.PRICE_LOCK.locked())

    def test_price_scheduler_starts_immediately_and_accounts_for_request_time(self):
        clock = Mock()
        clock.monotonic.side_effect = [100, 103]
        clock.time.return_value = 1793760000
        with patch.object(service, "time", clock), patch.object(service, "refresh_market_prices") as collect, \
                patch.object(service.asyncio, "sleep", side_effect=asyncio.CancelledError) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(service.price_updater())
        collect.assert_called_once_with()
        sleep.assert_awaited_once_with(57)
        self.assertIsNone(service.PRICE_NEXT_CHECK)

    def test_live_quotes_do_not_overwrite_closes_and_survive_failed_or_older_source(self):
        before = self.client.get("/api/dashboard").json()
        at = datetime.now(timezone.utc) - timedelta(seconds=2)
        payload = {"pollingInterval": 7000, "datas": [{"itemCode": stock["symbol"],
            "stockExchangeType": {"code": "KS", "zoneId": "Asia/Seoul", "delayTime": 0},
            "closePrice": "120,000", "compareToPreviousClosePrice": "-2,000", "marketStatus": "OPEN",
            "marketSessionType": "regularMarket", "localTradedAt": at.isoformat()} for stock in service.STOCKS]}
        with service.LOCK, service.PRICE_LOCK, patch.object(service, "read_json", return_value=payload) as fetch:
            service.refresh_live_prices()  # Neither slow pipeline can block latest quotes.
            fetch.assert_called_once_with(service.LIVE_URL)
        with patch.object(service, "LIVE_OFF_HOURS", 1):  # Same cadence at any time of day.
            saved = self.client.get("/api/quotes").json()
        self.assertEqual(len(saved["stocks"]), 2)
        self.assertEqual(saved["stocks"][0]["liveQuotes"][0]["price"], 120000)
        self.assertEqual(saved["stocks"][0]["liveQuotes"][0]["previousClose"], 122000)
        self.assertEqual(saved["livePolling"]["intervalSeconds"], 3)
        after = self.client.get("/api/dashboard").json()
        for previous, current in zip(before["stocks"], after["stocks"]):
            self.assertEqual(previous["regularClose"], current["regularClose"])
            self.assertEqual(previous["rows"], current["rows"])
        for item in payload["datas"]:
            item.update(closePrice="100,000", localTradedAt=(at - timedelta(seconds=7)).isoformat())
        with patch.object(service, "read_json", return_value=payload):
            service.refresh_live_prices()
        self.assertEqual(self.client.get("/api/quotes").json()["stocks"], saved["stocks"])
        success = self.client.get("/api/quotes").json()["status"][0]["success"]
        with patch.object(service, "read_json", side_effect=TimeoutError("source timeout")), self.assertLogs(service.LOG, level="ERROR"):
            service.refresh_live_prices()
        failed = self.client.get("/api/quotes").json()
        self.assertEqual(failed["stocks"], saved["stocks"])
        self.assertEqual(failed["status"][0]["success"], success)
        self.assertIn("timeout", failed["status"][0]["error"])
        self.assertFalse(service.LIVE_LOCK.locked())
        self.assertEqual(self.client.post("/api/quotes").status_code, 405)

    def test_live_scheduler_starts_immediately_and_respects_seven_second_cadence(self):
        clock = Mock()
        clock.monotonic.side_effect = [100, 101]
        clock.time.return_value = 1793760000
        with patch.object(service, "time", clock), patch.object(service, "LIVE_INTERVAL", 7), \
                patch.object(service, "refresh_live_prices") as collect, \
                patch.object(service.asyncio, "sleep", side_effect=asyncio.CancelledError) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(service.live_updater())
        collect.assert_called_once_with()
        sleep.assert_awaited_once_with(6)
        self.assertIsNone(service.LIVE_NEXT_CHECK)

    def test_live_collection_slows_outside_trading_window_and_wakes_for_the_open(self):
        kst = timezone(timedelta(hours=9))
        cases = [
            (datetime(2026, 11, 4, 12, 0, tzinfo=kst), 7, 7),  # Wednesday session
            (datetime(2026, 11, 4, 20, 5, tzinfo=kst), 7, 7),  # NXT close margin
            (datetime(2026, 11, 4, 21, 0, tzinfo=kst), 300, 300),
            (datetime(2026, 11, 4, 7, 49, tzinfo=kst), 300, 60),  # wakes at the window start
            (datetime(2026, 11, 7, 12, 0, tzinfo=kst), 300, 300),  # Saturday
            (datetime(2026, 11, 8, 23, 58, tzinfo=kst), 300, 300),  # Sunday, Monday still far off
        ]
        with patch.object(service, "LIVE_INTERVAL", 7), patch.object(service, "LIVE_OFF_HOURS", 300):
            for now, interval, delay in cases:
                self.assertEqual(service.live_interval(now), interval, now)
                self.assertEqual(service.live_delay(now), delay, now)
            # An operator's longer interval also applies outside the window.
            with patch.object(service, "LIVE_INTERVAL", 600):
                self.assertEqual(service.live_interval(cases[2][0]), 600)

    def test_live_collection_defaults_to_three_seconds_and_ignores_source_suggestion(self):
        self.assertEqual(service.LIVE_INTERVAL, 3)
        now = datetime.now(timezone.utc)
        payload = {"pollingInterval": 7000, "datas": [{"itemCode": stock["symbol"],
            "stockExchangeType": {"code": "KS", "zoneId": "Asia/Seoul", "delayTime": 0},
            "closePrice": "120,000", "compareToPreviousClosePrice": "-2,000", "marketStatus": "OPEN",
            "marketSessionType": "regularMarket", "localTradedAt": now.isoformat()} for stock in service.STOCKS]}
        with patch.object(service, "read_json", return_value=payload):
            service.refresh_live_prices()
        self.assertEqual(service.LIVE_INTERVAL, 3)
        self.assertEqual(service.LIVE_FAILURES, 0)

    def test_live_collection_backs_off_while_the_source_fails_and_recovers(self):
        clock = Mock()
        clock.time.return_value = 1793760000  # Wednesday 11:40 KST
        delays = []
        for failures in (1, 2, 3, 20):
            service.LIVE_FAILURES = failures
            clock.monotonic.side_effect = [100, 100]
            with patch.object(service, "time", clock), patch.object(service, "LIVE_INTERVAL", 1), patch.object(service, "refresh_live_prices"), \
                    patch.object(service.asyncio, "sleep", side_effect=asyncio.CancelledError) as sleep:
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(service.live_updater())
            delays.append(sleep.await_args.args[0])
        self.assertEqual(delays, [2, 4, 8, 60])
        service.LIVE_FAILURES = 0
        with patch.object(service, "read_json", side_effect=TimeoutError("down")), self.assertLogs(service.LOG, level="ERROR"):
            service.refresh_live_prices()
            service.refresh_live_prices()
        self.assertEqual(service.LIVE_FAILURES, 2)
        with patch.object(service, "read_json", return_value={"datas": []}), self.assertLogs(service.LOG, level="ERROR"):
            service.refresh_live_prices()  # Unusable data counts as a failure too.
        self.assertEqual(service.LIVE_FAILURES, 3)

    def test_one_live_cycle_commits_once_regardless_of_rows_written(self):
        now = datetime.now(timezone.utc)
        payload = {"datas": [{"itemCode": stock["symbol"], "stockExchangeType": {"code": "KS", "zoneId": "Asia/Seoul", "delayTime": 0},
            "closePrice": "120,000", "compareToPreviousClosePrice": "-2,000", "marketStatus": "OPEN",
            "marketSessionType": "afterMarket", "localTradedAt": now.isoformat()} for stock in service.STOCKS]}
        opened = []
        real = service.connection

        def counted():
            opened.append(1)
            return real()

        with patch.object(service, "read_json", return_value=payload), patch.object(service, "connection", counted):
            service.refresh_live_prices()
        self.assertEqual(len(opened), 1)
        quotes = self.client.get("/api/quotes").json()
        self.assertEqual(quotes["stocks"][0]["afterHours"][0]["price"], 120000)
        self.assertIsNone(quotes["status"][0]["error"])

    def test_klines_are_stored_apart_from_verified_closes_and_served_per_period(self):
        today = datetime.now(service.KST).date()
        monday = today - timedelta(days=today.weekday() + 7)
        days = [monday - timedelta(days=7), monday - timedelta(days=6), monday, monday + timedelta(days=1)]
        before = self.client.get("/api/dashboard").json()["stocks"][0]["regularClose"]

        def source(url, *_):
            if "/day?" in url:
                self.assertIn("startDateTime=19900101", url)
                return [{"localDate": f"{d:%Y%m%d}", "openPrice": 100, "highPrice": 110, "lowPrice": 90, "closePrice": 105,
                         "accumulatedTradingVolume": 10, "foreignRetentionRate": 1.0} for d in days]
            latest = f"{days[-1]:%Y%m%d}"
            step = "0900" if "/minute?" in url else "0905"
            return [{"localDateTime": latest + step + "00", "openPrice": 100, "highPrice": 110, "lowPrice": 90, "currentPrice": 105, "accumulatedTradingVolume": 3}]

        with patch.object(service, "read_json", side_effect=source) as fetch:
            service.refresh_market_klines()
        self.assertEqual(fetch.call_count, 6)
        for period, count in (("day", 4), ("week", 2), ("month", None), ("minute", 1), ("five", 1)):
            response = self.client.get(f"/api/kline/A000660/{period}")
            self.assertEqual(response.status_code, 200, period)
            self.assertEqual(response.headers["cache-control"], "public, max-age=15")
            if count:
                self.assertEqual(len(response.json()["bars"]), count, period)
        week = self.client.get("/api/kline/A000660/week").json()
        self.assertEqual(week["bars"][0], [days[0].isoformat(), 100, 110, 90, 105, 20])
        self.assertEqual(len(self.client.get("/api/kline/A000660/day", params={"limit": 2}).json()["bars"]), 2)
        self.assertEqual(self.client.get("/api/dashboard").json()["stocks"][0]["regularClose"], before)
        for path in ("/api/kline/A999999/day", "/api/kline/A000660/hour"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        self.assertEqual(self.client.get("/api/kline/A000660/day", params={"limit": -1}).status_code, 422)
        self.assertEqual(self.client.post("/api/kline/A000660/day").status_code, 405)

    def nxt_quote_payload(self, at, price="1,275,000", status="OPEN"):
        return {"datas": [{"itemCode": stock["symbol"], "stockExchangeType": {"code": "KS", "zoneId": "Asia/Seoul", "delayTime": 0},
            "closePrice": "1,264,000", "compareToPreviousClosePrice": "-2,000", "marketStatus": "OPEN", "marketSessionType": "regularMarket",
            "localTradedAt": "2026-11-05T15:30:00+09:00",
            "overMarketPriceInfo": {"tradingSessionType": "AFTER_MARKET", "overMarketStatus": status, "overPrice": price,
                                    "compareToPreviousClosePrice": "-2,000", "localTradedAt": at, "tradeStopType": {"name": "TRADING"}}} for stock in service.STOCKS]}

    def collect_nxt(self, at, price="1,275,000", status="OPEN"):
        with patch.object(service, "read_json", return_value=self.nxt_quote_payload(at, price, status)), \
                patch.object(service, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 5, 21, 0, tzinfo=service.KST)
            service.refresh_live_prices()

    def test_nxt_quotes_build_a_minute_line_served_with_intraday_klines(self):
        for at, price in (("15:41:03", "1,275,000"), ("15:41:57", "1,276,000"), ("15:42:00", "1,277,000"), ("15:46:00", "1,278,000"), ("16:00:00", "1,279,000")):
            self.collect_nxt(f"2026-11-05T{at}.123+09:00", price)
        self.collect_nxt("2026-11-05T20:05:00+09:00", "1,999,000")
        self.collect_nxt("2026-11-05T08:30:00+09:00", "1,999,000")
        self.collect_nxt("2026-11-05T16:30:00+09:00", "1,999,000", "CLOSE")
        self.assertEqual(service.LIVE_FAILURES, 0)
        with service.connection() as db:
            db.execute("INSERT INTO kline_bars VALUES('A000660','minute','2026-11-05 15:30',1,1,1,1,1)")
            db.execute("INSERT INTO kline_bars VALUES('A000660','minute5','2026-11-05 15:30',1,1,1,1,1)")
            db.execute("INSERT INTO kline_bars VALUES('A000660','day','2026-11-05',1,1,1,1,1)")
            db.execute("INSERT INTO nxt_minutes VALUES('A000660','2026-11-04 15:41',5)")
        minute = self.client.get("/api/kline/A000660/minute").json()["nxt"]
        self.assertEqual(minute, [["2026-11-05 15:41", 1276000], ["2026-11-05 15:42", 1277000], ["2026-11-05 15:46", 1278000], ["2026-11-05 16:00", 1279000]])
        five = self.client.get("/api/kline/A000660/five").json()["nxt"]
        self.assertEqual(five, [["2026-11-05 15:40", 1277000], ["2026-11-05 15:45", 1278000], ["2026-11-05 16:00", 1279000]])
        self.assertNotIn("nxt", self.client.get("/api/kline/A000660/day").json())
        self.assertEqual(self.client.get("/api/kline/A005930/minute").json()["nxt"], [])

    def test_old_nxt_minutes_are_pruned_with_the_kline_refresh(self):
        today = datetime.now(service.KST).date()
        with service.connection() as db:
            db.execute("INSERT INTO nxt_minutes VALUES('A000660',?,1)", ((today - timedelta(days=31)).isoformat() + " 15:41",))
            db.execute("INSERT INTO nxt_minutes VALUES('A000660',?,2)", ((today - timedelta(days=29)).isoformat() + " 15:41",))
        with patch.object(service, "refresh_klines"):
            service.refresh_market_klines()
        with service.connection() as db:
            self.assertEqual([row[0] for row in db.execute("SELECT close FROM nxt_minutes")], [2])

    def test_failed_or_truncated_kline_refresh_keeps_saved_history(self):
        today = datetime.now(service.KST).date()
        rows = [{"localDate": f"{today - timedelta(days=n):%Y%m%d}", "openPrice": 100, "highPrice": 110, "lowPrice": 90, "closePrice": 105, "accumulatedTradingVolume": 1} for n in range(10)]
        with patch.object(service, "read_json", side_effect=lambda url, *_: rows if "/day?" in url else []):
            service.refresh_market_klines()
        self.assertEqual(len(self.client.get("/api/kline/A000660/day").json()["bars"]), 10)
        service.KLINE_FULL_DAYS.clear()
        with patch.object(service, "read_json", side_effect=lambda url, *_: rows[:3] if "/day?" in url else []), self.assertLogs(service.LOG, level="ERROR"):
            service.refresh_market_klines()
        self.assertEqual(len(self.client.get("/api/kline/A000660/day").json()["bars"]), 10)
        status = self.client.get("/api/dashboard").json()["status"]
        self.assertIn("shrank", next(s["error"] for s in status if s["module"] == "A000660:kline"))
        with patch.object(service, "read_json", side_effect=TimeoutError("down")), self.assertLogs(service.LOG, level="ERROR"):
            service.refresh_market_klines()
        self.assertEqual(len(self.client.get("/api/kline/A000660/day").json()["bars"]), 10)

    def test_session_baseline_never_comes_from_another_trading_day(self):
        day = "2026-11-03"
        with service.connection() as db:
            db.execute("INSERT OR REPLACE INTO kline_bars VALUES('A000660','minute',?,1,1,1,1,1)", (day + " 09:00",))
            db.execute("INSERT OR REPLACE INTO live_quotes VALUES('A000660','KRX',?)", (json.dumps({"date": "2026-11-04", "previousClose": 111, "asOf": "2026-11-04T08:00:00+09:00"}),))
            db.execute("INSERT OR REPLACE INTO regular_closes VALUES('A000660',?,?)", (day, json.dumps({"date": day, "close": 5, "previousClose": 222})))
        self.assertEqual(self.client.get("/api/kline/A000660/minute").json()["previousClose"], 222)
        with service.connection() as db:
            db.execute("UPDATE live_quotes SET payload=json_set(payload,'$.date',?) WHERE code='A000660' AND market='KRX'", (day,))
        self.assertEqual(self.client.get("/api/kline/A000660/minute").json()["previousClose"], 111)

    def test_updater_survives_database_error_while_scheduling_next_run(self):
        def locked():
            raise service.sqlite3.OperationalError("database is locked")

        with patch.object(service, "refresh_all"), patch.object(service, "connection", locked), \
                patch.object(service.asyncio, "sleep", side_effect=asyncio.CancelledError) as sleep, \
                self.assertLogs(service.LOG, level="ERROR"):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(service.updater())
        sleep.assert_awaited_once_with(1800)

    def test_flow_refresh_requests_enough_days_to_close_an_outage_gap(self):
        latest = date.fromisoformat(self.client.get("/api/dashboard").json()["stocks"][0]["latest"]["baseDate"])
        days = service.flow_history_days("A000660", latest + timedelta(days=30))
        self.assertEqual(days, 35)
        self.assertEqual(service.flow_history_days("A000660", latest), 5)
        self.assertEqual(service.flow_history_days("A000660", latest - timedelta(days=1)), 5)
        self.assertEqual(service.flow_history_days("A999999", latest), 120)

        outage = latest + timedelta(days=30)

        def history(code, days):
            rows, day = [], outage
            while len(rows) < days:
                if day.weekday() < 5:
                    rows.append({"baseDate": day.isoformat(), "updatedAt": day.isoformat() + "T18:00:00+09:00"})
                day -= timedelta(days=1)
            return {"rows": rows}

        with patch.object(service, "TossClient") as client, patch.object(service, "refresh_documents"), \
                patch.object(service, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.combine(outage, datetime.min.time(), service.KST)
            client.return_value.history.side_effect = history
            service.refresh_all()
        with service.connection() as db:
            stored = [row[0] for row in db.execute("SELECT date FROM flows WHERE code='A000660' ORDER BY date")]
        weekdays = [day for day in (latest + timedelta(days=n) for n in range(1, 31)) if day.weekday() < 5]
        self.assertLessEqual({day.isoformat() for day in weekdays}, set(stored))

    def test_flow_window_runs_each_minute_on_weekdays_in_beijing_time(self):
        beijing = service.BEIJING
        cases = [
            (datetime(2026, 11, 4, 14, 29, 30, tzinfo=beijing), 30),
            (datetime(2026, 11, 4, 14, 30, 0, tzinfo=beijing), 60),
            (datetime(2026, 11, 4, 15, 30, 0, tzinfo=service.KST), 60),
            (datetime(2026, 11, 4, 14, 44, 59, tzinfo=beijing), 1),
            (datetime(2026, 11, 4, 14, 20, 0, tzinfo=beijing), 600),
            (datetime(2026, 11, 4, 14, 45, 0, tzinfo=beijing), 85500),  # Tomorrow 14:30.
            (datetime(2026, 11, 6, 14, 45, 0, tzinfo=beijing), 258300),  # Friday to Monday.
            (datetime(2026, 11, 7, 14, 30, 0, tzinfo=beijing), 172800),  # Saturday to Monday.
        ]
        for now, delay in cases:
            self.assertEqual(service.flow_window_delay(now), delay, now)
        now, slots = datetime(2026, 11, 4, 14, 29, 30, tzinfo=beijing), []
        while now.date() == date(2026, 11, 4):
            now += timedelta(seconds=service.flow_window_delay(now))
            slots.append(now.strftime("%H:%M"))
        self.assertEqual(slots[:-1], [f"14:{minute}" for minute in range(30, 46)])

    def test_flow_window_fetches_only_flows_even_while_the_slow_refresh_runs(self):
        day = date(2026, 11, 4).isoformat()
        with service.LOCK, patch.object(service, "TossClient") as client, patch.object(service, "refresh_documents") as documents, \
                patch.object(service, "flow_window_delay", return_value=1), \
                patch.object(service.asyncio, "sleep", side_effect=[None, asyncio.CancelledError]):
            client.return_value.history.return_value = {"rows": [{"baseDate": day, "updatedAt": day + "T15:35:00+09:00"}]}
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(service.flow_updater())
        self.assertEqual(client.return_value.history.call_count, 2)
        documents.assert_not_called()
        with service.connection() as db:
            self.assertEqual(db.execute("SELECT updated FROM flows WHERE code='A000660' AND date=?", (day,)).fetchone()[0], day + "T15:35:00+09:00")

    def test_flow_window_backs_off_while_toss_fails_and_recovers(self):
        day = date(2026, 11, 4).isoformat()
        rows = {"rows": [{"baseDate": day, "updatedAt": day + "T15:35:00+09:00"}]}
        rejected = RuntimeError("Toss HTTP 429")
        with patch.object(service, "TossClient") as client, patch.object(service, "flow_window_delay", return_value=1), \
                patch.object(service.asyncio, "sleep", side_effect=[None, None, None, asyncio.CancelledError]) as sleep, \
                self.assertLogs(service.LOG, level="ERROR"):
            client.return_value.history.side_effect = [rejected, rows, rejected, rejected, rows, rows]
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(service.flow_updater())
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [1, 120, 240, 1])

    def kind_batch(self, declared, amount, start="2026-10-29", end="2027-01-28", shares=1000000):
        return {"declaredOn": declared, "periodStart": start, "periodEnd": end, "plannedShares": 14920000, "tradedShares": shares, "tradedKrw": amount}

    def refresh_kind(self, hynix, samsung=None, trades=()):
        samsung = samsung if samsung is not None else [self.kind_batch("2026-08-21", 14999999899750, "2026-08-24", "2026-11-21")]
        declared = iter([hynix, samsung])
        with patch.object(service, "read_kind", return_value="page"), patch.object(service, "parse_declared", side_effect=lambda *_: next(declared)), \
                patch.object(service, "parse_trades", return_value=list(trades)):
            service.refresh_market_repurchases()

    def batch(self, title):
        return next(item for item in self.client.get("/api/dashboard").json()["repurchases"] if item["title"] == title)

    def test_repurchase_progress_comes_from_kind_by_amount(self):
        self.assertIsNone(self.batch("10月新增回购计划")["progress"])
        self.refresh_kind([self.kind_batch("2026-08-19", 40004339063500, "2026-08-20", "2026-11-19", 23693272), self.kind_batch("2026-10-28", 1449883471000)],
                          trades=[("2026-10-21", 250000), ("2026-10-29", 250000), ("2026-11-03", 250000)])
        october = self.batch("10月新增回购计划")
        self.assertEqual((october["progress"], october["purchasedKrw"], october["status"], october["asOf"]), (7.25, 1449883471000, "执行中", "2026-11-03"))
        august = self.batch("8月回购批次")
        self.assertEqual((august["progress"], august["asOf"], august["status"]), (100, "2026-10-21", "购入完成 · 注销待披露"))
        self.assertEqual(self.batch("8月员工激励回购批次")["progress"], 100)
        with service.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM status WHERE module LIKE '%:repurchase' AND error IS NULL").fetchone()[0], 2)

    def test_a_kind_failure_keeps_the_last_progress_and_warns(self):
        batches = [self.kind_batch("2026-08-19", 40004339063500, "2026-08-20", "2026-11-19"), self.kind_batch("2026-10-28", 1449883471000)]
        self.refresh_kind(batches)
        with patch.object(service, "read_kind", side_effect=OSError("KIND down")), self.assertLogs(service.LOG, level="ERROR"):
            service.refresh_market_repurchases()
        self.assertEqual(self.batch("10月新增回购计划")["progress"], 7.25)
        self.assertIn("A000660:repurchase", self.client.get("/api/health").json()["staleModules"])

    def test_a_falling_total_is_rejected_and_a_batch_mismatch_warns(self):
        batches = [self.kind_batch("2026-08-19", 40004339063500, "2026-08-20", "2026-11-19"), self.kind_batch("2026-10-28", 1449883471000)]
        self.refresh_kind(batches)
        with self.assertLogs(service.LOG, level="ERROR"):
            self.refresh_kind([batches[0], self.kind_batch("2026-10-28", 1000)])
        self.assertEqual(self.batch("10月新增回购计划")["purchasedKrw"], 1449883471000)
        with self.assertLogs(service.LOG, level="ERROR") as logs:
            self.refresh_kind([*batches, self.kind_batch("2027-02-01", 5, "2027-02-02", "2027-04-30")])
        self.assertIn("2027-02-01", "".join(logs.output))
        with service.connection() as db:
            self.assertIn("2027-02-01", db.execute("SELECT error FROM status WHERE module='A000660:repurchase'").fetchone()[0])

    def test_an_unfinished_batch_reads_as_ended_after_its_period(self):
        self.refresh_kind([self.kind_batch("2026-08-19", 40004339063500, "2026-08-20", "2026-11-19"), self.kind_batch("2026-10-28", 1000, end="2026-10-30")])
        self.assertEqual(self.batch("10月新增回购计划")["status"], "执行期结束")
        self.refresh_kind([self.kind_batch("2026-08-19", 40004339063500, "2026-08-20", "2026-11-19"), self.kind_batch("2026-10-28", 20000000000000)])
        self.assertEqual(self.batch("10月新增回购计划")["status"], "购入完成")

    def test_repurchase_status_tolerates_the_six_hour_cadence(self):
        self.assertEqual(service.kind_interval(), 21600)
        self.assertEqual(service.stale_tolerance("A000660:repurchase"), 64800)
        with patch.dict(service.os.environ, {"KIND_UPDATE_SECONDS": "60"}):
            self.assertEqual(service.kind_interval(), 3600)

    def test_health_separates_process_liveness_from_data_freshness(self):
        healthy = self.client.get("/api/health")
        self.assertEqual(healthy.status_code, 200)
        self.assertTrue(healthy.json()["dataOk"])
        self.assertEqual(healthy.json()["staleModules"], [])
        # A failing source is reported but must not mark the process as down.
        service.module_status("A000660:flows", "source down")
        failing = self.client.get("/api/health")
        self.assertEqual(failing.status_code, 200)
        self.assertTrue(failing.json()["ok"])
        self.assertFalse(failing.json()["dataOk"])
        self.assertEqual(failing.json()["staleModules"], ["A000660:flows"])
        service.module_status("A000660:flows")
        self.assertTrue(self.client.get("/api/health").json()["dataOk"])
        # Fresh success only lasts as long as the module's own tolerance.
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        with service.connection() as db:
            db.execute("UPDATE status SET success=? WHERE module='A000660:flows'", (old,))
        self.assertEqual(self.client.get("/api/health").json()["staleModules"], ["A000660:flows"])

    def test_a_single_failed_live_poll_is_not_reported_until_it_lasts(self):
        service.module_status("A000660:live")
        service.module_status("A000660:live", "ssl eof")
        self.assertTrue(self.client.get("/api/health").json()["dataOk"])
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        with service.connection() as db:
            db.execute("UPDATE status SET success=? WHERE module='A000660:live'", (old,))
        self.assertEqual(self.client.get("/api/health").json()["staleModules"], ["A000660:live"])

    def test_health_fails_when_a_background_task_has_died(self):
        dead = Mock()
        dead.done.return_value = True
        dead.get_name.return_value = "updater"
        with patch.object(service, "TASKS", [dead]):
            response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(response.json()["deadTasks"], ["updater"])

    def test_csv_bounds_and_units(self):
        stock = self.client.get("/api/dashboard").json()["stocks"][0]
        latest = stock["latest"]
        response = self.client.get(f'/api/export/{stock["code"]}.csv', params={"start": latest["baseDate"], "end": latest["baseDate"]})
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(int(rows[1][3]), latest["netForeignerBuyVolume"])
        self.assertEqual(self.client.get("/api/export/A999999.csv").status_code, 404)

    def test_public_routes_do_not_expose_state_and_are_read_only(self):
        self.assertTrue(self.client.get("/api/health").json()["ok"])
        for path in ("/runtime/dashboard.sqlite3", "/.env", "/assets/../data/repurchases.json", "/api/refresh"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        self.assertEqual(self.client.post("/api/dashboard").status_code, 405)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(self.client.get("/assets/app.js").status_code, 200)

    def test_only_verified_regular_closes_reach_history_and_csv(self):
        with service.connection() as db:
            db.execute("DELETE FROM regular_closes")
        stock = self.client.get("/api/dashboard").json()["stocks"][0]
        day = stock["latest"]["baseDate"]
        # The flow API's raw close remains available for auditing, never fallback.
        self.assertIsNone(stock["latest"]["regularClose"])
        response = self.client.get(f'/api/export/{stock["code"]}.csv', params={"start": day, "end": day})
        self.assertEqual(list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))[1][1], "")
        record = {"date": day, "close": 1398000, "previousClose": 1472000, "asOf": day + "T15:30:00+09:00"}
        service.save_regular_prices(stock["code"], [record])
        # A new day's previous-session snapshot must retain its comparison base.
        service.save_regular_prices(stock["code"], [{k:v for k,v in record.items() if k != "previousClose"}])
        updated = self.client.get("/api/dashboard").json()["stocks"][0]
        self.assertEqual(updated["regularClose"]["previousClose"], 1472000)
        self.assertEqual(updated["latest"]["regularClose"], 1398000)
        self.assertEqual(updated["latest"]["close"], stock["latest"]["close"])
        response = self.client.get(f'/api/export/{stock["code"]}.csv', params={"start": day, "end": day})
        self.assertEqual(list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))[1][1], "1398000")

    def test_translation_cache_and_daily_budget_avoid_duplicate_requests(self):
        source = {"responseStatus": 200, "responseData": {"translatedText": "公司业绩公告"}}
        with patch.object(service, "read_json", return_value=source) as request:
            self.assertEqual(service.translate("Earnings announcement"), "公司业绩公告")
            self.assertEqual(service.translate("Earnings announcement"), "公司业绩公告")
            self.assertEqual(request.call_count, 1)
            with service.connection() as db:
                db.execute("INSERT OR REPLACE INTO translation_usage VALUES(?,4500)", (service.stamp()[:10],))
            self.assertIsNone(service.translate("Another headline"))
            self.assertEqual(request.call_count, 1)

    def test_busy_news_feed_does_not_hide_older_official_filings(self):
        with service.connection() as db:
            for i in range(160):
                item = {"id": str(i), "code": "A000660", "kind": "news", "publishedAt": "2026-11-03"}
                db.execute("INSERT INTO documents VALUES(?,?,?,?,?)", ("A000660", str(i), "news", item["publishedAt"], json.dumps(item)))
            item = {"id": "filing", "code": "A000660", "kind": "filings", "publishedAt": "2026-10-01"}
            db.execute("INSERT INTO documents VALUES(?,?,?,?,?)", ("A000660", "filing", "filings", item["publishedAt"], json.dumps(item)))
        self.assertTrue(any(d["id"] == "filing" for d in self.client.get("/api/dashboard").json()["documents"]))


if __name__ == "__main__":
    unittest.main()
