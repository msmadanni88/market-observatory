/* ===========================================================================
   SIGNAL RESOLUTION - THE ONE COPY
   ---------------------------------------------------------------------------
   Every page that shows the state of a signal loads this file. There is
   exactly one implementation of "what happened to this trade", so two pages
   cannot hold two opinions about one trade.

   That was a real bug, not a hypothetical: the panel replayed the candles and
   said a HYPE short had been stopped out, while the analytics page read the
   committed file and said the same trade was still ACTIVE. Both were running
   correct code. They were answering different questions.

   THREE SOURCES, IN THIS ORDER OF AUTHORITY

     1. THE LEDGER, once it has closed a record. The pipeline settles trades
        from the candles and commits the result. A closed record is final.
     2. WHAT THIS BROWSER ALREADY SAW. When the browser resolves an open
        record to a closed state it writes that down locally, permanently,
        and never revises it. This is what keeps history when the candle
        window has moved past the trade and nobody can replay it any more.
     3. THE LIVE REPLAY. Bars since publication, walked forward. This is what
        knows a trade closed twenty minutes ago, hours before the pipeline
        next runs.

   A state is never invented. Where no source can answer, the answer is
   "unknown" and the page says so, rather than defaulting to pending and
   quietly asserting something false.

   WHY A REPLAY AND NOT A SNAPSHOT
   Asking "is the price in the entry zone right now" cannot see a path. Price
   ran down through the zone AND through the stop, then came back out, and the
   snapshot said PENDING because at that instant it was outside the zone. A
   bar walk sees the whole journey.

   WHERE A BAR CONTAINS BOTH THE STOP AND THE TARGET it is scored a LOSS. The
   bar does not say which came first, and the flattering assumption is how a
   measured win rate becomes fiction.
   =========================================================================== */
