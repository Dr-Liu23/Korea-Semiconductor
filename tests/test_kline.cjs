const assert = require("node:assert/strict");
const {test} = require("node:test");
const {movingAverage, weekStart, mergeLive, mergeNxt, mergeRecent, option, dataOption, summary} = require("../web/assets/kline.js");

const quote = (price, asOf = "2026-11-04T12:53:25.195+09:00", extra = {}) => ({price, asOf, date: asOf.slice(0, 10), session: "REGULAR_MARKET", marketStatus: "OPEN", ...extra});

test("after-hours KRX quotes extend the chart until the session ends", () => {
  const after = (price, asOf, extra = {}) => quote(price, asOf, {session: "AFTER_MARKET", ...extra});
  const day = [["2026-11-05", 100, 110, 90, 105, 500]];
  assert.equal(mergeLive(day, "day", after(120, "2026-11-05T16:02:03+09:00")), true);
  assert.deepEqual(day[0], ["2026-11-05", 100, 120, 90, 120, 500]);
  const minute = [["2026-11-05 16:01", 100, 100, 100, 100, 7]];
  assert.equal(mergeLive(minute, "minute", after(101, "2026-11-05T16:02:03+09:00")), true);
  assert.deepEqual(minute[1], ["2026-11-05 16:02", 101, 101, 101, 101, null]);
  assert.equal(mergeLive(minute, "minute", after(102, "2026-11-05T20:01:00+09:00")), false);
  assert.equal(mergeLive(minute, "minute", after(102, "2026-11-05T16:03:00+09:00", {marketStatus: "CLOSE"})), false);
  assert.equal(mergeLive(minute, "minute", quote(102, "2026-11-05T15:45:00+09:00")), false);
});

test("NXT quotes extend only their own line, and only during its session today", () => {
  const nxt = (price, asOf, extra = {}) => quote(price, asOf, {session: "AFTER_MARKET", ...extra});
  const state = {tradingDay: "2026-11-05", bars: [["2026-11-05 15:30", 90, 90, 90, 90, 1]], nxt: [["2026-11-05 15:41", 100]]};
  assert.equal(mergeNxt(state, "minute", nxt(101, "2026-11-05T15:41:30+09:00")), true);
  assert.deepEqual(state.nxt, [["2026-11-05 15:41", 101]]);
  assert.equal(mergeNxt(state, "minute", nxt(101, "2026-11-05T15:41:33+09:00")), false);
  assert.equal(mergeNxt(state, "minute", nxt(102, "2026-11-05T15:42:00+09:00")), true);
  assert.equal(state.nxt.length, 2);
  assert.equal(state.bars.length, 1);
  for (const [period, item] of [["day", nxt(9, "2026-11-05T15:43:00+09:00")], ["minute", nxt(9, "2026-11-04T15:43:00+09:00")], ["minute", nxt(9, "2026-11-05T20:01:00+09:00")],
    ["minute", nxt(9, "2026-11-05T15:43:00+09:00", {marketStatus: "CLOSE"})], ["minute", nxt(9, "2026-11-05T15:40:00+09:00")]]) assert.equal(mergeNxt(state, period, item), false);
  const five = {tradingDay: "2026-11-05", nxt: [["2026-11-05 15:40", 100]]};
  assert.equal(mergeNxt(five, "five", nxt(101, "2026-11-05T15:44:59+09:00")), true);
  assert.deepEqual(five.nxt, [["2026-11-05 15:40", 101]]);
  assert.equal(mergeNxt({tradingDay: "2026-11-05"}, "minute", nxt(9, "2026-11-05T15:43:00+09:00")), false);
});

test("intraday views draw NXT beside KRX, and the five-day axis includes NXT-only times", () => {
  const state = {tradingDay: "2026-11-05", previousClose: 95, bars: [["2026-11-05 15:30", 90, 90, 90, 90, 1], ["2026-11-05 16:00", 91, 91, 91, 91, 2]], nxt: [["2026-11-05 15:45", 100], ["2026-11-05 16:00", 101]]};
  const minute = option("minute", state);
  assert.deepEqual(minute.series.map(item => item.name), ["KRX", "成交量", "NXT"]);
  assert.deepEqual(minute.legend.data, ["KRX", "NXT"]);
  assert.equal(minute.series[2].data[(15 - 9) * 60 + 45], 100);
  assert.equal(minute.series[0].data[(15 - 9) * 60 + 45], null);
  const tip = minute.tooltip.formatter([{dataIndex: (15 - 9) * 60 + 45}]);
  assert.match(tip, /NXT/);
  assert.doesNotMatch(tip, /KRX|成交量/);
  const five = option("five", state);
  assert.deepEqual(five.xAxis[0].data, ["2026-11-05 15:30", "2026-11-05 15:45", "2026-11-05 16:00"]);
  assert.deepEqual(five.series[0].data, [90, null, 91]);
  assert.deepEqual(five.series[2].data, [null, 100, 101]);
  assert.match(five.tooltip.formatter([{dataIndex: 1}]), /NXT/);
  assert.equal(option("five", {...state, nxt: undefined}).series[2].data.every(value => value === null), true);
  assert.equal(option("day", {...state, bars: [["2026-11-05", 90, 91, 89, 90, 1]]}).series.some(item => item.name === "NXT"), false);
});

