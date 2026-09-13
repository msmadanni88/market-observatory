#!/usr/bin/env python3
# ===========================================================================
# VENUE ROUTER
# ---------------------------------------------------------------------------
# Every signal should answer three questions the chart cannot:
#   where do I execute this, what does it really cost, and can I even reach it
#
# Fee figures below are list rates for the lowest volume tier. They move.
# Verify against the venue before sizing anything that depends on them, and
# update VENUES when they change - the file is data, not logic.
#
# Access flags reflect published venue policy on restricted jurisdictions,
# not legal advice. Confirm your own situation.
# ===========================================================================

from dataclasses import dataclass, field, asdict


# ===========================================================================
# VENUE TABLE
# ===========================================================================

VENUES = {
    # ---- decentralised perps: no KYC, self custody, reachable anywhere ----
    "hyperliquid": {
        "name": "Hyperliquid",
        "kind": "perp_dex",
        "custody": "self",
        "kyc": False,
        "geo_restricted": False,
        "maker_bps": 1.5,
        "taker_bps": 4.5,
        "funding_interval_h": 1,
        "gas_per_trade_usd": 0.0,
        "withdraw_fee_usd": 1.0,
        "assets": ["crypto_perp"],
        "min_notional_usd": 10,
        "max_leverage": 50,
        "notes": "Hourly funding instead of 8h. Deep books on majors, thin "
                 "on long tail. Order book is fully onchain.",
    },
    "dydx": {
        "name": "dYdX v4",
        "kind": "perp_dex",
        "custody": "self",
        "kyc": False,
        "geo_restricted": False,
        "maker_bps": 2.0,
        "taker_bps": 5.0,
        "funding_interval_h": 1,
        "gas_per_trade_usd": 0.0,
        "withdraw_fee_usd": 0.5,
        "assets": ["crypto_perp"],
        "min_notional_usd": 10,
        "max_leverage": 20,
        "notes": "Own chain. Fewer markets than Hyperliquid.",
    },
    "gmx": {
        "name": "GMX v2",
        "kind": "perp_dex",
        "custody": "self",
        "kyc": False,
        "geo_restricted": False,
        "maker_bps": 5.0,
        "taker_bps": 7.0,
        "funding_interval_h": 1,
        "gas_per_trade_usd": 0.4,
        "withdraw_fee_usd": 0.4,
        "assets": ["crypto_perp"],
        "min_notional_usd": 10,
        "max_leverage": 50,
        "notes": "Pool based, no order book. Price impact is low but the "
                 "open and close fee is roughly double an order book venue.",
    },

    # ---- centralised perps: cheaper, but access depends on jurisdiction ---
    "binance": {
        "name": "Binance Futures",
        "kind": "perp_cex",
        "custody": "custodial",
        "kyc": True,
        "geo_restricted": True,
        "maker_bps": 2.0,
        "taker_bps": 5.0,
        "funding_interval_h": 8,
        "gas_per_trade_usd": 0.0,
        "withdraw_fee_usd": 1.0,
        "assets": ["crypto_perp", "crypto_spot"],
        "min_notional_usd": 5,
        "max_leverage": 125,
        "notes": "Deepest books and the reference price for this system. "
                 "Restricts a number of jurisdictions.",
    },
    "bybit": {
        "name": "Bybit",
        "kind": "perp_cex",
        "custody": "custodial",
        "kyc": True,
        "geo_restricted": True,
        "maker_bps": 2.0,
        "taker_bps": 5.5,
        "funding_interval_h": 8,
        "gas_per_trade_usd": 0.0,
        "withdraw_fee_usd": 1.0,
        "assets": ["crypto_perp", "crypto_spot"],
        "min_notional_usd": 5,
        "max_leverage": 100,
        "notes": "Good long tail coverage.",
    },
    "okx": {
        "name": "OKX",
        "kind": "perp_cex",
        "custody": "custodial",
        "kyc": True,
        "geo_restricted": True,
        "maker_bps": 2.0,
        "taker_bps": 5.0,
        "funding_interval_h": 8,
        "gas_per_trade_usd": 0.0,
        "withdraw_fee_usd": 1.0,
        "assets": ["crypto_perp", "crypto_spot"],
        "min_notional_usd": 5,
        "max_leverage": 100,
        "notes": "",
    },

    # ---- spot and self custody, for the hold book -------------------------
    "dex_swap": {
        "name": "DEX aggregator",
        "kind": "spot_dex",
        "custody": "self",
        "kyc": False,
        "geo_restricted": False,
        "maker_bps": 0.0,
        "taker_bps": 30.0,
        "funding_interval_h": None,
        "gas_per_trade_usd": 2.0,
        "withdraw_fee_usd": 0.0,
        "assets": ["crypto_spot"],
        "min_notional_usd": 50,
        "max_leverage": 1,
        "notes": "Trust Wallet swap routes here. Cost is the pool fee plus "
                 "gas plus slippage - expensive for small size, fine for a "
                 "position you intend to hold for months.",
    },
    "cex_spot": {
        "name": "CEX spot",
        "kind": "spot_cex",
        "custody": "custodial",
        "kyc": True,
        "geo_restricted": True,
        "maker_bps": 10.0,
        "taker_bps": 10.0,
        "funding_interval_h": None,
        "gas_per_trade_usd": 0.0,
        "withdraw_fee_usd": 2.0,
        "assets": ["crypto_spot"],
        "min_notional_usd": 5,
        "max_leverage": 1,
        "notes": "Cheapest spot execution if you can reach it, but the coin "
                 "sits with the venue until you withdraw.",
    },

    # ---- non crypto -------------------------------------------------------
    "metal_physical": {
        "name": "Physical metal",
        "kind": "commodity",
        "custody": "self",
        "kyc": False,
        "geo_restricted": False,
        "maker_bps": 0.0,
        "taker_bps": 200.0,
        "funding_interval_h": None,
        "gas_per_trade_usd": 0.0,
        "withdraw_fee_usd": 0.0,
        "assets": ["metal"],
        "min_notional_usd": 100,
        "max_leverage": 1,
        "notes": "Dealer spread on small bars runs 2 to 5 percent each way. "
                 "Only sane for a hold measured in years, never a trade.",
    },
    "tokenised_metal": {
        "name": "Tokenised metal",
        "kind": "commodity",
        "custody": "self",
        "kyc": False,
        "geo_restricted": False,
        "maker_bps": 0.0,
        "taker_bps": 30.0,
        "funding_interval_h": None,
        "gas_per_trade_usd": 2.0,
        "withdraw_fee_usd": 0.0,
        "assets": ["metal"],
        "min_notional_usd": 50,
        "max_leverage": 1,
        "notes": "PAXG and XAUT track spot gold and settle onchain. Carries "
                 "issuer risk that physical does not.",
    },
    "commodity_perp": {
        "name": "Commodity perp",
        "kind": "commodity_perp",
        "custody": "self",
        "kyc": False,
        "geo_restricted": False,
        "maker_bps": 1.5,
        "taker_bps": 4.5,
        "funding_interval_h": 1,
        "gas_per_trade_usd": 0.0,
        "withdraw_fee_usd": 1.0,
        "assets": ["metal", "energy", "index"],
        "min_notional_usd": 10,
        "max_leverage": 20,
        "notes": "Hyperliquid and a few others list XAU, WTI and index perps. "
                 "Thinner than crypto majors - check depth before sizing.",
    },
}


