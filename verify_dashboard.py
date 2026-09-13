#!/usr/bin/env python3
# ===========================================================================
# VERIFY DASHBOARD
# ---------------------------------------------------------------------------
# Static regression harness for index.html and the ledger. This exists
# because a fix that silently breaks two other things is worse than no fix
# at all, and "it looked right in the screenshot" is not a test.
#
#   python verify_dashboard.py
#
# It checks, without a browser:
#   1. the page's javascript parses            (node --check)
#   2. python and javascript agree on a signal reference, character for
#      character, on real inputs
#   3. every CSS custom property the stylesheet READS is DEFINED by all four
#      themes - the class of bug that made the brown theme show night text
#   4. every table's <th> count matches the colspan of its detail row
#   5. every canvas the lab renders has its plan written into labPlans
#   6. the shipped ledger parses and every record carries a reference
#
# Exit 0 means the page is safe to open. Run it before every commit.
# ===========================================================================

import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(ROOT, "index.html")

ok, bad, note = [], [], []


def fail(msg):
    bad.append(msg)


def good(msg):
    ok.append(msg)


html = open(HTML, encoding="utf-8").read()

# ------------------------------------------------------------------ 1. JS
scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
if not scripts:
    fail("no <script> block found in index.html")
js = "\n".join(scripts)

with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                 encoding="utf-8") as fh:
    fh.write(js)
    jspath = fh.name
try:
    r = subprocess.run(["node", "--check", jspath],
                       capture_output=True, text=True)
    if r.returncode == 0:
        good("page javascript parses (%d lines)" % js.count("\n"))
    else:
        fail("JS SYNTAX: " + (r.stderr or r.stdout).strip()[:400])
except FileNotFoundError:
    note.append("node not found - javascript was not syntax checked")

# ------------------------------------------------- 2. reference agreement
sys.path.insert(0, ROOT)
ref_ok = True
try:
    import signal_ledger as SL

    cases = [
        ({"symbol": "HYPEUSDT", "tf": "30m", "side": "short"}, 1757781900),
        ({"symbol": "BTCUSDT", "tf": "4h", "side": "long"}, 1757700000),
        ({"symbol": "1000PEPEUSDT", "tf": "15m", "side": "long"}, 1700000000),
        ({"symbol": "ETHUSDC", "tf": "1d", "side": "short"}, 1699999999),
        ({"symbol": "XMRUSDT", "tf": "1h", "side": "long"}, 0),
    ]
    py = [SL._ref(s, t) for s, t in cases]

    # Run the page's own signalRef, lifted out of the file, against the same
    # inputs. If these two ever drift the archive and the card would label
    # the same trade differently, which is exactly the kind of quiet wrong
    # this harness is for.
    m = re.search(r"function signalRef\(symbol,tf,side,ts\)\{.*?\n\}", js, re.S)
    cb = re.search(r"const coinBase=[^\n]+", js)
    if not m or not cb:
        fail("could not lift signalRef/coinBase out of index.html")
        ref_ok = False
    else:
        prog = (cb.group(0) + "\n" + m.group(0) + "\n" +
                "console.log(JSON.stringify(" +
                json.dumps([[c[0]["symbol"], c[0]["tf"], c[0]["side"], c[1]]
                            for c in cases]) +
                ".map(a=>signalRef(a[0],a[1],a[2],a[3]))));")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(prog)
            p2 = fh.name
        r = subprocess.run(["node", p2], capture_output=True, text=True)
        os.unlink(p2)
        if r.returncode != 0:
            fail("signalRef did not run: " + r.stderr.strip()[:300])
            ref_ok = False
        else:
            jsrefs = json.loads(r.stdout.strip())
            for (sig, ts), a, b in zip(cases, py, jsrefs):
                if a != b:
                    fail("REFERENCE MISMATCH %s %s: python=%s js=%s"
                         % (sig["symbol"], sig["tf"], a, b))
                    ref_ok = False
            if ref_ok:
                good("reference codes identical in python and the page "
                     "(%s)" % py[0])
except Exception as e:
    fail("reference check could not run: %s: %s" % (type(e).__name__, e))

os.unlink(jspath)

# ------------------------------------------------------- 3. theme coverage
# Every --token the stylesheet reads must be declared by every theme block,
# or a theme silently inherits another theme's colour.
css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))
used = set(re.findall(r"var\((--[a-z0-9-]+)", css))
theme_blocks = re.findall(
    r"(?:html\[data-theme=\"([a-z]+)\"\]|^:root)\s*\{([^}]*)\}",
    css, re.S | re.M)
themes = {}
for name, body in theme_blocks:
    themes.setdefault(name or "root", "")
    themes[name or "root"] += body

root_defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", themes.get("root", "")))
named = {k: v for k, v in themes.items() if k != "root"}
if not named:
    fail("no html[data-theme=...] blocks found - themes are not scoped to "
         "the root element")
