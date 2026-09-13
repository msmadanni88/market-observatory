#!/usr/bin/env python3
# ===========================================================================
# SIGNAL ENGINE v2
# ---------------------------------------------------------------------------
# Replaces the decision layer of watcher.py. It reads the same live.json
# snapshot the watcher already writes - no data collection changes - and
# judges it differently.
#
# WHY IT WAS REWRITTEN
# --------------------
# The old engine produced 18 signals and every single one was long. That is
# not a market observation, it is a bug, and it came from three places:
#
#   1. The verdict gate tested `len(conflicts) >= 2` BEFORE it tested score,
#      so a textbook downtrend - ADX 71, below cloud, red future cloud,
#      RSI 21 - returned "NO EDGE" because two conflicts happened to be lit.
#
#   2. `poor reward:risk` was computed as up_room / dn_room. That is the LONG
#      ratio. It was then applied as a conflict to both directions. A bad
#      long R:R is a GOOD short R:R - the old code treated the best short
#      setups as disqualifying.
#
#   3. `no room to resistance` existed with no `no room to support` twin, so
#      shorts never got their own version of the check and longs were held to
#      a standard shorts were not.
#
# The fix is one idea: a conflict is not absolute, it is relative to the
# direction being proposed. "Stretched low" kills a short and helps a long.
# So every side is scored independently, against its own obstacles.
#
# WHAT ELSE IS ENCODED
# --------------------
# Every rule below traces to a logged mistake, not to theory:
#
#   entry distance   11 of 18 setups cancelled before entry; the median dead
#                    setup sat 1.59% away, the only winner filled at -0.03%
#   event gate       a position was opened that had to survive a Fed chair
#                    speech before it could reach its own thesis
#   what is bidding  a short thesis on a token unlock failed because only
#                    1.75% of a prior unlock ever reached exchanges
#   crowding         the best call in the log came from top-trader L/S at
#                    2.01 with OI doubled into a rally, not from the chart
#   two timeframes   a divergence visible on one timeframe is an artifact
#   sizing           leverage is an output of risk and stop, never an input
# ===========================================================================

import json
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta

TEHRAN = timezone(timedelta(hours=3, minutes=30))


# ===========================================================================
# TUNABLES
# ===========================================================================

CFG = {
    # Sizing. The archive showed a saved config of 10.2% risk per trade
    # against a win rate measured on two closed trades. At the engine's own
    # minimum R:R of 1.5, full Kelly at a 45% win rate is 8.3% and at 40% it
    # is zero. 10.2% is above full Kelly on an unmeasured edge, and five
    # losses in a row - routine at any realistic win rate - is a 42%
    # drawdown. Until the ledger has 30+ closed trades, size small.
    "risk_pct": 1.0,
    "max_risk_pct": 2.0,

    # Entry must be reachable. Both gates apply; the tighter one wins.
    "max_entry_atr": 0.75,
    "max_entry_pct": 1.20,

    # Quality floors.
    "min_rr": 1.8,
    "min_confidence": 62.0,
    "min_score": 4,

    # Stop placement as a multiple of ATR.
    "stop_atr": {"tight": 1.0, "balanced": 1.5, "wide": 2.2},

    # Snapshot must be fresh.
    "max_age_sec": 900,

    # Crowding thresholds on top-trader long/short ratio.
    "ls_crowded_long": 1.60,
    "ls_crowded_short": 0.62,

    # IMPORTANT UNIT NOTE: watcher.py stores funding as a PERCENT already -
    # live.json carries 0.01 meaning 0.01%, not 1%. An earlier threshold of
    # 0.0005 treated it as a decimal fraction, so ordinary 0.01% funding lit
    # the "crowded" flag on literally every symbol and the printed value was
    # 100x too large. Thresholds below are in the same percent units as the
    # feed. Typical baseline is 0.01; sustained 0.05+ is a real crowding tax.
    "funding_hot": 0.05,

    # Block anything whose horizon crosses a critical event this close.
    "event_block_days": 2,
}


