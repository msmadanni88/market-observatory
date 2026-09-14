#!/usr/bin/env python3
# ===========================================================================
# SETTLE
# ---------------------------------------------------------------------------
# Walks every OPEN record forward against the candles and writes down what
# happened. Publishes nothing, scans nothing, sends nothing.
#
#     python settle.py
#
# WHY THIS EXISTS SEPARATELY FROM THE BRIEF
# The brief runs twice a day. A trade stopped out at 09:12 was therefore
# recorded as still open until 17:30, and anyone reading the committed file
# in between saw a lie - not a rounding error, a wrong answer to "what
# happened to this trade".
#
# The dashboard papers over that by replaying the candles in the browser,
# but a browser that nobody has open records nothing. This does the same
# arithmetic on a schedule so the committed history is right whether or not
# anyone is looking, which is the only version of "reliable" that counts.
#
# It is deliberately cheap: it touches only the markets with an open record,
# and a run with nothing open costs one file read.
# ===========================================================================

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import signal_ledger  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=os.path.join(ROOT, "brief"),
                    help="directory holding signals.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="work it out and print it, write nothing")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    path = os.path.join(args.out, "signals.json")
    if not os.path.isfile(path):
        print("no ledger at %s - nothing to settle" % path)
        return 0

    before = json.load(open(path, encoding="utf-8"))
    open_before = [r for r in (before.get("records") or [])
                   if r.get("status") in signal_ledger.OPEN_STATES]
    if not open_before:
        print("nothing open - the ledger is already settled")
        return 0

    print("settling %d open record(s) ..." % len(open_before))
    for r in open_before:
        print("  %s  %s %s %s  %s" % (r.get("ref") or r.get("id"),
                                      r.get("symbol"), r.get("tf"),
                                      r.get("side"), r.get("status")))

    # No signals in, so nothing is published. update() still walks every
    # open record forward, which is the whole job.
    data = signal_ledger.update([], path=path, now=time.time(),
                                quiet=args.quiet, dry_run=args.dry_run)

    after = {r.get("id"): r for r in (data.get("records") or [])}
    changed = 0
    for r in open_before:
        now_rec = after.get(r.get("id"))
        if not now_rec:
            continue
        if now_rec.get("status") != r.get("status"):
            changed += 1
            print("  %s  %s -> %s   %s"
                  % (r.get("ref") or r.get("id"), r.get("status"),
                     now_rec.get("status"),
                     now_rec.get("resolve_note") or ""))

    print("settled %d of %d" % (changed, len(open_before)))
    # A non-zero count is what the workflow uses to decide whether there is
    # anything worth committing.
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write("changed=%d\n" % changed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
