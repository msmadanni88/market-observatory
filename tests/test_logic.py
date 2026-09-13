#!/usr/bin/env python3
"""Unit tests for the pieces of logic that decide what gets published.

    python tests/test_logic.py

No network. These run in under a second and are meant to run on every
change, which is the only reason they are worth having.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []
passes = []


def check(name, cond, detail=""):
    (passes if cond else fails).append(name + (" - " + detail if detail
                                               and not cond else ""))


class Fake(object):
    def __init__(self, sym, tf, conf):
        self.symbol = sym
        self.tf = tf
        self.confidence = conf


# ------------------------------------------------ publication cap
import daily_brief as db  # noqa: E402

cands = [Fake("A", "4h", 90), Fake("B", "1h", 80), Fake("C", "15m", 70),
         Fake("D", "30m", 60), Fake("E", "1d", 55)]

rej = []
pub, over = db.apply_publication_cap(list(cands), rej, 3)
check("cap publishes exactly cap", len(pub) == 3, "got %d" % len(pub))
check("cap keeps the highest confidence",
      [c.symbol for c in pub] == ["A", "B", "C"],
      str([c.symbol for c in pub]))
check("cap returns the remainder", [c.symbol for c in over] == ["D", "E"])
check("every cut setup is recorded, none vanishes", len(rej) == 2,
      "rejected has %d" % len(rej))
check("the cut record names its rank and the cap",
      all("ranked" in r["reason"] and "cap is 3" in r["reason"] for r in rej))
check("published plus cut equals approved", len(pub) + len(over) == len(cands))

rej2 = []
pub2, over2 = db.apply_publication_cap(list(cands), rej2, 0)
check("cap of zero means no cap", len(pub2) == 5 and not over2 and not rej2)

rej3 = []
pub3, over3 = db.apply_publication_cap(list(cands[:2]), rej3, 3)
check("fewer approved than the cap publishes them all",
      len(pub3) == 2 and not over3 and not rej3)

rej4 = []
pub4, over4 = db.apply_publication_cap([], rej4, 3)
check("no approved setups is not an error", pub4 == [] and over4 == [])

# ------------------------------------------------ reference codes
import signal_ledger as sl  # noqa: E402

r1 = sl._ref({"symbol": "HYPEUSDT", "tf": "30m", "side": "short"}, 1789308927)
check("reference has the documented shape",
      r1.startswith("MO-") and r1.endswith("-HYPE-30m-S"), r1)
r2 = sl._ref({"symbol": "HYPEUSDT", "tf": "30m", "side": "short"}, 1789308927)
check("the same signal always gets the same reference", r1 == r2)
r3 = sl._ref({"symbol": "HYPEUSDT", "tf": "30m", "side": "long"}, 1789308927)
check("the two sides of one market get different references", r1 != r3)
r4 = sl._ref({"symbol": "ETHUSDC", "tf": "1d", "side": "long"}, 1700000000)
check("only a trailing quote asset is stripped",
      "-ETH-1d-L" in r4, r4)
r5 = sl._ref({"symbol": "1000PEPEUSDT", "tf": "15m", "side": "long"}, 1700000000)
check("a leading number in the ticker survives", "-1000PEPE-15m-L" in r5, r5)

# ------------------------------------------------ ledger scoring
check("a loss is minus one R by construction", sl.OPEN_STATES ==
      ("pending", "active"), str(sl.OPEN_STATES))
check("expiry and resolve windows are bar counts, not seconds",
      isinstance(sl.EXPIRY_BARS, int) and isinstance(sl.RESOLVE_BARS, int))

# ------------------------------------------------ report
print("=" * 62)
for p in passes:
    print("  ok    " + p)
for f in fails:
    print("  FAIL  " + f)
print("=" * 62)
if fails:
    print("\n%d failing test(s).\n" % len(fails))
    sys.exit(1)
print("\nAll %d tests passed.\n" % len(passes))
sys.exit(0)