# ===========================================================================
# RESULT TYPES
# ===========================================================================

@dataclass
class SideRead:
    side: str
    score: float = 0.0
    supports: list = field(default_factory=list)
    blockers: list = field(default_factory=list)
    hard_blocks: list = field(default_factory=list)

    @property
    def viable(self):
        return not self.hard_blocks


@dataclass
class Candidate:
    symbol: str
    tf: str
    side: str
    strategy: str
    confidence: float
    market_price: float
    entry: float
    entry_zone: list
    stop: float
    targets: list
    rr: float
    atr: float
    entry_distance_pct: float
    entry_distance_atr: float
    supports: list = field(default_factory=list)
    blockers: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    position: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)
    rejected: str = ""


# ===========================================================================
# HELPERS
# ===========================================================================

def num(v, default=None):
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def first(seq, default=None):
    if isinstance(seq, (list, tuple)) and seq:
        return num(seq[0], default)
    return default


# ===========================================================================
# DIRECTION-RELATIVE READ
# ---------------------------------------------------------------------------
# The whole fix lives here. Each side is scored against its own obstacles.
# A hard block disqualifies the side outright; a blocker just costs points.
# ===========================================================================

def read_side(snap, side):
    r = SideRead(side=side)
    ind = snap.get("indicators") or {}
    lv = snap.get("levels") or {}
    pat = snap.get("patterns") or {}
    dv = snap.get("derivs") or {}

    px = num(snap.get("price"))
    atr = num(ind.get("atr"))
    if not px or not atr or atr <= 0:
        r.hard_blocks.append("missing price or ATR")
        return r

    long_ = side == "long"

    # ---- trend structure -------------------------------------------------
    bias = str(ind.get("bias") or "").upper()
    ich = str(ind.get("ich_pos") or "").lower()
    fut = str(ind.get("future_cloud") or "").lower()

    if long_:
        if bias == "BULLISH":
            r.score += 2; r.supports.append("structure bullish")
        elif bias == "BEARISH":
            r.score -= 2; r.blockers.append("structure bearish")
        if "above" in ich:
            r.score += 2; r.supports.append("above cloud")
        elif "below" in ich:
            r.score -= 2; r.blockers.append("below cloud")
        if fut == "green":
            r.score += 1; r.supports.append("future cloud green")
        elif fut == "red":
            r.score -= 1; r.blockers.append("future cloud red")
    else:
        if bias == "BEARISH":
            r.score += 2; r.supports.append("structure bearish")
        elif bias == "BULLISH":
            r.score -= 2; r.blockers.append("structure bullish")
        if "below" in ich:
            r.score += 2; r.supports.append("below cloud")
        elif "above" in ich:
            r.score -= 2; r.blockers.append("above cloud")
        if fut == "red":
            r.score += 1; r.supports.append("future cloud red")
        elif fut == "green":
            r.score -= 1; r.blockers.append("future cloud green")

    if "inside" in ich:
        r.hard_blocks.append("price inside the cloud")

    # ---- trend strength --------------------------------------------------
    adx = num(ind.get("adx"), 0) or 0
    pdi = num(ind.get("pdi"), 0) or 0
    mdi = num(ind.get("mdi"), 0) or 0
    if adx < 20:
        r.hard_blocks.append(f"no trend, ADX {adx:.0f}")
    else:
        aligned = (pdi > mdi) if long_ else (mdi > pdi)
        if aligned:
            r.score += 2 if adx >= 30 else 1
            r.supports.append(f"ADX {adx:.0f} aligned")
        else:
            r.score -= 2
            r.blockers.append(f"ADX {adx:.0f} against this side")

    # ---- stretch, evaluated relative to the side -------------------------
    # This is the bug that made every signal long. Being stretched low is
    # late for a short and early for a long. The old code treated it as a
    # conflict for both.
    rsi = num(ind.get("rsi14"))
    st = num(ind.get("stochrsi"))
    if rsi is not None and st is not None:
        hot = rsi > 70 or st > 85
        cold = rsi < 30 or st < 15
        if long_ and hot:
            r.blockers.append(f"stretched high, RSI {rsi:.0f} - chasing")
            r.score -= 2
        elif long_ and cold:
            r.score += 1
            r.supports.append(f"RSI {rsi:.0f} washed out, not chasing")
        elif (not long_) and cold:
            r.blockers.append(f"stretched low, RSI {rsi:.0f} - late")
            r.score -= 2
        elif (not long_) and hot:
            r.score += 1
            r.supports.append(f"RSI {rsi:.0f} overbought, selling strength")

    # ---- room, computed for THIS side ------------------------------------
    res = first(lv.get("res"))
    sup = first(lv.get("sup"))
    if res and sup and res > sup:
        up_room = (res - px) / px * 100.0
        dn_room = (px - sup) / px * 100.0
        if long_:
            room, against, target_name = up_room, dn_room, "resistance"
            near = (res - px) < atr * 0.6
        else:
            room, against, target_name = dn_room, up_room, "support"
            near = (px - sup) < atr * 0.6
        ratio = (room / against) if against > 0 else 0.0
        if ratio < 1.0:
            r.blockers.append(f"room ratio {ratio:.2f} against this side")
            r.score -= 1
        elif ratio >= 2.0:
            r.score += 1
            r.supports.append(f"room ratio {ratio:.2f}")
        if near:
            r.hard_blocks.append(
                f"{target_name} under 0.6 ATR away - no room to pay")

    # ---- divergence, relative --------------------------------------------
    d = pat.get("divergence")
    if isinstance(d, dict) and d.get("type"):
        t = str(d["type"]).lower()
        if long_ and "bullish" in t:
            r.score += 2; r.supports.append("bullish divergence")
        elif long_ and "bearish" in t:
            r.score -= 2; r.blockers.append("bearish divergence")
        elif (not long_) and "bearish" in t:
            r.score += 2; r.supports.append("bearish divergence")
        elif (not long_) and "bullish" in t:
            r.score -= 2; r.blockers.append("bullish divergence")

    # ---- liquidity sweep, relative ---------------------------------------
    sweep = str(pat.get("sweep") or "").lower()
    if long_ and "low" in sweep:
        r.score += 2; r.supports.append("swept lows")
    elif (not long_) and "high" in sweep:
        r.score += 2; r.supports.append("swept highs")

    # ---- tape agreement --------------------------------------------------
    # Genuine ambiguity blocks both sides equally. This one was never the bug.
    td = num(dv.get("tape_delta"))
    if td is not None and abs(td) > 12:
        agrees = (td > 0) if long_ else (td < 0)
        if agrees:
            r.score += 1; r.supports.append(f"tape {td:+.0f}%")
        else:
            r.score -= 1; r.blockers.append(f"tape {td:+.0f}% against")

    # ---- crowding --------------------------------------------------------
    # The strongest call in the whole log came from here, not from the chart:
    # top-trader L/S at 2.01 while OI doubled into a rally.
    ls = num(dv.get("ls_top"))
    if ls is not None:
        if long_ and ls >= CFG["ls_crowded_long"]:
            r.score -= 2
            r.blockers.append(f"top traders {ls:.2f} long - crowded")
        elif (not long_) and ls >= CFG["ls_crowded_long"]:
            r.score += 2
            r.supports.append(f"top traders {ls:.2f} long - fuel below")
        elif (not long_) and ls <= CFG["ls_crowded_short"]:
            r.score -= 2
            r.blockers.append(f"top traders {ls:.2f} - shorts crowded")
        elif long_ and ls <= CFG["ls_crowded_short"]:
            r.score += 2
            r.supports.append(f"top traders {ls:.2f} short - fuel above")

    fund = num(dv.get("funding"))
    if fund is not None:
        hot = CFG["funding_hot"]
        if long_ and fund > hot:
            r.score -= 1
            r.blockers.append(f"funding {fund:+.4f}% - paying to be long")
        elif (not long_) and fund < -hot:
            r.score -= 1
            r.blockers.append(f"funding {fund:+.4f}% - paying to be short")
        elif (not long_) and fund > hot:
            r.score += 1
            r.supports.append(f"funding {fund:+.4f}% - collected while short")
        elif long_ and fund < -hot:
            r.score += 1
            r.supports.append(f"funding {fund:+.4f}% - collected while long")

    # ---- what is bidding underneath --------------------------------------
    # The unlock thesis failed because nothing asked what was holding price
    # up. Spot volume dominating perp volume means real demand, not leverage.
    ratio_ps = num(dv.get("perp_spot_ratio"))
    if ratio_ps is not None:
        if (not long_) and ratio_ps < 3:
            r.blockers.append(
                f"perp/spot {ratio_ps:.1f}x - real spot bid underneath")
            r.score -= 1
        elif (not long_) and ratio_ps > 10:
            r.score += 1
            r.supports.append(
                f"perp/spot {ratio_ps:.1f}x - leverage driven, thin spot bid")

    oi_1h = num(dv.get("oi_1h"))
    if oi_1h is not None and abs(oi_1h) > 3:
        rising = oi_1h > 0
        if long_ and rising:
            r.score += 1; r.supports.append(f"OI {oi_1h:+.1f}% with price")
        elif (not long_) and rising:
            r.score += 1; r.supports.append(f"OI {oi_1h:+.1f}% - fuel")

    # ---- higher timeframe agreement --------------------------------------
    # A read that only exists on one timeframe is an artifact. Counter-trend
    # against a decided higher timeframe is not forbidden, but it needs an
    # actual reversal trigger - a liquidity sweep or a divergence - not just
    # a strong lower-timeframe score. Without one it is a hard block, because
    # confidence built purely on the lower timeframe reads far too high.
    htf = snap.get("htf") or {}
    hb = str(htf.get("bias") or "").upper()
    sweep_ok = (long_ and "low" in sweep) or ((not long_) and "high" in sweep)
    div_ok = False
    if isinstance(d, dict) and d.get("type"):
        t = str(d["type"]).lower()
        div_ok = ("bullish" in t) if long_ else ("bearish" in t)
    reversal_trigger = sweep_ok or div_ok

    if hb:
        agrees = (long_ and hb == "BULLISH") or ((not long_) and hb == "BEARISH")
        if agrees:
            r.score += 2
            r.supports.append(f"HTF {hb.lower()} agrees")
        elif hb in ("BULLISH", "BEARISH"):
            if reversal_trigger:
                r.score -= 3
                r.blockers.append(
                    f"counter-trend to {hb.lower()} HTF, justified only by "
                    f"the reversal trigger")
            else:
                r.hard_blocks.append(
                    f"HTF {hb.lower()} disagrees and there is no reversal "
                    f"trigger to justify fading it")
    else:
        r.blockers.append("no higher timeframe confirmation")
        r.score -= 1

    return r


