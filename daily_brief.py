#!/usr/bin/env python3
# ===========================================================================
# DAILY BRIEF
# ---------------------------------------------------------------------------
# One scheduled run produces the whole daily output:
#
#   1. universe scan            rank every liquid perp, detect sector rotation
#   2. position monitor         re-price open trades, flag level breaches
#   3. event calendar           block setups that must cross a binary event
#   4. judgment layer           compact digest -> Anthropic API -> narrative
#   5. delivery                 Telegram, plus JSON and Markdown on disk
#
# Designed to run on GitHub Actions with no server. Every secret comes from
# the environment. Nothing is hardcoded.
#
# Environment:
#   TELEGRAM_BOT_TOKEN     from @BotFather
#   TELEGRAM_CHAT_ID       from @userinfobot
#   ANTHROPIC_API_KEY      optional - without it the mechanical brief still
#                          sends, just without the narrative layer
#   BRIEF_OUT              output directory, default ./brief
#   BRIEF_MARGIN           account size for sizing, default 1000
#   BRIEF_RISK_PCT         risk per trade, default 1.0
#
# Usage:
#   python daily_brief.py
#   python daily_brief.py --dry-run        build it, print it, send nothing
#   python daily_brief.py --no-ai          skip the API call
# ===========================================================================

import argparse
import json
import os
from dataclasses import asdict
import sys
import time
from datetime import datetime, timezone, timedelta

try:
    import requests
except ImportError:
    sys.exit("requests not installed.  pip install requests")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TEHRAN = timezone(timedelta(hours=3, minutes=30))
TG_API = "https://api.telegram.org"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-6"
TG_LIMIT = 3900          # real limit is 4096; leave room for markup


# ===========================================================================
# EVENT CALENDAR
# ---------------------------------------------------------------------------
# Hand maintained. The lesson that produced this file: on 27 Aug a position
# was opened that had to survive a Fed chair speech before it could reach its
# own thesis. That is a broken structure regardless of the chart.
#
# Format: ISO date, label, severity, and which assets it touches.
# ===========================================================================

CALENDAR = [
    {"date": "2026-09-16", "label": "FOMC decision + dot plot",
     "severity": "critical", "scope": "all",
     "note": "range 3.50-3.75%, hike priced near 88%"},
    {"date": "2026-09-17", "label": "Bank of England decision",
     "severity": "low", "scope": "all"},
    {"date": "2026-09-29", "label": "HYPE unlock, same size as August",
     "severity": "medium", "scope": "HYPE"},
    {"date": "2026-10-13", "label": "September CPI",
     "severity": "critical", "scope": "all"},
]


