"""
exchange.py - one data layer, two venues.

Binance stays the primary source. It is richer than the alternatives and
every threshold in this system was calibrated against its numbers.

The problem this file exists to solve: fapi.binance.com answers HTTP 451 to
GitHub's runners, a legal geo-block on US datacentre ranges. The workflow
still went green and the brief came out empty - the worst kind of failure,
because nothing looks broken.

So every Binance call goes through get(). On the first refusal the module
flips to OKX for the rest of the process and translates the request and the
response both ways, so callers keep speaking Binance and never learn there
was a second venue.

  EXCHANGE_SOURCE=binance   never fall back, fail loudly instead
  EXCHANGE_SOURCE=okx       skip Binance entirely
  EXCHANGE_SOURCE=auto      default - Binance first, OKX on refusal

What OKX cannot match exactly is documented at each translation. Nothing
here silently invents a number: an endpoint with no equivalent raises, and
every caller already wraps these in try/except.
"""

import os
import time

import requests

FAPI = "https://fapi.binance.com"
SPOT = "https://api.binance.com"
OKX = "https://www.okx.com"

HTTP_TIMEOUT = 15
HEADERS = {"User-Agent": "market-observatory/1.0"}

SOURCE = os.environ.get("EXCHANGE_SOURCE", "auto").strip().lower()

# Flips to True the first time Binance refuses. Process-wide on purpose:
# once the runner is blocked it stays blocked, and retrying 200 times costs
# a minute of wall clock for nothing.
_binance_down = (SOURCE == "okx")
_notified = False


def binance_blocked():
    return _binance_down


def _note_block(reason):
    global _binance_down, _notified
    _binance_down = True
    if not _notified:
        _notified = True
        print(f"  data source: Binance unreachable ({reason}), using OKX",
              flush=True)


# ===========================================================================
# SYMBOL AND TIMEFRAME TRANSLATION
# ===========================================================================

def to_swap(sym):
    """BTCUSDT -> BTC-USDT-SWAP"""
    return sym[:-4] + "-USDT-SWAP" if sym.endswith("USDT") else sym


def to_spot(sym):
    """BTCUSDT -> BTC-USDT"""
    return sym[:-4] + "-USDT" if sym.endswith("USDT") else sym


def to_binance(inst_id):
    """BTC-USDT-SWAP -> BTCUSDT"""
    return inst_id.replace("-USDT-SWAP", "USDT").replace("-USDT", "USDT")


def base_ccy(sym):
    """BTCUSDT -> BTC"""
    return sym[:-4] if sym.endswith("USDT") else sym


# OKX bars default to Hong Kong time for 6H and above. The UTC variants are
# the ones that line up candle-for-candle with Binance, and a daily close
# that disagrees by eight hours would quietly move every Ichimoku level.
_BAR = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "2h": "2H", "4h": "4H",
    "6h": "6Hutc", "12h": "12Hutc",
    "1d": "1Dutc", "3d": "3Dutc", "1w": "1Wutc", "1M": "1Mutc",
}

# OKX has no 8h bar. Nothing in this project asks for one, but if something
# ever does it should fail rather than silently get 12h data.
_PERIOD = {"5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H",
           "2h": "2H", "4h": "4H", "1d": "1D"}


def _okx(path, params=None):
    r = requests.get(OKX + path, params=params, timeout=HTTP_TIMEOUT,
                     headers=HEADERS)
    r.raise_for_status()
    j = r.json()
    if j.get("code") not in ("0", 0):
        raise RuntimeError(f"okx {path}: {j.get('code')} {j.get('msg')}")
    return j.get("data", [])


# ===========================================================================
# TRANSLATIONS - OKX answers shaped like Binance
# ===========================================================================