# Fallback funding per period when no live rate is passed in. Roughly the
# long-run average on a neutral market. Used only so long-horizon routing
# never reports funding as zero, which would flatter perps badly.
TYPICAL_FUNDING_PER_PERIOD = 0.0001


# Which asset classes each symbol family belongs to.
ASSET_CLASS = {
    "XAU": "metal", "XAG": "metal", "PAXG": "metal", "XAUT": "metal",
    "WTI": "energy", "BRENT": "energy", "NG": "energy",
    "SPX": "index", "NDX": "index", "DXY": "index",
}


# ===========================================================================
# COST MODEL
# ===========================================================================

@dataclass
class TradeCost:
    venue: str
    venue_name: str
    entry_fee_usd: float = 0.0
    exit_fee_usd: float = 0.0
    gas_usd: float = 0.0
    funding_usd: float = 0.0
    slippage_usd: float = 0.0
    total_usd: float = 0.0
    total_pct_of_margin: float = 0.0
    breakeven_move_pct: float = 0.0
    warnings: list = field(default_factory=list)


def square_root_impact(notional_usd, daily_volume_usd, daily_vol_pct,
                       y=1.0):
    """Slippage from the square-root law - chapter 5.2 of the knowledge base.

        I = Y * sigma * sqrt(phi)

    phi is order size over daily volume, sigma is daily volatility. Linear
    cost models understate large orders badly, which is the whole point of
    using this instead of a flat number.
    """
    if not daily_volume_usd or daily_volume_usd <= 0:
        return None
    phi = notional_usd / daily_volume_usd
    return y * (daily_vol_pct / 100.0) * (phi ** 0.5) * 100.0


