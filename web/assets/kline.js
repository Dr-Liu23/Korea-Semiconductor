/* K-line data helpers and chart options. Live prices only touch the newest bar in memory. */
(function (root) {
  "use strict";
  const UP = "#c44141", DOWN = "#168568", MUTED = "#8c99ac", GRID = "#edf1f6";
  const NXT_COLOR = "#2458c5";
  const AVERAGES = [[5, "#e8a33d"], [10, "#2458c5"], [20, "#8a55c7"], [60, "#2a9d9d"]];
  const VISIBLE = {day: 120, week: 104, month: 120};
  const INTRADAY = new Set(["minute", "five"]);
  const NOTES = {
    intraday: "KRX 为 Naver 分钟数据，最新价随 3 秒报价并入，成交量约每分钟更新。NXT 无公开分钟数据，由本站按 3 秒报价累积，从 2026-11-05 起，无成交量，服务中断时会有缺口。",
    candle: "Naver 日线，历史已由来源按拆股调整，周K、月K由日K汇总。K线收盘价含盘后变动，可能不同于 KRX 常规收盘价；已核实的常规收盘价见日K提示框。交易时段内最新价随报价并入最后一根。NXT 没有日线数据，未画入。"
  };
  const price = value => typeof value === "number" ? value.toLocaleString("zh-CN") : "—";
  const volume = value => typeof value !== "number" ? "未提供" : value < 10000 ? value.toLocaleString("zh-CN") + " 股" : (value / 10000).toLocaleString("zh-CN", {maximumFractionDigits: 2}) + " 万股";
  const percent = (value, base) => base ? (value / base * 100 - 100).toFixed(2).replace(/^(?!-)/, "+") + "%" : "—";
  const row = (name, value) => `<div style="display:flex;justify-content:space-between;gap:18px"><span>${name}</span><b>${value}</b></div>`;
  const barColor = bar => bar[4] >= bar[1] ? UP : DOWN;

  function movingAverage(bars, size) {
    let sum = 0;
    return bars.map((bar, index) => {
      sum += bar[4];
      if (index >= size) sum -= bars[index - size][4];
      return index >= size - 1 ? Math.round(sum / size * 100) / 100 : null;
    });
  }
  function weekStart(day) {
    const date = new Date(day + "T00:00:00Z");
    date.setUTCDate(date.getUTCDate() - (date.getUTCDay() + 6) % 7);
    return date.toISOString().slice(0, 10);
  }
  function samePeriod(barDay, day, period) {
    if (period === "week") return weekStart(barDay) === weekStart(day);
    return period === "month" ? barDay.slice(0, 7) === day.slice(0, 7) : barDay === day;
  }
  // After the close Naver still reports KRX as an open regular session, stamped 15:30:00.
  function regularSessionLive(quote) {
    return quote?.session === "REGULAR_MARKET" && quote.marketStatus === "OPEN" && quote.asOf.slice(11, 16) < "15:30";
  }
  function afterSessionLive(quote) {
    return quote?.session === "AFTER_MARKET" && quote.marketStatus === "OPEN" && quote.asOf.slice(11, 16) <= "20:00";
  }
  function barTime(quote, period) {
    const minutes = Number(quote.asOf.slice(14, 16));
    return `${quote.date} ${quote.asOf.slice(11, 14)}${String(period === "five" ? minutes - minutes % 5 : minutes).padStart(2, "0")}`;
  }
  function mergeLive(bars, period, quote) {
    if (!bars.length || !(regularSessionLive(quote) || afterSessionLive(quote))) return false;
    const last = bars.at(-1), value = quote.price;
    let at = last[0];
    if (INTRADAY.has(period)) {
      if (last[0].slice(0, 10) !== quote.date) return false;
      at = barTime(quote, period);
      if (at < last[0]) return false;
    } else if (!samePeriod(last[0], quote.date, period)) return false;
    if (at !== last[0]) {
      bars.push([at, value, value, value, value, null]);
      return true;
    }
    if (last[4] === value) return false;
    last[4] = value;
    last[2] = Math.max(last[2], value);
    last[3] = Math.min(last[3], value);
    return true;
  }
  function mergeNxt(state, period, quote) {
    const time = quote?.asOf.slice(11, 16);
    if (!INTRADAY.has(period) || !state.nxt || quote?.marketStatus !== "OPEN" || quote.date !== state.tradingDay || time < "09:00" || time > "20:00") return false;
    const at = barTime(quote, period), last = state.nxt.at(-1);
    if (last && at < last[0]) return false;
    if (last?.[0] === at) {
      if (last[1] === quote.price) return false;
      last[1] = quote.price;
    } else state.nxt.push([at, quote.price]);
    return true;
  }
  function mergeRecent(bars, recent) {
    return recent.length ? bars.filter(bar => bar[0] < recent[0][0]).concat(recent) : bars;
  }
  const sessionTimes = (() => {
    const times = [];
    for (let minute = 9 * 60; minute <= 20 * 60; minute++) times.push(`${String(Math.floor(minute / 60)).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`);
    return times;
  })();

  function axes(categories, labels) {
    const line = {axisLine: {lineStyle: {color: "#c8d2df"}}, axisTick: {show: false}};
    return [
      {type: "category", data: categories, ...line, axisLabel: {color: MUTED, fontSize: 9, ...labels}, splitLine: {show: false}},
      {type: "category", gridIndex: 1, data: categories, ...line, axisLabel: {show: false}}
    ];
  }
  function layout() {
    return {
      animation: false,
      axisPointer: {link: [{xAxisIndex: "all"}]},
      grid: [{left: 64, right: 14, top: 30, bottom: "31%"}, {left: 64, right: 14, top: "74%", bottom: 42}]
    };
  }
  function valueAxes(priceAxis) {
    const label = {color: MUTED, fontSize: 9};
    return [
      {type: "value", scale: true, splitNumber: 4, axisLabel: label, axisLine: {show: false}, axisTick: {show: false}, splitLine: {lineStyle: {color: GRID, type: "dashed"}}, ...priceAxis},
      {type: "value", gridIndex: 1, splitNumber: 2, axisLabel: {...label, formatter: value => value >= 10000 ? value / 10000 + "万" : value}, axisLine: {show: false}, axisTick: {show: false}, splitLine: {lineStyle: {color: GRID, type: "dashed"}}}
    ];
  }
  function tooltip(formatter) {
    return {trigger: "axis", axisPointer: {type: "cross", label: {fontSize: 9}}, confine: true, backgroundColor: "#fff", borderColor: "#e6ebf2", textStyle: {fontSize: 11, color: "#30435b"}, padding: 10, formatter};
  }
  const volumeSeries = data => ({name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, barMaxWidth: 12, data});
  const volumeData = bars => bars.map(bar => ({value: bar[5], itemStyle: {color: barColor(bar), opacity: .65}}));
  const legend = names => ({top: 2, left: 64, itemWidth: 14, itemHeight: 2, textStyle: {fontSize: 9, color: "#78869b"}, data: names});
  // Real gaps stay visible: this line only exists where we collected a quote.
  const nxtSeries = data => ({name: "NXT", type: "line", connectNulls: false, showSymbol: false, data, lineStyle: {width: 1.2, color: NXT_COLOR}, itemStyle: {color: NXT_COLOR}});

  function candleData(state) {
    const dates = state.bars.map(bar => bar[0]);
    return {
      xAxis: [{data: dates}, {data: dates}],
      series: [
        {data: state.bars.map(bar => [bar[1], bar[4], bar[3], bar[2]])},
        ...AVERAGES.map(([size]) => ({data: movingAverage(state.bars, size)})),
        {data: volumeData(state.bars)}
      ]
    };
  }
  function candleOption(state, period) {
    const count = state.bars.length, zoom = {xAxisIndex: [0, 1], startValue: Math.max(0, count - VISIBLE[period]), endValue: count - 1};
    const label = period === "month" ? value => value.slice(0, 7) : value => value;
    const data = candleData(state);
    const titles = {day: at => at, week: at => at + " 当周", month: at => at.slice(0, 7)};
    return {
      ...layout(),
      legend: legend(AVERAGES.map(([size]) => "MA" + size)),
      tooltip: tooltip(params => {
        const index = params[0].dataIndex, bar = state.bars[index], previous = state.bars[index - 1];
        if (!bar) return "";
        const regular = period === "day" ? state.closes?.get(bar[0]) : null;
        return `<b>${titles[period](bar[0])}</b>` + row("开盘", price(bar[1])) + row("最高", price(bar[2])) + row("最低", price(bar[3])) + row("收盘（K线）", price(bar[4])) +
          (regular ? row("KRX常规收盘（已核实）", price(regular)) : "") + row("涨跌幅", previous ? percent(bar[4], previous[4]) : "—") + row("成交量", volume(bar[5]));
      }),
      xAxis: axes(data.xAxis[0].data, {formatter: label}),
      yAxis: valueAxes({}),
      dataZoom: [{type: "inside", ...zoom}, {type: "slider", ...zoom, bottom: 6, height: 20, showDetail: false, borderColor: "#e6ebf2"}],
      series: [
        {name: "K线", type: "candlestick", data: data.series[0].data, itemStyle: {color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN}},
        ...AVERAGES.map(([size, color], index) => ({name: "MA" + size, type: "line", data: data.series[index + 1].data, showSymbol: false, lineStyle: {width: 1, color}, itemStyle: {color}})),
        volumeSeries(data.series.at(-1).data)
      ]
    };
  }

  function minuteData(state) {
    const byTime = new Map(state.bars.map(bar => [bar[0].slice(11), bar]));
    const last = state.bars.at(-1), base = state.previousClose ?? state.bars[0]?.[1];
    const nxt = new Map((state.nxt || []).map(([at, close]) => [at.slice(11), close]));
    return {
      series: [
        {data: sessionTimes.map(time => byTime.get(time)?.[4] ?? null), lineStyle: {width: 1.5, color: last?.[4] >= base ? UP : DOWN}, itemStyle: {color: last?.[4] >= base ? UP : DOWN}},
        {data: sessionTimes.map(time => {
          const bar = byTime.get(time);
          return bar ? {value: bar[5], itemStyle: {color: barColor(bar), opacity: .65}} : null;
        })},
        {data: sessionTimes.map(time => nxt.get(time) ?? null)}
      ]
    };
  }
  function minuteOption(state) {
    const data = minuteData(state), base = state.previousClose;
    const marks = [{xAxis: "15:30", label: {formatter: "15:30 常规收盘", position: "insideEndTop", fontSize: 9}}];
    if (base) marks.push({yAxis: base, label: {formatter: "昨收 " + price(base), position: "insideEndTop", fontSize: 9}});
    const bound = (edge, key) => value => Math.round(edge(value[key], base ?? value[key]));
    return {
      ...layout(),
      legend: legend(["KRX", "NXT"]),
      tooltip: tooltip(params => {
        const time = sessionTimes[params[0].dataIndex], bar = state.bars.find(item => item[0].slice(11) === time);
        const nxt = state.nxt?.find(item => item[0].slice(11) === time)?.[1];
        return `<b>${bar?.[0] ?? time}</b>` + (bar ? row("KRX", price(bar[4])) + (base ? row("较昨收", percent(bar[4], base)) : "") : "") +
          (nxt ? row("NXT", price(nxt)) + (base ? row("较昨收", percent(nxt, base)) : "") : "") + (bar ? row("成交量", volume(bar[5])) : "");
      }),
      xAxis: axes(sessionTimes, {interval: (index, value) => /^(09|11|13|15|17|19):00$/.test(value)}),
      yAxis: valueAxes({min: bound(Math.min, "min"), max: bound(Math.max, "max")}),
      series: [
        {name: "KRX", type: "line", connectNulls: true, showSymbol: false, data: data.series[0].data, lineStyle: data.series[0].lineStyle, itemStyle: data.series[0].itemStyle,
          markLine: {silent: true, symbol: "none", lineStyle: {color: "#a9b5c5", type: "dashed", width: 1}, data: marks}},
        volumeSeries(data.series[1].data),
        nxtSeries(data.series[2].data)
      ]
    };
  }

  function fiveData(state) {
    const last = state.bars.at(-1), first = state.bars[0], color = last?.[4] >= first?.[1] ? UP : DOWN;
    // KRX has few bars before its after-market opens, so NXT's times join the axis.
    const bars = new Map(state.bars.map(bar => [bar[0], bar])), nxt = new Map(state.nxt || []);
    const times = [...new Set([...bars.keys(), ...nxt.keys()])].sort();
    state.times = times;
    return {
      xAxis: [{data: times}, {data: times}],
      series: [
        {data: times.map(time => bars.get(time)?.[4] ?? null), lineStyle: {width: 1.5, color}, itemStyle: {color}},
        {data: times.map(time => bars.has(time) ? {value: bars.get(time)[5], itemStyle: {color: barColor(bars.get(time)), opacity: .65}} : null)},
        {data: times.map(time => nxt.get(time) ?? null)}
      ]
    };
  }
  function fiveOption(state) {
    const data = fiveData(state);
    const dayStart = (index, value) => index === 0 || value.slice(0, 10) !== state.times[index - 1]?.slice(0, 10);
    const [priceAxis, volumeAxis] = axes(data.xAxis[0].data, {interval: dayStart, formatter: value => value.slice(5, 10)});
    return {
      ...layout(),
      legend: legend(["KRX", "NXT"]),
      tooltip: tooltip(params => {
        const time = state.times[params[0].dataIndex], bar = state.bars.find(item => item[0] === time), nxt = state.nxt?.find(item => item[0] === time)?.[1];
        return `<b>${time}</b>` + (bar ? row("KRX", price(bar[4])) : "") + (nxt ? row("NXT", price(nxt)) : "") + (bar ? row("成交量", volume(bar[5])) : "");
      }),
      xAxis: [{...priceAxis, splitLine: {show: true, interval: dayStart, lineStyle: {color: GRID}}}, volumeAxis],
      yAxis: valueAxes({}),
      series: [
        {name: "KRX", type: "line", connectNulls: true, showSymbol: false, data: data.series[0].data, lineStyle: data.series[0].lineStyle, itemStyle: data.series[0].itemStyle},
        volumeSeries(data.series[1].data),
        nxtSeries(data.series[2].data)
      ]
    };
  }

  // Chart setup for a newly selected view; keeps zoom out of the per-tick updates below.
  function option(period, state) {
    if (!state.bars.length) return {title: {text: "K线数据同步中", left: "center", top: "middle", textStyle: {fontSize: 12, color: MUTED, fontWeight: 400}}};
    return period === "minute" ? minuteOption(state) : period === "five" ? fiveOption(state) : candleOption(state, period);
  }
  function dataOption(period, state) {
    return period === "minute" ? minuteData(state) : period === "five" ? fiveData(state) : candleData(state);
  }
  function note(period) {
    return INTRADAY.has(period) ? NOTES.intraday : NOTES.candle;
  }
  function summary(period, state) {
    const last = state.bars.at(-1);
    if (!last) return null;
    const previous = INTRADAY.has(period) ? null : state.bars.at(-2);
    const base = period === "minute" ? state.previousClose : previous?.[4];
    const session = period === "minute" ? state.bars : period === "five" ? state.bars.filter(bar => bar[0].slice(0, 10) === last[0].slice(0, 10)) : [last];
    return {
      at: last[0], close: last[4], open: session[0][1],
      high: Math.max(...session.map(bar => bar[2])), low: Math.min(...session.map(bar => bar[3])),
      change: base ? last[4] / base * 100 - 100 : null
    };
  }
  const api = {movingAverage, weekStart, regularSessionLive, mergeLive, mergeNxt, mergeRecent, option, dataOption, note, summary};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.KoreaKline = api;
})(globalThis);
