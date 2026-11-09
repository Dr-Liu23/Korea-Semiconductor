const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {test} = require("node:test");
const flowDetails = require("../web/assets/flow-detail.js");
const kline = require("../web/assets/kline.js");

const today = "2026-11-04";
function dashboard() {
  const rows = Array.from({length: 25}, (_, i) => ({baseDate: `2026-10-${String(i + 1).padStart(2, "0")}`, updatedAt: "2026-10-25T18:00:00+09:00"}));
  return {
    stocks: ["A000660", "A005930"].map(code => ({code, symbol: code.slice(1), name: code, english: code, color: "#000", rows, latest: rows.at(-1), regularClose: dashboardClose})),
    documents: [], repurchases: dashboardRepurchases, status: dashboardStatus, updateSeconds: 1800, kindUpdateSeconds: 21600
  };
}
let dashboardStatus = [], dashboardRepurchases = [], dashboardClose = null;
function bars(url) {
  if (url.includes("/minute")) return [[`${today} 09:00`, 100, 110, 90, 105, 5]];
  if (url.includes("limit=3")) return [[today, 100, 110, 90, 106, 9]];
  return [["2026-11-03", 90, 100, 80, 95, 7], [today, 100, 110, 90, 105, 5]];
}

// Just enough DOM to run the real page script without a browser.
function startPage() {
  const elements = new Map(), documentListeners = {}, intervals = new Map(), requests = [], drawn = [];
  const element = () => {
    const listeners = {};
    return {
      listeners, children: [], dataset: {}, classList: {toggle() {}}, setAttribute() {},
      querySelector: () => null, querySelectorAll: () => [],
      addEventListener: (type, listener) => { listeners[type] = listener; }
    };
  };
  const select = selector => {
    if (!elements.has(selector)) elements.set(selector, element());
    return elements.get(selector);
  };
  let livePrice = 120, previousClose = 95, quoteAsOf = `${today}T12:53:25+09:00`, quoteSessionName = "REGULAR_MARKET";
  const context = {
    document: {
      activeElement: null, hidden: false, querySelector: select, querySelectorAll: () => [],
      getElementById: id => select("#" + id),
      addEventListener: (type, listener) => { documentListeners[type] = listener; }
    },
    window: {addEventListener() {}},
    echarts: {init: () => ({setOption: (option, notMerge) => drawn.push({option, notMerge}), resize() {}})},
    KoreaFlowDetails: flowDetails,
    KoreaKline: kline,
    IntersectionObserver: class { observe() {} },
    fetch: async url => {
      requests.push(url);
      if (url.includes("/api/kline/")) return {ok: true, json: async () => ({bars: bars(url), previousClose, tradingDay: today})};
      if (url.includes("/api/quotes")) return {ok: true, json: async () => ({
        stocks: [{code: "A000660", afterHours: [], liveQuotes: [{exchange: "KRX", price: livePrice, asOf: quoteAsOf, date: today, session: quoteSessionName, marketStatus: "OPEN", changePercent: 20, previousClose: 100}]}],
        status: [], livePolling: {revision: String(livePrice) + quoteAsOf + quoteSessionName, intervalSeconds: 1}
      })};
      return {ok: true, json: async () => dashboard()};
    },
    setInterval: (callback, delay) => { intervals.set(delay, callback); }, setTimeout, clearTimeout, AbortSignal, console
  };
  vm.runInNewContext(fs.readFileSync("web/assets/app.js", "utf8"), context);
  const settled = async () => { for (let i = 0; i < 8; i++) await new Promise(setImmediate); };
  const click = (attribute, dataset) => documentListeners.click({target: {closest: selector => selector === `[${attribute}]` ? {dataset} : null}});
  return {select, documentListeners, intervals, requests, drawn, settled, click, setPreviousClose: value => { previousClose = value; }, setQuoteAsOf: value => { quoteAsOf = value; }, setQuoteSession: value => { quoteSessionName = value; }};
}

test("background refresh keeps the expanded table; choosing a range collapses it", async () => {
  const page = startPage();
  await page.settled();
  const more = page.select("#table-more");
  assert.equal(more.textContent, "展开全部 20 个交易日");

  more.listeners.click();
  assert.equal(more.textContent, "收起明细");

  page.select("#refresh").listeners.click();
  await page.settled();
  assert.equal(more.textContent, "收起明细");

  page.click("data-days", {days: "10"});
  assert.equal(more.textContent, "展开全部 10 个交易日");
});

