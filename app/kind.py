"""Read KIND's treasury-share tables. KIND has no API, so a changed page must fail loudly."""
import html
import re

KIND_URL = "https://kind.krx.co.kr/corpgeneral/treasurystk.do"
ISSUERS = {
    "A000660": ("00066", "SK하이닉스", "KR7000660001"),
    "A005930": ("00593", "삼성전자", "KR7005930003"),
}
METHODS = {"decl": "searchDeclOfTreasuryStkAcqDisp", "trd": "searchTrdOfTreasuryStkAcqDisp"}
DECLARED_TITLE = "신고일,종목명,자사주/취득·처분<br />구분,기간,신고수량,체결수량누계,체결수량<br />비율누계,체결금액누계"
TRADED_TITLE = "매매일,종목명,자사주/취득·처분<br />구분,신청수량,당일체결수량,체결율(%)"
NO_RESULTS = "조회된 결과값이 없습니다"
ACQUIRED = "직접 / 취득"


def kind_form(code, kind, start, end):
    issuer, name, isin = ISSUERS[code]
    return {"method": METHODS[kind], "searchGubun": kind, "searchCodeType": "char", "repIsuSrtCd": code, "isurCd": issuer,
            "searchCorpName": name, "corpName": name, "comAbbrv": name, "repIsuCd": isin, "pageIndex": "1", "currentPageSize": "100",
            "orderMode": "1", "orderStat": "D", "marketType": "all", "trstkGubun": "all", "acqDispGubun": "all", "fromDate": start, "toDate": end}


def text_of(cell):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cell)).replace("\xa0", " ")).strip()


def number(value):
    if not re.fullmatch(r"\d{1,3}(,\d{3})*|\d+", value):
        raise ValueError(f"unexpected KIND number: {value!r}")
    return int(value.replace(",", ""))


def table_rows(page, title, width, code):
    if NO_RESULTS in page:
        return []
    if f"fn_InitTitle('{title}'" not in page:
        raise ValueError("unexpected KIND page: table title not found")
    rows = []
    for row in re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", page):
        cells = [text_of(cell) for cell in re.findall(r"<td[^>]*>([\s\S]*?)</td>", row)]
        if not cells:
            continue
        if len(cells) != width:
            raise ValueError(f"unexpected KIND row width: {len(cells)}")
        if cells[1] != ISSUERS[code][1]:
            raise ValueError(f"KIND returned another company: {cells[1]}")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[0]):
            raise ValueError(f"unexpected KIND date: {cells[0]!r}")
        if cells[2] == ACQUIRED:
            rows.append(cells)
    return rows


def parse_declared(page, code):
    batches = []
    for day, _, _, period, planned, traded, _, amount in table_rows(page, DECLARED_TITLE, 8, code):
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", period)
        if len(dates) != 2:
            raise ValueError(f"unexpected KIND period: {period!r}")
        batches.append({"declaredOn": day, "periodStart": dates[0], "periodEnd": dates[1], "plannedShares": number(planned),
                        "tradedShares": number(traded), "tradedKrw": number(amount)})
    return batches


def parse_trades(page, code):
    return [(day, number(traded)) for day, _, _, _, traded, _ in table_rows(page, TRADED_TITLE, 6, code)]


def last_trades(batches, trades):
    # Execution periods overlap once a batch finishes early, so the next batch's start ends this one.
    ordered = sorted(batches, key=lambda batch: batch["periodStart"])
    latest = {}
    for batch, following in zip(ordered, ordered[1:] + [None]):
        days = [day for day, shares in trades if shares > 0 and batch["periodStart"] <= day <= batch["periodEnd"]
                and (not following or day < following["periodStart"])]
        latest[batch["declaredOn"]] = max(days, default=None)
    return latest
