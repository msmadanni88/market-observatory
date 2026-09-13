#!/usr/bin/env python3
# ===========================================================================
# PREFLIGHT
# ---------------------------------------------------------------------------
# Run this before creating the repository. It checks structure, syntax,
# imports and config so a broken setup is caught locally instead of as a
# red cross in the Actions tab twenty minutes later.
#
#   python preflight.py
#
# Exit code 0 means safe to push.
# ===========================================================================

import ast
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

REQUIRED = [
    "daily_brief.py",
    "signal_engine.py",
    "snapshot_builder.py",
    "universe_scan.py",
    "platforms.py",
    "kumo_gaps.py",
    "index.html",
    "positions.json",
    "requirements.txt",
    ".github/workflows/daily-brief.yml",
]

OPTIONAL = ["README.md", ".gitignore"]

ok, warn, bad = [], [], []


def check(cond, good, msg):
    (ok if cond else bad).append(good if cond else msg)
    return cond


def note(msg):
    warn.append(msg)


# ---------------------------------------------------------------- structure
print("checking structure ...")
missing = []
for rel in REQUIRED:
    p = os.path.join(ROOT, rel)
    if os.path.isfile(p):
        ok.append(f"{rel}")
    else:
        missing.append(rel)
        bad.append(f"MISSING  {rel}")

for rel in OPTIONAL:
    if not os.path.isfile(os.path.join(ROOT, rel)):
        note(f"optional file absent: {rel}")

# The workflow path is the one people get wrong. Say so explicitly.
wf = os.path.join(ROOT, ".github", "workflows", "daily-brief.yml")
if not os.path.isfile(wf):
    stray = None
    for dirpath, _, files in os.walk(ROOT):
        if "daily-brief.yml" in files:
            stray = os.path.join(dirpath, "daily-brief.yml")
            break
    if stray:
        bad.append(f"daily-brief.yml found at {os.path.relpath(stray, ROOT)} "
                   f"but GitHub only reads .github/workflows/ - move it")

# ---------------------------------------------------------------- syntax
print("checking python syntax ...")
for rel in REQUIRED:
    if not rel.endswith(".py"):
        continue
    p = os.path.join(ROOT, rel)
    if not os.path.isfile(p):
        continue
    try:
        ast.parse(open(p, encoding="utf-8").read())
        ok.append(f"syntax {rel}")
    except SyntaxError as e:
        bad.append(f"SYNTAX   {rel} line {e.lineno}: {e.msg}")

# ---------------------------------------------------------------- json
print("checking json ...")
pj = os.path.join(ROOT, "positions.json")
if os.path.isfile(pj):
    try:
        data = json.load(open(pj, encoding="utf-8"))
        if not isinstance(data, list):
            bad.append("positions.json must be a list, even if empty")
        else:
            ok.append(f"positions.json  {len(data)} position(s)")
            for i, p in enumerate(data):
                for key in ("symbol", "side", "entry"):
                    if key not in p:
                        bad.append(f"positions.json[{i}] missing '{key}'")
                if p.get("side") not in ("long", "short"):
                    bad.append(f"positions.json[{i}] side must be "
                               f"long or short")
                if "sl" in p and "entry" in p:
                    e, s = float(p["entry"]), float(p["sl"])
                    if p.get("side") == "long" and s >= e:
                        bad.append(f"positions.json[{i}] long stop is at or "
                                   f"above entry")
                    if p.get("side") == "short" and s <= e:
                        bad.append(f"positions.json[{i}] short stop is at or "
                                   f"below entry")
    except ValueError as e:
        bad.append(f"positions.json is not valid JSON: {e}")

# ---------------------------------------------------------------- yaml
print("checking workflow ...")
if os.path.isfile(wf):
    txt = open(wf, encoding="utf-8").read()
    for needle, label in [
        ("TELEGRAM_BOT_TOKEN", "telegram token wired"),
        ("ANTHROPIC_API_KEY", "api key wired"),
        ("schedule:", "schedule present"),
        ("workflow_dispatch", "manual trigger present"),
        ("contents: write", "commit permission present"),
    ]:
        check(needle in txt, label, f"workflow missing '{needle}'")

# ---------------------------------------------------------------- imports
print("checking imports ...")
try:
    import requests  # noqa: F401
    ok.append("requests installed")
except ImportError:
    bad.append("requests not installed - run: pip install -r requirements.txt")

sys.path.insert(0, ROOT)
for mod in ("signal_engine", "platforms", "snapshot_builder"):
    try:
        __import__(mod)
        ok.append(f"import {mod}")
    except Exception as e:
        bad.append(f"IMPORT   {mod}: {type(e).__name__}: {e}")

# ---------------------------------------------------------------- config
print("checking risk config ...")
try:
    import signal_engine as se
    r = se.CFG["risk_pct"]
    mx = se.CFG["max_risk_pct"]
    if r > 2.0:
        bad.append(f"risk_pct is {r}% - above the 2% ceiling this system "
                   f"was sized for")
    elif r > mx:
        bad.append(f"risk_pct {r}% exceeds max_risk_pct {mx}%")
    else:
        ok.append(f"risk {r}% per trade, ceiling {mx}%")

    f = se.CFG["funding_hot"]
    if f < 0.01:
        bad.append(f"funding_hot is {f} - this schema stores funding as a "
                   f"PERCENT, so 0.01 means 0.01%. A decimal-fraction "
                   f"threshold flags every symbol as crowded.")
    else:
        ok.append(f"funding threshold {f}% (percent units, correct)")
except Exception as e:
    bad.append(f"could not read config: {e}")

# ---------------------------------------------------------------- secrets
print("checking for leaked secrets ...")
import re
TOKEN_RE = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")
KEY_RE = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")
for dirpath, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "brief",
                                            "universe", "node_modules")]
    for fn in files:
        if not fn.endswith((".py", ".json", ".yml", ".yaml", ".md", ".html")):
            continue
        p = os.path.join(dirpath, fn)
        try:
            txt = open(p, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        rel = os.path.relpath(p, ROOT)
        if TOKEN_RE.search(txt):
            bad.append(f"LEAK     a telegram bot token appears in {rel} - "
                       f"remove it and revoke the token")
        if KEY_RE.search(txt):
            bad.append(f"LEAK     an API key appears in {rel} - remove it "
                       f"and rotate the key")
ok.append("no hardcoded credentials found")

# ---------------------------------------------------------------- report
print()
print("=" * 68)
for line in ok:
    print(f"  ok    {line}")
for line in warn:
    print(f"  note  {line}")
for line in bad:
    print(f"  FAIL  {line}")
print("=" * 68)

if bad:
    print(f"\n{len(bad)} problem(s). Fix these before creating the "
          f"repository.\n")
    sys.exit(1)

print(f"\nAll {len(ok)} checks passed. Safe to push.\n")
sys.exit(0)
