/* Tests for lib/resolve.js - the state machine every page now shares.
 *
 *     node tests/test_resolve.js
 *
 * Synthetic candles, no network. These are the cases that actually went
 * wrong in the field, written down so they cannot go wrong again quietly.
 */
"use strict";

// A minimal localStorage so the observation store can be exercised here.
var store = {};
global.window = {
  localStorage: {
    getItem: function (k) { return k in store ? store[k] : null; },
    setItem: function (k, v) { store[k] = String(v); },
    removeItem: function (k) { delete store[k]; }
  }
};
require("../lib/resolve.js");
var MO = global.window.MO;

var pass = [], fail = [];
function check(name, cond, detail) {
  if (cond) pass.push(name);
  else fail.push(name + (detail ? " - " + detail : ""));
}
function eq(name, got, want) {
  check(name, got === want, "got " + JSON.stringify(got) +
    ", wanted " + JSON.stringify(want));
}

// Bars are {t,o,h,l,c}, t in seconds, one hour apart.
var T0 = 1700000000;
function bar(i, h, l) {
  return { t: T0 + i * 3600, o: (h + l) / 2, h: h, l: l, c: (h + l) / 2 };
}

// A short: entry 100, zone 99.5-100.5, stop 102, target 96.
var SHORT = { entry_zone: [99.5, 100.5], symbol: "X", tf: "1h" };
var SPLAN = { side: "short", entry: 100, stop: 102, targets: [96, 94] };

// A long: entry 100, zone 99.5-100.5, stop 98, target 104.
var LONG = { entry_zone: [99.5, 100.5], symbol: "X", tf: "1h" };
var LPLAN = { side: "long", entry: 100, stop: 98, targets: [104, 106] };

// ---------------------------------------------------------------- pending
eq("a market that never reaches the zone stays pending",
  MO.replay(SHORT, SPLAN, T0, [bar(0, 98, 97), bar(1, 98.5, 97.5)]).state,
  "pending");

// ---------------------------------------------------------------- active
eq("touching the zone activates the trade",
  MO.replay(SHORT, SPLAN, T0, [bar(0, 100.2, 99.8)]).state,
  "active");

// ---------------------------------------------------------------- win
eq("reaching the first target is a win",
  MO.replay(SHORT, SPLAN, T0, [bar(0, 100.2, 99.8), bar(1, 99, 95.5)]).state,
  "win");
check("a win is scored at the target distance in R",
  Math.abs(MO.replay(SHORT, SPLAN, T0,
    [bar(0, 100.2, 99.8), bar(1, 99, 95.5)]).resultR - 2) < 1e-9,
  "entry 100, target 96, stop 102 is 4/2 = 2R");

// ---------------------------------------------------------------- loss
eq("touching the stop is a loss",
  MO.replay(SHORT, SPLAN, T0, [bar(0, 100.2, 99.8), bar(1, 102.5, 100)]).state,
  "loss");
eq("a loss is always minus one R",
  MO.replay(SHORT, SPLAN, T0,
    [bar(0, 100.2, 99.8), bar(1, 102.5, 100)]).resultR, -1);

// ------------------------------------------------- the pessimistic rule
var both = MO.replay(SHORT, SPLAN, T0,
  [bar(0, 100.2, 99.8), bar(1, 103, 95)]);
eq("a bar holding both the stop and the target is scored a LOSS",
  both.state, "loss");
check("and it says why", /one bar/.test(both.note), both.note);

// ------------------------------------------------------- invalidation
// Price runs the WRONG way past the stop without ever touching the zone.
var inv = MO.replay(SHORT, SPLAN, T0, [bar(0, 103, 102.5)]);
eq("passing the stop without ever trading the zone is INVALIDATED, not a loss",
  inv.state, "invalidated");
check("invalidated carries no result, because there was no fill",
  inv.resultR == null, String(inv.resultR));

// The same on the long side.
eq("invalidation works on the long side too",
  MO.replay(LONG, LPLAN, T0, [bar(0, 97.5, 97)]).state, "invalidated");

// ----------------------------------------------------- the path problem
// The bug that started all of this: price ran DOWN through the zone, on
// through the stop, and back out. A snapshot taken afterwards sees price
// outside the zone and says "pending". The walk sees the whole journey.
var path = MO.replay(LONG, LPLAN, T0, [bar(0, 101, 97), bar(1, 101, 100.5)]);
eq("a bar that passes through the zone AND the stop is a loss, not pending",
  path.state, "loss");

// ------------------------------------------------------------ excursions
var exc = MO.replay(SHORT, SPLAN, T0,
  [bar(0, 100.2, 99.8), bar(1, 101, 98), bar(2, 100, 99)]);
check("MFE is the best move in the trade's favour, in R",
  Math.abs(exc.mfe - 1) < 1e-9, "entry 100, low 98, risk 2 => 1R, got " +
  exc.mfe);
check("MAE is the worst move against, in R",
  Math.abs(exc.mae - 0.5) < 1e-9, "entry 100, high 101, risk 2 => 0.5R, got " +
  exc.mae);

// -------------------------------------------------------------- staleness
var stale = MO.replay(SHORT, SPLAN, T0 + 999999, [bar(0, 100, 99)]);
check("bars that do not reach publication are reported stale, not pending",
  stale.stale === true, JSON.stringify(stale));