test("the range presets drive the flow summary, table and export", async () => {
  const page = startPage();
  await page.settled();
  assert.match(page.select("#flow-summary").innerHTML, /共 20 个交易日/);
  page.click("data-days", {days: "5"});
  assert.match(page.select("#flow-summary").innerHTML, /共 5 个交易日/);
  assert.equal(page.select("#table-more").hidden, true);
  assert.match(page.select("#export").href, /start=2026-10-21&end=2026-10-25/);
  page.click("data-days", {days: "10"});
  assert.equal(page.select("#table-more").hidden, true);
  page.click("data-days", {days: "20"});
  assert.equal(page.select("#table-more").hidden, false);
  // A background refresh keeps the chosen range.
  page.click("data-days", {days: "5"});
  page.select("#refresh").listeners.click();
  await page.settled();
  assert.match(page.select("#flow-summary").innerHTML, /共 5 个交易日/);
  assert.equal(page.drawn.some(call => call.option.series?.some(item => item.name === "A000660")), false);
});

test("prices are polled every second and the K-line opens on the day view", async () => {
  const page = startPage();
  await page.settled();
  assert.ok(page.intervals.has(1000));
  assert.deepEqual(page.requests.filter(url => url.includes("/api/kline/")), ["/api/kline/A000660/minute"]);
});

test("switching period loads it once, then refreshes only recent bars and keeps the zoom", async () => {
  const page = startPage();
  await page.settled();
  page.click("data-kline", {kline: "day"});
  await page.settled();
  assert.equal(page.requests.at(-1), "/api/kline/A000660/day");
  const full = page.drawn.findLast(call => call.option.series?.[0]?.type === "candlestick");
  assert.equal(full.notMerge, true);

  page.drawn.length = 0;
  page.intervals.get(30000)();
  await page.settled();
  assert.equal(page.requests.at(-1), "/api/kline/A000660/day?limit=3");
  const refresh = page.drawn.at(-1);
  assert.equal(refresh.notMerge, undefined);
  assert.equal(refresh.option.dataZoom, undefined);
  assert.deepEqual(refresh.option.series[0].data.at(-1), [100, 106, 90, 110]);
  assert.equal(refresh.option.series[0].data.length, 2);
});

test("a live quote redraws the newest bar only when its price changes", async () => {
  const page = startPage();
  await page.settled();
  page.click("data-kline", {kline: "day"});
  await page.settled();

  page.drawn.length = 0;
  page.intervals.get(1000)();
  await page.settled();
  const tick = page.drawn.findLast(call => call.option.xAxis);
  assert.deepEqual(tick.option.series[0].data.at(-1), [100, 120, 90, 120]);
  assert.match(page.select("#kline-summary").innerHTML, /120/);

  page.drawn.length = 0;
  page.intervals.get(1000)();
  await page.settled();
  assert.equal(page.drawn.filter(call => call.option.xAxis).length, 0);
});

test("the headline price follows the regular session and then stays on the close", async () => {
  const headline = page => page.select("#stock-cards").innerHTML.split("</article>")[0];
  const tick = async page => { page.intervals.get(1000)(); await page.settled(); };
  dashboardClose = {date: today, close: 110, previousClose: 100};
  const page = startPage();
  await page.settled();
  await tick(page);
  assert.match(headline(page), /盘中最新价/);
  assert.match(headline(page), /<small>₩<\/small>120</);
  assert.match(headline(page), /\+20\.00%/);

  page.setQuoteAsOf(`${today}T17:10:00+09:00`);
  page.setQuoteSession("AFTER_MARKET");
  await tick(page);
  assert.match(headline(page), /收盘价 · 2026-11-04/);
  assert.match(headline(page), /<small>₩<\/small>110</);
  assert.match(headline(page), /\+10\.00%/);
  assert.doesNotMatch(headline(page), /盘后最新价|报价滞后|class="spark"/);

  page.setQuoteAsOf("2020-01-02T15:29:59+09:00");
  page.setQuoteSession("REGULAR_MARKET");
  await tick(page);
  assert.match(headline(page), /盘中最新价 · 报价滞后/);
  dashboardClose = null;
});

test("before the close is verified the frozen 15:30 quote stands in, and nothing else does", async () => {
  const headline = page => page.select("#stock-cards").innerHTML.split("</article>")[0];
  const page = startPage();
  page.setQuoteAsOf(`${today}T15:30:00+09:00`);
  await page.settled();
  page.intervals.get(1000)();
  await page.settled();
  assert.match(headline(page), /收盘价 · 2026-11-04/);
  assert.match(headline(page), /<small>₩<\/small>120</);
  page.setQuoteAsOf(`${today}T17:10:00+09:00`);
  page.setQuoteSession("AFTER_MARKET");
  page.intervals.get(1000)();
  await page.settled();
  assert.match(headline(page), /等待收盘价/);
  assert.doesNotMatch(headline(page), /<small>₩<\/small>120</);
});