else:
    # A token only has to appear in every NAMED theme if any named theme
    # overrides it at all; tokens that live purely on :root are shared.
    overridden = set()
    for body in named.values():
        overridden |= set(re.findall(r"(--[a-z0-9-]+)\s*:", body))
    holes = []
    for tok in sorted(overridden):
        for name, body in named.items():
            if not re.search(re.escape(tok) + r"\s*:", body):
                holes.append("%s missing %s" % (name, tok))
    if holes:
        for hcase in holes:
            fail("THEME HOLE: " + hcase + " - it will inherit the previous "
                 "theme's value")
    else:
        good("all %d themed tokens defined by every one of: %s"
             % (len(overridden), ", ".join(sorted(named))))

    # Tokens the page sets inline per element - the confidence arc and the
    # grid column count - are defined at render time, not in the stylesheet.
    inline = set(re.findall(r'style="(--[a-z0-9-]+):', html))
    inline |= set(re.findall(r"\.style\.setProperty\(\"(--[a-z0-9-]+)\"", html))
    undefined = sorted(t for t in used
                       if t not in root_defined and t not in overridden
                       and t not in inline
                       and not re.search(re.escape(t) + r"\s*:", css))
    if inline:
        good("%d token(s) set inline at render time: %s"
             % (len(inline), ", ".join(sorted(inline))))
    if undefined:
        fail("undefined CSS token(s) read by the stylesheet: %s"
             % ", ".join(undefined))
    else:
        good("no undefined CSS tokens")

# ----------------------------------------------------- 4. table arithmetic
# A colspan that does not match the header count shears the detail row off
# the bottom of the table. Silent, and ugly. Each table is checked by name
# rather than by a loose scan, because a scan that runs past the end of one
# table into the next reports a number that means nothing.

# The archive: a literal header, a detail row underneath every row.
arch = re.search(r'archive-table"><thead><tr>\'\+(.*?)\'</tr></thead>', html, re.S)
if not arch:
    fail("archive table header not found")
else:
    n = arch.group(1).count("<th>")
    seg = html[arch.end():arch.end() + 4000]
    spans = sorted({int(v) for v in
                    re.findall(r'archive-detail"[^\n]*colspan="(\d+)"',
                               seg.replace("\n", " "))})
    tds = re.search(r'<tr class="archive-row"(.*?)</tr>\'\+', seg, re.S)
    ntd = tds.group(1).count("<td") if tds else -1
    if spans and spans != [n]:
        fail("ARCHIVE TABLE: %d header cells but colspan %s on the detail "
             "row" % (n, spans))
    elif ntd != n:
        fail("ARCHIVE TABLE: %d header cells but %d cells in the row"
             % (n, ntd))
    else:
        good("archive table: %d headers, %d cells, colspan %d - consistent"
             % (n, ntd, n))

# The comparison table: headers come from COLS plus a Trend column.
cols = re.search(r"const COLS=\[(.*?)\];", html, re.S)
if cols:
    ncol = cols.group(1).count("[\"") + 1  # + the Trend column
    gap = re.search(r'group-space"><tr><td colspan="(\d+)"', html)
    if gap and int(gap.group(1)) != ncol:
        fail("COMPARISON TABLE: %d columns but the spacer row spans %s"
             % (ncol, gap.group(1)))
    else:
        good("comparison table: %d columns, spacer row matches" % ncol)

# ------------------------------------------------------ 5. chart data path
# Every card canvas is keyed by data-sig and read out of labPlans. If the
# renderer stops writing that key the chart goes blank - which is the exact
# regression this harness was written after.
if 'labPlans[key]={' not in js:
    fail("the lab no longer writes labPlans[key] - card charts will be blank")
elif 'labPlans[cv.dataset.sig]' not in js:
    fail("paintSetupCharts no longer reads labPlans - card charts will be "
         "blank")
else:
    keyw = re.search(r"const key=sig\.symbol\+\"\|\"\+sig\.tf\+\"\|\"\+sig\.side",
                     js)
    dsig = re.search(r'data-sig="\'\+esc\(key\)', js) or \
        re.search(r'data-sig="\'\+esc\(key\)\+\'"', js)
    if not keyw:
        fail("the lab's card key is no longer symbol|tf|side - the chart "
             "lookup will miss")
    else:
        good("card charts: key written and read through labPlans")

