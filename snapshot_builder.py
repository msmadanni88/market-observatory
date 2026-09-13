#!/usr/bin/env python3
# ===========================================================================
# SNAPSHOT BUILDER
# ---------------------------------------------------------------------------
# Produces the exact snapshot shape signal_engine.py expects, straight from
# public exchange endpoints. This is what lets the whole pipeline run on a
# scheduled runner with nothing installed locally - the desktop watcher is
# no longer in the critical path.
#
# Output matches the live.json schema the watcher already writes, so both
# producers feed the same consumer:
#
#   {symbol, tf, ts, price, verified, quotes, spread_pct,
#    indicators{...}, levels{...}, patterns{...}, derivs{...}, htf{...}}
#
# UNITS, because one of these already caused a silent bug:
#   funding is stored as a PERCENT - 0.01 means 0.01%, matching the watcher
#   oi is in base units, oi_1h and oi_24h are percent changes
# ===========================================================================

import math
import statistics
import time

import requests

import exchange

FAPI = "https://fapi.binance.com"
HTTP_TIMEOUT = 15
HEADERS = {"User-Agent": "snapshot-builder/1.0"}

# Which higher timeframe confirms which. A read that exists on only one
# timeframe is an artifact, so every snapshot carries its parent's bias.
HTF_OF = {"5m": "1h", "15m": "1h", "30m": "4h", "1h": "4h",
          "2h": "12h", "4h": "1d", "6h": "1d", "12h": "1d",
          "1d": "1w", "1w": "1M"}


def get(path, params=None, base=FAPI):
    # Binance first, OKX when Binance refuses. See exchange.py.
    return exchange.get(path, params, base)


def klines(symbol, tf, limit=320):
    raw = get("/fapi/v1/klines",
              {"symbol": symbol, "interval": tf, "limit": limit})
    return [{"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4]), "v": float(k[5])}
            for k in raw]


# ===========================================================================
# CROSS-VENUE PRICE CHECK
# ---------------------------------------------------------------------------
# A snapshot that is not verified across venues never becomes a signal.
# One venue printing a wick nobody else saw is how a stop gets hunted on
# data that did not exist in the real market.
# ===========================================================================

# These deliberately do NOT go through exchange.get. A venue check is only
# worth anything if each quote comes from a different book - routing a
# failed Binance call to OKX would count OKX twice and call it agreement.
# A venue that will not answer simply drops out of the list.
VENUE_TICKS = {
    "binance": lambda s: float(requests.get(
        "https://fapi.binance.com/fapi/v1/ticker/price", params={"symbol": s},
        timeout=HTTP_TIMEOUT, headers=HEADERS).json()["price"]),
    "binance-spot": lambda s: float(requests.get(
        "https://api.binance.com/api/v3/ticker/price", params={"symbol": s},
        timeout=HTTP_TIMEOUT, headers=HEADERS).json()["price"]),
    "bybit": lambda s: float(requests.get(
        "https://api.bybit.com/v5/market/tickers",
        params={"category": "linear", "symbol": s},
        timeout=HTTP_TIMEOUT, headers=HEADERS
    ).json()["result"]["list"][0]["lastPrice"]),
    "okx": lambda s: float(requests.get(
        "https://www.okx.com/api/v5/market/ticker",
        params={"instId": s[:-4] + "-USDT-SWAP"},
        timeout=HTTP_TIMEOUT, headers=HEADERS).json()["data"][0]["last"]),
    "gate": lambda s: float(requests.get(
        "https://api.gateio.ws/api/v4/futures/usdt/tickers",
        params={"contract": s[:-4] + "_USDT"},
        timeout=HTTP_TIMEOUT, headers=HEADERS).json()[0]["last"]),
    # Added after the GitHub runner turned out to reach neither Binance nor
    # Bybit. Without these, two of the five venues were all that answered
    # from there, and min_venues is two - one timeout and a snapshot goes
    # unverified for no good reason.
    "bitget": lambda s: float(requests.get(
        "https://api.bitget.com/api/v2/mix/market/ticker",
        params={"symbol": s, "productType": "USDT-FUTURES"},
        timeout=HTTP_TIMEOUT, headers=HEADERS).json()["data"][0]["lastPr"]),
    "mexc": lambda s: float(requests.get(
        "https://contract.mexc.com/api/v1/contract/ticker",
        params={"symbol": s[:-4] + "_USDT"},
        timeout=HTTP_TIMEOUT, headers=HEADERS).json()["data"]["lastPrice"]),
}

MAX_SPREAD_PCT = 0.60


def verify_price(symbol, min_venues=2):
    quotes, errors = {}, {}
    for name, fn in VENUE_TICKS.items():
        try:
            p = fn(symbol)
            if p > 0:
                quotes[name] = p
        except Exception as exc:
            errors[name] = type(exc).__name__
    if len(quotes) < min_venues:
        return {"price": None, "verified": False, "quotes": quotes,
                "spread_pct": None, "errors": errors,
                "verify_text": f"only {len(quotes)} venue(s) answered"}
    vals = list(quotes.values())
    med = statistics.median(vals)
    spread = (max(vals) - min(vals)) / med * 100.0 if med else None
    ok = spread is not None and spread <= MAX_SPREAD_PCT
    return {"price": med, "verified": ok, "quotes": quotes,
            "spread_pct": round(spread, 4) if spread is not None else None,
            "errors": errors,
            "verify_text": (f"verified across {len(quotes)} venues, "
                            f"spread {spread:.3f}%") if ok else
                           f"spread {spread:.3f}% exceeds {MAX_SPREAD_PCT}%"}


# ===========================================================================
# INDICATORS
# ===========================================================================

def ema(vals, p):
    if len(vals) < p:
        return None
    k = 2.0 / (p + 1)
    out = sum(vals[:p]) / p
    for v in vals[p:]:
        out = v * k + out * (1 - k)
    return out


def rsi(cl, p=14):
    if len(cl) < p + 1:
        return None
    g = l = 0.0
    for i in range(1, p + 1):
        d = cl[i] - cl[i - 1]
        g += max(d, 0.0); l += max(-d, 0.0)
    ag, al = g / p, l / p
    for i in range(p + 1, len(cl)):
        d = cl[i] - cl[i - 1]
        ag = (ag * (p - 1) + max(d, 0.0)) / p
        al = (al * (p - 1) + max(-d, 0.0)) / p
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1 + ag / al)


