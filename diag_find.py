#!/usr/bin/env python3
"""diag_find.py — look inside a 10-K the way the legal extractor sees it.

    python3 diag_find.py <filing URL> ["regex" [max_hits [context_chars]]]

Prints (1) the text of the last "Item 3. Legal Proceedings" heading that has
text after it (first 1,500 chars) and (2) every match of the regex with its
position in the cleaned document, % through the document, and surrounding
context.  Default regex finds note headings about litigation/contingencies.

Example (XOM, Nov 3 2026):
    python3 diag_find.py https://www.sec.gov/Archives/edgar/data/34088/000003408826000045/xom-20251231.htm
"""
import re
import sys
import urllib.request

url = sys.argv[1]
pattern = sys.argv[2] if len(sys.argv) > 2 else r'litigation and other contingencies|commitments and contingencies|contingent liabilities'
max_hits = int(sys.argv[3]) if len(sys.argv) > 3 else 25
ctx = int(sys.argv[4]) if len(sys.argv) > 4 else 160

req = urllib.request.Request(url, headers={'User-Agent': 'Jay jay@example.com'})
raw = urllib.request.urlopen(req, timeout=60).read().decode('utf-8', errors='ignore')
clean = re.sub(r'<[^>]+>', ' ', raw)
for a, b in (('&nbsp;', ' '), ('&amp;', '&'), ('&#160;', ' '), ('&#8226;', '•'),
             ('&#8211;', '-'), ('&#8217;', "'")):
    clean = clean.replace(a, b)
clean = re.sub(r'&#\d+;', ' ', clean)
clean = re.sub(r'\s+', ' ', clean)
n = len(clean)
print(f"document: {n:,} characters\n")

heads = [m.start() for m in re.finditer(r'(?i)ITEM\s+3[\.\s]+LEGAL\s+PROCEEDINGS', clean)]
print(f"== Item 3 headings: {len(heads)} found; showing the last one with real text ==")
if heads:
    pos = heads[-1]
    seg = clean[pos:pos + 1500]
    cut = re.search(r'(?i)\bitem\s+4\b', seg[40:])
    print(f"pos {pos:,} ({100 * pos // n}%)")
    print(seg[:40 + cut.start()] if cut else seg)
print()

print(f"== matches for /{pattern}/ ==")
for i, m in enumerate(re.finditer(pattern, clean, re.I)):
    if i >= max_hits:
        print(f"... more matches not shown (raise max_hits)")
        break
    s = max(0, m.start() - ctx)
    print(f"[{i}] pos {m.start():,} ({100 * m.start() // n}%): ...{clean[s:m.end() + ctx]}...")
    print()