# ===========================================================================
# BUILD
# ===========================================================================

def build(snap, cfg=None, now_ts=None, events=None, model="balanced"):
    """Judge one snapshot. Returns (Candidate | None, reason)."""
    c = dict(CFG)
    if cfg:
        c.update(cfg)

    sym = snap.get("symbol")
    tf = snap.get("tf")

    if not snap.get("verified"):
        return None, "price not verified across venues"

    ts = num(snap.get("ts"), 0) or 0
    if now_ts and (now_ts - ts) > c["max_age_sec"]:
        return None, f"snapshot stale by {int(now_ts - ts)}s"

    ind = snap.get("indicators") or {}
    px = num(snap.get("price"))
    atr = num(ind.get("atr"))
    if not px or not atr or atr <= 0:
        return None, "missing price or ATR"

    # Score both sides. This is what the old engine never did.
    reads = {s: read_side(snap, s) for s in ("long", "short")}
    viable = [r for r in reads.values() if r.viable and r.score >= c["min_score"]]
    if not viable:
        best = max(reads.values(), key=lambda r: r.score)
        why = (best.hard_blocks[0] if best.hard_blocks
               else f"best side scored {best.score:.0f}, floor is {c['min_score']}")
        return None, why
    read = max(viable, key=lambda r: r.score)
    side = read.side
    long_ = side == "long"

    # ---- entry ----------------------------------------------------------
    # 11 of 18 archived setups died unfilled at a median 1.59% away. The one
    # winner filled essentially at market. Entries go just inside the pullback,
    # never out at the edge of the zone where a limit waits forever.
    pull = atr * 0.35
    entry = px - pull if long_ else px + pull
    half = atr * 0.12
    zone = sorted([entry - half, entry + half])

    dist_pct = abs(entry - px) / px * 100.0
    dist_atr = abs(entry - px) / atr
    if dist_atr > c["max_entry_atr"] or dist_pct > c["max_entry_pct"]:
        return None, (f"entry {dist_pct:.2f}% / {dist_atr:.2f} ATR away - "
                      f"beyond reach gate")

    # ---- stop ------------------------------------------------------------
    mult = c["stop_atr"].get(model, 1.5)
    stop = entry - atr * mult if long_ else entry + atr * mult

    # ---- targets ---------------------------------------------------------
    lv = snap.get("levels") or {}
    struct = first(lv.get("res")) if long_ else first(lv.get("sup"))
    risk = abs(entry - stop)
    raw = []
    if struct and ((struct > entry) if long_ else (struct < entry)):
        raw.append((round(struct, 8), "observed structure"))
    for mult_r in (2.5, 3.5):
        t = entry + risk * mult_r if long_ else entry - risk * mult_r
        raw.append((round(t, 8), f"derived {mult_r}R"))

    # Order by distance from entry, nearest first. Without this the
    # structural level can land behind a derived extension, R:R gets
    # measured to the furthest target, and every setup clears the floor on
    # a number that was never real. A tight stop made one test read 7.85R.
    raw.sort(key=lambda p: abs(p[0] - entry))
    # Drop duplicates that round to the same level.
    seen, ordered = set(), []
    for price, kind in raw:
        key = round(price, 8)
        if key in seen:
            continue
        seen.add(key)
        ordered.append((price, kind))
    targets = [p for p, _ in ordered][:3]
    kinds = [k for _, k in ordered][:3]

    # R:R is measured to the nearest target - the conservative reading.
    rr = abs(targets[0] - entry) / risk if risk > 0 else 0.0
    if rr < c["min_rr"]:
        return None, f"first target R:R {rr:.2f} below floor {c['min_rr']}"

    # ---- event gate ------------------------------------------------------
    warnings = []
    for e in (events or []):
        if e.get("days_away", 99) <= c["event_block_days"] and \
                e.get("severity") == "critical":
            return None, (f"{e['label']} in {e['days_away']}d - a setup that "
                          f"must cross a binary event to reach its thesis is "
                          f"a broken structure")
        if e.get("days_away", 99) <= 7:
            warnings.append(f"{e['label']} in {e['days_away']}d")

    # ---- confidence ------------------------------------------------------
    # Technical confluence only. This is NOT a win probability and must not
    # be read as one: the ledger holds two closed trades, so nothing here is
    # calibrated against anything. The ceiling is deliberately 85 - a number
    # in the nineties would imply a certainty this system has not earned and
    # would invite oversizing, which is how the 10.2% risk setting happened.
    # Revisit the scale only after 30+ closed trades, per chapter 8.9.
    # Caps were originally 12 and 6, but real reads score up to 18 with as
    # many as 10 supports, so every clean setup saturated at the identical
    # number and the scale discriminated nothing. Bounds now sit above the
    # observed range so the score actually moves the output.
    conf = 44.0
    conf += min(max(read.score, 0), 18) * 1.55
    conf -= len(read.blockers) * 3.5
    conf += min(len(read.supports), 10) * 0.7
    if rr >= 2.5:
        conf += 3
    if dist_atr < 0.3:
        conf += 2
    if not (snap.get("htf") or {}).get("bias"):
        conf -= 4          # unconfirmed on the higher timeframe
    conf = round(max(0.0, min(85.0, conf)), 1)
    if conf < c["min_confidence"]:
        return None, f"confidence {conf} below floor {c['min_confidence']}"

    # ---- sizing ----------------------------------------------------------
    margin = num(c.get("margin"), 1000.0) or 1000.0
    risk_pct = min(num(c.get("risk_pct"), 1.0) or 1.0, c["max_risk_pct"])
    risk_usd = margin * risk_pct / 100.0
    qty = risk_usd / risk if risk > 0 else 0.0
    notional = qty * entry
    # Leverage is what falls out, never what goes in.
    lev = notional / margin if margin > 0 else 0.0
    mmr = 0.005
    liq = (entry * (1 - 1 / lev + mmr) if long_ and lev > 1
           else entry * (1 + 1 / lev - mmr) if lev > 1 else None)

    if liq is not None:
        liq_first = (liq > stop) if long_ else (liq < stop)
        if liq_first:
            warnings.append(
                "liquidation sits before the stop at this leverage - "
                "the stop would never fire")

    dv = snap.get("derivs") or {}
    return Candidate(
        symbol=sym, tf=tf, side=side,
        strategy=pick_strategy(snap, side),
        confidence=conf, market_price=round(px, 8),
        entry=round(entry, 8), entry_zone=[round(z, 8) for z in zone],
        stop=round(stop, 8), targets=targets, rr=round(rr, 2),
        atr=round(atr, 8),
        entry_distance_pct=round(dist_pct, 3),
        entry_distance_atr=round(dist_atr, 3),
        supports=read.supports, blockers=read.blockers, warnings=warnings,
        position={
            "margin": margin, "risk_pct": risk_pct,
            "risk_usd": round(risk_usd, 2), "qty": round(qty, 8),
            "notional": round(notional, 2), "leverage": round(lev, 2),
            "liquidation": round(liq, 8) if liq else None,
            "target_kinds": kinds,
        },
        context={
            "score": read.score,
            "other_side_score": reads["short" if long_ else "long"].score,
            "funding": dv.get("funding"),
            "ls_top": dv.get("ls_top"),
            "oi_1h": dv.get("oi_1h"),
            "perp_spot_ratio": dv.get("perp_spot_ratio"),
            "fng": dv.get("fng"),
            "adx": ind.get("adx"), "rsi14": ind.get("rsi14"),
        },
    ), ""


