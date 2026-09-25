"""Average score_sample.py runs: python scripts/compare_scores.py <dir with before_run{1..3}.txt and after_run{1..3}.txt>"""
import re
import statistics
import sys
from collections import defaultdict

TMP = sys.argv[1]
LINE = re.compile(r"^\[([a-z_]+)\] (placeholder|real payload)\s+total=(\d+)/50\s+spec=(\d+) cat=(\d+) merch=(\d+) why=(\d+) eng=(\d+)(.*)$")


def load(prefix, n=3):
    runs = []
    for i in range(1, n + 1):
        rows = []
        try:
            text = open(f"{TMP}/{prefix}_run{i}.txt", encoding="utf-8", errors="ignore").read()
        except FileNotFoundError:
            continue
        text = re.sub(r"\x1b\[[0-9;]*m", "", text)
        for line in text.splitlines():
            m = LINE.match(line.strip())
            if m and "JUDGE FAILED" not in m.group(9):
                k, ph, tot, sp, ca, me, wh, en = m.group(1), m.group(2), *(int(x) for x in m.groups()[2:8])
                rows.append({"key": (k, ph), "total": tot, "spec": sp, "cat": ca, "merch": me, "why": wh, "eng": en})
        runs.append(rows)
    return runs


def avg(rows, f):
    return sum(r[f] for r in rows) / len(rows) if rows else float("nan")


before, after = load("before"), load("after")
print(f"runs: before={len(before)} after={len(after)}")
for name, runs in (("BEFORE", before), ("AFTER", after)):
    per_run = [avg(r, "total") for r in runs]
    print(f"{name}: per-run totals {[round(x, 1) for x in per_run]}  mean={statistics.mean(per_run):.1f}  judged/run={[len(r) for r in runs]}")
    for f in ("spec", "cat", "merch", "why", "eng"):
        print(f"   {f:<6} {statistics.mean(avg(r, f) for r in runs):.2f}")

common = {r["key"] for run in before for r in run} & {r["key"] for run in after for r in run}
print(f"\nLike-for-like on {len(common)} trigger kinds judged in both sets (before used strict consent, so fewer customer triggers):")
def sel(runs):
    return [[r for r in run if r["key"] in common] for run in runs]
for name, runs in (("BEFORE", sel(before)), ("AFTER", sel(after))):
    per_run = [avg(r, "total") for r in runs]
    print(f"  {name}: mean={statistics.mean(per_run):.1f}  per-run={[round(x, 1) for x in per_run]}")

print("\nPer kind (mean total /50 over runs):")
by = defaultdict(lambda: {"b": [], "a": []})
for run in before:
    for r in run:
        by[r["key"]]["b"].append(r["total"])
for run in after:
    for r in run:
        by[r["key"]]["a"].append(r["total"])
for k in sorted(by, key=lambda k: k[0]):
    b, a = by[k]["b"], by[k]["a"]
    bs = f"{statistics.mean(b):5.1f}" if b else "  n/a"
    as_ = f"{statistics.mean(a):5.1f}" if a else "  n/a"
    print(f"  {k[0]:<26}{k[1]:<13} before {bs}   after {as_}")