(function (root) {
  "use strict";

  var CLOSED = ["win", "loss", "expired", "invalidated"];
  var OPEN = ["pending", "active"];

  function isClosed(s) { return CLOSED.indexOf(s) >= 0; }
  function isOpen(s) { return OPEN.indexOf(s) >= 0; }
  function fin(v) { return v != null && isFinite(v); }

  /* -------------------------------------------------------------------
     THE REPLAY
     bars must be the candle series for this signal's own market and
     timeframe, oldest first, each {t,o,h,l,c} with t in SECONDS. The
     caller supplies them, so this file has no opinion about fetching.
     ------------------------------------------------------------------- */
  function replay(sig, plan, publishedTs, bars) {
    var out = {
      state: "pending", resolvedAt: null, resultR: null,
      mfe: null, mae: null, barsSeen: 0, note: "", stale: false
    };
    var zone = (sig.entry_zone || []).filter(fin);
    var lo = zone.length === 2 ? Math.min(zone[0], zone[1]) : plan.entry;
    var hi = zone.length === 2 ? Math.max(zone[0], zone[1]) : plan.entry;
    var stop = plan.stop;
    var tp1 = (plan.targets || [])[0];
    var long = plan.side === "long";
    var risk = Math.abs(plan.entry - stop);

    if (!publishedTs || !fin(stop) || !fin(tp1) || !(risk > 0)) {
      out.stale = true;
      out.note = "the plan is incomplete, so nothing can be replayed";
      return out;
    }

    var all = bars || [];
    var seen = [];
    for (var i = 0; i < all.length; i++) {
      if (all[i] && all[i].t >= publishedTs - 1) seen.push(all[i]);
    }
    if (!seen.length) {
      // The series does not reach back to publication. Saying "pending"
      // here would be asserting something this data cannot support.
      out.stale = true;
      out.note = "the candle window no longer reaches back to publication";
      return out;
    }
    out.barsSeen = seen.length;

    var state = "pending", best = null, worst = null;
    for (var j = 0; j < seen.length; j++) {
      var b = seen[j];
      if (state === "pending") {
        if (b.l <= hi && b.h >= lo) {
          state = "active";
        } else {
          // The entry can sit the far side of the market - a breakdown
          // short waits for price to come DOWN to it. If price runs the
          // other way and passes the stop without ever trading through
          // the zone, the plan is dead but it is NOT a loss: there was
          // never a fill to lose on. Calling it a stop-out would blame
          // the engine for a trade nobody could have taken.
          var pastStop = long ? (b.l <= stop) : (b.h >= stop);
          if (pastStop) {
            out.state = "invalidated";
            out.resolvedAt = b.t;
            out.note = "price passed the stop without trading through the " +
              "entry zone";
            return out;
          }
          continue;
        }
      }
      // Excursions are measured from the entry, in R, once the trade is live.
      var up = long ? (b.h - plan.entry) : (plan.entry - b.l);
      var dn = long ? (plan.entry - b.l) : (b.h - plan.entry);
      best = best == null ? up : Math.max(best, up);
      worst = worst == null ? dn : Math.max(worst, dn);

      var hitStop = long ? (b.l <= stop) : (b.h >= stop);
      var hitTp = long ? (b.h >= tp1) : (b.l <= tp1);
      if (hitStop) {
        out.state = "loss";
        out.resolvedAt = b.t;
        out.resultR = -1;
        out.note = hitTp
          ? "stop and target inside one bar, scored against the trade"
          : "stop touched";
        break;
      }
      if (hitTp) {
        out.state = "win";
        out.resolvedAt = b.t;
        out.resultR = Math.abs(tp1 - plan.entry) / risk;
        out.note = "first target touched";
        break;
      }
    }
    if (!out.resolvedAt) out.state = state;
    out.mfe = best == null ? null : best / risk;
    out.mae = worst == null ? null : worst / risk;
    return out;
  }

  /* -------------------------------------------------------------------
     WHAT THIS BROWSER ALREADY SAW
     Append-only. A resolution, once written, is never revised or removed
     by this code - that is the whole point. It is what carries history
     across the gap between pipeline runs, and across the page being
     closed and reopened days later when the candles no longer reach.
     ------------------------------------------------------------------- */
  var OBS_KEY = "market_observatory_observed_v1";

  function obsAll() {
    try {
      return JSON.parse(root.localStorage.getItem(OBS_KEY)) || {};
    } catch (e) { return {}; }
  }
  function obsWrite(map) {
    try {
      root.localStorage.setItem(OBS_KEY, JSON.stringify(map));
      return true;
    } catch (e) { return false; }
  }
  /* Records a closed outcome the browser worked out for itself. Returns
     true only if this was new. An existing entry is left exactly as it
     was: the FIRST sighting is the most trustworthy one, because it was
     made when the candles still covered the trade. */
  function observe(rec, res) {
    if (!rec || !rec.id || !res || !isClosed(res.state)) return false;
    var m = obsAll();
    if (m[rec.id]) return false;
    m[rec.id] = {
      ref: rec.ref || null, symbol: rec.symbol, tf: rec.tf, side: rec.side,
      state: res.state, resolvedAt: res.resolvedAt || null,
      resultR: res.resultR == null ? null : res.resultR,
      mfe: res.mfe == null ? null : res.mfe,
      mae: res.mae == null ? null : res.mae,
      note: res.note || "", observedAt: Math.floor(Date.now() / 1000)
    };
    return obsWrite(m);
  }
  function observedFor(id) { return obsAll()[id] || null; }

  /* -------------------------------------------------------------------
     THE ANSWER
     One record in, one settled view out, with its provenance attached so
     the page can say WHERE the number came from rather than presenting
     three different sources as one anonymous fact.

     getBars(symbol, tf) is supplied by the page. It may return an empty
     array; that is handled, not an error.
     ------------------------------------------------------------------- */
  function resolveRecord(rec, getBars) {
    if (!rec) return null;

    // 1. The ledger, once it has closed something. Final.
    if (isClosed(rec.status)) {
      return {
        state: rec.status, resultR: rec.result_r,
        mfe: rec.mfe_r, mae: rec.mae_r,
        note: rec.resolve_note || "", source: "ledger", settled: true,
        stale: false
      };
    }

    var plan = {
      side: rec.side, entry: rec.entry, stop: rec.stop,
      targets: rec.targets || []
    };
    var bars = [];
    try {
      bars = getBars ? (getBars(rec.symbol, rec.tf) || []) : [];
    } catch (e) { bars = []; }
    var r = replay({ symbol: rec.symbol, tf: rec.tf,
      entry_zone: rec.entry_zone }, plan, rec.published_ts, bars);

    // 2. What this browser already saw. A CLOSE IS FINAL. A trade that has
    //    been stopped out cannot become open again later because a venue
    //    served a slightly different candle, and history that can revise
    //    itself is not history. If a later replay closes it differently,
    //    the recorded one stands and the disagreement is flagged rather
    //    than silently resolved.
    var seen = observedFor(rec.id);
    if (seen) {
      return {
        state: seen.state, resultR: seen.resultR, mfe: seen.mfe,
        mae: seen.mae, note: seen.note, source: "observed",
        settled: false, stale: false, observedAt: seen.observedAt,
        disputed: isClosed(r.state) && r.state !== seen.state ? r.state : null
      };
    }

    // 3. The live replay, for anything not yet settled anywhere.
    if (isClosed(r.state)) {
      observe(rec, r);
      if (r.mae == null && rec.mae_r != null) r.mae = rec.mae_r;
      if (r.mfe == null && rec.mfe_r != null) r.mfe = rec.mfe_r;
      r.source = "replay";
      r.settled = false;
      return r;
    }

    // Still open, or nothing can answer.
    if (r.stale) {
      return {
        state: isOpen(rec.status) ? rec.status : "unknown",
        resultR: null, mfe: rec.mfe_r, mae: rec.mae_r,
        note: r.note, source: "ledger-stale", settled: false, stale: true
      };
    }
    // The replay says pending but the pipeline had already seen a fill.
    if (r.state === "pending" && rec.status === "active") r.state = "active";
    if (r.mae == null && rec.mae_r != null) r.mae = rec.mae_r;
    if (r.mfe == null && rec.mfe_r != null) r.mfe = rec.mfe_r;
    r.source = "replay";
    r.settled = false;
    return r;
  }

  /* -------------------------------------------------------------------
     INTEGRITY
     Run over every record on every load. It is cheap, and it is the only
     thing that catches a page quietly showing a number from a source
     that has since been contradicted.

     Three things are worth saying out loud:
       BEHIND      the ledger still calls a trade open that the candles
                   have already closed. Normal between runs - but the
                   page must show the settled state, not the stale one.
       CONTRADICTS the ledger closed it one way and this browser recorded
                   another. That is a real problem and says so.
       UNKNOWN     nothing can answer. Better said than guessed.
     ------------------------------------------------------------------- */
  function audit(records, getBars) {
    var issues = [];
    (records || []).forEach(function (rec) {
      var v = resolveRecord(rec, getBars);
      if (!v) return;
      // The ledger is behind whenever it still calls a trade open that has
      // been closed here - whether the close was worked out this second or
      // recorded earlier. Requiring source==="replay" hid it the moment the
      // outcome got written down, which is to say almost immediately.
      if (isOpen(rec.status) && isClosed(v.state)) {
        issues.push({
          level: "behind", id: rec.id, ref: rec.ref,
          text: (rec.ref || rec.id) + " is recorded as " + rec.status +
            " but the candles have already closed it as " + v.state +
            (v.source === "observed" ? ", recorded here " +
              (v.observedAt ? "at " + new Date(v.observedAt * 1000)
                .toISOString().slice(0, 16).replace("T", " ") + " UTC" :
                "earlier") : "") +
            ". Shown as " + v.state + "; the pipeline files it on its " +
            "next run."
        });
      }
      var seen = observedFor(rec.id);
      if (seen && isClosed(rec.status) && seen.state !== rec.status) {
        issues.push({
          level: "contradicts", id: rec.id, ref: rec.ref,
          text: (rec.ref || rec.id) + " was recorded here as " + seen.state +
            " but the ledger now says " + rec.status +
            ". One of the two is wrong and this needs looking at."
        });
      }
      if (v.disputed) {
        issues.push({
          level: "contradicts", id: rec.id, ref: rec.ref,
          text: (rec.ref || rec.id) + " was recorded here as " + v.state +
            " and the candles now read it as " + v.disputed +
            ". The recorded one stands, because a close is final - but the " +
            "two should not differ and this needs looking at."
        });
      }
      if (v.state === "unknown") {
        issues.push({
          level: "unknown", id: rec.id, ref: rec.ref,
          text: (rec.ref || rec.id) + " cannot be resolved from any source " +
            "available here."
        });
      }
    });
    return issues;
  }

  /* Sanity checks on the record itself, independent of any outcome. A
     stop on the wrong side of the entry is not a rendering problem, it is
     a broken record, and it should be visible rather than drawn. */
  function validate(rec) {
    var bad = [];
    if (!rec) return ["record is missing"];
    if (!fin(rec.entry)) bad.push("entry is not a number");
    if (!fin(rec.stop)) bad.push("stop is not a number");
    if (fin(rec.entry) && fin(rec.stop)) {
      if (rec.side === "long" && rec.stop >= rec.entry)
        bad.push("long stop is at or above the entry");
      if (rec.side === "short" && rec.stop <= rec.entry)
        bad.push("short stop is at or below the entry");
    }
    var t = (rec.targets || []).filter(fin);
    if (!t.length) bad.push("no usable target");
    else if (fin(rec.entry)) {
      if (rec.side === "long" && t[0] <= rec.entry)
        bad.push("long target is at or below the entry");
      if (rec.side === "short" && t[0] >= rec.entry)
        bad.push("short target is at or above the entry");
    }
    if (!rec.published_ts) bad.push("no publication time");
    return bad;
  }

  /* One reference per signal, identical here, in the ledger, in the CSV
     and on every page. Deterministic from the publication minute and the
     market: MO-260913-1745-HYPE-30m-S */
  function coinBase(s) {
    return String(s || "").replace(/USDT$|USDC$|USD$/i, "");
  }
  function signalRef(symbol, tf, side, ts) {
    var d = new Date(((Number(ts) || 0) + 3.5 * 3600) * 1000);
    var p = function (n) { return String(n).padStart(2, "0"); };
    var stamp = String(d.getUTCFullYear()).slice(2) + p(d.getUTCMonth() + 1) +
      p(d.getUTCDate()) + "-" + p(d.getUTCHours()) + p(d.getUTCMinutes());
    return "MO-" + stamp + "-" + coinBase(symbol) + "-" + tf + "-" +
      (side === "long" ? "L" : "S");
  }

  root.MO = {
    CLOSED_STATES: CLOSED,
    OPEN_STATES: OPEN,
    isClosed: isClosed,
    isOpen: isOpen,
    replay: replay,
    resolveRecord: resolveRecord,
    observe: observe,
    observedFor: observedFor,
    observedAll: obsAll,
    audit: audit,
    validate: validate,
    signalRef: signalRef,
    coinBase: coinBase
  };
})(typeof window !== "undefined" ? window : this);