check("and no state is invented for them", stale.state === "pending" &&
  stale.barsSeen === 0, JSON.stringify(stale));

// A plan with no usable stop cannot be replayed and must say so.
check("an incomplete plan is stale rather than silently pending",
  MO.replay(SHORT, { side: "short", entry: 100, stop: null, targets: [96] },
    T0, [bar(0, 100, 99)]).stale === true);

// ------------------------------------------------------ record resolution
// Each case gets its own id. The observation store is deliberately
// permanent, so records sharing an id across cases would leak state from
// one test into the next - which is exactly what happened the first time
// these were written, and is how the precedence rule below got pinned down.
var seq = 0;
function rec(over) {
  seq += 1;
  return Object.assign({
    id: "X|1h|short@" + T0 + "#" + seq, ref: "MO-TEST", symbol: "X", tf: "1h",
    side: "short", entry: 100, stop: 102, targets: [96, 94],
    entry_zone: [99.5, 100.5], published_ts: T0, status: "active",
    result: "open", result_r: null, mae_r: 0.2, mfe_r: 0.3
  }, over || {});
}
var barsOpen = [bar(0, 100.2, 99.8), bar(1, 100.5, 99.5)];
var barsStopped = [bar(0, 100.2, 99.8), bar(1, 102.4, 100)];

eq("a ledger record the pipeline closed is taken as final",
  MO.resolveRecord(rec({ status: "loss", result_r: -1 }),
    function () { return barsOpen; }).state, "loss");
eq("and its provenance says so",
  MO.resolveRecord(rec({ status: "loss", result_r: -1 }),
    function () { return barsOpen; }).source, "ledger");

var settledRec = rec();
var settled = MO.resolveRecord(settledRec, function () { return barsStopped; });
eq("an open record the candles have already closed is settled here",
  settled.state, "loss");
eq("and is marked as coming from the replay", settled.source, "replay");

var stillOpen = MO.resolveRecord(rec(), function () { return barsOpen; });
eq("an open record the candles agree is open stays open",
  stillOpen.state, "active");

// ------------------------------------------------- the durable record
// The replay above wrote that loss down. Now take the candles away
// entirely - as happens when the page is opened days later and the window
// no longer reaches back - and the outcome must survive.
var afterwards = MO.resolveRecord(settledRec, function () { return []; });
eq("an outcome this browser saw survives the candles moving on",
  afterwards.state, "loss");
eq("and is marked as recorded here", afterwards.source, "observed");

// A CLOSE IS FINAL. Feed the same record candles that say it is still open
// and it must NOT re-open: history that revises itself is not history.
var reopened = MO.resolveRecord(settledRec, function () { return barsOpen; });
eq("a recorded close is never re-opened by a later replay",
  reopened.state, "loss");

// And if a later replay closes it the OTHER way, the recorded one stands
// and the disagreement is raised rather than silently swallowed.
var winBars = [bar(0, 100.2, 99.8), bar(1, 99, 95.5)];
var disputed = MO.resolveRecord(settledRec, function () { return winBars; });
eq("a contradicting replay does not overwrite the record",
  disputed.state, "loss");
eq("but the contradiction is reported", disputed.disputed, "win");
check("and the audit raises it",
  MO.audit([settledRec], function () { return winBars; })
    .some(function (i) { return i.level === "contradicts"; }));

// A record never seen, with no candles, must NOT be invented.
var unseen = MO.resolveRecord(
  rec({ id: "NEVER|1h|short@1", status: "pending" }),
  function () { return []; });
eq("a record nothing can resolve keeps the ledger's own state",
  unseen.state, "pending");
check("and is flagged stale rather than presented as checked",
  unseen.stale === true, JSON.stringify(unseen));

// The store must not revise itself.
var before = JSON.stringify(MO.observedAll());
MO.resolveRecord(settledRec, function () { return winBars; });
eq("an observation is never overwritten once written",
  JSON.stringify(MO.observedAll()), before);

// ---------------------------------------------------------------- audit
var issues = MO.audit([rec()], function () { return barsStopped; });
check("the audit reports a ledger that is behind the candles",
  issues.some(function (i) { return i.level === "behind"; }),
  JSON.stringify(issues));

// -------------------------------------------------------------- validate
check("a long stop above the entry is reported as a broken record",
  MO.validate({ side: "long", entry: 100, stop: 101, targets: [110],
    published_ts: 1 }).length > 0);
check("a short target above the entry is reported as a broken record",
  MO.validate({ side: "short", entry: 100, stop: 102, targets: [110],
    published_ts: 1 }).length > 0);
check("a sound record reports nothing",
  MO.validate({ side: "short", entry: 100, stop: 102, targets: [96],
    published_ts: 1 }).length === 0);

// ---------------------------------------------------------------- report
console.log("=".repeat(62));
pass.forEach(function (p) { console.log("  ok    " + p); });
fail.forEach(function (f) { console.log("  FAIL  " + f); });
console.log("=".repeat(62));
if (fail.length) {
  console.log("\n" + fail.length + " failing test(s).\n");
  process.exit(1);
}
console.log("\nAll " + pass.length + " tests passed.\n");