def stoch_rsi(cl, p=14, look=14):
    series = []
    for i in range(len(cl) - look, len(cl) + 1):
        if i < p + 1:
            continue
        v = rsi(cl[:i], p)
        if v is not None:
            series.append(v)
    if len(series) < 2:
        return None
    lo, hi = min(series), max(series)
    if hi == lo:
        return 50.0
    return (series[-1] - lo) / (hi - lo) * 100.0


def true_ranges(c):
    return [max(c[i]["h"] - c[i]["l"],
                abs(c[i]["h"] - c[i - 1]["c"]),
                abs(c[i]["l"] - c[i - 1]["c"]))
            for i in range(1, len(c))]


def atr(c, p=14):
    tr = true_ranges(c)
    if len(tr) < p:
        return None
    a = sum(tr[:p]) / p
    for x in tr[p:]:
        a = (a * (p - 1) + x) / p
    return a


def adx(c, p=14):
    """Wilder's ADX with +DI and -DI."""
    if len(c) < p * 2 + 2:
        return None, None, None
    plus, minus, tr = [], [], []
    for i in range(1, len(c)):
        up = c[i]["h"] - c[i - 1]["h"]
        dn = c[i - 1]["l"] - c[i]["l"]
        plus.append(up if (up > dn and up > 0) else 0.0)
        minus.append(dn if (dn > up and dn > 0) else 0.0)
        tr.append(max(c[i]["h"] - c[i]["l"],
                      abs(c[i]["h"] - c[i - 1]["c"]),
                      abs(c[i]["l"] - c[i - 1]["c"])))

    def smooth(xs):
        s = sum(xs[:p]); out = [s]
        for x in xs[p:]:
            s = s - s / p + x
            out.append(s)
        return out

    st, sp, sm = smooth(tr), smooth(plus), smooth(minus)
    dx = []
    for i in range(len(st)):
        if st[i] == 0:
            continue
        pdi = sp[i] / st[i] * 100.0
        mdi = sm[i] / st[i] * 100.0
        denom = pdi + mdi
        if denom:
            dx.append(abs(pdi - mdi) / denom * 100.0)
    if len(dx) < p:
        return None, None, None
    a = sum(dx[:p]) / p
    for x in dx[p:]:
        a = (a * (p - 1) + x) / p
    pdi = sp[-1] / st[-1] * 100.0 if st[-1] else None
    mdi = sm[-1] / st[-1] * 100.0 if st[-1] else None
    return a, pdi, mdi