test("moving average needs a full window and uses closes", () => {
  const bars = [1, 2, 3, 4].map(close => ["d", 0, 0, 0, close, 0]);
  assert.deepEqual(movingAverage(bars, 3), [null, null, 2, 3]);
});

test("weeks start on Monday, including across a year boundary", () => {
  assert.equal(weekStart("2026-11-04"), "2026-11-02");
  assert.equal(weekStart("2026-11-08"), "2026-11-02");
  assert.equal(weekStart("2026-01-02"), "2025-12-29");
});

test("a live price extends the newest day bar's range and never changes its open or volume", () => {
  const bars = [["2026-11-04", 100, 110, 90, 105, 500]];
  assert.equal(mergeLive(bars, "day", quote(120)), true);
  assert.deepEqual(bars[0], ["2026-11-04", 100, 120, 90, 120, 500]);
  assert.equal(mergeLive(bars, "day", quote(80)), true);
  assert.deepEqual(bars[0], ["2026-11-04", 100, 120, 80, 80, 500]);
  assert.equal(mergeLive(bars, "day", quote(80)), false);
});

test("a closed market or a live price for another day changes nothing", () => {
  const bars = [["2026-11-03", 100, 110, 90, 105, 500]], before = JSON.stringify(bars);
  for (const [period, item] of [
    ["day", quote(120)],
    ["day", quote(120, "2026-11-03T12:00:00+09:00", {marketStatus: "CLOSE"})],
    ["minute", quote(120)],
    ["day", null]
  ]) assert.equal(mergeLive(bars, period, item), false);
  assert.equal(JSON.stringify(bars), before);
  assert.equal(mergeLive([], "day", quote(120)), false);
});

test("week and month bars take live prices within their own period only", () => {
  const week = [["2026-11-02", 100, 110, 90, 105, 500]];
  assert.equal(mergeLive(week, "week", quote(120, "2026-11-06T12:00:00+09:00")), true);
  assert.equal(mergeLive(week, "week", quote(130, "2026-11-09T12:00:00+09:00")), false);
  assert.equal(week[0][4], 120);
  const month = [["2026-11-02", 100, 110, 90, 105, 500]];
  assert.equal(mergeLive(month, "month", quote(120, "2026-11-30T12:00:00+09:00")), true);
  assert.equal(mergeLive(month, "month", quote(130, "2026-12-01T12:00:00+09:00")), false);
});

test("a new minute starts a bar with unknown volume and five-minute bars use their bucket", () => {
  const minute = [["2026-11-04 12:52", 100, 100, 100, 100, 7]];
  assert.equal(mergeLive(minute, "minute", quote(101)), true);
  assert.deepEqual(minute[1], ["2026-11-04 12:53", 101, 101, 101, 101, null]);
  const five = [["2026-11-04 12:50", 100, 100, 100, 100, 7]];
  assert.equal(mergeLive(five, "five", quote(101)), true);
  assert.equal(five.length, 1);
  assert.equal(five[0][4], 101);
  assert.equal(mergeLive(five, "five", quote(102, "2026-11-04T12:55:01+09:00")), true);
  assert.equal(five[1][0], "2026-11-04 12:55");
  const closed = [["2026-11-04", 100, 110, 90, 105, 500]];
  assert.equal(mergeLive(closed, "day", quote(999, "2026-11-04T15:30:00+09:00")), false);
  assert.equal(closed[0][4], 105);
  assert.equal(mergeLive(closed, "day", quote(106, "2026-11-04T15:29:59+09:00")), true);
  const ahead = [["2026-11-04 13:00", 100, 100, 100, 100, 7]];
  assert.equal(mergeLive(ahead, "minute", quote(101)), false);
});

