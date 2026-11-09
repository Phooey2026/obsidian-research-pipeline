#!/usr/bin/env python3
"""diag_stale.py TICKER [TICKER ...]  — show WHY jansky_review's stale-language matcher fires.

Loads STALE_LANGUAGE and _check_stale_language() from jansky_review.py WITHOUT running the
review, runs them over the newest research file's Jupiter summaries, and prints each hit with
the surrounding text, so false positives can be tuned from real context."""
import ast, glob, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "jansky_review.py"), encoding="utf-8").read()
tree = ast.parse(src)
wanted = []
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name == "_check_stale_language":
        wanted.append(ast.get_source_segment(src, node))
    if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "STALE_LANGUAGE" for t in node.targets):
        wanted.append(ast.get_source_segment(src, node))
ns = {"re": re}
exec("\n\n".join(reversed(wanted)), ns)
STALE = ns["STALE_LANGUAGE"]
check = ns["_check_stale_language"]

files = sorted(glob.glob(os.path.join(HERE, "data", "research_*.json")))
if not files:
    sys.exit("no data/research_*.json found")
rows = json.load(open(files[-1]))
print("research file:", os.path.basename(files[-1]))
tickers = [t.upper() for t in sys.argv[1:]]
for r in rows:
    if tickers and r.get("ticker", "").upper() not in tickers:
        continue
    summ = r.get("summary") or ""
    hits = check(summ)
    print("\n== %s  hits=%s" % (r.get("ticker"), hits))
    low = summ.lower()
    for ph in hits:
        # Print EVERY occurrence: the matcher may have fired on a later one, not the first.
        for k, m in enumerate(re.finditer(r"\b" + re.escape(ph.lower()) + r"\b", low), 1):
            a, b = max(0, m.start() - 160), min(len(summ), m.end() + 160)
            print("  [%s #%d] ...%s..." % (ph, k, summ[a:b].replace("\n", " ")))
