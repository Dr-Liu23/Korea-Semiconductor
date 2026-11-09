"""Fetch completed Korean investor-trading days using an anonymous device cookie.

Python standard library only. The device identifier follows the public web
client's WTS-<uuid hex> convention; it is not a Toss account credential.
"""

import argparse
import json
import re
import time
import uuid
from datetime import date, datetime, time as day_time, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ENDPOINT = "https://wts-info-api.tossinvest.com/api/v1/stock-infos/trade/trend/trading-trend"
KST = timezone(timedelta(hours=9))
INSTITUTIONS = (
    "financialInvestment", "insurance", "trust", "privateEquityFund", "bank",
    "pensionFund", "otherFinancialInstitutions",
)


def normalize_code(code):
    value = code.upper().removeprefix("A")
    if not re.fullmatch(r"\d{6}", value):
        raise ValueError("product code must contain six digits, optionally prefixed with A")
    return "A" + value


def device_id(state_dir):
    path = Path(state_dir) / "toss-device-id"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("WTS-" + uuid.uuid4().hex + "\n", encoding="ascii")
        path.chmod(0o600)
    value = path.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"WTS-[0-9a-f]{32}", value):
        raise ValueError(f"invalid anonymous device identifier in {path}")
    return value


def net_field(prefix):
    return "net" + prefix[0].upper() + prefix[1:] + "BuyVolume"


def completed_rows(rows, now):
    local = now.astimezone(KST)
    chosen, excluded = [], []
    for row in rows:
        trade_date = date.fromisoformat(row["baseDate"])
        same_day_open = trade_date == local.date() and local.time() < day_time(15, 30)
        if trade_date > local.date() or same_day_open or row.get("inMarketTime") is not False:
            excluded.append(row["baseDate"])
        else:
            chosen.append(row)
    return sorted(chosen, key=lambda row: row["baseDate"]), sorted(set(excluded))


def audit(rows):
    if not rows:
        raise ValueError("no completed trading days")
    for row in rows:
        for prefix, flag in (
            ("individuals", "hasIndividual"),
            ("foreigner", "hasForeigner"),
            ("institution", "hasInstitution"),
        ):
            fields = (prefix + "BuyVolume", prefix + "SellVolume", net_field(prefix))
            if row.get(flag) is not True or any(type(row.get(k)) is not int for k in fields):
                raise ValueError(f"missing investor data: {row['baseDate']} {prefix}")
            if row[fields[0]] - row[fields[1]] != row[fields[2]]:
                raise ValueError(f"buy/sell/net mismatch: {row['baseDate']} {prefix}")
    latest = rows[-1]
    fields = [p + "BuyVolume" for p in INSTITUTIONS] + [net_field(p) for p in INSTITUTIONS]
    if any(type(latest.get(k)) is not int for k in fields):
        raise ValueError("latest institutional breakdown is incomplete")
    buy_sum = sum(latest[p + "BuyVolume"] for p in INSTITUTIONS)
    net_sum = sum(latest[net_field(p)] for p in INSTITUTIONS)
    if buy_sum != latest["institutionBuyVolume"] or net_sum != latest["netInstitutionBuyVolume"]:
        raise ValueError("latest institutional breakdown does not match its total")
    return {
        "mainBuySellNetIdentities": True,
        "latestInstitutionBuySumMatches": True,
        "latestInstitutionNetSumMatches": True,
        "latestSourceUpdatedAt": latest["updatedAt"],
        "unit": "shares",
    }


class TossClient:
    def __init__(self, state_dir, timeout=20):
        self.device = device_id(state_dir)
        self.timeout = timeout

    def page(self, product_code, size=60, cursor=None):
        params = {"productCode": normalize_code(product_code), "size": size}
        if cursor:
            # Follow the exact cursor returned by Toss, without deriving dates.
            params.update({k: cursor[k] for k in ("number", "key") if cursor.get(k) is not None})
        url = ENDPOINT + "?" + urlencode(params)
        request = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; KoreaDashboard/0.1)",
            "Accept": "application/json",
            "Cookie": "deviceId=" + self.device,
        })
        try:
            with urlopen(request, timeout=self.timeout) as response:
                content = response.read(16 * 1024 * 1024 + 1)
                if len(content) > 16 * 1024 * 1024:
                    raise ValueError("unexpectedly large Toss response")
                payload = json.loads(content)
        except HTTPError as error:
            # Never rotate identifiers or repeatedly retry a rejected request.
            detail = error.read(1000).decode("utf-8", "replace")
            raise RuntimeError(f"Toss HTTP {error.code}: {detail}") from error
        result = payload.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("body"), list):
            raise ValueError("unexpected Toss result.body response shape")
        if type(result.get("lastPage")) is not bool:
            raise ValueError("missing Toss pagination status")
        return url, payload

    def history(self, product_code, days=60, page_size=60):
        if days < 1 or not 1 <= page_size <= 120:
            raise ValueError("days must be positive; page size must be between 1 and 120")
        now = datetime.now(timezone.utc)
        pages, by_date, seen_cursors = [], {}, set()
        cursor = None
        size = min(page_size, days + 1)
        while True:
            url, payload = self.page(product_code, size=size, cursor=cursor)
            result = payload["result"]
            pages.append({"url": url, "httpStatus": 200, "response": payload})
            for row in result["body"]:
                key = row["baseDate"]
                previous = by_date.get(key)
                if previous is not None and previous != row:
                    raise ValueError(f"conflicting duplicate trading date: {key}")
                by_date[key] = row
            selected, excluded = completed_rows(list(by_date.values()), now)
            if len(selected) >= days or result["lastPage"]:
                break
            cursor = result.get("pagingParam")
            if not isinstance(cursor, dict) or cursor.get("number") is None or cursor.get("key") is None:
                raise ValueError("missing next-page cursor")
            marker = (cursor["number"], cursor["key"])
            if marker in seen_cursors:
                raise ValueError("Toss pagination cursor repeated")
            seen_cursors.add(marker)
            if len(pages) >= 1000:
                raise ValueError("pagination safety limit reached")
            time.sleep(0.5)
        if len(selected) < days:
            raise ValueError(f"requested {days} complete days; source only returned {len(selected)}")
        selected = selected[-days:]
        checks = audit(selected)
        return {
            "productCode": normalize_code(product_code),
            "sourceKind": "toss_http_live",
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
            "rows": selected,
            "checks": {**checks, "completedDays": len(selected), "excludedUnfinishedDates": excluded},
            "pages": pages,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product-code", required=True)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--page-size", type=int, default=60)
    parser.add_argument("--state-dir", type=Path, default=Path(".state"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = TossClient(args.state_dir).history(args.product_code, args.days, args.page_size)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({
        "productCode": payload["productCode"], "days": len(payload["rows"]),
        "firstDate": payload["rows"][0]["baseDate"], "lastDate": payload["rows"][-1]["baseDate"],
        "pages": len(payload["pages"]), "checks": payload["checks"],
        "output": str(args.output),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