def estimate_cost(venue_key, notional_usd, margin_usd, hold_hours=24.0,
                  funding_rate=0.0, taker_in=True, taker_out=True,
                  daily_volume_usd=None, daily_vol_pct=None):
    """Full round-trip cost for one position at one venue."""
    v = VENUES.get(venue_key)
    if not v:
        raise KeyError(f"unknown venue {venue_key!r}")

    c = TradeCost(venue=venue_key, venue_name=v["name"])

    in_bps = v["taker_bps"] if taker_in else v["maker_bps"]
    out_bps = v["taker_bps"] if taker_out else v["maker_bps"]
    c.entry_fee_usd = notional_usd * in_bps / 10000.0
    c.exit_fee_usd = notional_usd * out_bps / 10000.0
    c.gas_usd = v["gas_per_trade_usd"] * 2

    interval = v.get("funding_interval_h")
    if interval and funding_rate:
        periods = hold_hours / interval
        c.funding_usd = abs(notional_usd * funding_rate * periods)

    if daily_volume_usd and daily_vol_pct:
        slip_pct = square_root_impact(notional_usd, daily_volume_usd,
                                      daily_vol_pct)
        if slip_pct is not None:
            # Entry and exit both pay impact.
            c.slippage_usd = notional_usd * slip_pct / 100.0 * 2
            if slip_pct > 0.5:
                c.warnings.append(
                    f"impact {slip_pct:.2f}% per side - size is large "
                    f"relative to this book")

    c.total_usd = (c.entry_fee_usd + c.exit_fee_usd + c.gas_usd
                   + c.funding_usd + c.slippage_usd)
    if margin_usd > 0:
        c.total_pct_of_margin = c.total_usd / margin_usd * 100.0
    if notional_usd > 0:
        c.breakeven_move_pct = c.total_usd / notional_usd * 100.0

    if v["min_notional_usd"] > notional_usd:
        c.warnings.append(
            f"below venue minimum of ${v['min_notional_usd']}")
    if v["geo_restricted"]:
        c.warnings.append("venue restricts some jurisdictions - verify access")
    if c.breakeven_move_pct > 1.0:
        c.warnings.append(
            f"needs {c.breakeven_move_pct:.2f}% just to break even")

    return c


# ===========================================================================
# ROUTING
# ===========================================================================

def asset_class_of(base):
    return ASSET_CLASS.get(base.upper(), "crypto")


