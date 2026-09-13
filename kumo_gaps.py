#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kumo_gaps.py -- find and measure Ichimoku cloud gaps across many symbols.

THE IDEA
--------
Senkou B is the midpoint of the highest high and lowest low over N bars.
That is a ROLLING WINDOW. When an old extreme drops out of the window, the
midpoint does not drift - it JUMPS. The same happens to Senkou A when Tenkan
uses a long period (50 or 100 instead of 9).

A jump leaves a horizontal band of price that the cloud never occupied.
That band is a "kumo gap": a zone the market crossed quickly and spent almost
no time in. It is the same underlying idea as a low-volume node in a volume
profile, or a fair value gap: unfinished business that price often revisits.

This tool finds those gaps mathematically instead of by eye, across any
number of symbols and timeframes, and tells you which ones are still open.

USAGE
-----
  python kumo_gaps.py                          scan the default watchlist
  python kumo_gaps.py --tenkan 100             use the 100 setting
  python kumo_gaps.py --symbols SOLUSDT HYPEUSDT LINKUSDT
  python kumo_gaps.py --tfs 4h 1d 1w
  python kumo_gaps.py --max-dist 8             only gaps within 8% of price
  python kumo_gaps.py --backtest               measure the historical hit rate
  python kumo_gaps.py --backtest --tfs 4h --bars 60

ICHIMOKU SETTINGS
-----------------
Default here is 50 / 26 / 52 / 26 (Tenkan / Kijun / SenkouB / displacement),
matching the non-standard setup that makes the gaps legible. --tenkan 100
switches to the other variant. Both are supported because the gaps they
produce are different, and comparing them is part of the method.