def ichimoku(c, tenkan=9, kijun=26, senkou=52, disp=26):
    """Returns cloud position now, and the colour of the cloud ahead."""
    def mid(n, at):
        w = c[max(0, at - n + 1): at + 1]
        if len(w) < n:
            return None
        return (max(x["h"] for x in w) + min(x["l"] for x in w)) / 2.0

    last = len(c) - 1
    T, K = mid(tenkan, last), mid(kijun, last)
    if T is None or K is None:
        return {}
    # The cloud plotted at "now" was formed `disp` bars ago.
    back = last - disp
    if back < senkou:
        return {}
    A_now = (mid(tenkan, back) + mid(kijun, back)) / 2.0
    B_now = mid(senkou, back)
    top, bot = max(A_now, B_now), min(A_now, B_now)
    px = c[-1]["c"]
    pos = "above cloud" if px > top else "below cloud" if px < bot else "inside cloud"
    # The cloud ahead is built from today's midpoints.
    A_f = (T + K) / 2.0
    B_f = mid(senkou, last)
    fut = "green" if (B_f is not None and A_f > B_f) else "red"
    return {"tenkan": T, "kijun": K, "cloud_top": top, "cloud_bot": bot,
            "ich_pos": pos, "future_cloud": fut}


def swing_levels(c, k=5, look=180):
    """Nearest untested swing high above and swing low below."""
    seg = c[-look:] if len(c) > look else c
    px = c[-1]["c"]
    highs, lows = [], []
    for i in range(k, len(seg) - k):
        w = seg[i - k: i + k + 1]
        if seg[i]["h"] == max(x["h"] for x in w):
            highs.append(seg[i]["h"])
        if seg[i]["l"] == min(x["l"] for x in w):
            lows.append(seg[i]["l"])
    res = sorted([h for h in highs if h > px])
    sup = sorted([l for l in lows if l < px], reverse=True)
    return res[:3], sup[:3]


def structure(c, k=3, look=120):
    """Higher highs and higher lows, or the reverse."""
    seg = c[-look:] if len(c) > look else c
    hi, lo = [], []
    for i in range(k, len(seg) - k):
        w = seg[i - k: i + k + 1]
        if seg[i]["h"] == max(x["h"] for x in w):
            hi.append(seg[i]["h"])
        if seg[i]["l"] == min(x["l"] for x in w):
            lo.append(seg[i]["l"])
    if len(hi) >= 2 and len(lo) >= 2:
        if hi[-1] > hi[-2] and lo[-1] > lo[-2]:
            return "uptrend"
        if hi[-1] < hi[-2] and lo[-1] < lo[-2]:
            return "downtrend"
    return "ranging"


def sweep(c, k=3, look=90, back=6):
    """Did the last few bars take out a prior extreme then close back inside?"""
    seg = c[-look:] if len(c) > look else c
    if len(seg) < k * 2 + back + 2:
        return ""
    body = seg[:-back]
    prior_hi = max(x["h"] for x in body)
    prior_lo = min(x["l"] for x in body)
    recent = seg[-back:]
    took_hi = any(x["h"] > prior_hi for x in recent)
    took_lo = any(x["l"] < prior_lo for x in recent)
    close = seg[-1]["c"]
    if took_hi and close < prior_hi:
        return "swept highs"
    if took_lo and close > prior_lo:
        return "swept lows"
    return ""


def divergence(c, p=14, k=5, look=120):
    """Price extreme vs RSI extreme, on two separated pivots."""
    seg = c[-look:] if len(c) > look else c
    cl = [x["c"] for x in seg]
    if len(cl) < p + k * 2 + 10:
        return None
    rs = []
    for i in range(p + 1, len(cl) + 1):
        rs.append(rsi(cl[:i], p))
    off = len(cl) - len(rs)
    piv_hi, piv_lo = [], []
    for i in range(k, len(seg) - k):
        w = seg[i - k: i + k + 1]
        ri = i - off
        if ri < 0 or ri >= len(rs):
            continue
        if seg[i]["h"] == max(x["h"] for x in w):
            piv_hi.append((i, seg[i]["h"], rs[ri]))
        if seg[i]["l"] == min(x["l"] for x in w):
            piv_lo.append((i, seg[i]["l"], rs[ri]))
    if len(piv_hi) >= 2:
        a, b = piv_hi[-2], piv_hi[-1]
        if b[0] - a[0] >= 8 and b[1] > a[1] and b[2] < a[2]:
            return {"type": "bearish", "sep": b[0] - a[0]}
    if len(piv_lo) >= 2:
        a, b = piv_lo[-2], piv_lo[-1]
        if b[0] - a[0] >= 8 and b[1] < a[1] and b[2] > a[2]:
            return {"type": "bullish", "sep": b[0] - a[0]}
    return None


