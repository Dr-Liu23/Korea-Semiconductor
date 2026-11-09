import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("fetch_toss", ROOT / "scripts/fetch_toss.py")
toss = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(toss)


class InvestorDataTests(unittest.TestCase):
    def setUp(self):
        self.row = json.loads((ROOT / "data/samples/A000660_2026-11-03.browser.json").read_text(encoding="utf-8"))["result"]["body"][0]

    def test_source_arithmetic_and_missing_values(self):
        self.assertTrue(toss.audit([self.row])["mainBuySellNetIdentities"])
        self.row["netForeignerBuyVolume"] = None
        with self.assertRaisesRegex(ValueError, "missing investor data"):
            toss.audit([self.row])

    def test_institution_breakdown_corruption_is_rejected(self):
        self.row["netPensionFundBuyVolume"] += 1
        with self.assertRaisesRegex(ValueError, "breakdown does not match"):
            toss.audit([self.row])

    def test_today_excluded_before_close_even_if_source_marks_closed(self):
        rows, excluded = toss.completed_rows([self.row], datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc))
        self.assertEqual(rows, [])
        self.assertEqual(excluded, ["2026-11-03"])

    def test_intraday_flag_still_excluded_after_close(self):
        self.row["inMarketTime"] = True
        rows, _ = toss.completed_rows([self.row], datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(rows, [])

    def test_device_identifier_persists(self):
        with tempfile.TemporaryDirectory() as folder:
            value = toss.device_id(folder)
            self.assertRegex(value, r"^WTS-[0-9a-f]{32}$")
            self.assertEqual(value, toss.device_id(folder))


if __name__ == "__main__":
    unittest.main()