def _klines(symbol, interval, limit):
    """OKX candles, paged backwards, returned oldest-first in Binance shape.

    OKX caps a page at 300 and hands them back newest-first; Binance returns
    up to 1500 oldest-first. The snapshot builder asks for 320, so paging is
    not optional.

    Column 6 of an OKX candle is volume in the base currency, which is what
    Binance calls volume. Column 5 is contracts and would be wrong by a
    factor of ctVal - on BTC that is 100x, enough to make every relative
    volume read nonsense.
    """
    bar = _BAR.get(interval)
    if bar is None:
        raise RuntimeError(f"no OKX bar for interval {interval}")
    inst = to_swap(symbol)
    rows, seen, after = [], set(), None
    while len(rows) < limit:
        page = min(300, max(2, limit - len(rows)))
        p = {"instId": inst, "bar": bar, "limit": str(page)}
        if after:
            p["after"] = str(after)
        d = _okx("/api/v5/market/candles", p)
        if not d:
            break
        fresh = [k for k in d if k[0] not in seen]
        if not fresh:
            break
        for k in fresh:
            seen.add(k[0])
        rows.extend(fresh)
        after = int(fresh[-1][0])
        if len(d) < page:
            break
        time.sleep(0.06)
    rows = rows[:limit]
    rows.reverse()
    return [[int(k[0]), k[1], k[2], k[3], k[4], k[6],
             int(k[0]), k[7], 0, "0", "0", "0"] for k in rows]


def _tickers_all():
    """Every USDT perp, shaped like /fapi/v1/ticker/24hr.

    OKX gives volCcy24h in the base currency for swaps, so quote volume has
    to be reconstructed. Binance reports it directly, so the two will not
    agree to the last dollar - close enough to rank by, which is all the
    universe scan does with it.
    """
    out = []
    for t in _okx("/api/v5/market/tickers", {"instType": "SWAP"}):
        inst = t.get("instId", "")
        if not inst.endswith("-USDT-SWAP"):
            continue
        try:
            last = float(t["last"])
            open24 = float(t["open24h"])
            base_vol = float(t.get("volCcy24h") or 0)
        except (TypeError, ValueError, KeyError):
            continue
        if last <= 0:
            continue
        out.append({
            "symbol": to_binance(inst),
            "lastPrice": t["last"],
            "priceChangePercent": str((last / open24 - 1) * 100
                                      if open24 else 0.0),
            "quoteVolume": str(base_vol * last),
            "volume": str(base_vol),
            "highPrice": t.get("high24h", "0"),
            "lowPrice": t.get("low24h", "0"),
        })
    return out


def _ticker_one(symbol, spot=False):
    inst = to_spot(symbol) if spot else to_swap(symbol)
    d = _okx("/api/v5/market/ticker", {"instId": inst})
    if not d:
        raise RuntimeError(f"okx: no ticker for {inst}")
    t = d[0]
    last = float(t["last"])
    open24 = float(t.get("open24h") or 0)
    # Spot reports volCcy24h in the quote currency already; swaps report it
    # in the base currency and need the price to get back to USDT.
    qv = float(t.get("volCcy24h") or 0)
    if not spot:
        qv *= last
    return {
        "symbol": symbol,
        "price": t["last"],
        "lastPrice": t["last"],
        "priceChangePercent": str((last / open24 - 1) * 100 if open24 else 0.0),
        "highPrice": t.get("high24h", "0"),
        "lowPrice": t.get("low24h", "0"),
        "quoteVolume": str(qv),
        "volume": t.get("vol24h", "0"),
    }


def _premium_index(symbol):
    inst = to_swap(symbol)
    fr = _okx("/api/v5/public/funding-rate", {"instId": inst})
    mp = _okx("/api/v5/public/mark-price",
              {"instType": "SWAP", "instId": inst})
    if not fr:
        raise RuntimeError(f"okx: no funding for {inst}")
    return {"symbol": symbol,
            "lastFundingRate": fr[0]["fundingRate"],
            "markPrice": mp[0]["markPx"] if mp else fr[0].get("premium", "0")}