def pick_strategy(snap, side):
    pat = snap.get("patterns") or {}
    ind = snap.get("indicators") or {}
    sweep = str(pat.get("sweep") or "").lower()
    if (side == "long" and "low" in sweep) or \
       (side == "short" and "high" in sweep):
        return "Liquidity Sweep Reversal"
    d = pat.get("divergence")
    if isinstance(d, dict) and d.get("type"):
        return "Divergence Reversal"
    adx = num(ind.get("adx"), 0) or 0
    if adx >= 35:
        return "Trend Continuation"
    return "Confirmed Breakout Retest"


# ===========================================================================
# SCAN MANY
# ===========================================================================

def judge_all(snapshots, cfg=None, now_ts=None, events=None, model="balanced"):
    approved, rejected = [], []
    for s in snapshots:
        cand, why = build(s, cfg=cfg, now_ts=now_ts, events=events,
                          model=model)
        if cand:
            approved.append(cand)
        else:
            rejected.append({"symbol": s.get("symbol"), "tf": s.get("tf"),
                             "reason": why})
    approved.sort(key=lambda c: -c.confidence)
    return approved, rejected


def summarise(approved, rejected):
    """The denominator matters as much as the numerator - chapter 9.3."""
    from collections import Counter
    by_side = Counter(c.side for c in approved)
    reasons = Counter(r["reason"].split(" - ")[0].split(",")[0]
                      for r in rejected)
    return {
        "approved": len(approved),
        "rejected": len(rejected),
        "long": by_side.get("long", 0),
        "short": by_side.get("short", 0),
        "top_rejection_reasons": reasons.most_common(6),
    }