def events_within(days, scope=None, today=None):
    today = today or datetime.now(TEHRAN).date()
    out = []
    for e in CALENDAR:
        try:
            d = datetime.strptime(e["date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        delta = (d - today).days
        if 0 <= delta <= days:
            if scope and e.get("scope") not in ("all", scope):
                continue
            out.append({**e, "days_away": delta})
    return sorted(out, key=lambda x: x["days_away"])


# ===========================================================================
# POSITIONS
# ---------------------------------------------------------------------------
# positions.json lives beside this file and is the only thing you edit by
# hand. Everything else is generated.
#
#   [{"symbol":"XMRUSDT","side":"long","entry":508,"sl":468,
#     "tp1":598,"tp2":700,"opened":"2026-09-08","note":"privacy laggard"}]
# ===========================================================================

def load_positions(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, ValueError, OSError):
        return []


def price_now(symbol):
    # Through exchange.py so an open position is still priced when Binance
    # refuses the runner. "price unavailable" next to a live position is
    # the one line in this brief that must never be a plumbing artifact.
    try:
        import exchange
        return exchange.price_anywhere(symbol)
    except Exception:
        return None


def monitor(positions):
    out = []
    for p in positions:
        sym = p.get("symbol")
        px = price_now(sym)
        if px is None:
            out.append({**p, "price": None, "status": "price unavailable"})
            continue

        entry = float(p["entry"])
        side = str(p.get("side", "long")).lower()
        sign = 1.0 if side == "long" else -1.0
        pnl_pct = (px / entry - 1.0) * 100.0 * sign

        sl, tp1, tp2 = p.get("sl"), p.get("tp1"), p.get("tp2")
        status, flags = "open", []

        if sl is not None:
            hit = px <= float(sl) if side == "long" else px >= float(sl)
            if hit:
                status = "STOP HIT"
            else:
                d = abs(px - float(sl)) / px * 100.0
                if d < 1.0:
                    flags.append(f"stop {d:.2f}% away")
        if tp1 is not None:
            hit = px >= float(tp1) if side == "long" else px <= float(tp1)
            if hit:
                status = "TP1 HIT"
        if tp2 is not None:
            hit = px >= float(tp2) if side == "long" else px <= float(tp2)
            if hit:
                status = "TP2 HIT"

        rr_left = None
        if sl is not None and tp1 is not None:
            risk = abs(px - float(sl))
            reward = abs(float(tp1) - px)
            rr_left = round(reward / risk, 2) if risk > 0 else None

        scope_events = events_within(7, scope=sym.replace("USDT", ""))
        for e in scope_events:
            if e["severity"] in ("critical", "medium"):
                flags.append(f"{e['label']} in {e['days_away']}d")

        out.append({**p, "price": round(px, 6),
                    "pnl_pct": round(pnl_pct, 2),
                    "rr_remaining": rr_left,
                    "status": status, "flags": flags})
    return out


# ===========================================================================
# SCAN
# ===========================================================================

def run_scan(out_dir, top, quiet=True):
    """Import the scanner in-process so one run shares the HTTP session."""
    try:
        import universe_scan as us
    except ImportError:
        return None, "universe_scan.py not found beside daily_brief.py"

    try:
        uni = us.universe(us.DEFAULT_QUOTE_MIN, top)
    except Exception as exc:
        return None, f"universe fetch failed: {type(exc).__name__}: {exc}"

    rows = []
    for i, row in enumerate(uni, 1):
        if not quiet:
            print(f"\r  [{i}/{len(uni)}] {row['symbol']:<14}",
                  end="", flush=True)
        try:
            candles = us.daily_candles(row["symbol"])
            drv = us.derivs(row["symbol"])
            a = us.analyse(row, candles, drv)
            if a:
                rows.append(a)
        except Exception:
            pass
        time.sleep(us.SLEEP_BETWEEN)
    if not quiet:
        print()

    def topn(k, n=8):
        return sorted(rows, key=lambda r: -r[k])[:n]

    # Enrich only the ranked heads - a gap lookup is two network calls per
    # symbol, so running it across all 150 would triple the runtime for
    # rows nobody reads.
    heads, seen = [], set()
    for k in ("score_extension", "score_compression", "score_momentum"):
        for r in topn(k):
            if r["symbol"] not in seen:
                seen.add(r["symbol"])
                heads.append(r)
    if not quiet:
        print(f"  gap lookup on {len(heads)} ranked symbols ...")
    enrich_with_gaps(heads, quiet=quiet)

    return {
        "sectors": us.sector_table(rows),
        "extended": topn("score_extension"),
        "compressed": topn("score_compression"),
        "momentum": topn("score_momentum"),
        "count": len(rows),
    }, None


# ===========================================================================
# KUMO GAP ENRICHMENT
# ---------------------------------------------------------------------------
# The scanner answers "where is price stretched or coiled". kumo_gaps answers
# "where is price likely to travel". Separately each is half an idea.
#
# A compressed symbol with an open gap overhead is a different proposition
# from a compressed symbol with nothing above it. This step attaches the
# nearest open gap to every ranked candidate so the brief can say not just
# that something is coiled, but where it would go if it uncoiled.
#
# Gaps are levels, not signals - the same caveat kumo_gaps.py makes itself.
# ===========================================================================

# Same universe and timeframes the old setup engine used, preserved so the
# archive stays comparable across the engine change.
SETUP_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT",
                 "SUIUSDT", "HYPEUSDT", "XRPUSDT"]
SETUP_TFS = ["15m", "30m", "1h", "2h", "4h"]

GAP_TFS = ["4h", "1d"]
GAP_MAX_DIST = 12.0        # percent; beyond this the gap is not actionable
GAP_MIN_PCT = 0.35         # ignore hairline gaps


def enrich_with_gaps(rows, tenkan=50, quiet=True):
    """Attach the nearest open kumo gap to each row, in place."""
    try:
        import kumo_gaps as kg
    except ImportError:
        for r in rows:
            r["gap"] = None
            r["gap_error"] = "kumo_gaps.py not found"
        return rows

    for r in rows:
        sym = r.get("symbol") or (r.get("sym", "") + "USDT")
        best = None
        for tf in GAP_TFS:
            try:
                c = kg.get_klines(sym, tf, 600)
                if not c:
                    continue
                px = c[-1]["c"]
                gaps = kg.open_gaps(c, tenkan_p=tenkan, kijun_p=26,
                                    senkou_p=52, disp=26,
                                    min_pct=GAP_MIN_PCT)
                for g in gaps:
                    if px < g["lo"]:
                        dist, side = (g["lo"] - px) / px * 100.0, "above"
                    elif px > g["hi"]:
                        dist, side = (g["hi"] - px) / px * 100.0, "below"
                    else:
                        dist, side = 0.0, "inside"
                    if abs(dist) > GAP_MAX_DIST:
                        continue
                    cand = {"tf": tf, "lo": round(g["lo"], 6),
                            "hi": round(g["hi"], 6),
                            "mid": round(g["mid"], 6),
                            "side": side, "dist_pct": round(dist, 2),
                            "size_pct": round(g["size_pct"], 2),
                            "age_bars": g["age_bars"]}
                    if best is None or abs(cand["dist_pct"]) < abs(best["dist_pct"]):
                        best = cand
                time.sleep(0.12)
            except Exception:
                continue
        r["gap"] = best
    return rows


# Measured, not assumed. Backtest of 13 Sep 2026 over 24 symbols, Tenkan 50,
# 50-bar horizon: 4h 55.9% of 422 gaps filled, 1d 45.8% of 428, 50.8% overall.
# Printed on every brief so "gap 8% above" is never read as a forecast. The
# samples overlap heavily across symbols that move together, so the effective
# sample is far smaller than the counts suggest and 4h is not a proven edge.
KUMO_BASE_RATE_NOTE = (
    "GAP BASE RATE\n"
    " measured 13 Sep 2026, 24 symbols, 50-bar horizon\n"
    " 4h 55.9% of 422 gaps filled  |  1d 45.8% of 428  |  50.8% overall\n"
    " near a coin flip. A gap is a destination, not a reason.\n")


def gap_phrase(r):
    """One short clause describing the nearest gap, or empty."""
    g = r.get("gap")
    if not g:
        return ""
    if g["side"] == "inside":
        return f"price inside a {g['tf']} gap"
    arrow = "up" if g["side"] == "above" else "down"
    return (f"{g['tf']} gap {g['dist_pct']:+.1f}% {arrow} "
            f"({g['lo']:g}-{g['hi']:g})")


# ===========================================================================
# DIGEST
# ---------------------------------------------------------------------------
# Compact on purpose. The API call costs tokens; sending 200 full rows would
# be wasteful and would bury the signal. Send the ranked heads and the
# sector table, nothing else.
# ===========================================================================

def run_setups(events, margin, risk_pct, quiet=True):
    """Build fresh snapshots and judge them with the v2 engine.

    This is what removes the desktop watcher from the critical path: the
    snapshots are built here, from the same public endpoints, in the same
    shape the engine already consumes.
    """
    try:
        import snapshot_builder as sb
        import signal_engine as se
    except ImportError as exc:
        return [], [], f"engine import failed: {exc}"

    if not quiet:
        print(f"  building {len(SETUP_SYMBOLS)}x{len(SETUP_TFS)} snapshots ...")
    snaps = sb.build_many(SETUP_SYMBOLS, SETUP_TFS, verify=True, quiet=quiet)
    approved, rejected = se.judge_all(
        snaps, cfg={"margin": margin, "risk_pct": risk_pct},
        now_ts=time.time(), events=events)
    return approved, rejected, None


def apply_publication_cap(approved, rejected, cap):
    """Publish at most `cap` setups, and record every one that was cut.

    `approved` arrives sorted by confidence, so the cut is by rank.

    The setups that do not make the cut are APPENDED TO `rejected` IN
    PLACE with their rank and confidence spelled out. That is the whole
    point: a setup the engine approved and the run chose not to send is
    still something the engine approved, and burying it would make the
    published record flattering rather than true.

    cap <= 0 means no cap.

    Returns (published, over).
    """
    if cap is None or cap <= 0 or len(approved) <= cap:
        return approved, []
    total = len(approved)
    over = approved[cap:]
    for rank, c in enumerate(over, start=cap + 1):
        rejected.append({
            "symbol": getattr(c, "symbol", None),
            "tf": getattr(c, "tf", None),
            "reason": ("below the publication cap - ranked %d of %d approved "
                       "at %.0f%% confidence, cap is %d per run"
                       % (rank, total, getattr(c, "confidence", 0) or 0, cap)),
        })
    return approved[:cap], over


def build_digest(scan, positions, evts):
    def slim(r):
        return {
            "sym": r["base"], "px": r["price"], "rsi": r["rsi14"],
            "ema20": r["ema20_dist_pct"], "d7": r["ret_7d_pct"],
            "d30": r["ret_30d_pct"], "atr_ptl": r["atr_pctile"],
            "vol_surge": r["vol_surge"], "fund": r.get("funding"),
            "sector": r.get("sector"), "gap": r.get("gap"),
            "why": (r.get("notes_extension")
                    or r.get("notes_compression")
                    or r.get("notes_momentum"))[:3],
        }

    return {
        "generated_tehran": datetime.now(TEHRAN).strftime("%Y-%m-%d %H:%M"),
        "generated_ts": time.time(),
        "scanned": scan["count"] if scan else 0,
        "sectors": [
            {k: s[k] for k in ("sector", "median_7d_pct", "median_30d_pct",
                               "leader", "leader_rsi", "laggard",
                               "laggard_rsi")}
            for s in (scan["sectors"][:8] if scan else [])
        ],
        "most_extended": [slim(r) for r in (scan["extended"] if scan else [])],
        "most_compressed": [slim(r) for r in (scan["compressed"] if scan else [])],
        "trending_with_room": [slim(r) for r in (scan["momentum"] if scan else [])],
        "open_positions": positions,
        "events_next_14d": evts,
    }


# ===========================================================================
# JUDGMENT LAYER
# ===========================================================================

SYSTEM_PROMPT = """You are the analysis layer of a personal market research \
system. You receive a mechanical scan of the perpetual futures market plus \
the operator's open positions, and you produce a short daily brief.

The operator is an experienced retail trader running his own risk. He does \
not want hedged non-answers and he does not want reckless calls. He wants \
the analysis a careful analyst would actually write.

Hard rules, learned from logged mistakes in this system:

1. Never build a thesis on a structural assumption without asking what \
happened the last time. A token unlock thesis failed badly because \
historical data showed only 1.75% of a previous unlock reached exchanges.

2. "Extended" is a condition, not a signal. Always ask what is bidding \
underneath. An asset with ETF inflows, a buyback, or a treasury holder \
behaves differently from one running on leverage alone.

3. Never propose an entry that must cross a binary macro event to reach its \
own thesis. If the calendar shows a critical event inside the horizon, say \
so and hold the idea.

4. Put entries inside the zone, never on the round number at its edge. A \
short at 110.50 never filled because the high was 110.38.

5. A divergence visible on only one timeframe is an artifact. Require two \
consecutive timeframes.

6. Leverage is an output of risk and stop distance, never an input.

7. Distinguish leverage trades from multi-month holds. They route to \
different venues, carry different costs, and need different reasoning. A \
perpetual held for months bleeds funding and is usually the wrong \
instrument for a hold.

Output format, plain text, no markdown headers, under 2500 characters:

STATE - two sentences on what the market is actually doing
ROTATION - which sector is moving and, crucially, which member has \
participated without going vertical
POSITIONS - one line per open position: what changed, what to watch
IDEA - at most one new idea, or explicitly none. If you give one, include \
entry, invalidation, target, and why now. If the calendar blocks it, say the \
idea exists but is gated and give the date it unblocks.
RISK - the single thing most likely to make today's read wrong

If nothing meets the bar, say so. A day with no idea is a valid output and \
is better than a manufactured one. Never present a probability as though it \
were calibrated - this system's logged hit rate is roughly 40%."""


def call_anthropic(digest, api_key, timeout=90):
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 1200,
        "system": SYSTEM_PROMPT,
        "messages": [{
            "role": "user",
            "content": ("Today's scan and positions:\n\n"
                        + json.dumps(digest, ensure_ascii=False, indent=1,
                                     default=str))
        }],
    }
    r = requests.post(
        ANTHROPIC_API, timeout=timeout,
        headers={"x-api-key": api_key,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json=payload)
    if r.status_code != 200:
        return None, f"API {r.status_code}: {r.text[:200]}"
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", [])
                   if b.get("type") == "text")
    usage = data.get("usage", {})
    return {"text": text.strip(), "usage": usage}, None


