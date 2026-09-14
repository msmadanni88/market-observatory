#!/usr/bin/env python3
"""Python and JavaScript must resolve the same signal the same way.

    python tests/test_agreement.py

signal_ledger._walk decides what happened to a trade and writes it into the
committed history. lib/resolve.js decides what happened to a trade and shows
it on every page. Two implementations of one definition is two places for it
to drift, and a drift here is invisible: both sides keep producing plausible
outcomes that quietly disagree, and the archive stops meaning anything.

So both are run over the same synthetic candles and every verdict compared.
No network: the candles are built here, deliberately, to hit each branch of
the state machine including the ones that only show up in bad weather.
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import signal_ledger as SL  # noqa: E402

T0 = 1700000000
STEP = 3600


def bar(i, h, l):
    return {"t": T0 + i * STEP, "o": (h + l) / 2.0, "h": h, "l": l,
            "c": (h + l) / 2.0, "v": 0}


def rec(side, entry, stop, targets, zone):
    return {
        "id": "T", "symbol": "X", "tf": "1h", "side": side, "entry": entry,
        "stop": stop, "targets": targets, "entry_zone": zone,
        "published_ts": T0, "status": "pending", "result": "open",
        "result_r": None, "risk_per_unit": abs(entry - stop),
        "mfe_r": None, "mae_r": None, "activated_ts": None,
        "bars_to_activate": None, "bars_to_resolve": None,
        "resolve_note": "", "resolved_ts": None,
    }


SHORT = dict(side="short", entry=100.0, stop=102.0, targets=[96.0, 94.0],
             zone=[99.5, 100.5])
LONG = dict(side="long", entry=100.0, stop=98.0, targets=[104.0, 106.0],
            zone=[99.5, 100.5])

CASES = [
    ("never reaches the zone", SHORT, [bar(0, 98, 97), bar(1, 98.5, 97.5)]),
    ("touches the zone and sits there", SHORT,
     [bar(0, 100.2, 99.8), bar(1, 100.4, 99.6)]),
    ("reaches the target", SHORT, [bar(0, 100.2, 99.8), bar(1, 99, 95.5)]),
    ("hits the stop", SHORT, [bar(0, 100.2, 99.8), bar(1, 102.5, 100)]),
    ("stop and target in one bar", SHORT,
     [bar(0, 100.2, 99.8), bar(1, 103, 95)]),
    ("passes the stop without ever filling", SHORT, [bar(0, 103, 102.5)]),
    ("fills and stops inside a single bar", LONG,
     [bar(0, 101, 97), bar(1, 101, 100.5)]),
    ("long reaches the target", LONG, [bar(0, 100.2, 99.8), bar(1, 105, 100)]),
    ("long invalidated", LONG, [bar(0, 97.5, 97)]),
    ("long stops out", LONG, [bar(0, 100.2, 99.8), bar(1, 100.5, 97.5)]),
    ("exact touch of the stop counts", SHORT,
     [bar(0, 100.2, 99.8), bar(1, 102.0, 101)]),
    ("exact touch of the target counts", SHORT,
     [bar(0, 100.2, 99.8), bar(1, 99, 96.0)]),
    ("zone edge exactly touched activates", SHORT,
     [bar(0, 99.5, 99.0), bar(1, 99.4, 99.0)]),
    ("a long run of quiet bars", SHORT,
     [bar(0, 100.2, 99.8)] + [bar(i, 100.3, 99.7) for i in range(1, 30)]),
]

py_out = []
for name, spec, bars in CASES:
    r = rec(**spec)
    SL._walk(r, bars)
    py_out.append({
        "name": name, "status": r["status"],
        "result_r": None if r["result_r"] is None else round(r["result_r"], 6),
        "mfe": None if r["mfe_r"] is None else round(r["mfe_r"], 3),
        "mae": None if r["mae_r"] is None else round(r["mae_r"], 3),
    })

payload = [{"name": n, "spec": s, "bars": b} for n, s, b in CASES]
prog = """
var store={};
global.window={localStorage:{getItem:function(k){return k in store?store[k]:null;},
  setItem:function(k,v){store[k]=String(v);}}};
require(%s);
var MO=global.window.MO;
var CASES=%s;
console.log(JSON.stringify(CASES.map(function(c){
  var plan={side:c.spec.side,entry:c.spec.entry,stop:c.spec.stop,
    targets:c.spec.targets};
  var r=MO.replay({symbol:"X",tf:"1h",entry_zone:c.spec.zone},plan,%d,c.bars);
  var round=function(v,n){return v==null?null:
    Math.round(v*Math.pow(10,n))/Math.pow(10,n);};
  return {name:c.name,status:r.state,result_r:round(r.resultR,6),
    mfe:round(r.mfe,3),mae:round(r.mae,3)};
})));
""" % (json.dumps(os.path.join(ROOT, "lib", "resolve.js").replace("\\", "/")),
       json.dumps(payload), T0)

with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                 encoding="utf-8") as fh:
    fh.write(prog)
    p = fh.name
proc = subprocess.run(["node", p], capture_output=True, text=True)
os.unlink(p)
if proc.returncode != 0:
    print("the javascript resolver did not run:")
    print(proc.stderr.strip()[:800])
    sys.exit(1)
js_out = json.loads(proc.stdout.strip())

ok, bad = [], []
for p_, j_ in zip(py_out, js_out):
    name = p_["name"]
    diffs = []
    if p_["status"] != j_["status"]:
        diffs.append("status python=%s js=%s" % (p_["status"], j_["status"]))
    # A pending trade has no excursions to compare in python, which only
    # records them once the trade is live; the branches that matter are the
    # resolved ones.
    if p_["status"] == j_["status"] and p_["status"] != "pending":
        for k in ("result_r", "mfe", "mae"):
            a, b = p_[k], j_[k]
            if a is None and b is None:
                continue
            if a is None or b is None or abs(a - b) > 1e-6:
                diffs.append("%s python=%s js=%s" % (k, a, b))
    if diffs:
        bad.append("%s: %s" % (name, "; ".join(diffs)))
    else:
        ok.append("%s -> %s" % (name, p_["status"]))

print("=" * 66)
for line in ok:
    print("  ok    " + line)
for line in bad:
    print("  FAIL  " + line)
print("=" * 66)
if bad:
    print("\n%d disagreement(s) between the pipeline and the pages.\n"
          % len(bad))
    sys.exit(1)
print("\nPython and the browser agree on all %d cases.\n" % len(ok))
