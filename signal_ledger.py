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
    """Identifies the same setup across runs.

    The engine re-publishes a live setup every run while it is still valid. The
    entry is rounded because the level drifts a few ticks between runs as ATR
    updates - without that, one setup would enter the ledger twice a day and
    every rate would be computed over duplicates.
    """
    entry = sig.get("entry")
    tag = "%.6g" % entry if isinstance(entry, (int, float)) else "?"
    return "|".join([str(sig.get("symbol")), str(sig.get("tf")),
                     str(sig.get("side")), tag])


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

    status = rec.get("status")
    activated = rec.get("activated_ts")
    seen = 0

    for b in bars:
        seen += 1
        if status == "pending":
            if b["l"] <= hi_z and b["h"] >= lo_z:
                status, activated = "active", b["t"]
                rec["activated_ts"] = activated
                rec["activated_time_tehran"] = _tehran(activated)
            elif seen >= EXPIRY_BARS:
                rec["status"] = "expired"
                rec["result"] = "never activated"
                rec["resolved_ts"] = b["t"]
                rec["resolve_note"] = ("price never reached the entry zone in %d bars"
                                       % EXPIRY_BARS)
                return rec
            else:
                continue

        if status == "active":
            hit_stop = (b["l"] <= stop) if long else (b["h"] >= stop)
            hit_tp = (b["h"] >= tp1) if long else (b["l"] <= tp1)
            if hit_stop:
                rec["status"] = "loss"
                rec["result"] = "stop" if not hit_tp else "stop and target in one bar"
                rec["result_r"] = -1.0
                rec["resolved_ts"] = b["t"]
                rec["resolve_note"] = ("both levels inside one bar, scored against "
                                       "the trade" if hit_tp else "stop touched first")
                return rec
            if hit_tp:
                risk = rec.get("risk_per_unit") or 0
                rec["status"] = "win"
                rec["result"] = "tp1"
                rec["result_r"] = (abs(tp1 - rec["entry"]) / risk) if risk else None
                rec["resolved_ts"] = b["t"]
                rec["resolve_note"] = "first target touched"
                return rec
            if activated and (b["t"] - activated) / max(1, TF_SECONDS.get(rec["tf"], 60)) \
                    >= RESOLVE_BARS:
                rec["status"] = "expired"
                rec["result"] = "unresolved after %d bars" % RESOLVE_BARS
                rec["resolved_ts"] = b["t"]
                rec["resolve_note"] = "neither level was reached in the window"
                return rec

    rec["status"] = status
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
    }


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