test("fresh bars from the server replace overlapping rows, dropping locally built ones", () => {
  const bars = [["a", 1, 1, 1, 1, 1], ["b", 2, 2, 2, 2, 2], ["c", 3, 3, 3, 3, null]];
  assert.deepEqual(mergeRecent(bars, [["b", 9, 9, 9, 9, 9]]), [["a", 1, 1, 1, 1, 1], ["b", 9, 9, 9, 9, 9]]);
  assert.equal(mergeRecent(bars, []), bars);
});

test("candlesticks use ECharts' open, close, low, high order and show the newest window", () => {
  const bars = Array.from({length: 200}, (_, i) => [`day${i}`, 100, 130, 90, 120, i]);
  const built = option("day", {bars});
  assert.deepEqual(built.series[0].data[0], [100, 120, 90, 130]);
  assert.equal(built.series[0].type, "candlestick");
  assert.equal(built.dataZoom[0].startValue, 80);
  assert.equal(built.dataZoom[0].endValue, 199);
  assert.deepEqual(built.legend.data, ["MA5", "MA10", "MA20", "MA60"]);
  assert.equal(built.series.at(-1).yAxisIndex, 1);
  assert.equal(built.series.length, dataOption("day", {bars}).series.length);
  assert.equal(dataOption("day", {bars}).dataZoom, undefined);
});

test("day tooltip shows a verified regular close beside the K-line close", () => {
  const state = {bars: [["2026-11-02", 1, 1, 1, 1480000, 1], ["2026-11-03", 1480000, 1500000, 1390000, 1414000, 5]], closes: new Map([["2026-11-03", 1398000]])};
  const html = option("day", state).tooltip.formatter([{dataIndex: 1}]);
  assert.match(html, /收盘（K线）.*1,414,000/);
  assert.match(html, /KRX常规收盘（已核实）.*1,398,000/);
  assert.match(html, /-4\.46%/);
  assert.doesNotMatch(option("week", state).tooltip.formatter([{dataIndex: 1}]), /常规收盘/);
  assert.doesNotMatch(option("day", state).tooltip.formatter([{dataIndex: 0}]), /常规收盘/);
});

test("the day view spans the whole session and marks the close and baseline", () => {
  const state = {previousClose: 1398000, bars: [["2026-11-04 09:00", 1458000, 1469000, 1458000, 1468000, 107957], ["2026-11-04 12:52", 1305000, 1305000, 1305000, 1305000, 10]]};
  const built = option("minute", state), data = built.series[0].data;
  assert.equal(built.xAxis[0].data[0], "09:00");
  assert.equal(built.xAxis[0].data.at(-1), "20:00");
  assert.equal(data[0], 1468000);
  assert.equal(data.at(-1), null);
  assert.equal(built.series[0].lineStyle.color, "#168568");
  assert.deepEqual(built.series[0].markLine.data.map(mark => mark.xAxis ?? mark.yAxis), ["15:30", 1398000]);
  assert.equal(built.yAxis[0].max({min: 1300000, max: 1390000}), 1398000);
  assert.match(built.tooltip.formatter([{dataIndex: 0}]), /\+5\.01%/);
  assert.equal(option("minute", {previousClose: null, bars: state.bars}).series[0].markLine.data.length, 1);
});

test("the five-day view labels each trading day once and an empty view explains itself", () => {
  const bars = [["2026-11-03 09:00", 1, 1, 1, 1, 1], ["2026-11-03 09:05", 1, 1, 1, 1, 1], ["2026-11-04 09:00", 1, 1, 1, 1, 1]];
  const axis = option("five", {bars}).xAxis[0];
  assert.deepEqual(bars.map((bar, index) => axis.axisLabel.interval(index, bar[0])), [true, false, true]);
  assert.equal(axis.axisLabel.formatter(bars[2][0]), "11-04");
  assert.match(option("day", {bars: []}).title.text, /同步中/);
});

test("summary compares with the prior bar, or the session baseline for the day view", () => {
  const bars = [["a", 100, 110, 90, 100, 1], ["b", 100, 130, 95, 120, 1]];
  assert.equal(Math.round(summary("day", {bars}).change), 20);
  const minutes = [["2026-11-04 09:00", 100, 105, 99, 104, 1], ["2026-11-04 09:01", 104, 108, 103, 107, 1]];
  const result = summary("minute", {bars: minutes, previousClose: 100});
  assert.deepEqual([result.open, result.high, result.low, result.close, Math.round(result.change)], [100, 108, 99, 107, 7]);
  assert.equal(summary("minute", {bars: minutes, previousClose: null}).change, null);
  assert.equal(summary("day", {bars: []}), null);
});