# ===========================================================================
# TELEGRAM
# ===========================================================================

def tg_send(token, chat_id, text, timeout=20):
    sent, errors = 0, []
    for chunk in split_message(text):
        try:
            r = requests.post(
                f"{TG_API}/bot{token}/sendMessage", timeout=timeout,
                json={"chat_id": chat_id, "text": chunk,
                      "disable_web_page_preview": True})
            if r.status_code == 200:
                sent += 1
            else:
                errors.append(f"{r.status_code}: {r.text[:120]}")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        time.sleep(0.4)
    return sent, errors


def split_message(text, limit=TG_LIMIT):
    if len(text) <= limit:
        return [text]
    out, buf = [], ""
    for para in text.split("\n\n"):
        piece = (buf + "\n\n" + para) if buf else para
        if len(piece) <= limit:
            buf = piece
            continue
        if buf:
            out.append(buf)
        while len(para) > limit:
            out.append(para[:limit])
            para = para[limit:]
        buf = para
    if buf:
        out.append(buf)
    return out


# ===========================================================================
# RENDER
# ===========================================================================

def render_mechanical(digest, signals=None, rejected=None):
    L = [f"MARKET BRIEF  {digest['generated_tehran']} Tehran",
         f"scanned {digest['scanned']} perps", ""]

    # Signals go first - it is the only part that asks for a decision.
    # An empty result is printed as a result, not omitted: knowing the
    # engine looked and found nothing is the point of the denominator.
    sigs = signals or []
    L.append("SETUPS")
    if sigs:
        for s_ in sigs[:3]:
            pos = s_.get("position", {})
            L.append(f" {s_['side'].upper()} {s_['symbol']} {s_['tf']}"
                     f"   confidence {s_['confidence']}")
            L.append(f"   entry {s_['entry']:g}  stop {s_['stop']:g}"
                     f"  tp1 {s_['targets'][0]:g}")
            L.append(f"   R:R 1:{s_['rr']}  leverage {pos.get('leverage')}x"
                     f"  risk ${pos.get('risk_usd')}")
            L.append(f"   entry sits {s_['entry_distance_pct']:.2f}% "
                     f"({s_['entry_distance_atr']:.2f} ATR) from market")
            for w in (s_.get("supports") or [])[:3]:
                L.append(f"   + {w}")
            for w in (s_.get("warnings") or []):
                L.append(f"   ! {w}")
            L.append("")
    else:
        considered = digest.get("setups_considered", 0)
        L.append(f" nothing cleared the gates. {considered} combinations "
                 f"were checked.")
        if rejected:
            from collections import Counter
            top = Counter(r["reason"].split(" - ")[0].split(",")[0]
                          for r in rejected).most_common(3)
            for reason, n in top:
                L.append(f"   {n}x  {reason}")
        L.append("")

    if digest["events_next_14d"]:
        L.append("CALENDAR")
        for e in digest["events_next_14d"]:
            mark = "!!" if e["severity"] == "critical" else "-"
            L.append(f" {mark} {e['days_away']}d  {e['label']}")
        L.append("")

    if digest["open_positions"]:
        L.append("POSITIONS")
        for p in digest["open_positions"]:
            if p.get("price") is None:
                L.append(f" {p['symbol']}  price unavailable")
                continue
            L.append(f" {p['symbol']} {p['side']} @ {p['entry']}")
            L.append(f"   now {p['price']}  pnl {p['pnl_pct']:+.2f}%"
                     f"  status {p['status']}")
            if p.get("rr_remaining"):
                L.append(f"   R:R from here 1:{p['rr_remaining']}")
            for f in p.get("flags", []):
                L.append(f"   ! {f}")
        L.append("")

    if digest["sectors"]:
        L.append("SECTOR ROTATION  7d median")
        for s in digest["sectors"][:5]:
            lag = (f"  laggard {s['laggard']} RSI {s['laggard_rsi']}"
                   if s.get("laggard") else "")
            L.append(f" {s['sector']:<11} {s['median_7d_pct']:+6.1f}%"
                     f"  lead {s['leader']} RSI {s['leader_rsi']}{lag}")
        L.append("")

    def blk(title, rows):
        if not rows:
            return
        L.append(title)
        for r in rows[:5]:
            L.append(f" {r['sym']:<7} RSI {str(r['rsi']):<5}"
                     f" EMA20 {r['ema20']:+6.1f}%  7d {r['d7']:+6.1f}%")
            bits = list(r["why"][:2])
            g = r.get("gap")
            if g:
                arrow = {"above": "up", "below": "down",
                         "inside": "inside"}[g["side"]]
                bits.append(f"{g['tf']} gap {g['dist_pct']:+.1f}% {arrow}")
            if bits:
                L.append(f"   {', '.join(bits)}")
        L.append("")

    blk("MOST EXTENDED", digest["most_extended"])
    blk("MOST COMPRESSED", digest["most_compressed"])
    blk("TRENDING WITH ROOM", digest["trending_with_room"])
    L.append(KUMO_BASE_RATE_NOTE)
    return "\n".join(L)


