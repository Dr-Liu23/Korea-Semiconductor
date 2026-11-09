/* Verified KRX regular closes and after-hours snapshots are kept separate. */
"use strict";
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const numeric = value => typeof value === "number" && Number.isFinite(value);
const fmt = (value, digits = 0) => numeric(value) ? value.toLocaleString("zh-CN", {maximumFractionDigits: digits, minimumFractionDigits: digits}) : "—";
const signed = (value, digits = 2) => numeric(value) ? (value > 0 ? "+" : "") + fmt(value, digits) : "—";
const direction = value => numeric(value) ? value > 0 ? "positive" : value < 0 ? "negative" : "neutral" : "neutral";
const timeKst = value => value ? new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Seoul", month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(value)) + " KST" : "尚未同步";
const quoteTime = value => value ? new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Seoul",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}).format(new Date(value))+" KST" : "等待报价";
const quoteSession = q => q.tradeStop&&q.tradeStop!=="TRADING"?"暂停 / 限制交易":"盘中最新价";
const quoteStale = q => Date.now()-Date.parse(q.asOf)>Math.max((payload?.livePolling?.intervalSeconds||7)*4,30)*1000;
const href = value => /^https?:\/\//.test(value || "") ? esc(value) : "#";
const categories = [
  {name:"个人", field:"netIndividualsBuyVolume", color:"#018A67", english:"INDIVIDUALS"},
  {name:"外资", field:"netForeignerBuyVolume", color:"#1868B2", english:"FOREIGN INVESTORS"},
  {name:"机构", field:"netInstitutionBuyVolume", color:"#DE582B", english:"INSTITUTIONS"},
  {name:"其他法人", field:"netOtherCorporationBuyVolume", color:"#F3A332", english:"OTHER CORPORATIONS"}
];
let payload, activeCode = "A000660", rangeDays = 20, rangeStart = "", rangeEnd = "", newsFilter = "all", expanded = false;
let klinePeriod = "minute", klineDrawn = "", pointerDown = false;
const klineData = new Map();
const klineKey = () => activeCode + "|" + klinePeriod;
const charts = new Map();
const current = () => payload.stocks.find(s => s.code === activeCode);
const selectedRows = stock => stock.rows.filter(r => r.baseDate >= rangeStart && r.baseDate <= rangeEnd);
const scale = value => numeric(value) ? value / 10000 : null;
function chart(id, options) {
  const node = document.getElementById(id);
  if (!charts.has(id)) charts.set(id, echarts.init(node, null, {renderer:"canvas"}));
  charts.get(id).setOption({animation:false, textStyle:{fontFamily:'"Microsoft YaHei",system-ui,sans-serif',color:"#78869b",fontSize:10}, ...options}, true);
}
const axis = {axisLine:{show:false},axisTick:{show:false},axisLabel:{color:"#8c99ac",fontSize:9},splitLine:{lineStyle:{color:"#edf1f6",type:"dashed"}}};
const tooltip = {trigger:"axis",confine:true,backgroundColor:"#fff",borderColor:"#e6ebf2",textStyle:{fontSize:11,color:"#30435b"},padding:12};