test("a repurchase batch shows KIND's amount and date, and a batch without them stays undisclosed", async () => {
  const base = {code: "A000660", name: "SK 海力士", title: "批次", amountKrw: 2e13, purpose: "注销", status: "执行中", note: "说明", announcedAt: "2026-10-28", checkedAt: "2026-11-04", url: "https://example.com/", sourceName: "公告"};
  dashboardRepurchases = [
    {...base, progress: 7.25, purchasedKrw: 1449883471000, purchasedShares: 1000000, plannedShares: 14920000, asOf: "2026-11-03", kindFetchedAt: "2026-11-04T07:00:00+00:00", kindUrl: "https://kind.krx.co.kr/"},
    {...base, progress: null, purchasedKrw: null, asOf: null}
  ];
  const page = startPage();
  await page.settled();
  dashboardRepurchases = [];
  const [known, unknown] = page.select("#repurchase-cards").innerHTML.split("</article>");
  assert.match(known, /<b>7\.25%<\/b>/);
  assert.match(known, /累计成交 1\.45 万亿韩元 · 100 万股（申报 1,492 万股）/);
  assert.match(known, /成交截至 2026-11-03/);
  assert.match(known, /KIND 成交数据/);
  assert.doesNotMatch(known, /说明|KIND 读取/);
  assert.match(unknown, /<b>待披露<\/b>/);
  assert.doesNotMatch(unknown, /累计成交|KIND/);
});

test("an untranslated title raises no notice, but a failing source still warns", async () => {
  const page = startPage();
  await page.settled();
  assert.equal(page.select("#stock-cards").innerHTML.includes("源快照"), false);
  page.select("#notice").hidden = false;
  const now = new Date().toISOString();
  dashboardStatus = [{module: "A000660:flows", success: now, attempt: now, error: "down"}];
  page.select("#refresh").listeners.click();
  await page.settled();
  dashboardStatus = [];
  assert.equal(page.select("#notice").hidden, false);
  assert.match(page.select("#notice").textContent, /部分数据源检查失败/);
  assert.doesNotMatch(page.select("#notice").textContent, /页面底部/);
  page.select("#refresh").listeners.click();
  await page.settled();
  assert.equal(page.select("#notice").hidden, true);
});

test("price cards no longer list the NXT quote, regular close or flow details", async () => {
  const page = startPage();
  await page.settled();
  const cards = page.select("#stock-cards").innerHTML;
  assert.match(cards, /stock-price/);
  assert.match(cards, /after-hours/);
  assert.doesNotMatch(cards, /nxt-live|closing-source|stock-meta|外资持股|收盘交易日/);
});

test("a slow response for a previous selection does not overwrite the current chart", async () => {
  const page = startPage();
  await page.settled();
  page.click("data-stock", {stock: "A005930"});
  await page.settled();
  page.drawn.length = 0;
  page.click("data-kline", {kline: "week"});
  page.click("data-kline", {kline: "month"});
  await page.settled();
  assert.ok(page.requests.includes("/api/kline/A005930/week"));
  assert.ok(page.requests.includes("/api/kline/A005930/month"));
  assert.equal(page.drawn.filter(call => call.notMerge && call.option.series?.[0]?.type === "candlestick").length, 1);
});

test("after a background refresh the tooltip reads the refreshed bars", async () => {
  const page = startPage();
  await page.settled();
  page.click("data-kline", {kline: "day"});
  await page.settled();
  const full = page.drawn.findLast(call => call.notMerge && call.option.series?.[0]?.type === "candlestick");
  assert.match(full.option.tooltip.formatter([{dataIndex: 1}]), /105/);
  page.intervals.get(30000)();
  await page.settled();
  assert.match(full.option.tooltip.formatter([{dataIndex: 1}]), /106/);
});

test("a new trading day rebuilds the chart so its baseline cannot go stale", async () => {
  const page = startPage();
  await page.settled();
  page.setPreviousClose(99);
  page.drawn.length = 0;
  page.intervals.get(30000)();
  await page.settled();
  const rebuilt = page.drawn.findLast(call => call.option.series?.[0]?.markLine);
  assert.equal(rebuilt.notMerge, true);
  assert.deepEqual(rebuilt.option.series[0].markLine.data.at(-1).yAxis, 99);
});

test("cards are not rebuilt while pressed, by the per-second tick or the full refresh", async () => {
  const page = startPage();
  await page.settled();
  const cards = page.select("#stock-cards");
  page.documentListeners.pointerdown();
  cards.innerHTML = "HELD";
  page.intervals.get(1000)();
  await page.settled();
  assert.equal(cards.innerHTML, "HELD");
  page.intervals.get(60000)();
  await page.settled();
  assert.equal(cards.innerHTML, "HELD");
  page.documentListeners.pointerup();
  page.intervals.get(1000)();
  await page.settled();
  assert.notEqual(cards.innerHTML, "HELD");
});

test("one failed live poll stays quiet; a failed slow source still warns", async () => {
  const now = new Date().toISOString();
  dashboardStatus = [{module: "A000660:live", success: now, attempt: now, error: "ssl eof"}];
  try {
    const quiet = startPage();
    await quiet.settled();
    assert.equal(quiet.select("#notice").hidden, true);
    dashboardStatus = [{module: "A000660:flows", success: now, attempt: now, error: "down"}];
    const warned = startPage();
    await warned.settled();
    assert.equal(warned.select("#notice").hidden, false);
  } finally {
    dashboardStatus = [];
  }
});
