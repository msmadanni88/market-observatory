#!/usr/bin/env python3
# ===========================================================================
# UNIVERSE SCANNER
# ---------------------------------------------------------------------------
# Scans every liquid USDT perpetual on Binance once per run and ranks the
# whole market instead of a hand-picked watchlist.
#
# Why this exists: between 27 Aug and 7 Sep 2026 the largest move in crypto
# was a privacy-coin rotation - ZEC ran 370% and reached rank 9 by market cap.
# The watchlist had ten symbols and none of them were privacy coins, so the
# rotation was invisible. A mechanical scan would have surfaced it on day one.
#
# Output:
#   <out>/universe_latest.json   machine readable, for the panel and Telegram
#   <out>/universe_latest.md     human readable digest
#   <out>/history/YYYY-MM-DD_HHMM.json  append-only archive
#
# Usage:
#   python universe_scan.py
#   python universe_scan.py --top 250 --out /var/lib/market/universe
#   python universe_scan.py --quote-min 30000000 --quiet
#
# Requires: requests
# ===========================================================================

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta

try:
    import requests
except ImportError:
    sys.exit("requests not installed.  pip install requests")


# ===========================================================================
# CONFIG
# ===========================================================================

FAPI = "https://fapi.binance.com"
TEHRAN = timezone(timedelta(hours=3, minutes=30))
HTTP_TIMEOUT = 15
HEADERS = {"User-Agent": "universe-scanner/1.0"}

# Minimum 24h quote volume in USDT for a symbol to be considered tradeable.
# Below this the order book is too thin for the entries this system produces.
DEFAULT_QUOTE_MIN = 25_000_000

# How many symbols to pull klines for, ordered by volume.
DEFAULT_TOP = 200

# Bars of daily history. 220 covers the 200-period EMA with headroom.
DAILY_BARS = 220

# Pause between kline calls. Binance allows 2400 weight/min on fapi;
# klines with limit<=100 cost 2, limit<=500 cost 5. 200 symbols x 5 = 1000.
SLEEP_BETWEEN = 0.05

# Sector tags. Used to detect rotation - the thing the watchlist missed.
SECTORS = {
    "privacy":    ["XMR", "ZEC", "DASH", "SCRT", "ROSE", "KEEP", "ARRR"],
    "l1_major":   ["BTC", "ETH", "SOL", "AVAX", "ADA", "DOT", "ATOM", "NEAR",
                   "APT", "SUI", "SEI", "TIA", "INJ"],
    "l2":         ["ARB", "OP", "MATIC", "STRK", "MANTA", "METIS", "ZK"],
    "defi":       ["UNI", "AAVE", "MKR", "CRV", "LDO", "PENDLE", "ENA", "ETHFI",
                   "COMP", "SNX", "DYDX"],
    "perp_dex":   ["HYPE", "DYDX", "GMX", "VRTX", "DRIFT"],
    "oracle_rwa": ["LINK", "PYTH", "ONDO", "CFG", "POLYX"],
    "meme":       ["DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI", "BOME"],
    "ai":         ["FET", "RNDR", "TAO", "AKT", "ARKM", "AI", "WLD"],
    "exchange":   ["BNB", "OKB", "CRO", "KCS", "GT"],
    "payments":   ["XRP", "XLM", "LTC", "BCH", "ALGO"],
}


# ===========================================================================
# HTTP
# ===========================================================================

def get(path, params=None, base=FAPI):
    r = requests.get(base + path, params=params, timeout=HTTP_TIMEOUT,
                     headers=HEADERS)
    r.raise_for_status()
    return r.json()


# ===========================================================================
# INDICATORS
# ---------------------------------------------------------------------------
# Deliberately self-contained so the scanner can run without watcher.py.
# ===========================================================================

def ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    out = sum(values[:period]) / period
    for v in values[period:]:
        out = v * k + out * (1.0 - k)
    return out


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = sum(gains) / period, sum(losses) / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))