# ===========================================================================
# DERIVATIVES
# ===========================================================================

def derivs(symbol):
    d = {}
    mark = None
    try:
        pi = get("/fapi/v1/premiumIndex", {"symbol": symbol})
        # Stored as percent to match the watcher schema.
        d["funding"] = round(float(pi["lastFundingRate"]) * 100, 6)
        d["fund_apr"] = round(d["funding"] * 3 * 365, 3)
        mark = float(pi["markPrice"])
        d["mark"] = mark
    except Exception:
        pass
    try:
        oi = float(get("/fapi/v1/openInterest", {"symbol": symbol})["openInterest"])
        d["oi"] = oi
        hist = get("/futures/data/openInterestHist",
                   {"symbol": symbol, "period": "1h", "limit": 25})
        if hist:
            vals = [float(h["sumOpenInterest"]) for h in hist]
            if len(vals) >= 2 and vals[-2]:
                d["oi_1h"] = round((vals[-1] / vals[-2] - 1) * 100, 3)
            if vals[0]:
                d["oi_24h"] = round((vals[-1] / vals[0] - 1) * 100, 3)
            d["oi_peak24"] = max(vals)
    except Exception:
        pass
    try:
        ls = get("/futures/data/topLongShortPositionRatio",
                 {"symbol": symbol, "period": "1h", "limit": 24})
        if ls:
            d["ls_top"] = round(float(ls[-1]["longShortRatio"]), 4)
            d["ls_top_24h"] = round(float(ls[0]["longShortRatio"]), 4)
    except Exception:
        pass
    try:
        gl = get("/futures/data/globalLongShortAccountRatio",
                 {"symbol": symbol, "period": "1h", "limit": 2})
        if gl:
            d["ls_retail"] = round(float(gl[-1]["longShortRatio"]), 4)
    except Exception:
        pass
    try:
        t24 = get("/fapi/v1/ticker/24hr", {"symbol": symbol})
        d["chg24"] = float(t24["priceChangePercent"])
        d["high24"] = float(t24["highPrice"])
        d["low24"] = float(t24["lowPrice"])
        perp_q = float(t24["quoteVolume"])
        d["vol_perp"] = perp_q
        s24 = get("/api/v3/ticker/24hr", {"symbol": symbol},
                  base="https://api.binance.com")
        spot_q = float(s24["quoteVolume"])
        d["vol_spot"] = spot_q
        d["spot"] = float(s24["lastPrice"])
        if spot_q > 0:
            # High ratio means the move is leverage driven with a thin spot
            # bid underneath. That distinction is what the unlock thesis
            # missed, so it is collected deliberately.
            d["perp_spot_ratio"] = round(perp_q / spot_q, 3)
        if mark and d.get("spot"):
            d["basis"] = round((mark / d["spot"] - 1) * 100, 4)
    except Exception:
        pass
    try:
        trades = get("/fapi/v1/aggTrades", {"symbol": symbol, "limit": 1000})
        buy = sum(float(t["q"]) for t in trades if not t["m"])
        sell = sum(float(t["q"]) for t in trades if t["m"])
        tot = buy + sell
        if tot > 0:
            d["tape_delta"] = round((buy - sell) / tot * 100, 2)
            d["tape_buy"] = buy
            d["tape_sell"] = sell
    except Exception:
        pass
    return d


# ===========================================================================
# BUILD ONE SNAPSHOT
# ===========================================================================

