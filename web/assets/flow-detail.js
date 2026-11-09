/* Share-count identities and daily change from exchange prices; no inference of cash amounts. */
(function (root) {
  "use strict";
  const valid = value => typeof value === "number" && Number.isFinite(value);
  const institutions = [
    ["金融投资", "financialInvestment"], ["保险", "insurance"],
    ["投资信托", "trust"], ["私募基金", "privateEquityFund"],
    ["银行", "bank"], ["养老金", "pensionFund"],
    ["其他金融机构", "otherFinancialInstitutions"]
  ];
  function buildRows(record) {
    if (!record) return [];
    function detail(name, prefix, color, level = 0, available = true) {
      const netField = "net" + prefix[0].toUpperCase() + prefix.slice(1) + "BuyVolume";
      const buy = available && valid(record[prefix + "BuyVolume"]) ? record[prefix + "BuyVolume"] : null;
      const net = available && valid(record[netField]) ? record[netField] : null;
      const providedSell = record[prefix + "SellVolume"];
      const sellDerived = available && !valid(providedSell) && valid(buy) && valid(net);
      const sell = available && valid(providedSell) ? providedSell : sellDerived ? buy - net : null;
      return {name, prefix, color, level, buy, sell, net, sellDerived};
    }
    return [
      detail("个人", "individuals", "#018A67", 0, record.hasIndividual === true),
      detail("外资", "foreigner", "#1868B2", 0, record.hasForeigner === true),
      detail("机构合计", "institution", "#DE582B", 0, record.hasInstitution === true),
      ...institutions.map(([name, prefix]) => detail(name, prefix, "#DE582B", 1, record.hasInstitution === true)),
      detail("其他法人", "otherCorporation", "#F3A332")
    ];
  }
  function dailyChanges(rows) {
    // `base` is the day's exchange reference: the previous KRX close on every day checked.
    // So the next row's base gives a close for days without a verified one, unlike Toss `close`.
    return new Map(rows.map((row, index) => {
      const verified = valid(row.regularClose), close = verified ? row.regularClose : rows[index + 1]?.base;
      if (!valid(row.base) || row.base <= 0 || !valid(close)) return [row.baseDate, null];
      return [row.baseDate, {percent: (close - row.base) / row.base * 100, derived: !verified}];
    }));
  }
  const api = {buildRows, dailyChanges};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.KoreaFlowDetails = api;
})(globalThis);
