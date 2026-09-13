#!/usr/bin/env python3
"""Freeze one candle series so the gap check needs no network.

    python tests/make_fixture.py [SYMBOL] [TF] [BARS]

Run again only if the fixture is lost. A FIXED series is the point: the
two implementations must agree on the same input every time, and pulling
fresh candles each run would make a real disagreement look like a data
difference.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import kumo_gaps  # noqa: E402

sym = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
tf = sys.argv[2] if len(sys.argv) > 2 else "12h"
bars = int(sys.argv[3]) if len(sys.argv) > 3 else 600

c = kumo_gaps.get_klines(sym, tf, bars)
if not c:
    print("no candles returned")
    sys.exit(1)

out = os.path.join(ROOT, "tests", "candles.json")
with open(out, "w", encoding="utf-8") as fh:
    json.dump({"symbol": sym, "tf": tf,
               "candles": [{"t": k["t"], "o": k["o"], "h": k["h"],
                            "l": k["l"], "c": k["c"]} for k in c]}, fh)
print("wrote %d candles to %s" % (len(c), out))