// The headline follows the regular session only; after-hours prices have their own rows.
function headlinePrice(s, live) {
  const closing=s.regularClose;
  if(KoreaKline.regularSessionLive(live)) return {live:true,price:live.price,change:live.changePercent};
  // Naver's frozen 15:30 quote is the close itself, until the verified close arrives.
  if(live?.session==="REGULAR_MARKET"&&live.date>(closing?.date||"")) return {price:live.price,change:live.changePercent,date:live.date};
  const previous=closing?.previousClose??(live?.date===closing?.date?live?.previousClose:null);
  return {price:closing?.close,change:previous?(closing.close/previous-1)*100:null,date:closing?.date};
}
function renderCards() {
  // Rebuilding the cards mid-click or mid-selection would swallow the gesture; the next tick catches up.
  const selection=window.getSelection?.();
  if(pointerDown||(selection&&!selection.isCollapsed&&$("#stock-cards").contains(selection.anchorNode))) return;
  const focused=document.activeElement?.closest("[data-stock]"), focusedStock=focused?.dataset.stock;
  const focusedRegion=focused?.closest("#stock-cards,#flow-company-switch,#latest-company-switch,#kline-company-switch")?.id;
  $("#stock-cards").innerHTML = payload.stocks.map(s => {
    const closing = s.regularClose;
    const live=(s.liveQuotes||[]).find(q=>q.exchange==="KRX");
    const headline=headlinePrice(s,live),delayed=headline.live&&quoteStale(live);
    const after = ["KRX", "NXT"].map(exchange => {
      const q = (s.afterHours || []).find(item => item.exchange === exchange);
      if (!q) return `<div class="after-absent"><span>${exchange} 盘后</span><small>暂无已保存的盘后报价</small></div>`;
      const delta = closing?.date === q.date ? (q.price/closing.close-1)*100 : null;
      return `<div class="after-row"><span>${esc(q.exchange)} 盘后<small>${q.granularity==="1min"?"分钟价":"报价"}</small></span><b>₩ ${fmt(q.price)}</b><span class="${direction(delta)}">${signed(delta)}%<small>较常规收盘</small></span><time>${esc(q.asOf.slice(0,10))} ${esc(q.asOf.slice(11,19))} KST · Naver</time></div>`;
    }).join("");
    return `<article class="card stock-card ${s.code===activeCode?"active":""}" style="--stock-color:${esc(s.color)}" role="button" tabindex="0" data-stock="${esc(s.code)}" aria-pressed="${s.code===activeCode}" aria-label="选择${esc(s.name)}，KRX${headline.live?"最新报价":"收盘价"}${fmt(headline.price)}韩元"><div class="stock-card-top"><div class="stock-logo">${s.symbol==="000660"?"SK":"S"}</div><div><div class="stock-name">${esc(s.name)}</div><div class="stock-english">${esc(s.english)}</div></div><span class="stock-code">${esc(s.symbol)} · KRX</span></div><div class="live-label ${delayed?"quote-delayed":headline.live?"":"quote-closed"}"><i></i>${headline.live?esc(quoteSession(live))+(delayed?" · 报价滞后":""):headline.date?"收盘价 · "+esc(headline.date):"等待收盘价"}</div><div class="stock-price-row"><div><div class="stock-price"><small>₩</small>${fmt(headline.price)}</div><div class="price-change ${direction(headline.change)}">${numeric(headline.change)?signed(headline.change)+"%":"—"}<span>较前一交易日收盘</span></div></div></div><div class="after-hours"><div class="after-heading">盘后价格<span>独立行情快照</span></div>${after || '<div class="after-empty">暂无已取得的盘后报价</div>'}</div></article>`;
  }).join("");
  $("#flow-company-switch").innerHTML = payload.stocks.map(s => `<button type="button" data-stock="${esc(s.code)}" style="--stock-color:${esc(s.color)}" class="${s.code===activeCode?"selected":""}" aria-pressed="${s.code===activeCode}">${esc(s.name)}</button>`).join("");
  $("#latest-company-switch").innerHTML = $("#flow-company-switch").innerHTML;
  $("#kline-company-switch").innerHTML = $("#flow-company-switch").innerHTML;
  if(focusedRegion&&focusedStock) document.getElementById(focusedRegion).querySelector(`[data-stock="${focusedStock}"]`)?.focus({preventScroll:true});
}
function setPreset(days) {
  rangeDays = days;
  const dates = [...new Set(payload.stocks.flatMap(s=>s.rows.map(r=>r.baseDate)))].sort();
  rangeStart = dates[Math.max(0,dates.length-days)] || "";
  rangeEnd = dates.at(-1) || "";
  $("#range-presets").querySelectorAll("button").forEach(b => { b.classList.toggle("selected",Number(b.dataset.days)===rangeDays); b.setAttribute("aria-pressed",String(Number(b.dataset.days)===rangeDays)); });
}
function renderLatestFlows() {
  const stock = current(), record = stock.latest;
  $("#latest-flow-title").textContent = stock.name + " · " + (record?.baseDate || "暂无完整交易日数据");
  $("#latest-flow-caption").textContent = record ? `完整交易日 · 源更新 ${record.updatedAt.slice(0,10)} ${record.updatedAt.slice(11,19)} KST · 净买入 = 买入 − 卖出` : "等待后台更新";
  $("#latest-flow-table").innerHTML = record ? KoreaFlowDetails.buildRows(record).map(row => `<tr class="${row.level ? "institution-detail" : "investor-total"}"><th scope="row"><span class="category-dot" style="--category-color:${row.color}"></span>${esc(row.name)}${row.level ? '<small>机构细分</small>' : ""}</th><td>${fmt(scale(row.buy),4)}</td><td>${fmt(scale(row.sell),4)}${row.sellDerived ? '<sup title="由买入减净买入计算">*</sup>' : ""}</td><td class="${direction(row.net)}">${signed(scale(row.net),4)}</td></tr>`).join("") : '<tr><td colspan="4">暂无已保存的完整交易日数据</td></tr>';
}
function renderFlows() {
  const stock=current(), rows=selectedRows(stock);
  $("#flow-summary").innerHTML=`<b>${esc(stock.name)}</b><span>${esc(rangeStart)} — ${esc(rangeEnd)}</span><span>共 ${rows.length} 个交易日</span><span>买入 − 卖出 = 净买入 · 正值买入，负值卖出</span>`;
  if (!$("#flow-charts").children.length) {
    $("#flow-charts").innerHTML=categories.map((c,i)=>`<article class="card flow-card" style="--category-color:${c.color}"><div class="card-heading"><div><h3><span class="category-dot"></span>${c.name}</h3><p>${c.english}</p></div><div class="flow-total" id="total-${i}"></div></div><div id="flow-${i}" class="chart flow-chart" role="img" aria-label="${c.name}每日净买卖柱状图，单位万股"></div></article>`).join("");
  }
  categories.forEach((c,i)=>{
    const values=rows.map(r=>scale(r[c.field])), complete=values.length>0 && values.every(numeric), sum=complete?values.reduce((a,b)=>a+b,0):null;
    $(`#total-${i}`).innerHTML=`${signed(sum)}<small>区间合计 / 万股${complete?"":" · 含缺失"}</small>`;
    chart(`flow-${i}`,{
      grid:{left:57,right:21,top:18,bottom:rows.length<=10?50:36},tooltip:{...tooltip,valueFormatter:v=>numeric(v)?signed(v,4)+" 万股":"未提供"},
      xAxis:{...axis,type:"category",data:rows.map(r=>r.baseDate),axisLine:{show:true,onZero:true,lineStyle:{color:"#c8d2df"}},axisLabel:{...axis.axisLabel,margin:rows.length<=10?22:8,formatter:v=>v.slice(5)}},
      yAxis:{...axis,type:"value",splitNumber:4,min:value=>Math.min(0,value.min),max:value=>Math.max(0,value.max)},
      series:[{name:c.name,type:"bar",barMaxWidth:21,itemStyle:{color:c.color,borderRadius:1},data:values,label:{show:rows.length<=10,position:"outside",fontSize:9,color:c.color,formatter:p=>numeric(p.value)?fmt(p.value,1):""}}],aria:{enabled:true}
    });
  });
  renderTable();
}
function renderTable() {
  const rows=selectedRows(current()).slice().reverse(), shown=expanded?rows:rows.slice(0,10), changes=KoreaFlowDetails.dailyChanges(current().rows);
  $("#flow-table").innerHTML=shown.length?shown.map(r=>{const change=changes.get(r.baseDate);return `<tr><td>${esc(r.baseDate)}</td><td>${fmt(r.regularClose)}</td><td class="${direction(change?.percent)}">${change?signed(change.percent)+"%":"—"}${change?.derived?'<sup title="由下一交易日基准价推算">*</sup>':""}</td>${categories.map(c=>`<td class="${direction(r[c.field])}">${signed(scale(r[c.field]))}</td>`).join("")}</tr>`;}).join(""):'<tr><td colspan="7">所选区间暂无完整交易日数据</td></tr>';
  $("#table-caption").textContent=`${current().name} · 净买卖单位：万股 · 未核实的常规收盘价显示 — · 带 * 的涨跌幅由下一交易日基准价推算`;
  $("#table-more").hidden=rows.length<=10;
  $("#table-more").textContent=expanded?"收起明细":"展开全部 "+rows.length+" 个交易日";
  $("#export").href=`/api/export/${activeCode}.csv?start=${encodeURIComponent(rangeStart)}&end=${encodeURIComponent(rangeEnd)}`;
}
function renderNews() {
  const grouped=new Map();
  payload.documents.filter(d=>d.kind==="news").forEach(d=>{
    if (grouped.has(d.id)) grouped.get(d.id).codes.push(d.code);
    else grouped.set(d.id,{...d,codes:[d.code]});
  });
  const items=[...grouped.values()].filter(d=>newsFilter==="all"||d.codes.includes(newsFilter)).slice(0,16);
  $("#news-list").innerHTML=items.length?items.map(d=>`<a class="news-item" href="${href(d.url)}" target="_blank" rel="noopener noreferrer"><div class="news-meta">${d.codes.map(code=>`<span class="ticker-tag">${code==="A000660"?"海力士":"三星"}</span>`).join("")}<span>${esc(d.publisher)}</span><time>${esc(timeKst(d.publishedAt))}</time></div><div class="news-title">${esc(d.titleZh||d.originalTitle)} <span class="news-arrow">↗</span></div><div class="news-original">${d.titleZh?esc(d.originalTitle):"中文标题暂不可用 · 查看原文"}</div></a>`).join(""):'<div class="empty">暂无已同步新闻，后台将继续检查。</div>';
  $("#news-filter").querySelectorAll("button").forEach(b=>{b.classList.toggle("selected",b.dataset.news===newsFilter);b.setAttribute("aria-pressed",String(b.dataset.news===newsFilter));});
  const filings=payload.documents.filter(d=>d.kind==="filings"&&d.code===activeCode).slice(0,8);
  $("#filings-company").textContent=current().name+" · 随上方股票选择切换";
  $("#filings-list").innerHTML=filings.length?filings.map(d=>`<a class="filing-item" href="${href(d.url)}" target="_blank" rel="noopener noreferrer"><time>${esc(d.publishedAt.slice(0,10))}</time><div>${esc(d.titleZh||d.originalTitle)}<small>${esc(d.publisher)}${d.indexedLink?" · 请在索引中按日期查找":" · 查看原始披露 ↗"}</small></div></a>`).join(""):'<div class="empty">暂无已同步公告</div>';
}
function renderRepurchases() {
  $("#repurchase-cards").innerHTML=payload.repurchases.map(r=>`<article class="card repurchase-card" style="--stock-color:${r.code==='A000660'?'#EA5B0C':'#1428A0'}"><div class="repurchase-top">${esc(r.name)}</div><h3>${esc(r.title)}</h3><div class="repurchase-amount">${fmt(r.amountKrw/1e12)}<small>万亿韩元</small></div><div class="repurchase-purpose">${esc(r.purpose)}</div><div class="progress-label"><span>${esc(r.status)}</span><b>${numeric(r.progress)?fmt(r.progress,r.progress%1?2:0)+"%":"待披露"}</b></div><div class="progress-track ${numeric(r.progress)?"":"unknown"}">${numeric(r.progress)?`<span style="width:${Math.max(0,Math.min(100,r.progress))}%"></span>`:""}</div>${r.kindFetchedAt?`<p class="repurchase-note">累计成交 ${fmt(r.purchasedKrw/1e12,2)} 万亿韩元 · ${fmt(r.purchasedShares/1e4)} 万股（申报 ${fmt(r.plannedShares/1e4)} 万股）</p>`:""}<div class="repurchase-dates">决议 ${esc(r.announcedAt)}<br>${r.kindFetchedAt?r.asOf?"成交截至 "+esc(r.asOf):"":`${r.asOf?"披露截至 "+esc(r.asOf):"未单列完成日期"} · 核对 ${esc(r.checkedAt)}`}</div><a class="source-link" href="${href(r.url)}" target="_blank" rel="noopener noreferrer">${esc(r.sourceName)} ↗</a>${r.kindUrl?`<a class="source-link" href="${href(r.kindUrl)}" target="_blank" rel="noopener noreferrer">KIND 成交数据 ↗</a>`:""}${r.planUrl?`<a class="source-link" href="${href(r.planUrl)}" target="_blank" rel="noopener noreferrer">查看原始回购决议 ↗</a>`:""}</article>`).join("");
}
function renderKlineSummary(state) {
  const stock=current(), info=KoreaKline.summary(klinePeriod,state);
  $("#kline-summary").innerHTML=info?`<b>${esc(stock.name)}</b><span>${esc(info.at)}</span><span class="${direction(info.close-info.open)}">₩ ${fmt(info.close)}</span>${numeric(info.change)?`<span class="${direction(info.change)}">${signed(info.change)}%</span>`:""}<span>开 ${fmt(info.open)}</span><span>高 ${fmt(info.high)}</span><span>低 ${fmt(info.low)}</span>`:`<b>${esc(stock.name)}</b><span>K线数据同步中</span>`;
  $("#kline-note").textContent=KoreaKline.note(klinePeriod);
}
function drawKline() {
  const key=klineKey(), state=klineData.get(key), instance=charts.get("kline-chart");
  if(!state||!payload) return;
  if(klinePeriod==="day") state.closes=new Map(current().rows.filter(r=>numeric(r.regularClose)).map(r=>[r.baseDate,r.regularClose]));
  if(instance&&klineDrawn===key) instance.setOption(KoreaKline.dataOption(klinePeriod,state));
  else {
    chart("kline-chart",KoreaKline.option(klinePeriod,state));
    klineDrawn=state.bars.length?key:"";
  }
  renderKlineSummary(state);
}
async function loadKline() {
  const key=klineKey(), [code,period]=key.split("|"), state=klineData.get(key);
  // Long histories download once; afterwards only the newest bars can change.
  const recent=state?.bars.length&&!["minute","five"].includes(period);
  try {
    const response=await fetch(`/api/kline/${code}/${period}${recent?"?limit=3":""}`,{signal:AbortSignal.timeout(20000)});
    if(!response.ok) throw new Error("HTTP "+response.status);
    const next=await response.json();
    const saved=klineData.get(key), bars=recent?KoreaKline.mergeRecent(saved.bars,next.bars):next.bars;
    // A new session changes the baseline drawn at setup, so rebuild instead of patching.
    if(saved&&(saved.tradingDay!==next.tradingDay||saved.previousClose!==next.previousClose)&&klineDrawn===key) klineDrawn="";
    // Updated in place: the chart's tooltip keeps reading this same object.
    if(saved) Object.assign(saved,next,{bars});
    else klineData.set(key,next);
    if(key===klineKey()) drawKline();
  } catch(error) {
    if(key===klineKey()&&!klineData.has(key)) $("#kline-summary").innerHTML="<span>K线暂时无法载入，稍后自动重试</span>";
    console.warn("K-line refresh failed",error);
  }
}
function showKline() {
  const key=klineKey();
  if(key===klineDrawn) return;
  $("#kline-periods").querySelectorAll("button").forEach(b=>{b.classList.toggle("selected",b.dataset.kline===klinePeriod);b.setAttribute("aria-pressed",String(b.dataset.kline===klinePeriod));});
  if(klineData.has(key)) drawKline();
  loadKline();
}
function tickKline() {
  const state=klineData.get(klineKey()), quotes=current().liveQuotes||[];
  if(!state) return;
  const krx=KoreaKline.mergeLive(state.bars,klinePeriod,quotes.find(q=>q.exchange==="KRX"));
  const nxt=KoreaKline.mergeNxt(state,klinePeriod,quotes.find(q=>q.exchange==="NXT"));
  if(krx||nxt) drawKline();
}
function renderStatus() {
  const polling=payload.pricePolling || {intervalSeconds:60};
  const statuses=payload.status || [], stale=s=>{
    const price=/:(prices|after_nxt)$/.test(s.module), live=s.module.endsWith(":live");
    const tolerance=live?Math.max((payload.livePolling?.intervalSeconds||7)*4,30):price?Math.max(polling.intervalSeconds*3,180):s.module.endsWith(":repurchase")?payload.kindUpdateSeconds*3:Math.max(payload.updateSeconds*3,7200);
    // At one request per second a single failed poll is noise; only a lasting gap should warn.
    return (s.error&&!live) || !s.success || Date.now()-Date.parse(s.success)>tolerance*1000;
  };
  const warnings=statuses.filter(stale);
  const live=payload.livePolling || {intervalSeconds:7};
  $("#sync-time").textContent=live.lastSuccess?"行情采集 "+quoteTime(live.lastSuccess):"行情首次采集中";
  $("#price-polling").textContent=`最新价每 ${live.intervalSeconds} 秒主动采集 · 页面自动更新 · ${live.nextCheckAt?"下次检查 "+quoteTime(live.nextCheckAt):"等待调度"}`;
  const notice=$("#notice");
  notice.hidden=!warnings.length&&!payload.refreshInProgress;
  notice.textContent=warnings.length?"部分数据源检查失败或更新延后，当前保留最近成功的数据。请核对交易日期。":payload.refreshInProgress?"后台正在检查更新，当前展示最近已保存的数据。":"";
}
function render() {
  renderCards(); showKline(); renderLatestFlows(); renderFlows(); renderNews(); renderRepurchases(); renderStatus();
}
async function load() {
  const button=$("#refresh"); if(button.disabled) return;
  button.disabled=true;
  try {
    const response=await fetch("/api/dashboard",{cache:"no-store",signal:AbortSignal.timeout(20000)});
    if(!response.ok) throw new Error("HTTP "+response.status);
    const next=await response.json();
    if(!Array.isArray(next.stocks)||next.stocks.length!==2) throw new Error("Invalid dashboard response");
    payload=next;
    setPreset(rangeDays);
    render();
  } catch(error) {
    $("#notice").hidden=false;
    $("#notice").textContent=payload?"暂时无法连接服务器，仍展示上次载入的数据。稍后可点击刷新显示。":"数据暂时无法载入，请稍后点击刷新显示。";
    if(!payload) $("#stock-cards").innerHTML='<div class="card empty">暂时无法取得股票数据</div>';
    console.error("Dashboard refresh failed",error);
  } finally { button.disabled=false; }
}
let checkingPrices=false;
async function checkPrices() {
  if(document.hidden || checkingPrices) return;
  checkingPrices=true;
  try {
    const response=await fetch("/api/quotes",{cache:"no-store",signal:AbortSignal.timeout(10000)});
    if(!response.ok) throw new Error("HTTP "+response.status);
    const next=await response.json();
    if(!payload) {await load();return;}
    const changed=next.livePolling.revision!==payload.livePolling?.revision;
    payload.livePolling=next.livePolling;
    if(changed) {
      next.stocks.forEach(stock=>{const current=payload.stocks.find(s=>s.code===stock.code);if(current) {current.liveQuotes=stock.liveQuotes;current.afterHours=stock.afterHours;}});
      payload.status=[...(payload.status||[]).filter(s=>!s.module.endsWith(":live")),...next.status];
    }
    renderCards();renderStatus();tickKline();
  } catch(error) {
    // The full dashboard refresh below also retries after temporary outages.
    console.warn("Price update check failed",error);
  } finally {checkingPrices=false;}
}
document.addEventListener("click",event=>{
  const stock=event.target.closest("[data-stock]");
  if(stock && payload) {activeCode=stock.dataset.stock;expanded=false;render();}
  const preset=event.target.closest("[data-days]");
  if(preset && payload) {expanded=false;setPreset(Number(preset.dataset.days));renderFlows();}
  const period=event.target.closest("[data-kline]");
  if(period && payload) {klinePeriod=period.dataset.kline;showKline();}
  const filter=event.target.closest("[data-news]");
  if(filter && payload) {newsFilter=filter.dataset.news;renderNews();}
});
$("#stock-cards").addEventListener("keydown",e=>{if((e.key==="Enter"||e.key===" ")&&e.target.closest("[data-stock]")){e.preventDefault();e.target.closest("[data-stock]").click();}});
$("#table-more").addEventListener("click",()=>{if(payload){expanded=!expanded;renderTable();}});
$("#refresh").addEventListener("click",load);
let resizeTimer;
const resize=()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>charts.forEach(c=>c.resize()),100);};
window.addEventListener("resize",resize);
if("ResizeObserver" in window) new ResizeObserver(resize).observe($(".workspace"));
const navObserver=new IntersectionObserver(entries=>{
  for(const e of entries) if(e.isIntersecting) document.querySelectorAll(".nav-item").forEach(a=>a.classList.toggle("active",a.hash==="#"+e.target.id));
},{rootMargin:"-10% 0px -65% 0px",threshold:0});
document.querySelectorAll("main .section").forEach(s=>navObserver.observe(s));
load();
document.addEventListener("pointerdown",()=>{pointerDown=true;});
for(const type of ["pointerup","pointercancel"]) document.addEventListener(type,()=>{pointerDown=false;});
window.addEventListener("blur",()=>{pointerDown=false;});
setInterval(checkPrices,1000);
setInterval(()=>{if(payload&&!document.hidden) loadKline();},30000);
setInterval(()=>{if(!document.hidden) load();},60000);
document.addEventListener("visibilitychange",()=>{if(!document.hidden) load();});