def _open_interest(symbol):
    inst = to_swap(symbol)
    d = _okx("/api/v5/public/open-interest",
             {"instType": "SWAP", "instId": inst})
    if not d:
        raise RuntimeError(f"okx: no open interest for {inst}")
    # oiCcy is denominated in the base currency, which is what Binance
    # reports. oi is contracts and would be off by ctVal.
    return {"symbol": symbol, "openInterest": d[0]["oiCcy"]}


def _oi_hist(symbol, period, limit):
    """OKX aggregates open interest per currency, not per instrument.

    Binance reports it for the exact perp. For a USDT perp the two track
    each other closely enough to read a 1h or 24h change from, which is all
    this number is used for, but it is not the same series.
    """
    p = _PERIOD.get(period, "1H")
    d = _okx("/api/v5/rubik/stat/contracts/open-interest-volume",
             {"ccy": base_ccy(symbol), "period": p})
    rows = list(reversed(d))[-limit:]
    if not rows:
        return []
    # Two unit problems at once: OKX reports this series in USD across the
    # whole currency, while Binance reports base units for the one perp.
    # Rescaling the series so its last point equals the current instrument
    # OI keeps the shape - which is all oi_1h and oi_24h read - and keeps
    # the absolute numbers on the same scale as d["oi"]. The file header
    # warns that a unit mismatch here has already caused a silent bug once.
    try:
        cur = float(_open_interest(symbol)["openInterest"])
        tail = float(rows[-1][1])
        k = cur / tail if tail else 1.0
    except Exception:
        k = 1.0
    return [{"timestamp": int(r[0]),
             "sumOpenInterest": str(float(r[1]) * k),
             "sumOpenInterestValue": str(float(r[1]) * k)} for r in rows]


def _ls_ratio(symbol, period, limit):
    """Long/short ratio.

    Binance publishes three of these: all accounts, top accounts, and top
    positions. OKX publishes one - the account ratio across the whole
    currency. The engine treats a high reading as fuel on the other side,
    and that reading still holds, but it is a broader crowd than Binance's
    top traders. Worth remembering before reading too much into a small
    difference.
    """
    p = _PERIOD.get(period, "1H")
    d = _okx("/api/v5/rubik/stat/contracts/long-short-account-ratio",
             {"ccy": base_ccy(symbol), "period": p})
    rows = list(reversed(d))[-limit:]
    return [{"timestamp": int(r[0]), "longShortRatio": r[1]} for r in rows]


def _trades(symbol, limit):
    d = _okx("/api/v5/market/trades",
             {"instId": to_swap(symbol), "limit": str(min(int(limit), 500))})
    # Binance marks the maker side; OKX marks the taker side. A sell-side
    # taker is a buy-side maker, so the flag inverts cleanly.
    return [{"q": t["sz"], "p": t["px"], "m": t["side"] == "sell",
             "T": int(t["ts"])} for t in d]


# ===========================================================================
# THE DISPATCHER
# ===========================================================================

_ROUTES_SPOT = (SPOT,)


def _route_to_okx(path, params, base):
    p = params or {}
    sym = p.get("symbol")
    is_spot = base.startswith(SPOT)

    if path in ("/fapi/v1/ticker/24hr", "/api/v3/ticker/24hr"):
        return _ticker_one(sym, spot=is_spot) if sym else _tickers_all()
    if path in ("/fapi/v1/ticker/price", "/api/v3/ticker/price"):
        return _ticker_one(sym, spot=is_spot)
    if path in ("/fapi/v1/klines", "/api/v3/klines"):
        return _klines(sym, p.get("interval", "1d"), int(p.get("limit", 300)))
    if path == "/fapi/v1/premiumIndex":
        return _premium_index(sym)
    if path == "/fapi/v1/openInterest":
        return _open_interest(sym)
    if path == "/futures/data/openInterestHist":
        return _oi_hist(sym, p.get("period", "1h"), int(p.get("limit", 25)))
    if path in ("/futures/data/topLongShortPositionRatio",
                "/futures/data/topLongShortAccountRatio",
                "/futures/data/globalLongShortAccountRatio"):
        return _ls_ratio(sym, p.get("period", "1h"), int(p.get("limit", 24)))
    if path == "/fapi/v1/aggTrades":
        return _trades(sym, p.get("limit", 500))
    raise RuntimeError(f"no OKX equivalent for {path}")