# --------------------------------------------------- 6. shipped ledger
lp = os.path.join(ROOT, "brief", "signals.json")
if os.path.isfile(lp):
    try:
        led = json.load(open(lp, encoding="utf-8"))
        recs = led.get("records") or []
        noref = [r.get("id") for r in recs if not r.get("ref")]
        if noref:
            note.append("%d ledger record(s) have no reference yet - the "
                        "page computes one for them, the next run files it"
                        % len(noref))
        else:
            good("all %d ledger record(s) carry a reference" % len(recs))
        opens = [r for r in recs
                 if r.get("status") in ("pending", "active")]
        good("ledger: %d record(s), %d still open" % (len(recs), len(opens)))

        # Every ledger field the PAGE reads must exist on a real record.
        # The page once asked for r.state while the pipeline writes
        # r.status - the read silently returned undefined and an open
        # position on an off-grid market never got a price. A field name
        # that does not exist is not a typo you see, it is a feature that
        # quietly does nothing.
        if recs:
            have = set(recs[0].keys())
            # Only scan blocks that are demonstrably iterating ledger
            # records. A page-wide scan for "r." is meaningless - r is a
            # row, a rect and a range elsewhere.
            blocks = []
            for m in re.finditer(
                    r"(ledger\s*&&\s*ledger\.records|ledger\.records|"
                    r"archiveRecords\(\)|recordLive\()", js):
                blocks.append(js[max(0, m.start() - 200):m.start() + 2500])
            # Fields the browser computes and attaches to a record itself.
            derived = {"live", "liveR", "liveMae", "liveMfe", "liveNote",
                       "settled", "mine"}
            read = set()
            for b in blocks:
                read |= set(re.findall(r"\brec\.([a-z_][a-zA-Z0-9_]*)", b))
                read |= set(re.findall(r"\brecord\.([a-z_][a-zA-Z0-9_]*)", b))
                # r.x only where r is bound by a records iteration
                for it in re.finditer(
                        r"(?:records|recs)[^\n]{0,40}?"
                        r"(?:forEach|map|find|filter|some|every)\("
                        r"\s*(\w+)\s*=>", b):
                    v = it.group(1)
                    read |= set(re.findall(
                        r"\b" + re.escape(v) + r"\.([a-z_][a-zA-Z0-9_]*)", b))
            ghost = sorted(f for f in read
                           if f not in have and f not in derived)
            if ghost:
                fail("the page reads ledger field(s) that no record has: %s "
                     "- these silently read undefined"
                     % ", ".join(ghost))
            else:
                good("every ledger field the page reads (%d of them) exists "
                     "on a real record" % len(read))
    except ValueError as e:
        fail("brief/signals.json is not valid JSON: %s" % e)
else:
    note.append("brief/signals.json not present yet")

# ------------------------------------------------------------- 7. circles
# The confidence ring is a circle. It stopped being one the moment it was
# allowed to shrink inside a flex row, so both guards are asserted here.
conf = re.search(r"\.confidence\{([^}]*)\}", css)
if not conf:
    fail(".confidence rule not found")
else:
    body = conf.group(1)
    if "aspect-ratio:1/1" not in body.replace(" ", ""):
        fail(".confidence has no aspect-ratio - it can be squashed oval")
    elif not re.search(r"\.setup-card-top>\.confidence[^{]*\{[^}]*flex:0 0 auto",
                       css):
        fail(".confidence is not flex:0 0 auto in the card row - it will go "
             "oval when the row runs out of width")
    else:
        good("confidence ring is pinned circular (aspect-ratio + flex basis)")

# ------------------------------------------------------- 8. no transition
# The theme bug. body carried transition:background,color, the render loop
# restarted both every second, and they sat at currentTime 0 forever - so
# body painted the OLD theme while every child painted the new one. Nothing
# on body may transition while a repaint loop exists.
for m in re.finditer(r"(?<![\w-])body\s*\{([^}]*)\}", css):
    if "transition" in m.group(1):
        fail("body has a transition (%s) - the repaint loop will freeze it "
             "at frame zero and the theme will half-apply"
             % m.group(1).strip()[:80])
        break
else:
    good("body has no transition - theme switches apply whole")

# ------------------------------------------------- 9. live p/l on open cards
if 'data-lpnl="' not in js:
    fail("open cards no longer carry the live P/L block")
elif "paintLivePnl()" not in js:
    fail("paintLivePnl is never called - the live P/L would never update")
else:
    inpaint = re.search(r"function paintPrices\(\)\{.*?\n\}", js, re.S)
    if not inpaint or "paintLivePnl()" not in inpaint.group(0):
        fail("paintLivePnl is not called from paintPrices - the live P/L "
             "would not update on the price tick")
    else:
        cells = re.search(r'data-lpnl="\'\+esc\(key\)\+\'">(.*?)</div>\'', js, re.S)
        want = ["lp1", "lp2", "lp3", "lp1b", "lp2b", "lp3b"]
        miss = [c for c in want
                if ('class="%s"' % c) not in js]
        if miss:
            fail("live P/L block is missing cell(s): %s" % ", ".join(miss))
        else:
            good("live P/L: 3 cells rendered on open cards and repainted on "
                 "every price tick")

# -------------------------------------------------------------- report
print("=" * 70)
for line in ok:
    print("  ok    " + line)
for line in note:
    print("  note  " + line)
for line in bad:
    print("  FAIL  " + line)
print("=" * 70)
if bad:
    print("\n%d problem(s).\n" % len(bad))
    sys.exit(1)
print("\nAll %d checks passed.\n" % len(ok))
sys.exit(0)
