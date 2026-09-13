"""
signal_ledger.py - the append-only record of every signal this system published,
and the only honest source of a win rate.

The old local panel measured win rate because a watcher sat there tracking each
signal second by second. Nothing sits there now: the pipeline wakes twice a day
and goes away. So the ledger works the other way round - each run writes down
what it published, and the NEXT run goes back over the open records and asks the
candles what happened in between.

The rule for resolution is deliberately strict, because a lenient one produces a
flattering number that means nothing:

  pending    published, price never reached the entry zone
  active     entry was touched
  win        after activation, TP1 was touched before the stop
  loss       after activation, the stop was touched before TP1
  expired    never activated within the expiry window

Within a single candle both the stop and the target can be inside the range. The
candle does not say which came first, so that bar is scored a LOSS. That is the
pessimistic assumption and it is the correct one: assuming the good fill is how
a backtest lies to you.

Nothing here is a prediction. It is a record of what already happened.
"""

import json
import os
import time

import exchange

# How long a signal has to activate before it is written off. In bars of its own
# timeframe, so a 15m signal is not judged on the same clock as a daily one.
EXPIRY_BARS = 24

# How far past activation TP1 or the stop is still attributed to the signal.
RESOLVE_BARS = 120

TF_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
    "12h": 43200, "1d": 86400, "3d": 259200, "1w": 604800,
}

OPEN_STATES = ("pending", "active")


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("records"), list):
            return data
    except (FileNotFoundError, ValueError, OSError):
        pass
    return {"records": [], "tracking_since": None}