NOTE
----
A gap is a level, not a signal. The backtest exists so you can see the real
hit rate rather than remembering only the gaps that filled.
"""

import argparse
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ===========================================================================
# CONFIG
# ===========================================================================

HTTP_TIMEOUT = 20
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

BINANCE_F = "https://fapi.binance.com"
BYBIT = "https://api.bybit.com"

# 2h and above, as you use
DEFAULT_TFS = ["2h", "4h", "6h", "12h", "1d", "1w"]

DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "HYPEUSDT", "SUIUSDT", "TONUSDT",
    "LTCUSDT", "ARBUSDT", "OPUSDT", "APTUSDT", "NEARUSDT", "INJUSDT",
    "ATOMUSDT", "DOTUSDT", "UNIUSDT", "AAVEUSDT", "FILUSDT", "TIAUSDT",
]

BYBIT_IV = {"2h": "120", "4h": "240", "6h": "360", "12h": "720",
            "1d": "D", "1w": "W"}

TF_HOURS = {"2h": 2, "4h": 4, "6h": 6, "8h": 8, "12h": 12,
            "1d": 24, "3d": 72, "1w": 168}


# ===========================================================================
# DATA
# ===========================================================================

def http_get(url, params=None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def get_klines(symbol, tf, limit=600):
    r = http_get(f"{BINANCE_F}/fapi/v1/klines",
                 {"symbol": symbol, "interval": tf, "limit": limit})
    if isinstance(r, list) and len(r) > 60:
        return [{"t": int(k[0]) // 1000, "o": float(k[1]), "h": float(k[2]),
                 "l": float(k[3]), "c": float(k[4]), "v": float(k[5])} for k in r]
    iv = BYBIT_IV.get(tf)
    if iv:
        r = http_get(f"{BYBIT}/v5/market/kline",
                     {"category": "linear", "symbol": symbol,
                      "interval": iv, "limit": min(limit, 1000)})
        try:
            lst = r["result"]["list"]
        except (TypeError, KeyError):
            lst = []
        # Fall through to OKX rather than giving up - this used to return
        # None here, which is why a blocked Bybit killed the whole column.
        if lst and r.get("retCode") == 0 and len(lst) > 60:
            return [{"t": int(k[0]) // 1000, "o": float(k[1]), "h": float(k[2]),
                     "l": float(k[3]), "c": float(k[4]), "v": float(k[5])}
                    for k in reversed(lst)]
    # Third try. On GitHub's runners neither of the first two answers -
    # Binance returns 451 and Bybit's CDN blocks the country outright - so
    # without this the gap column is simply absent from every brief.
    try:
        import exchange
        raw = exchange.okx_klines(symbol, tf, limit)
    except Exception:
        return None
    if len(raw) > 60:
        return [{"t": int(k[0]) // 1000, "o": float(k[1]), "h": float(k[2]),
                 "l": float(k[3]), "c": float(k[4]), "v": float(k[5])}
                for k in raw]
    return None


# ===========================================================================
# ICHIMOKU + GAP DETECTION
# ===========================================================================

def _mid(c, length, i):
    """Midpoint of the highest high and lowest low over `length` bars ending at i.
    This rolling window is exactly what produces the jumps."""
    if i - length + 1 < 0:
        return None
    hi = -float("inf")
    lo = float("inf")
    for j in range(i - length + 1, i + 1):
        if c[j]["h"] > hi:
            hi = c[j]["h"]
        if c[j]["l"] < lo:
            lo = c[j]["l"]
    return (hi + lo) / 2.0


def cloud_series(c, tenkan_p=50, kijun_p=26, senkou_p=52):
    """Returns raw (undisplaced) spanA / spanB. Index i means: this is the cloud
    that will be PLOTTED at i + displacement."""
    n = len(c)
    A = [None] * n
    B = [None] * n
    # rolling extremes done incrementally would be faster, but n is small
    for i in range(n):
        t = _mid(c, tenkan_p, i)
        k = _mid(c, kijun_p, i)
        b = _mid(c, senkou_p, i)
        B[i] = b
        if t is not None and k is not None:
            A[i] = (t + k) / 2.0
    return A, B


def find_gaps(c, tenkan_p=50, kijun_p=26, senkou_p=52, disp=26,
              min_pct=0.35):
    """Detect discontinuities where consecutive cloud bands do not overlap.

    Returns a list of dicts:
      lo, hi      the price band the cloud skipped over
      dir         'up' if the cloud jumped up, 'down' if it jumped down
      i           raw index where the jump happened
      t           timestamp of that bar
      plot_t      timestamp where this cloud is drawn (i + disp)
      size_pct    band height as a percent of price
    """
    A, B = cloud_series(c, tenkan_p, kijun_p, senkou_p)
    n = len(c)
    gaps = []
    for i in range(1, n):
        a0, b0, a1, b1 = A[i - 1], B[i - 1], A[i], B[i]
        if None in (a0, b0, a1, b1):
            continue
        prev_lo, prev_hi = min(a0, b0), max(a0, b0)
        cur_lo, cur_hi = min(a1, b1), max(a1, b1)

        if cur_lo > prev_hi:                 # cloud jumped UP
            lo, hi, d = prev_hi, cur_lo, "up"
        elif cur_hi < prev_lo:               # cloud jumped DOWN
            lo, hi, d = cur_hi, prev_lo, "down"
        else:
            continue

        ref = c[i]["c"]
        size_pct = (hi - lo) / ref * 100 if ref else 0
        if size_pct < min_pct:               # ignore hairline jumps
            continue

        step = c[1]["t"] - c[0]["t"] if n > 1 else 0
        gaps.append({
            "lo": lo, "hi": hi, "mid": (lo + hi) / 2, "dir": d,
            "i": i, "t": c[i]["t"], "plot_t": c[i]["t"] + disp * step,
            "size_pct": size_pct,
        })
    return gaps


def gap_filled(c, gap, from_index=None):
    """Has price traded inside the gap band since it appeared?
    Returns (filled, bars_taken). The gap becomes visible at bar i, because the
    cloud is drawn ahead of price - so that is where we start looking."""
    start = from_index if from_index is not None else gap["i"]
    for j in range(start, len(c)):
        if c[j]["l"] <= gap["hi"] and c[j]["h"] >= gap["lo"]:
            return True, j - start
    return False, len(c) - start


def open_gaps(c, **kw):
    """Gaps that price has NOT revisited yet."""
    out = []
    for g in find_gaps(c, **kw):
        filled, bars = gap_filled(c, g)
        if not filled:
            g["age_bars"] = len(c) - g["i"]
            out.append(g)
    return out


# ===========================================================================
# SCANNER
# ===========================================================================

def scan(symbols, tfs, tenkan_p, kijun_p, senkou_p, disp, max_dist, min_pct,
         limit, quiet=False):
    rows = []
    total = len(symbols) * len(tfs)
    done = 0
    for sym in symbols:
        for tf in tfs:
            done += 1
            if not quiet:
                sys.stdout.write(f"\r  scanning {done}/{total}  {sym} {tf}      ")
                sys.stdout.flush()
            c = get_klines(sym, tf, limit)
            time.sleep(0.12)
            if not c:
                continue
            px = c[-1]["c"]
            gaps = open_gaps(c, tenkan_p=tenkan_p, kijun_p=kijun_p,
                             senkou_p=senkou_p, disp=disp, min_pct=min_pct)
            for g in gaps:
                if px < g["lo"]:
                    dist = (g["lo"] - px) / px * 100
                    side = "above"
                elif px > g["hi"]:
                    dist = (g["hi"] - px) / px * 100
                    side = "below"
                else:
                    dist, side = 0.0, "inside"
                if abs(dist) > max_dist:
                    continue
                rows.append({
                    "sym": sym, "tf": tf, "px": px, "lo": g["lo"], "hi": g["hi"],
                    "mid": g["mid"], "dir": g["dir"], "side": side,
                    "dist": dist, "size": g["size_pct"],
                    "age": g["age_bars"], "t": g["t"],
                })
    if not quiet:
        sys.stdout.write("\r" + " " * 60 + "\r")
    return rows


def fnum(v):
    if v is None:
        return "--"
    a = abs(v)
    d = 2 if a >= 1000 else 3 if a >= 10 else 4 if a >= 1 else 6
    return f"{v:,.{d}f}"


def print_scan(rows, tenkan_p):
    if not rows:
        print("\nNo open gaps found within the distance filter.\n")
        return
    rows.sort(key=lambda r: abs(r["dist"]))
    print(f"\n{'='*92}")
    print(f"  OPEN KUMO GAPS   (Tenkan {tenkan_p})   "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*92}")
    print(f"{'symbol':<11}{'tf':<5}{'price':>11}{'gap low':>11}{'gap high':>11}"
          f"{'width':>8}{'where':>8}{'distance':>10}{'age':>6}")
    print("-" * 92)
    for r in rows:
        print(f"{r['sym']:<11}{r['tf']:<5}{fnum(r['px']):>11}"
              f"{fnum(r['lo']):>11}{fnum(r['hi']):>11}"
              f"{r['size']:>7.2f}%{r['side']:>8}"
              f"{r['dist']:>+9.2f}%{r['age']:>6}")
    print("-" * 92)
    print("where  = gap sits above / below the current price, or price is inside it")
    print("age    = bars since the gap formed and stayed unfilled")
    print("A gap is a level, not a signal. Run --backtest to see the real hit rate.\n")


# ===========================================================================
# BACKTEST  -- the honest part
# ===========================================================================

def backtest(symbols, tfs, tenkan_p, kijun_p, senkou_p, disp, min_pct,
             limit, horizon, quiet=False):
    """For every historical gap, did price come back, and how fast?

    Only gaps old enough to have had a fair chance are counted, so a gap that
    formed two bars ago does not get scored as a failure.
    """
    stats = {}
    total = len(symbols) * len(tfs)
    done = 0
    for sym in symbols:
        for tf in tfs:
            done += 1
            if not quiet:
                sys.stdout.write(f"\r  testing {done}/{total}  {sym} {tf}      ")
                sys.stdout.flush()
            c = get_klines(sym, tf, limit)
            time.sleep(0.12)
            if not c:
                continue
            gaps = find_gaps(c, tenkan_p=tenkan_p, kijun_p=kijun_p,
                             senkou_p=senkou_p, disp=disp, min_pct=min_pct)
            s = stats.setdefault(tf, {"n": 0, "hit": 0, "bars": [],
                                      "sizes": [], "by_dir": {}})
            for g in gaps:
                # need `horizon` bars of future data to judge fairly
                if len(c) - g["i"] < horizon:
                    continue
                s["n"] += 1
                s["sizes"].append(g["size_pct"])
                filled, bars = gap_filled(c, g)
                d = s["by_dir"].setdefault(g["dir"], {"n": 0, "hit": 0})
                d["n"] += 1
                if filled and bars <= horizon:
                    s["hit"] += 1
                    s["bars"].append(bars)
                    d["hit"] += 1
    if not quiet:
        sys.stdout.write("\r" + " " * 60 + "\r")
    return stats


def print_backtest(stats, horizon, tenkan_p, nsym):
    print(f"\n{'='*80}")
    print(f"  KUMO GAP BACKTEST   Tenkan {tenkan_p}   "
          f"{nsym} symbols   horizon {horizon} bars")
    print(f"{'='*80}")
    if not stats:
        print("  No data.\n")
        return
    print(f"{'tf':<6}{'gaps':>7}{'filled':>8}{'hit rate':>10}"
          f"{'median bars':>13}{'mean bars':>11}{'avg width':>11}")
    print("-" * 80)
    grand_n = grand_h = 0
    for tf in sorted(stats, key=lambda x: TF_HOURS.get(x, 0)):
        s = stats[tf]
        if s["n"] == 0:
            continue
        grand_n += s["n"]
        grand_h += s["hit"]
        rate = s["hit"] / s["n"] * 100
        bars = sorted(s["bars"])
        med = bars[len(bars) // 2] if bars else 0
        mean = sum(bars) / len(bars) if bars else 0
        width = sum(s["sizes"]) / len(s["sizes"]) if s["sizes"] else 0
        print(f"{tf:<6}{s['n']:>7}{s['hit']:>8}{rate:>9.1f}%"
              f"{med:>13}{mean:>11.1f}{width:>10.2f}%")
    print("-" * 80)
    if grand_n:
        print(f"{'ALL':<6}{grand_n:>7}{grand_h:>8}{grand_h/grand_n*100:>9.1f}%")
    print()
    print("  How to read this:")
    print("  - A hit rate near 50% means the gap told you nothing.")
    print("  - What matters is hit rate AND how fast. A 90% rate that takes")
    print("    300 bars is useless for a leveraged trade.")
    print("  - Compare 'up' vs 'down' gaps below: if one side is much better,")
    print("    that is likely just the market's overall direction in this sample,")
    print("    not an edge. Re-run over a different period before believing it.")
    print()
    for tf in sorted(stats, key=lambda x: TF_HOURS.get(x, 0)):
        s = stats[tf]
        if not s["by_dir"]:
            continue
        parts = []
        for d, v in sorted(s["by_dir"].items()):
            if v["n"]:
                parts.append(f"{d} {v['hit']}/{v['n']} ({v['hit']/v['n']*100:.0f}%)")
        if parts:
            print(f"  {tf:<5} " + "   ".join(parts))
    print()


# ===========================================================================
# MAIN
# ===========================================================================

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    ap.add_argument("--tfs", nargs="+", default=DEFAULT_TFS)
    ap.add_argument("--tenkan", type=int, default=50,
                    help="Tenkan period: 50 or 100 (default 50)")
    ap.add_argument("--kijun", type=int, default=26)
    ap.add_argument("--senkou", type=int, default=52)
    ap.add_argument("--disp", type=int, default=26)
    ap.add_argument("--max-dist", type=float, default=10.0,
                    help="only show gaps within this %% of price")
    ap.add_argument("--min-width", type=float, default=0.35,
                    help="ignore gaps thinner than this %% of price")
    ap.add_argument("--limit", type=int, default=600, help="candles per symbol")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--bars", type=int, default=50,
                    help="backtest horizon in bars (default 50)")
    ap.add_argument("--both", action="store_true",
                    help="run both Tenkan 50 and 100 and compare")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    syms = [s.upper() for s in args.symbols]
    tenkans = [50, 100] if args.both else [args.tenkan]

    for tk in tenkans:
        if args.backtest:
            st = backtest(syms, args.tfs, tk, args.kijun, args.senkou,
                          args.disp, args.min_width, args.limit, args.bars,
                          args.quiet)
            print_backtest(st, args.bars, tk, len(syms))
        else:
            rows = scan(syms, args.tfs, tk, args.kijun, args.senkou, args.disp,
                        args.max_dist, args.min_width, args.limit, args.quiet)
            print_scan(rows, tk)


if __name__ == "__main__":
    main()
