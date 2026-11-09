"""Diagnose anonymous Toss initialization in an isolated backend browser.

Only public market data is requested. No personal browser profile or persisted
session is loaded, and Cookie values are never written to the report.
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright


ORIGIN = "https://wts-info-api.tossinvest.com"
TREND = "/api/v1/stock-infos/trade/trend/trading-trend"
URL = ORIGIN + TREND + "?productCode=A000660&size=1"


async def summarize(response):
    try:
        payload = await response.json()
    except Exception:
        return {"status": response.status, "json": False}
    result = payload.get("result")
    rows = result.get("body") if isinstance(result, dict) else None
    return {
        "status": response.status,
        "json": True,
        "rows": len(rows) if isinstance(rows, list) else None,
        "dates": [row.get("baseDate") for row in rows] if isinstance(rows, list) else [],
        "error": payload.get("error"),
    }


async def main():
    report = {"startedAt": datetime.now(timezone.utc).isoformat(), "events": []}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True, args=["--disable-dev-shm-usage"]
        )
        context = await browser.new_context(locale="ko-KR", timezone_id="Asia/Seoul")
        page = await context.new_page()
        relevant = []
        trend_seen = asyncio.Event()

        def received(response):
            parsed = urlsplit(response.url)
            if parsed.hostname and parsed.hostname.endswith("tossinvest.com"):
                if "/api/" in parsed.path:
                    report["events"].append(
                        {"host": parsed.hostname, "path": parsed.path, "status": response.status}
                    )
                    if parsed.path == TREND:
                        relevant.append(response)
                        trend_seen.set()

        page.on("response", received)
        page.on("pageerror", lambda error: report.setdefault("pageErrors", []).append(str(error)[:300]))
        report["directBeforeVisit"] = await summarize(await context.request.get(URL, timeout=20000))
        print(json.dumps({"step": "directBeforeVisit", **report["directBeforeVisit"]}), flush=True)

        try:
            response = await page.goto(
                "https://www.tossinvest.com/stocks/A000660/transaction-status",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            report["pageStatus"] = response.status if response else None
            try:
                await asyncio.wait_for(trend_seen.wait(), timeout=45)
            except asyncio.TimeoutError:
                report["trendResponseObserved"] = False
            report["title"] = await page.title()
            report["pageUrl"] = page.url
            report["browserTrendResponses"] = [await summarize(r) for r in relevant]
            report["cookieNamesAfterVisit"] = [c["name"] for c in await context.cookies()]
            report["directAfterVisit"] = await summarize(await context.request.get(URL, timeout=20000))
            print(json.dumps({"step": "directAfterVisit", **report["directAfterVisit"]}), flush=True)
            # Also test through Chromium's own navigation/network stack.
            api_page = await context.new_page()
            response = await api_page.goto(URL, wait_until="domcontentloaded", timeout=20000)
            report["browserNavigationAfterVisit"] = await summarize(response) if response else None
            await api_page.close()
        finally:
            report["events"] = report["events"][:100]
            await browser.close()
            path = Path("toss-browser-probe.json")
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
