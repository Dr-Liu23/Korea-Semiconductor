const assert = require("node:assert/strict");
const fs = require("node:fs");
const {test} = require("node:test");
const {buildRows, dailyChanges} = require("../web/assets/flow-detail.js");

test("latest-day breakdown reconciles all seven institution rows for both real sources", () => {
  for (const code of ["A000660", "A005930"]) {
    const record = JSON.parse(fs.readFileSync(`data/live/${code}.json`, "utf8")).rows.at(-1);
    const rows = buildRows(record), parts = rows.filter(row => row.level === 1);
    assert.equal(rows.length, 11);
    assert.equal(parts.length, 7);
    for (const row of rows) assert.equal(row.buy - row.sell, row.net);
    const institution = rows.find(row => row.prefix === "institution");
    for (const field of ["buy", "sell", "net"]) {
      assert.equal(parts.reduce((sum,row) => sum + row[field], 0), institution[field]);
    }
    assert.equal(rows.find(row => row.prefix === "otherCorporation").sellDerived, true);
    assert.equal(institution.sellDerived, false);
  }
});

test("missing values and unavailable categories stay missing; real zero is retained", () => {
  const rows = buildRows({hasIndividual:true,hasInstitution:false,individualsBuyVolume:0,individualsSellVolume:0,netIndividualsBuyVolume:0,financialInvestmentBuyVolume:0,netFinancialInvestmentBuyVolume:0,otherCorporationBuyVolume:10});
  assert.equal(rows[0].net, 0);
  for (const row of rows.filter(row => row.prefix === "foreigner" || row.prefix === "institution" || row.level)) {
    assert.equal(row.buy, null); assert.equal(row.sell, null); assert.equal(row.net, null);
  }
  assert.equal(rows.at(-1).sell, null);
  assert.equal(rows.at(-1).net, null);
  assert.deepEqual(buildRows(null), []);
});

test("daily change prefers the verified close, derives from the next base, and never invents one", () => {
  const changes = dailyChanges([
    {baseDate:"2026-10-29",base:1000,close:1999},
    {baseDate:"2026-10-30",base:1100,close:1999,regularClose:1210},
    {baseDate:"2026-11-02",close:1999},
    {baseDate:"2026-11-03",base:1000},
    {baseDate:"2026-11-04",base:1000,close:1999}
  ]);
  assert.equal(changes.get("2026-10-29").derived, true);
  assert.equal(changes.get("2026-10-29").percent.toFixed(2), "10.00");
  assert.equal(changes.get("2026-10-30").derived, false);
  assert.equal(changes.get("2026-10-30").percent.toFixed(2), "10.00");
  assert.equal(changes.get("2026-11-02"), null);
  assert.equal(changes.get("2026-11-03").percent, 0);
  assert.equal(changes.get("2026-11-04"), null);
});