def build_snapshot(symbol, tf, verify=True, with_derivs=True, htf_bias=None):
    c = klines(symbol, tf, 320)
    if len(c) < 60:
        return None
    cl = [x["c"] for x in c]
    px_close = cl[-1]

    v = (verify_price(symbol) if verify
         else {"price": px_close, "verified": True, "quotes": {},
               "spread_pct": 0.0, "verify_text": "verification skipped"})
    price = v["price"] or px_close

    a = atr(c, 14)
    adx_v, pdi, mdi = adx(c, 14)
    ich = ichimoku(c)
    res, sup = swing_levels(c)

    e7, e25, e99 = ema(cl, 7), ema(cl, 25), ema(cl, 99)
    e200 = ema(cl, 200) if len(cl) >= 200 else None
    bias = ("BULLISH" if (e7 and e25 and e7 > e25 and price > e25)
            else "BEARISH" if (e7 and e25 and e7 < e25 and price < e25)
            else "NEUTRAL")

    vols = [x["v"] for x in c]
    rel_vol = (vols[-1] / (sum(vols[-21:-1]) / 20.0)
               if len(vols) >= 21 and sum(vols[-21:-1]) > 0 else None)

    # Higher timeframe bias, fetched once and passed down when batching.
    if htf_bias is None:
        parent = HTF_OF.get(tf)
        if parent:
            try:
                hc = klines(symbol, parent, 150)
                hcl = [x["c"] for x in hc]
                h7, h25 = ema(hcl, 7), ema(hcl, 25)
                htf_bias = ("BULLISH" if h7 and h25 and h7 > h25
                            else "BEARISH" if h7 and h25 and h7 < h25
                            else "NEUTRAL")
            except Exception:
                htf_bias = None

    return {
        "symbol": symbol, "tf": tf, "ts": time.time(),
        "source": exchange.source_name(),
        "price": price,
        "verified": v["verified"],
        "verify_text": v["verify_text"],
        "quotes": v["quotes"],
        "spread_pct": v["spread_pct"],
        "indicators": {
            "bias": bias,
            "ich_pos": ich.get("ich_pos"),
            "future_cloud": ich.get("future_cloud"),
            "tenkan": ich.get("tenkan"), "kijun": ich.get("kijun"),
            "cloud_top": ich.get("cloud_top"), "cloud_bot": ich.get("cloud_bot"),
            "adx": adx_v, "pdi": pdi, "mdi": mdi,
            "rsi14": rsi(cl, 14), "rsi6": rsi(cl, 6),
            "stochrsi": stoch_rsi(cl),
            "atr": a,
            "atr_pct": (a / price * 100.0) if a and price else None,
            "rel_vol": rel_vol,
            "ema7": e7, "ema25": e25, "ema99": e99, "ema200": e200,
        },
        "levels": {
            "res": res, "sup": sup,
            "hi20": max(x["h"] for x in c[-20:]),
            "lo20": min(x["l"] for x in c[-20:]),
        },
        "patterns": {
            "structure": structure(c),
            "sweep": sweep(c),
            "divergence": divergence(c),
        },
        "derivs": derivs(symbol) if with_derivs else {},
        "htf": {"bias": htf_bias} if htf_bias else {},
    }


def build_many(symbols, timeframes, verify=True, quiet=True, pause=0.12):
    """One HTF fetch per symbol, reused across its timeframes."""
    out = []
    for sym in symbols:
        htf_cache = {}
        for tf in timeframes:
            parent = HTF_OF.get(tf)
            if parent and parent not in htf_cache:
                try:
                    hc = klines(sym, parent, 150)
                    hcl = [x["c"] for x in hc]
                    h7, h25 = ema(hcl, 7), ema(hcl, 25)
                    htf_cache[parent] = ("BULLISH" if h7 and h25 and h7 > h25
                                         else "BEARISH" if h7 and h25 and h7 < h25
                                         else "NEUTRAL")
                except Exception:
                    htf_cache[parent] = None
                time.sleep(pause)
            try:
                s = build_snapshot(sym, tf, verify=verify,
                                   htf_bias=htf_cache.get(parent))
                if s:
                    out.append(s)
                    if not quiet:
                        print(f"    {sym} {tf}  {s['price']}  "
                              f"{'verified' if s['verified'] else 'UNVERIFIED'}")
            except Exception as exc:
                if not quiet:
                    print(f"    {sym} {tf}  failed: {type(exc).__name__}")
            time.sleep(pause)
    return out


if __name__ == "__main__":
    import json
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "SOLUSDT"
    tf = sys.argv[2] if len(sys.argv) > 2 else "4h"
    s = build_snapshot(sym, tf)
    print(json.dumps(s, indent=2, default=str))