def _save(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _key(sig):
    """Identifies the same idea across runs.

    Market, timeframe and side - deliberately not the entry. The engine
    re-publishes a live setup every run and the entry drifts a few ticks each
    time as ATR updates; keying on it filed the same HYPE short twice and
    every rate would then be computed over duplicates. You cannot hold two
    shorts on the same market and timeframe at once, so one open record per
    key is the honest model.

    A republished setup keeps its ORIGINAL levels. Following the drift would
    mean the outcome could not be attributed to anything that was actually
    published.
    """
    return "|".join([str(sig.get("symbol")), str(sig.get("tf")),
                     str(sig.get("side"))])


def _tehran(ts):
    return time.strftime("%Y-%m-%d %H:%M",
                         time.gmtime(ts + 3.5 * 3600)) + " Tehran"


def _new_record(sig, ts):
    targets = [t for t in (sig.get("targets") or []) if isinstance(t, (int, float))]
    entry, stop = sig.get("entry"), sig.get("stop")
    risk = abs(entry - stop) if entry is not None and stop is not None else None
    return {
        "id": _key(sig) + "@" + str(int(ts)),
        "key": _key(sig),
        "symbol": sig.get("symbol"),
        "tf": sig.get("tf"),
        "side": sig.get("side"),
        "strategy": sig.get("strategy"),
        "trade_style": sig.get("tf"),
        "confidence": sig.get("confidence"),
        "entry": entry,
        "entry_zone": sig.get("entry_zone"),
        "stop": stop,
        "targets": targets,
        "rr": sig.get("rr"),
        "atr": sig.get("atr"),
        "risk_per_unit": risk,
        "published_ts": ts,
        "published_time_tehran": _tehran(ts),
        "last_seen_ts": ts,
        "republished": 1,
        "activated_ts": None,
        "activated_time_tehran": None,
        "resolved_ts": None,
        "status": "pending",
        "result": "open",
        "result_r": None,
        "resolve_note": "",
        # Excursions in R. The pair is what separates "the stop was badly
        # placed" from "the idea was wrong": a trade that went 1.8R in favour
        # before stopping out failed at management, not at entry.
        "mfe_r": None,
        "mae_r": None,
        "bars_to_activate": None,
        "bars_to_resolve": None,
        # Everything the engine saw at publication time, kept verbatim. This is
        # the feature row: without it the outcome column has nothing to learn
        # from, and reconstructing the state afterwards is guesswork.
        "supports": list(sig.get("supports") or []),
        "warnings": list(sig.get("warnings") or []),
        "blockers": list(sig.get("blockers") or []),
        "entry_distance_pct": sig.get("entry_distance_pct"),
        "entry_distance_atr": sig.get("entry_distance_atr"),
        "context": dict(sig.get("context") or {}),
        "position_at_publish": dict(sig.get("position") or {}),
    }


def _zone(rec):
    z = rec.get("entry_zone")
    if isinstance(z, (list, tuple)) and len(z) == 2 and None not in z:
        return min(z), max(z)
    e = rec.get("entry")
    return (e, e) if e is not None else (None, None)


def _candles_since(symbol, tf, since_ts, now):
    sec = TF_SECONDS.get(tf)
    if not sec:
        return []
    need = int((now - since_ts) / sec) + 6
    if need < 3:
        return []
    raw = exchange.get("/fapi/v1/klines",
                       {"symbol": symbol, "interval": tf,
                        "limit": min(max(need, 10), 1000)})
    out = []
    for k in raw:
        t = int(k[0]) // 1000
        if t < since_ts:
            continue
        out.append({"t": t, "o": float(k[1]), "h": float(k[2]),
                    "l": float(k[3]), "c": float(k[4])})
    return out


def _walk(rec, bars):
    """Replay the bars since publication and decide what happened.

    Both the stop and the target can sit inside one bar's range. The bar does
    not record which was touched first, so that case is scored a loss - the
    pessimistic reading. Assuming the good fill is exactly how a measured win
    rate turns into a flattering fiction.
    """
    long = rec.get("side") == "long"
    lo_z, hi_z = _zone(rec)
    stop, targets = rec.get("stop"), rec.get("targets") or []
    tp1 = targets[0] if targets else None
    if None in (lo_z, hi_z, stop) or tp1 is None:
        return rec

    entry = rec.get("entry")
    risk = rec.get("risk_per_unit") or abs(entry - stop)
    status = rec.get("status")
    activated = rec.get("activated_ts")
    seen = 0
    live_bars = 0
    best = rec.get("mfe_r")
    worst = rec.get("mae_r")

    def note_excursion(bar):
        nonlocal best, worst
        if not risk:
            return
        up = (bar["h"] - entry) if long else (entry - bar["l"])
        dn = (entry - bar["l"]) if long else (bar["h"] - entry)
        best = up / risk if best is None else max(best, up / risk)
        worst = dn / risk if worst is None else max(worst, dn / risk)

    def finish(state, result, note, bar, r_value):
        rec["status"] = state
        rec["result"] = result
        rec["result_r"] = r_value
        rec["resolved_ts"] = bar["t"]
        rec["resolve_note"] = note
        rec["mfe_r"] = None if best is None else round(best, 3)
        rec["mae_r"] = None if worst is None else round(worst, 3)
        rec["bars_to_resolve"] = live_bars
        return rec

    for b in bars:
        seen += 1
        if status == "pending":
            touched = b["l"] <= hi_z and b["h"] >= lo_z
            if touched:
                status, activated = "active", b["t"]
                rec["activated_ts"] = activated
                rec["activated_time_tehran"] = _tehran(activated)
                rec["bars_to_activate"] = seen
            else:
                # A gap can jump the entry zone entirely and land beyond the
                # stop. The trade was never enterable, so it is not a loss -
                # counting it as one would punish the engine for a fill that
                # never existed.
                past_stop = (b["h"] >= stop) if not long else (b["l"] <= stop)
                if past_stop:
                    return finish("invalidated", "gapped past the entry",
                                  "price passed the stop without ever trading "
                                  "through the entry zone", b, None)
                if seen >= EXPIRY_BARS:
                    return finish("expired", "never activated",
                                  "price never reached the entry zone in %d bars"
                                  % EXPIRY_BARS, b, None)
                continue

        if status == "active":
            live_bars += 1
            note_excursion(b)
            hit_stop = (b["l"] <= stop) if long else (b["h"] >= stop)
            hit_tp = (b["h"] >= tp1) if long else (b["l"] <= tp1)
            if hit_stop:
                return finish(
                    "loss",
                    "stop" if not hit_tp else "stop and target in one bar",
                    ("both levels inside one bar, scored against the trade"
                     if hit_tp else "stop touched first"), b, -1.0)
            if hit_tp:
                return finish("win", "tp1", "first target touched", b,
                              (abs(tp1 - entry) / risk) if risk else None)
            if activated and (b["t"] - activated) / max(
                    1, TF_SECONDS.get(rec["tf"], 60)) >= RESOLVE_BARS:
                return finish("expired",
                              "unresolved after %d bars" % RESOLVE_BARS,
                              "neither level was reached in the window", b, None)

    rec["status"] = status
    rec["mfe_r"] = None if best is None else round(best, 3)
    rec["mae_r"] = None if worst is None else round(worst, 3)
    return rec


def _rate(records):
    wins = sum(1 for r in records if r.get("status") == "win")
    losses = sum(1 for r in records if r.get("status") == "loss")
    decided = wins + losses
    rs = [r["result_r"] for r in records
          if r.get("status") in ("win", "loss") and r.get("result_r") is not None]
    return {
        "trades": decided,
        "wins": wins,
        "losses": losses,
        # None, not zero. A rate computed on nothing is not a rate, and a bold
        # 0.0% on an empty sample is the most misleading number a panel can show.
        "win_rate": (wins / decided * 100.0) if decided else None,
        "expectancy_r": (sum(rs) / len(rs)) if rs else None,
        "open": sum(1 for r in records if r.get("status") in OPEN_STATES),
        "expired": sum(1 for r in records if r.get("status") == "expired"),
        "invalidated": sum(1 for r in records if r.get("status") == "invalidated"),
    }


# One row per signal, flat, outcome in the last column. This is the file to
# point a model at: the features are what the engine saw when it published,
# the label is what the market did afterwards, and nothing in between was
# filled in by hand.
CSV_COLUMNS = [
    "id", "symbol", "tf", "side", "strategy", "confidence",
    "published_ts", "published_time_tehran",
    "entry", "stop", "tp1", "tp2", "rr", "atr", "risk_per_unit",
    "entry_distance_pct", "entry_distance_atr",
    "ctx_score", "ctx_other_side_score", "ctx_funding", "ctx_ls_top",
    "ctx_oi_1h", "ctx_perp_spot_ratio", "ctx_adx", "ctx_rsi14",
    "n_supports", "n_warnings", "n_blockers", "supports", "warnings",
    "republished", "bars_to_activate", "bars_to_resolve",
    "mfe_r", "mae_r", "status", "result", "result_r",
]


def _csv_row(r):
    c = r.get("context") or {}
    t = r.get("targets") or []
    return {
        "id": r.get("id"), "symbol": r.get("symbol"), "tf": r.get("tf"),
        "side": r.get("side"), "strategy": r.get("strategy"),
        "confidence": r.get("confidence"),
        "published_ts": r.get("published_ts"),
        "published_time_tehran": r.get("published_time_tehran"),
        "entry": r.get("entry"), "stop": r.get("stop"),
        "tp1": t[0] if len(t) > 0 else None,
        "tp2": t[1] if len(t) > 1 else None,
        "rr": r.get("rr"), "atr": r.get("atr"),
        "risk_per_unit": r.get("risk_per_unit"),
        "entry_distance_pct": r.get("entry_distance_pct"),
        "entry_distance_atr": r.get("entry_distance_atr"),
        "ctx_score": c.get("score"),
        "ctx_other_side_score": c.get("other_side_score"),
        "ctx_funding": c.get("funding"), "ctx_ls_top": c.get("ls_top"),
        "ctx_oi_1h": c.get("oi_1h"),
        "ctx_perp_spot_ratio": c.get("perp_spot_ratio"),
        "ctx_adx": c.get("adx"), "ctx_rsi14": c.get("rsi14"),
        "n_supports": len(r.get("supports") or []),
        "n_warnings": len(r.get("warnings") or []),
        "n_blockers": len(r.get("blockers") or []),
        "supports": "; ".join(r.get("supports") or []),
        "warnings": "; ".join(r.get("warnings") or []),
        "republished": r.get("republished"),
        "bars_to_activate": r.get("bars_to_activate"),
        "bars_to_resolve": r.get("bars_to_resolve"),
        "mfe_r": r.get("mfe_r"), "mae_r": r.get("mae_r"),
        "status": r.get("status"), "result": r.get("result"),
        "result_r": r.get("result_r"),
    }


def _write_csv(path, records):
    import csv
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for r in records:
            w.writerow(_csv_row(r))
    os.replace(tmp, path)


def _grouped(records, field):
    out = {}
    for r in records:
        out.setdefault(r.get(field) or "unknown", []).append(r)
    rows = []
    for name, group in out.items():
        row = _rate(group)
        row["name"] = name
        rows.append(row)
    rows.sort(key=lambda x: (-x["trades"], x["name"]))
    return rows


def update(signals, path="brief/signals.json", now=None, quiet=True, dry_run=False):
    """Record this run's signals, resolve the open ones, return the archive.

    Called once per run. Safe to call on a dry run - it will resolve and report
    but write nothing, so a test never pollutes the record.
    """
    now = now or time.time()
    data = _load(path)
    records = data.get("records") or []
    if not data.get("tracking_since"):
        data["tracking_since"] = _tehran(now)

    by_key = {}
    for r in records:
        if r.get("status") in OPEN_STATES:
            by_key[r.get("key")] = r

    added = 0
    for sig in signals or []:
        if sig.get("entry") is None or sig.get("stop") is None:
            continue
        k = _key(sig)
        if k in by_key:
            # Same setup, still open, published again. Count the repeat rather
            # than filing a second record for one trade.
            rec = by_key[k]
            rec["last_seen_ts"] = now
            rec["republished"] = int(rec.get("republished") or 1) + 1
            rec["confidence"] = sig.get("confidence", rec.get("confidence"))
            continue
        rec = _new_record(sig, now)
        records.append(rec)
        by_key[k] = rec
        added += 1

    resolved = 0
    for rec in records:
        if rec.get("status") not in OPEN_STATES:
            continue
        start = rec.get("activated_ts") or rec.get("published_ts")
        try:
            bars = _candles_since(rec["symbol"], rec["tf"], start, now)
        except Exception as exc:
            rec["resolve_note"] = "candles unavailable: " + type(exc).__name__
            continue
        if not bars:
            continue
        before = rec.get("status")
        _walk(rec, bars)
        if rec.get("status") != before:
            resolved += 1

    records.sort(key=lambda r: -(r.get("published_ts") or 0))
    # Cap the file so the repo does not grow without bound. Open records are
    # never dropped, however old - an unresolved trade is not noise.
    keep, closed = [], 0
    for r in records:
        if r.get("status") in OPEN_STATES:
            keep.append(r)
        elif closed < 600:
            keep.append(r)
            closed += 1
    records = keep

    data["records"] = records
    data["overall"] = _rate(records)
    data["by_symbol"] = _grouped(records, "symbol")
    data["by_style"] = _grouped(records, "tf")
    data["by_strategy"] = _grouped(records, "strategy")
    data["updated_tehran"] = _tehran(now)
    data["updated_ts"] = now

    if not dry_run:
        try:
            _save(path, data)
            _write_csv(os.path.splitext(path)[0] + ".csv", records)
        except OSError as exc:
            if not quiet:
                print("  ledger write failed: %s" % exc, flush=True)

    if not quiet:
        rate = data["overall"]["win_rate"]
        print("  ledger: %d new, %d resolved, %d tracked, win rate %s"
              % (added, resolved, len(records),
                 ("%.1f%%" % rate) if rate is not None else "not enough closed trades"),
              flush=True)
    return data


if __name__ == "__main__":
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else "brief/brief_latest.json"
    with open(src, "r", encoding="utf-8") as fh:
        brief = json.load(fh)
    out = update(brief.get("signals") or [], quiet=False, dry_run=True)
    print(json.dumps(out["overall"], indent=1))
