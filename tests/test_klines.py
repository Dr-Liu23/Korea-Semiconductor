"""K-line cleaning must drop bad source rows and never invent or repair prices."""
import unittest
from datetime import date, datetime, timedelta, timezone

from app.klines import aggregate, latest_days, parse_daily, parse_minutes

KST = timezone(timedelta(hours=9))
TODAY = date(2026, 11, 4)


def day(value, o=100, h=110, l=90, c=105, v=1000):
    return {"localDate": value, "openPrice": o, "highPrice": h, "lowPrice": l, "closePrice": c, "accumulatedTradingVolume": v}


def minute(value, o=100, h=110, l=90, c=105, v=10):
    return {"localDateTime": value, "openPrice": o, "highPrice": h, "lowPrice": l, "currentPrice": c, "accumulatedTradingVolume": v}


class DailyTests(unittest.TestCase):
    def test_halted_days_with_zero_prices_are_dropped(self):
        # Samsung's 2018 split halt reports a close with zero open, high and low.
        bars = parse_daily([day("20180427", 53000, 53900, 52000, 53000), day("20180430", 0, 0, 0, 53000, 0), day("20180504", 53000, 53900, 51800, 51900)], TODAY)
        self.assertEqual([bar[0] for bar in bars], ["2018-04-27", "2018-05-04"])

    def test_unadjusted_rows_before_a_discontinuity_are_cut(self):
        # Samsung 1990-02-28 closes at 43500, then 423 on the next adjusted row.
        bars = parse_daily([day("19900227", 43000, 44000, 42000, 43400), day("19900228", 43400, 44000, 43000, 43500), day("19900302", 420, 430, 415, 423), day("19900303", 423, 430, 420, 425)], TODAY)
        self.assertEqual([bar[0] for bar in bars], ["1990-03-02", "1990-03-03"])

    def test_a_limit_move_is_kept(self):
        self.assertEqual(len(parse_daily([day("20261102", c=100), day("20261103", o=100, h=130, l=100, c=130)], TODAY)), 2)

    def test_rounding_in_adjusted_history_is_kept_as_published(self):
        # Close one won under the low: real source rounding, shown unmodified.
        bars = parse_daily([day("20030311", 3141, 3337, 3141, 3140)], TODAY)
        self.assertEqual(bars[0][1:5], [3141, 3337, 3141, 3140])

    def test_inconsistent_range_and_future_rows_are_dropped(self):
        self.assertEqual(parse_daily([day("20261103"), day("20261102", 100, 110, 104, 105), day("20261105")], TODAY)[0][0], "2026-11-03")
        self.assertEqual(len(parse_daily([day("20261103"), day("20261105")], TODAY)), 1)

    def test_overflowed_or_missing_volume_stays_missing_and_zero_stays_zero(self):
        rows = [day("20261101", v=2147483647), day("20261102", v=None), day("20261103", v=0)]
        self.assertEqual([bar[5] for bar in parse_daily(rows, TODAY)], [None, None, 0])

    def test_bad_response_shapes_are_rejected(self):
        for rows in ({"code": "BAD"}, [], [day("20261199x")], [{"openPrice": 1}], [day("20180430", 0, 0, 0, 53000)]):
            with self.assertRaises(ValueError):
                parse_daily(rows, TODAY)


class MinuteTests(unittest.TestCase):
    NOW = datetime(2026, 11, 4, 12, 30, tzinfo=KST)

    def test_bars_use_per_bar_volume_and_exclude_future_minutes(self):
        bars = parse_minutes([minute("20261104123000", v=7), minute("20261104090000", v=5), minute("20261104123100")], self.NOW)
        self.assertEqual(bars, [["2026-11-04 09:00", 100, 110, 90, 105, 5], ["2026-11-04 12:30", 100, 110, 90, 105, 7]])

    def test_empty_day_is_valid_but_bad_shape_is_not(self):
        self.assertEqual(parse_minutes([], self.NOW), [])
        for rows in ({"code": "BAD"}, [minute("20261104")]):
            with self.assertRaises(ValueError):
                parse_minutes(rows, self.NOW)

    def test_latest_days_keeps_whole_trading_days(self):
        bars = [[f"2026-10-{d:02d} 09:00", 1, 1, 1, 1, 1] for d in range(20, 28)] + [["2026-10-27 09:05", 1, 1, 1, 1, 1]]
        kept = latest_days(bars, 5)
        self.assertEqual(sorted({bar[0][:10] for bar in kept}), [f"2026-10-{d}" for d in range(23, 28)])
        self.assertEqual(len(kept), 6)


class AggregateTests(unittest.TestCase):
    BARS = [["2026-10-30", 10, 12, 9, 11, 100], ["2026-11-02", 11, 15, 10, 14, 200], ["2026-11-03", 14, 16, 13, 13, 300], ["2026-11-09", 13, 13, 12, 12, 400]]

    def test_week_bars_use_iso_weeks_and_first_open_last_close(self):
        self.assertEqual(aggregate(self.BARS, "week"), [
            ["2026-10-30", 10, 12, 9, 11, 100],
            ["2026-11-02", 11, 16, 10, 13, 500],
            ["2026-11-09", 13, 13, 12, 12, 400]])

    def test_month_bars_cross_week_boundaries(self):
        self.assertEqual(aggregate(self.BARS, "month"), [
            ["2026-10-30", 10, 12, 9, 11, 100],
            ["2026-11-02", 11, 16, 10, 12, 900]])

    def test_week_spanning_new_year_stays_together(self):
        bars = [["2025-12-30", 1, 1, 1, 1, 1], ["2026-01-02", 2, 2, 2, 2, 2]]
        self.assertEqual(len(aggregate(bars, "week")), 1)
        self.assertEqual(len(aggregate(bars, "month")), 2)

    def test_unknown_volume_is_not_summed_as_zero(self):
        self.assertIsNone(aggregate([["2026-11-02", 1, 1, 1, 1, None], ["2026-11-03", 1, 1, 1, 1, 5]], "week")[0][5])


if __name__ == "__main__":
    unittest.main()