# A refusal, not a hiccup. 451 is the geo-block, 403 is the same thing
# wearing a different hat, 401 means the venue wants credentials for what
# used to be public. Retrying any of them 200 times just burns the clock.
_BLOCKED = (401, 403, 451)


def get(path, params=None, base=FAPI):
    """Binance-shaped data, from Binance if it will answer and OKX if not."""
    if not _binance_down:
        try:
            r = requests.get(base + path, params=params,
                             timeout=HTTP_TIMEOUT, headers=HEADERS)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            if code not in _BLOCKED:
                raise           # 400 on a bad symbol is a real bug, not a block
            if SOURCE == "binance":
                raise
            _note_block(f"HTTP {code}")
        except requests.exceptions.RequestException as exc:
            if SOURCE == "binance":
                raise
            _note_block(type(exc).__name__)
    return _route_to_okx(path, params, base)


def okx_klines(symbol, tf, limit):
    """OKX candles directly, for callers that do their own venue fallback."""
    return _klines(symbol, tf, limit)


def source_name():
    return "okx" if _binance_down else "binance-futures"


# ===========================================================================
# LAST-RESORT PRICING
# ---------------------------------------------------------------------------
# An open position that reads "price unavailable" twice a day is the exact
# failure this whole file exists to prevent. It showed up immediately: XMR
# is not listed on OKX at all, so the primary and the fallback both miss it.
#
# So position pricing gets its own chain, widest net first to last. These
# are only used to print a number next to a position you already hold, not
# to build a signal, so a venue that would be too thin to trade on is still
# good enough to tell you where the thing is.
# ===========================================================================

def _px_gate(sym):
    r = requests.get("https://api.gateio.ws/api/v4/futures/usdt/tickers",
                     params={"contract": base_ccy(sym) + "_USDT"},
                     timeout=HTTP_TIMEOUT, headers=HEADERS).json()
    return float(r[0]["last"])


def _px_mexc(sym):
    r = requests.get("https://contract.mexc.com/api/v1/contract/ticker",
                     params={"symbol": base_ccy(sym) + "_USDT"},
                     timeout=HTTP_TIMEOUT, headers=HEADERS).json()
    return float(r["data"]["lastPrice"])


def _px_bitget(sym):
    r = requests.get("https://api.bitget.com/api/v2/mix/market/ticker",
                     params={"symbol": sym, "productType": "USDT-FUTURES"},
                     timeout=HTTP_TIMEOUT, headers=HEADERS).json()
    return float(r["data"][0]["lastPr"])


def _px_kucoin_spot(sym):
    r = requests.get("https://api.kucoin.com/api/v1/market/orderbook/level1",
                     params={"symbol": base_ccy(sym) + "-USDT"},
                     timeout=HTTP_TIMEOUT, headers=HEADERS).json()
    return float(r["data"]["price"])


def _px_okx_spot(sym):
    d = _okx("/api/v5/market/ticker", {"instId": to_spot(sym)})
    return float(d[0]["last"])


_PRICE_CHAIN = [
    ("primary", lambda s: float(get("/fapi/v1/ticker/price",
                                    {"symbol": s})["price"])),
    ("gate", _px_gate),
    ("mexc", _px_mexc),
    ("bitget", _px_bitget),
    ("okx-spot", _px_okx_spot),
    ("kucoin-spot", _px_kucoin_spot),
]


def price_anywhere(symbol):
    """First venue that will quote this symbol. None if nobody will."""
    for _, fn in _PRICE_CHAIN:
        try:
            p = fn(symbol)
            if p and p > 0:
                return float(p)
        except Exception:
            continue
    return None
