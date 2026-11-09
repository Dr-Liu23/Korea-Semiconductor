"""Parse Naver chart bars and derive week/month K-lines from one cleaned daily series.

Naver daily closes keep moving after hours, so these bars must never feed the
verified regular_closes table. Bad source rows are dropped, never repaired.
"""
import math
import re
from datetime import date

BASE_URL = "https://api.stock.naver.com/chart/domestic/item/"
# The source reports volumes which overflowed a signed 32-bit counter at this value.
VOLUME_OVERFLOW = 2147483647
# Rounding of split-adjusted history legitimately breaks high/low by up to ~0.4%.
RANGE_TOLERANCE = 0.01
# KRX price limits make a larger move impossible; it marks unadjusted early rows.
MAX_DAILY_MOVE = 0.5


def nxt_minute(quote):
    """Minute a live NXT quote belongs to. No source offers NXT bars, so we keep our own."""
    at = quote["asOf"]
    if quote["exchange"] != "NXT" or quote["marketStatus"] != "OPEN" or not "09:00" <= at[11:16] <= "20:00":
        return None
    return f"{at[:10]} {at[11:16]}"


def nxt_series(rows, period):
    # The last quote of each bucket, labelled like the source's five-minute bars.
    closes = {}
    for at, close in rows:
        closes[at[:15] + str(int(at[15]) // 5 * 5) if period == "five" else at] = close
    return [[at, close] for at, close in closes.items()]


def history_url(symbol, period, start, end):
    return f"{BASE_URL}{symbol}/{period}?startDateTime={start:%Y%m%d}0000&endDateTime={end:%Y%m%d}2359"


def clean_bar(at, row, close_key):
    prices = [row.get(key) for key in ("openPrice", "highPrice", "lowPrice", close_key)]
    if not all(type(p) in (int, float) and math.isfinite(p) and p > 0 and int(p) == p for p in prices):
        return None
    opening, high, low, closing = (int(p) for p in prices)
    edge_low, edge_high = min(opening, closing), max(opening, closing)
    if low > edge_low * (1 + RANGE_TOLERANCE) or high < edge_high * (1 - RANGE_TOLERANCE):
        return None
    volume = row.get("accumulatedTradingVolume")
    if type(volume) is not int or not 0 <= volume < VOLUME_OVERFLOW:
        volume = None  # Unknown stays unknown rather than plotting an overflowed value.
    return [at, opening, high, low, closing, volume]


def parse_daily(rows, today):
    if not isinstance(rows, list):
        raise ValueError("unexpected daily chart response shape")
    bars = {}
    for row in rows:
        value = row.get("localDate") if isinstance(row, dict) else None
        if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
            raise ValueError("invalid daily chart date")
        at = f"{value[:4]}-{value[4:6]}-{value[6:]}"
        bar = clean_bar(at, row, "closePrice")
        if bar and at <= today.isoformat():
            bars[at] = bar
    ordered = [bars[at] for at in sorted(bars)]
    for index in range(len(ordered) - 1, 0, -1):
        if abs(ordered[index][4] / ordered[index - 1][4] - 1) > MAX_DAILY_MOVE:
            ordered = ordered[index:]
            break
    if not ordered:
        raise ValueError("no valid daily bars")
    return ordered


def parse_minutes(rows, now):
    if not isinstance(rows, list):
        raise ValueError("unexpected minute chart response shape")
    limit = now.strftime("%Y-%m-%d %H:%M")
    bars = {}
    for row in rows:
        value = row.get("localDateTime") if isinstance(row, dict) else None
        if not isinstance(value, str) or not re.fullmatch(r"\d{14}", value):
            raise ValueError("invalid minute chart time")
        at = f"{value[:4]}-{value[4:6]}-{value[6:8]} {value[8:10]}:{value[10:12]}"
        bar = clean_bar(at, row, "currentPrice")
        if bar and at <= limit:
            bars[at] = bar
    return [bars[at] for at in sorted(bars)]


def period_key(at, period):
    return tuple(date.fromisoformat(at).isocalendar()[:2]) if period == "week" else at[:7]


def aggregate(bars, period):
    groups = {}
    for bar in bars:
        groups.setdefault(period_key(bar[0], period), []).append(bar)
    result = []
    for part in groups.values():
        volumes = [bar[5] for bar in part]
        result.append([part[0][0], part[0][1], max(bar[2] for bar in part), min(bar[3] for bar in part), part[-1][4],
                       None if None in volumes else sum(volumes)])
    return result


def latest_days(bars, count):
    days = sorted({bar[0][:10] for bar in bars})[-count:]
    return [bar for bar in bars if bar[0][:10] in days]