# ===========================================================================
# SELF TEST
# ===========================================================================

if __name__ == "__main__":
    # The exact market state the old engine called "NO EDGE":
    # ADX 71, below cloud, red future cloud, RSI 21, bearish structure.
    downtrend = {
        "symbol": "SOLUSDT", "tf": "5m", "ts": 0, "verified": True,
        "price": 100.01,
        "indicators": {"bias": "BEARISH", "ich_pos": "below cloud",
                       "future_cloud": "red", "adx": 71.25, "pdi": 12.0,
                       "mdi": 34.0, "rsi14": 20.92, "stochrsi": 54.66,
                       "atr": 0.1934},
        "levels": {"res": [101.52], "sup": [97.80]},
        "patterns": {"sweep": "", "divergence": None},
        "derivs": {"funding": -0.000026, "tape_delta": 27.0, "ls_top": 1.75,
                   "oi_1h": 0.28, "perp_spot_ratio": 12.0, "fng": 71},
        "htf": {"bias": "BEARISH"},
    }

    print("=" * 74)
    print("the snapshot the old engine rejected")
    print("=" * 74)
    for s in ("long", "short"):
        r = read_side(downtrend, s)
        print(f"\n  {s.upper():<6} score {r.score:+.0f}   viable {r.viable}")
        for x in r.supports:
            print(f"    +  {x}")
        for x in r.blockers:
            print(f"    -  {x}")
        for x in r.hard_blocks:
            print(f"    X  {x}")

    cand, why = build(downtrend, cfg={"margin": 2000, "risk_pct": 1.0})
    print()
    if cand:
        p = cand.position
        print(f"  -> {cand.side.upper()} {cand.symbol} {cand.tf}"
              f"   confidence {cand.confidence}")
        print(f"     entry {cand.entry:.4f}  ({cand.entry_distance_pct:.2f}%,"
              f" {cand.entry_distance_atr:.2f} ATR from market)")
        print(f"     stop  {cand.stop:.4f}   targets {cand.targets}")
        print(f"     R:R {cand.rr}   leverage out {p['leverage']}x"
              f"   risk ${p['risk_usd']}")
        print(f"     strategy {cand.strategy}")
        for w in cand.warnings:
            print(f"     ! {w}")
    else:
        print(f"  -> rejected: {why}")

    print()
    print("=" * 74)
    print("event gate")
    print("=" * 74)
    c2, w2 = build(downtrend, cfg={"margin": 2000},
                   events=[{"label": "FOMC", "days_away": 1,
                            "severity": "critical"}])
    print(f"  {'approved' if c2 else 'rejected'}: {w2}")

    print()
    print("=" * 74)
    print("reach gate - same setup, but ATR wide enough to push entry away")
    print("=" * 74)
    wide = json.loads(json.dumps(downtrend))
    wide["indicators"]["atr"] = 4.0
    c3, w3 = build(wide, cfg={"margin": 2000})
    print(f"  {'approved' if c3 else 'rejected'}: {w3}")
