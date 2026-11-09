import copy
import unittest
from datetime import datetime

from app.prices import chart_prices, nxt_after_price, live_prices


def live_payload():
    return {"pollingInterval": 7000, "datas": [{"itemCode": "000660", "stockExchangeType": {"code": "KS", "zoneId": "Asia/Seoul", "delayTime": 0},
        "closePrice": "1,321,000", "compareToPreviousClosePrice": "-77,000", "marketStatus": "OPEN", "marketSessionType": "regularMarket",
        "localTradedAt": "2026-11-04T11:52:22+09:00", "tradeStopType": {"name": "TRADING"},
        "overMarketPriceInfo": {"overPrice": "1,320,000", "compareToPreviousClosePrice": "-78,000", "overMarketStatus": "OPEN",
            "tradingSessionType": "REGULAR_MARKET", "localTradedAt": "2026-11-04T11:52:22+09:00"}}]}


class LivePriceTests(unittest.TestCase):
    def setUp(self):
        self.payload = live_payload()
        self.now = datetime.fromisoformat("2026-11-04T11:52:23+09:00")

    def test_dynamic_close_price_is_live_and_venues_remain_separate(self):
        krx, nxt = live_prices("000660", self.payload, self.now)
        self.assertEqual((krx["exchange"], krx["price"], krx["previousClose"]), ("KRX", 1321000, 1398000))
        self.assertEqual((nxt["exchange"], nxt["price"], nxt["previousClose"]), ("NXT", 1320000, 1398000))
        self.assertAlmostEqual(krx["changePercent"], -77000 / 1398000 * 100)
        self.assertEqual(krx["session"], "REGULAR_MARKET")
        self.assertNotIn("close", krx)

    def test_missing_duplicate_wrong_exchange_and_future_quotes_rejected(self):
        variants = []
        missing = live_payload(); missing["datas"][0]["itemCode"] = "005930"; variants.append(missing)
        duplicate = live_payload(); duplicate["datas"] *= 2; variants.append(duplicate)
        wrong = live_payload(); wrong["datas"][0]["stockExchangeType"]["code"] = "NYSE"; variants.append(wrong)
        future = live_payload(); future["datas"][0]["localTradedAt"] = "2026-11-04T12:00:00+09:00"; variants.append(future)
        naive = live_payload(); naive["datas"][0]["localTradedAt"] = "2026-11-04T11:52:22"; variants.append(naive)
        invalid = live_payload(); invalid["datas"][0]["compareToPreviousClosePrice"] = "missing"; variants.append(invalid)
        for payload in variants:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                live_prices("000660", payload, self.now)

    def test_closed_status_and_unknown_session_are_preserved_without_guessing(self):
        self.payload["datas"][0]["marketStatus"] = "CLOSE"
        self.payload["datas"][0]["marketSessionType"] = "newSession"
        krx, _ = live_prices("000660", self.payload, self.now)
        self.assertEqual(krx["marketStatus"], "CLOSE")
        self.assertEqual(krx["session"], "UNKNOWN")


def point(timestamp, price):
    return {"localDateTime": timestamp, "currentPrice": price, "openPrice": price, "highPrice": price, "lowPrice": price}


class PriceSessionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.fromisoformat("2026-11-03T19:00:00+09:00")
        self.chart = {"code": "000660", "stockExchangeType": "KRX", "tradeBaseAt": "20261103", "lastTradeBaseAt": "20261102", "lastClosePrice": 1472000, "openTime": "20261103090000",
                      "priceInfos": [point("20261103090000", 1410000), point("20261103152900", 1400000), point("20261103153000", 1398000), point("20261103185900", 1413000)],
                      "lastPriceInfos": [point("20261102090000", 1402000), point("20261102153000", 1472000), point("20261102195900", 1480000)]}

    def test_regular_close_never_uses_last_after_hours_candle(self):
        regular, after = chart_prices("000660", self.chart, self.now)
        self.assertEqual([r["close"] for r in regular], [1472000, 1398000])
        self.assertEqual(regular[-1]["previousClose"], 1472000)
        self.assertEqual(regular[-1]["asOf"], "2026-11-03T15:30:00+09:00")
        self.assertEqual(after["price"], 1413000)
        self.assertEqual(after["session"], "AFTER_MARKET")

    def test_missing_1530_does_not_fall_back_to_1529_or_after_hours(self):
        self.chart["lastPriceInfos"] = []
        self.chart["priceInfos"] = [p for p in self.chart["priceInfos"] if p["localDateTime"] != "20261103153000"]
        with self.assertRaisesRegex(ValueError, "no verified"):
            chart_prices("000660", self.chart, self.now)

    def test_unfinished_day_cannot_be_published_as_closed(self):
        regular, after = chart_prices("000660", self.chart, datetime.fromisoformat("2026-11-03T15:00:00+09:00"))
        self.assertEqual([r["date"] for r in regular], ["2026-11-02"])
        self.assertEqual(after["date"], "2026-11-02")

    def test_ambiguous_snapshot_and_wrong_exchange_are_rejected(self):
        self.chart["priceInfos"][2]["highPrice"] = 1400000
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            chart_prices("000660", self.chart, self.now)

    def test_late_opening_day_does_not_assume_a_1530_close(self):
        self.chart["openTime"] = "20261103100000"
        regular, after = chart_prices("000660", self.chart, self.now)
        self.assertEqual([r["date"] for r in regular], ["2026-11-02"])
        self.assertEqual(after["date"], "2026-11-02")

    def test_truncated_previous_chart_is_checked_against_explicit_previous_close(self):
        self.chart["priceInfos"] = [point("20261103090000", 1400000)]
        self.chart["lastPriceInfos"] = [point("20261102153000", 1472000), point("20261102195900", 1480000)]
        regular, after = chart_prices("000660", self.chart, self.now)
        self.assertEqual(regular[0]["close"], 1472000)
        self.assertEqual(after["date"], "2026-11-02")
        self.chart["lastClosePrice"] = 1400000
        with self.assertRaisesRegex(ValueError, "no verified"):
            chart_prices("000660", self.chart, self.now)
        self.chart["stockExchangeType"] = "NXT"
        with self.assertRaisesRegex(ValueError, "KRX"):
            chart_prices("000660", self.chart, self.now)

    def test_nxt_after_hours_requires_correct_instrument_session_and_timestamp(self):
        info = {"tradingSessionType": "AFTER_MARKET", "overPrice": "1,414,000", "localTradedAt": "2026-11-03T18:59:12+09:00", "overMarketStatus": "OPEN"}
        payload = {"result": {"areas": [{"name": "SERVICE_ITEM", "datas": [{"cd": "000660", "nxtOverMarketPriceInfo": info}]}]}}
        quote = nxt_after_price("000660", payload, self.now)
        self.assertEqual(quote["price"], 1414000)
        self.assertEqual(quote["exchange"], "NXT")
        info["tradingSessionType"] = "PRE_MARKET"
        self.assertIsNone(nxt_after_price("000660", payload, self.now))
        with self.assertRaisesRegex(ValueError, "instrument"):
            nxt_after_price("005930", payload, self.now)