# ===========================================================================
# MAIN
# ===========================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.environ.get("BRIEF_OUT", "./brief"))
    ap.add_argument("--positions", default=os.path.join(HERE, "positions.json"))
    ap.add_argument("--top", type=int, default=150)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    now = datetime.now(TEHRAN)
    print(f"daily brief  {now:%Y-%m-%d %H:%M} Tehran")

    print("  scanning ...")
    scan, err = run_scan(args.out, args.top, quiet=not args.verbose)
    if err:
        print(f"  scan failed: {err}")

    print("  pricing positions ...")
    positions = monitor(load_positions(args.positions))

    evts = events_within(14)

    margin = float(os.environ.get("BRIEF_MARGIN", 1000))
    risk_pct = float(os.environ.get("BRIEF_RISK_PCT", 1.0))
    print("  judging setups ...")
    approved, rejected, se_err = run_setups(
        evts, margin, risk_pct, quiet=not args.verbose)
    if se_err:
        print(f"  setup engine: {se_err}")

    digest = build_digest(scan, positions, evts)

    # ------------------------------------------------------------------
    # HOW MANY SIGNALS A DAY.
    #
    # The gates decide what is a valid setup. They do not decide how many
    # a person can actually act on, and those are different questions. On
    # a trending day eight things can pass at once; taking eight is not a
    # portfolio, it is one directional bet placed eight times, and each
    # one still consumes attention while it waits for an entry.
    #
    # So the run publishes its best MAX_SIGNALS_PER_RUN and no more.
    # Two runs a day means the daily ceiling is twice that. approved is
    # already sorted by confidence, so the cut is by rank.
    #
    # What is cut is NOT hidden. It moves into the rejected list with its
    # rank and its confidence, appears in the brief's rejection summary,
    # and stays in the archive. The system is not allowed to quietly drop
    # a setup it approved - "we only publish the good ones" is how a
    # record stops meaning anything.
    # ------------------------------------------------------------------
    cap = int(os.environ.get("MAX_SIGNALS_PER_RUN", 3))
    considered = len(approved) + len(rejected)
    approved, over = apply_publication_cap(approved, rejected, cap)
    if over:
        print("  publication cap: %d approved, publishing the top %d"
              % (len(approved) + len(over), cap))

    signals = [asdict(c) for c in approved]
    digest["setups_considered"] = considered
    digest["setups_approved"] = len(approved) + len(over)
    digest["setups_published"] = len(approved)
    digest["publication_cap"] = cap
    digest["setups_over_cap"] = len(over)
    mech = render_mechanical(digest, signals, rejected)

    narrative, ai_err, usage = None, None, None
    key = os.environ.get("ANTHROPIC_API_KEY")
    if args.no_ai:
        ai_err = "skipped by flag"
    elif not key:
        ai_err = "ANTHROPIC_API_KEY not set"
    else:
        print("  calling judgment layer ...")
        res, ai_err = call_anthropic(digest, key)
        if res:
            narrative = res["text"]
            usage = res["usage"]

    message = mech
    if narrative:
        message += "\n" + "=" * 34 + "\nREAD\n\n" + narrative
    elif ai_err:
        message += f"\n[narrative unavailable: {ai_err}]"

    os.makedirs(args.out, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d")

    # The ledger is the only place a win rate can honestly come from. Each run
    # files what it published and re-reads the candles to settle what the last
    # run published. A dry run resolves and reports but writes nothing, so
    # testing never contaminates the record.
    archive = None
    try:
        import signal_ledger
        archive = signal_ledger.update(
            signals, path=os.path.join(args.out, "signals.json"),
            now=time.time(),
            published_ts=digest.get("generated_ts"),
            quiet=not getattr(args, "verbose", False),
            dry_run=bool(args.dry_run))
    except Exception as exc:
        print(f"  ledger skipped: {type(exc).__name__}: {exc}")

    with open(os.path.join(args.out, "brief_latest.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"digest": digest, "signals": signals,
                   "archive_overall": (archive or {}).get("overall"),
                   "rejected": rejected[:40], "narrative": narrative,
                   "usage": usage, "ai_error": ai_err,
                   "no_signal_reason": (
                       None if signals else
                       f"{digest.get('setups_considered', 0)} combinations "
                       f"checked, none cleared the entry, risk and calendar "
                       f"gates.")},
                  fh, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(args.out, "brief_latest.md"), "w",
              encoding="utf-8") as fh:
        fh.write(message)
    hist = os.path.join(args.out, "history")
    os.makedirs(hist, exist_ok=True)
    with open(os.path.join(hist, f"{stamp}.md"), "w", encoding="utf-8") as fh:
        fh.write(message)

    if args.dry_run:
        print("-" * 60)
        print(message)
        print("-" * 60)
        if usage:
            print(f"tokens in {usage.get('input_tokens')} "
                  f"out {usage.get('output_tokens')}")
        print("dry run - nothing sent")
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set - not sending")
        return 1

    sent, errors = tg_send(token, chat, message)
    print(f"  telegram: {sent} chunk(s) sent")
    for e in errors:
        print(f"    error {e}")
    if usage:
        print(f"  tokens in {usage.get('input_tokens')} "
              f"out {usage.get('output_tokens')}")
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())