def atr_pct(candles, period=14):
    """ATR as a percentage of price - comparable across symbols."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l = candles[i]["h"], candles[i]["l"]
        pc = candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    recent = trs[-period:]
    a = sum(recent) / len(recent)
    last = candles[-1]["c"]
    return (a / last * 100.0) if last else None


def pct_rank(value, series):
    """Where value sits in its own history, 0 to 100."""
    if not series:
        return None
    below = sum(1 for s in series if s < value)
    return below / len(series) * 100.0


# ===========================================================================
# FETCH
# ===========================================================================

def universe(quote_min, top):
    """Liquid USDT perps, ordered by 24h quote volume."""
    rows = get("/fapi/v1/ticker/24hr")
    out = []
    for r in rows:
        sym = r.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        # Skip leveraged / index products.
        if any(x in sym for x in ("UPUSDT", "DOWNUSDT", "BULL", "BEAR", "_")):
            continue
        try:
            qv = float(r.get("quoteVolume") or 0)
            px = float(r.get("lastPrice") or 0)
            chg = float(r.get("priceChangePercent") or 0)
        except (TypeError, ValueError):
            continue
        if qv < quote_min or px <= 0:
            continue
        out.append({"symbol": sym, "base": sym[:-4], "price": px,
                    "quote_vol_24h": qv, "chg_24h": chg})
    out.sort(key=lambda x: -x["quote_vol_24h"])
    return out[:top]


def daily_candles(symbol, limit=DAILY_BARS):
    raw = get("/fapi/v1/klines",
              {"symbol": symbol, "interval": "1d", "limit": limit})
    return [{"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4]), "v": float(k[5])}
            for k in raw]


def derivs(symbol):
    out = {"funding": None, "funding_annual": None, "oi_usd": None}
    try:
        d = get("/fapi/v1/premiumIndex", {"symbol": symbol})
        fr = float(d["lastFundingRate"])
        out["funding"] = fr
        out["funding_annual"] = fr * 3 * 365
        mark = float(d["markPrice"])
    except Exception:
        mark = None
    try:
        oi = float(get("/fapi/v1/openInterest",
                       {"symbol": symbol})["openInterest"])
        if mark:
            out["oi_usd"] = oi * mark
    except Exception:
        pass
    return out


# ===========================================================================
# SCORING
# ---------------------------------------------------------------------------
# Two independent scores. A symbol can rank high on both - that means it is
# moving hard and the direction is genuinely contested, which is worth
# knowing rather than hiding behind a single net number.
#
# Neither score is a signal. They rank where to look, not what to do.
# ===========================================================================

def analyse(row, candles, drv):
    closes = [c["c"] for c in candles]
    vols = [c["v"] for c in candles]
    px = row["price"]
    n = len(closes)
    if n < 60:
        return None

    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    e200 = ema(closes, 200) if n >= 200 else None
    r14 = rsi(closes, 14)
    a_pct = atr_pct(candles, 14)

    def dist(e):
        return ((px / e - 1.0) * 100.0) if e else None

    d20, d50, d200 = dist(e20), dist(e50), dist(e200)

    ret7 = (px / closes[-8] - 1.0) * 100.0 if n >= 8 else None
    ret30 = (px / closes[-31] - 1.0) * 100.0 if n >= 31 else None
    ret90 = (px / closes[-91] - 1.0) * 100.0 if n >= 91 else None

    vol20 = sum(vols[-21:-1]) / 20.0 if n >= 21 else None
    vol_surge = (vols[-1] / vol20) if vol20 else None

    hi60 = max(closes[-60:])
    lo60 = min(closes[-60:])
    span = hi60 - lo60
    pos60 = ((px - lo60) / span * 100.0) if span > 0 else 50.0

    atr_series = []
    for i in range(30, n):
        a = atr_pct(candles[:i + 1], 14)
        if a is not None:
            atr_series.append(a)
    atr_ptl = pct_rank(a_pct, atr_series) if (a_pct and atr_series) else None

    # ---------------- extension score: how stretched to the upside ---------
    ext = 0.0
    notes_ext = []
    if r14 is not None:
        if r14 >= 80:
            ext += 30; notes_ext.append(f"RSI {r14:.0f} extreme")
        elif r14 >= 72:
            ext += 20; notes_ext.append(f"RSI {r14:.0f} overbought")
        elif r14 >= 65:
            ext += 10
    if d20 is not None:
        if d20 >= 30:
            ext += 25; notes_ext.append(f"{d20:.0f}% above EMA20")
        elif d20 >= 18:
            ext += 15; notes_ext.append(f"{d20:.0f}% above EMA20")
        elif d20 >= 10:
            ext += 7
    if d50 is not None and d50 >= 45:
        ext += 15; notes_ext.append(f"{d50:.0f}% above EMA50")
    if ret7 is not None:
        if ret7 >= 40:
            ext += 20; notes_ext.append(f"+{ret7:.0f}% in 7d")
        elif ret7 >= 22:
            ext += 12; notes_ext.append(f"+{ret7:.0f}% in 7d")
    if pos60 >= 95:
        ext += 8
    # Declining volume into a high is the classic exhaustion tell.
    if vol_surge is not None and vol_surge < 0.7 and (ret7 or 0) > 15:
        ext += 10; notes_ext.append("volume fading into highs")
    if drv.get("funding") is not None and drv["funding"] > 0.0004:
        ext += 8; notes_ext.append("funding crowded long")

    # ---------------- compression score: coiled, not yet moved -------------
    comp = 0.0
    notes_comp = []
    if r14 is not None and 40 <= r14 <= 60:
        comp += 15; notes_comp.append(f"RSI {r14:.0f} neutral")
    if atr_ptl is not None and atr_ptl <= 25:
        comp += 25; notes_comp.append(f"volatility {atr_ptl:.0f}th pctile")
    elif atr_ptl is not None and atr_ptl <= 40:
        comp += 12
    if d20 is not None and abs(d20) <= 5:
        comp += 15; notes_comp.append("hugging EMA20")
    if 30 <= pos60 <= 70:
        comp += 10; notes_comp.append("mid-range")
    if e50 and e200 and e50 > e200 and px > e200:
        comp += 15; notes_comp.append("above rising 200")
    if vol_surge is not None and vol_surge >= 1.8:
        comp += 15; notes_comp.append(f"volume {vol_surge:.1f}x")

    # ---------------- momentum score: already trending, not yet extreme ----
    mom = 0.0
    notes_mom = []
    if ret30 is not None and ret30 > 0 and r14 is not None and r14 < 72:
        mom += 20; notes_mom.append(f"+{ret30:.0f}% 30d, RSI still {r14:.0f}")
    if e20 and e50 and e20 > e50:
        mom += 15
    if d200 is not None and 0 < d200 < 60:
        mom += 15; notes_mom.append("above 200 with room")
    if vol_surge is not None and vol_surge >= 1.3:
        mom += 10

    sector = None
    for name, bases in SECTORS.items():
        if row["base"] in bases:
            sector = name
            break

    return {
        **row,
        "sector": sector,
        "rsi14": round(r14, 1) if r14 is not None else None,
        "ema20_dist_pct": round(d20, 2) if d20 is not None else None,
        "ema50_dist_pct": round(d50, 2) if d50 is not None else None,
        "ema200_dist_pct": round(d200, 2) if d200 is not None else None,
        "ret_7d_pct": round(ret7, 2) if ret7 is not None else None,
        "ret_30d_pct": round(ret30, 2) if ret30 is not None else None,
        "ret_90d_pct": round(ret90, 2) if ret90 is not None else None,
        "atr_pct": round(a_pct, 2) if a_pct is not None else None,
        "atr_pctile": round(atr_ptl, 0) if atr_ptl is not None else None,
        "vol_surge": round(vol_surge, 2) if vol_surge is not None else None,
        "range_pos_60d": round(pos60, 0),
        "funding": drv.get("funding"),
        "funding_annual_pct": (round(drv["funding_annual"] * 100, 2)
                               if drv.get("funding_annual") is not None else None),
        "oi_usd": drv.get("oi_usd"),
        "score_extension": round(ext, 1),
        "score_compression": round(comp, 1),
        "score_momentum": round(mom, 1),
        "notes_extension": notes_ext,
        "notes_compression": notes_comp,
        "notes_momentum": notes_mom,
    }


# ===========================================================================
# SECTOR ROTATION
# ===========================================================================

def sector_table(rows):
    """Median 7d and 30d return per sector, plus the laggard inside each.

    The laggard column is the one that matters: in a hot sector the member
    with the lowest RSI has participated without going vertical, which is a
    different risk profile from the leader that already ran.
    """
    buckets = {}
    for r in rows:
        if not r.get("sector"):
            continue
        buckets.setdefault(r["sector"], []).append(r)

    out = []
    for name, members in buckets.items():
        r7 = sorted(m["ret_7d_pct"] for m in members
                    if m.get("ret_7d_pct") is not None)
        r30 = sorted(m["ret_30d_pct"] for m in members
                     if m.get("ret_30d_pct") is not None)
        if not r7:
            continue
        med7 = r7[len(r7) // 2]
        med30 = r30[len(r30) // 2] if r30 else None
        ranked = sorted(members, key=lambda m: m.get("ret_7d_pct") or -999)
        leader = ranked[-1]
        with_rsi = [m for m in members if m.get("rsi14") is not None]
        laggard = min(with_rsi, key=lambda m: m["rsi14"]) if with_rsi else None
        out.append({
            "sector": name,
            "members": len(members),
            "median_7d_pct": round(med7, 2),
            "median_30d_pct": round(med30, 2) if med30 is not None else None,
            "leader": leader["base"],
            "leader_7d_pct": leader.get("ret_7d_pct"),
            "leader_rsi": leader.get("rsi14"),
            "laggard": laggard["base"] if laggard else None,
            "laggard_rsi": laggard["rsi14"] if laggard else None,
            "laggard_7d_pct": laggard.get("ret_7d_pct") if laggard else None,
        })
    out.sort(key=lambda x: -x["median_7d_pct"])
    return out


# ===========================================================================
# RENDER
# ===========================================================================

def fmt_usd(v):
    if v is None:
        return "--"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"${v/div:,.2f}{unit}"
    return f"${v:,.0f}"


def render_md(payload):
    m = payload["meta"]
    L = []
    L.append("# Universe Scan")
    L.append("")
    L.append(f"- Generated: **{m['tehran']} Tehran**")
    L.append(f"- Scanned: {m['scanned']} of {m['universe']} liquid USDT perps")
    L.append(f"- Volume floor: {fmt_usd(m['quote_min'])} 24h")
    L.append("")
    L.append("Scores rank where to look. They are not signals and carry no "
             "direction of their own.")
    L.append("")

    L.append("## Sector rotation")
    L.append("")
    L.append("| Sector | n | 7d med | 30d med | Leader | RSI | Laggard | RSI |")
    L.append("|---|---:|---:|---:|---|---:|---|---:|")
    for s in payload["sectors"][:12]:
        L.append(
            f"| {s['sector']} | {s['members']} | {s['median_7d_pct']:+.1f}% "
            f"| {s['median_30d_pct']:+.1f}% "
            f"| {s['leader']} | {s['leader_rsi'] or '--'} "
            f"| {s['laggard'] or '--'} | {s['laggard_rsi'] or '--'} |")
    L.append("")
    L.append("A hot sector where the leader is above RSI 80 and a member is "
             "still near 60 is the single most useful row in this table.")
    L.append("")

    def block(title, key, rows, extra):
        L.append(f"## {title}")
        L.append("")
        for i, r in enumerate(rows, 1):
            notes = ", ".join(r[extra][:3]) or "--"
            L.append(
                f"**{i}. {r['base']}** - {r[key]:.0f} pts - "
                f"${r['price']:,.4g}")
            L.append(
                f"    RSI {r['rsi14']} | EMA20 {r['ema20_dist_pct']:+.1f}% | "
                f"7d {r['ret_7d_pct']:+.1f}% | 30d {r['ret_30d_pct']:+.1f}% | "
                f"vol {fmt_usd(r['quote_vol_24h'])}")
            L.append(f"    {notes}")
            L.append("")

    block("Most extended", "score_extension",
          payload["extended"], "notes_extension")
    block("Most compressed", "score_compression",
          payload["compressed"], "notes_compression")
    block("Trending with room", "score_momentum",
          payload["momentum"], "notes_momentum")

    L.append("---")
    L.append("")
    L.append("Generated automatically from public Binance futures data. "
             "Not trading advice.")
    return "\n".join(L)


# ===========================================================================
# MAIN
# ===========================================================================

def main():
    ap = argparse.ArgumentParser(description="Rank the whole perp market")
    ap.add_argument("--out", default="./universe")
    ap.add_argument("--top", type=int, default=DEFAULT_TOP)
    ap.add_argument("--quote-min", type=float, default=DEFAULT_QUOTE_MIN)
    ap.add_argument("--show", type=int, default=10,
                    help="rows per ranking block")
    ap.add_argument("--no-derivs", action="store_true",
                    help="skip funding and OI - roughly halves runtime")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    now_utc = datetime.now(timezone.utc)
    now_teh = now_utc.astimezone(TEHRAN)

    if not args.quiet:
        print("fetching universe ...", end=" ", flush=True)
    try:
        uni = universe(args.quote_min, args.top)
    except Exception as exc:
        sys.exit(f"universe fetch failed: {type(exc).__name__}: {exc}")
    if not args.quiet:
        print(f"{len(uni)} symbols above {fmt_usd(args.quote_min)}")

    rows, failed = [], []
    for i, row in enumerate(uni, 1):
        sym = row["symbol"]
        if not args.quiet:
            print(f"\r  [{i}/{len(uni)}] {sym:<14}", end="", flush=True)
        try:
            candles = daily_candles(sym)
            drv = {} if args.no_derivs else derivs(sym)
            a = analyse(row, candles, drv)
            if a:
                rows.append(a)
        except Exception as exc:
            failed.append({"symbol": sym, "error": f"{type(exc).__name__}"})
        time.sleep(SLEEP_BETWEEN)
    if not args.quiet:
        print()

    def topn(key):
        return sorted(rows, key=lambda r: -r[key])[:args.show]

    payload = {
        "meta": {
            "utc": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "tehran": now_teh.strftime("%Y-%m-%d %H:%M:%S"),
            "ts": now_utc.timestamp(),
            "universe": len(uni),
            "scanned": len(rows),
            "failed": len(failed),
            "quote_min": args.quote_min,
            "elapsed_sec": round(time.time() - t0, 1),
            "source": "binance-futures",
        },
        "sectors": sector_table(rows),
        "extended": topn("score_extension"),
        "compressed": topn("score_compression"),
        "momentum": topn("score_momentum"),
        "all": rows,
        "errors": failed,
    }

    os.makedirs(args.out, exist_ok=True)
    hist_dir = os.path.join(args.out, "history")
    os.makedirs(hist_dir, exist_ok=True)

    j = os.path.join(args.out, "universe_latest.json")
    with open(j, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)

    md = os.path.join(args.out, "universe_latest.md")
    with open(md, "w", encoding="utf-8") as fh:
        fh.write(render_md(payload))

    stamp = now_teh.strftime("%Y-%m-%d_%H%M")
    with open(os.path.join(hist_dir, stamp + ".json"), "w",
              encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, default=str)

    if not args.quiet:
        print("-" * 66)
        print(f"scanned {len(rows)} in {payload['meta']['elapsed_sec']}s"
              f"   failed {len(failed)}")
        print()
        print("hottest sectors")
        for s in payload["sectors"][:4]:
            print(f"  {s['sector']:<12} 7d {s['median_7d_pct']:+6.1f}%   "
                  f"leader {s['leader']} (RSI {s['leader_rsi']})   "
                  f"laggard {s['laggard']} (RSI {s['laggard_rsi']})")
        print()
        print("most extended")
        for r in payload["extended"][:5]:
            print(f"  {r['base']:<8} {r['score_extension']:5.0f}  "
                  f"RSI {r['rsi14']:<5}  EMA20 {r['ema20_dist_pct']:+6.1f}%  "
                  f"7d {r['ret_7d_pct']:+6.1f}%")
        print()
        print("most compressed")
        for r in payload["compressed"][:5]:
            print(f"  {r['base']:<8} {r['score_compression']:5.0f}  "
                  f"RSI {r['rsi14']:<5}  ATR pctile {r['atr_pctile']}")
        print()
        print(f"-> {md}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