def route(base, style, notional_usd, margin_usd, hold_hours=24.0,
          funding_rate=0.0, daily_volume_usd=None, daily_vol_pct=None,
          allow_custodial=True, allow_kyc=True):
    """Rank venues for one idea and explain the ranking.

    style is "leverage" or "hold" - they route very differently. A hold does
    not want funding at all, and is happy to pay a wider one-off spread to
    avoid it.
    """
    cls = asset_class_of(base)

    if cls == "crypto":
        want = "crypto_perp" if style == "leverage" else "crypto_spot"
    elif cls in ("metal", "energy", "index"):
        want = cls
    else:
        want = "crypto_spot"

    out = []
    for key, v in VENUES.items():
        if want not in v["assets"]:
            continue
        if style == "leverage" and v["max_leverage"] < 2:
            continue
        if style == "hold" and v["kind"].endswith("_dex") and \
                v["max_leverage"] > 1 and want.endswith("spot") is False:
            pass
        if not allow_custodial and v["custody"] == "custodial":
            continue
        if not allow_kyc and v["kyc"]:
            continue

        # Funding is a property of the instrument, not of your intent. A
        # perp held for three months pays funding for three months no matter
        # what you call the position. Zeroing it for "holds" was a bug that
        # made perps look cheaper than spot on long horizons - the opposite
        # of the truth. When no live rate is supplied, fall back to a
        # conservative typical rate so the horizon cost never reads as zero.
        if v.get("funding_interval_h"):
            rate = funding_rate if funding_rate else TYPICAL_FUNDING_PER_PERIOD
        else:
            rate = 0.0

        cost = estimate_cost(
            key, notional_usd, margin_usd,
            hold_hours=hold_hours, funding_rate=rate,
            daily_volume_usd=daily_volume_usd, daily_vol_pct=daily_vol_pct)
        if style == "hold" and v.get("funding_interval_h"):
            days = hold_hours / 24.0
            if days >= 14:
                cost.warnings.append(
                    f"perpetual held {days:.0f} days - funding compounds "
                    f"and is the dominant cost, not the fee")

        row = asdict(cost)
        row["custody"] = v["custody"]
        row["kyc"] = v["kyc"]
        row["max_leverage"] = v["max_leverage"]
        row["notes"] = v["notes"]
        out.append(row)

    out.sort(key=lambda r: r["total_usd"])
    if out:
        best = out[0]
        best["recommended"] = True
        for r in out[1:]:
            r["recommended"] = False
            extra = r["total_usd"] - best["total_usd"]
            r["vs_best_usd"] = round(extra, 2)
    return out


def explain(base, style, routed, hold_hours=24.0):
    """One short paragraph a human can read under a signal."""
    if not routed:
        return "No venue in the table lists this asset."
    best = routed[0]
    parts = [
        f"Cheapest route: {best['venue_name']}. "
        f"Round trip about ${best['total_usd']:.2f}, which is "
        f"{best['breakeven_move_pct']:.2f}% of notional before this trade "
        f"breaks even."
    ]
    if best["funding_usd"] > 0.01:
        parts.append(
            f"Funding over {hold_hours:.0f}h adds ${best['funding_usd']:.2f} "
            f"- the longer it is held the worse that gets.")
    if len(routed) > 1:
        second = routed[1]
        parts.append(
            f"{second['venue_name']} costs ${second.get('vs_best_usd', 0):.2f} "
            f"more.")
    if best["warnings"]:
        parts.append("Watch: " + "; ".join(best["warnings"]) + ".")
    return " ".join(parts)


# ===========================================================================
# SELF TEST
# ===========================================================================

if __name__ == "__main__":
    print("=" * 74)
    print("leverage trade: 3000 notional on 1000 margin, XMR, held 48h")
    print("=" * 74)
    r = route("XMR", "leverage", 3000, 1000, hold_hours=48,
              funding_rate=0.0001, daily_volume_usd=180_000_000,
              daily_vol_pct=6.0, allow_kyc=False)
    for x in r:
        flag = "<-- best" if x["recommended"] else ""
        print(f"  {x['venue_name']:<20} ${x['total_usd']:>7.2f}  "
              f"breakeven {x['breakeven_move_pct']:.3f}%  {flag}")
    print()
    print("  " + explain("XMR", "leverage", r, 48))

    print()
    print("=" * 74)
    print("hold: 2000 of gold exposure, 3 months")
    print("=" * 74)
    r2 = route("XAU", "hold", 2000, 2000, hold_hours=24 * 90)
    for x in r2:
        flag = "<-- best" if x["recommended"] else ""
        print(f"  {x['venue_name']:<20} ${x['total_usd']:>7.2f}  "
              f"breakeven {x['breakeven_move_pct']:.3f}%  {flag}")
    print()
    print("  " + explain("XAU", "hold", r2, 24 * 90))

    print()
    print("=" * 74)
    print("size sensitivity - square root law on a thin book")
    print("=" * 74)
    for n in (1_000, 10_000, 100_000, 1_000_000):
        s = square_root_impact(n, 20_000_000, 8.0)
        print(f"  ${n:>9,} notional on $20M daily volume -> "
              f"{s:.3f}% impact per side")
