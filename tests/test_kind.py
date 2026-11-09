import unittest
from pathlib import Path

from app.kind import DECLARED_TITLE, TRADED_TITLE, kind_form, last_trades, number, parse_declared, parse_trades

FIXTURES = Path(__file__).parent / "fixtures"


def declared_row(day="2026-10-28", name="SK하이닉스", kind="직접 / 취득", amount="1,449,883,471,000"):
    return f"""<tr><td class="first txc">{day}</td>
    <td title="{name}"><img src='/images/common/icn_t_yu.gif' alt='유가증권'> <a id="companysum" href="#companysum" title='{name}'> {name}</a> <img alt='V100'></td>
    <td class="txc"> {kind} </td><td class="txc"> &nbsp;&nbsp;&nbsp;2026-10-29<br/> ~ 2027-01-28 </td>
    <td class="txr"> 14,920,000 </td><td class="txr">1,000,000</td><td class="txr"> 6.7 % </td><td class="txr">{amount}</td></tr>"""


def declared_page(*rows, title=DECLARED_TITLE):
    return f"""<table class="list"><thead><tr class="first" id="title-contents"></tr></thead><tbody>{''.join(rows)}</tbody></table>
    <script>fn_InitTitle('{title}', 'true,true,false');</script>"""


def traded_page(*rows):
    cells = "".join(f"<tr><td>{day}</td><td>SK하이닉스</td><td>직접 / 취득</td><td>250,000</td><td>{shares}</td><td>100</td></tr>" for day, shares in rows)
    return f"<table><tbody>{cells}</tbody></table><script>fn_InitTitle('{TRADED_TITLE}', 'true');</script>"


class KindTests(unittest.TestCase):
    def test_saved_real_pages_parse(self):
        # Captured from KIND on 2026-11-04; hand-written pages above cannot catch a wrong table title.
        declared = parse_declared((FIXTURES / "kind_declared_hynix.html").read_text(encoding="utf-8"), "A000660")
        self.assertEqual({batch["declaredOn"]: batch["tradedKrw"] for batch in declared}["2026-08-19"], 40004339063500)
        october = next(batch for batch in declared if batch["declaredOn"] == "2026-10-28")
        self.assertEqual((october["periodStart"], october["periodEnd"], october["plannedShares"]), ("2026-10-29", "2027-01-28", 14920000))
        self.assertGreaterEqual(october["tradedShares"], 1000000)
        trades = parse_trades((FIXTURES / "kind_traded_hynix.html").read_text(encoding="utf-8"), "A000660")
        self.assertEqual(trades[0], ("2026-10-29", 250000))
        self.assertEqual(last_trades(declared, trades)["2026-08-19"], None)

    def test_declared_batches_keep_the_cumulative_figures(self):
        self.assertEqual(parse_declared(declared_page(declared_row()), "A000660"), [{
            "declaredOn": "2026-10-28", "periodStart": "2026-10-29", "periodEnd": "2027-01-28",
            "plannedShares": 14920000, "tradedShares": 1000000, "tradedKrw": 1449883471000}])

    def test_only_direct_acquisitions_count(self):
        page = declared_page(declared_row(kind="직접 / 처분"), declared_row(kind="신탁 / 취득"), declared_row())
        self.assertEqual(len(parse_declared(page, "A000660")), 1)

    def test_no_results_is_empty_but_any_other_page_fails(self):
        self.assertEqual(parse_declared("<td>조회된 결과값이 없습니다.</td>", "A000660"), [])
        with self.assertRaisesRegex(ValueError, "table title"):
            parse_declared("<td>잠시 후 다시 이용해 주세요.</td>", "A000660")
        with self.assertRaisesRegex(ValueError, "table title"):
            parse_declared(declared_page(declared_row(), title="신고일,종목명,체결금액"), "A000660")

    def test_misread_rows_fail_rather_than_store_a_wrong_number(self):
        for row, message in (
            (declared_row(name="삼성전자"), "another company"),
            (declared_row(day="어제"), "date"),
            (declared_row(amount="-"), "number"),
            ("<tr><td>2026-10-28</td><td>SK하이닉스</td></tr>", "width"),
        ):
            with self.assertRaisesRegex(ValueError, message):
                parse_declared(declared_page(row), "A000660")
        self.assertEqual(number("1,000,000"), 1000000)
        self.assertEqual(number("0"), 0)

    def test_trades_end_where_the_next_batch_starts(self):
        batches = [
            {"declaredOn": "2026-08-19", "periodStart": "2026-08-20", "periodEnd": "2026-11-19"},
            {"declaredOn": "2026-10-28", "periodStart": "2026-10-29", "periodEnd": "2027-01-28"},
            {"declaredOn": "2027-02-01", "periodStart": "2027-02-02", "periodEnd": "2027-04-30"},
        ]
        trades = parse_trades(traded_page(("2026-10-21", "250,000"), ("2026-10-29", "250,000"), ("2026-11-03", "250,000"), ("2026-11-04", "0")), "A000660")
        self.assertEqual(last_trades(batches, trades), {"2026-08-19": "2026-10-21", "2026-10-28": "2026-11-03", "2027-02-01": None})

    def test_query_names_the_issuer_and_method(self):
        form = kind_form("A005930", "trd", "2026-08-01", "2026-11-04")
        self.assertEqual((form["method"], form["isurCd"], form["repIsuCd"], form["fromDate"], form["toDate"]),
                         ("searchTrdOfTreasuryStkAcqDisp", "00593", "KR7005930003", "2026-08-01", "2026-11-04"))


if __name__ == "__main__":
    unittest.main()
