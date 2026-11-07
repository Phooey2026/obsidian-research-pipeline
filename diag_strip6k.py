#!/usr/bin/env python3
"""diag_strip6k.py — why did get_sec_earnings_release reject a 6-K as "no results headline"?

    python3 diag_strip6k.py <6-K document URL>

Runs the same steps as mcp/app3.py: document -> text, _strip_6k_cover, the results-
headline test.  Prints where the cover strip cut, what the text starts with afterwards,
and which words matched (or blocked) the headline test.

Example (Novo Nordisk Q3 2026 report, rejected on Nov 7 2026):
    python3 diag_strip6k.py https://www.sec.gov/Archives/edgar/data/353278/000162828026069805/caq32026.htm
"""
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mcp'))
import app3  # noqa: E402

url = sys.argv[1]
name = url.rsplit('/', 1)[-1]
req = urllib.request.Request(url, headers={'User-Agent': 'Jay jay@example.com'})
raw = urllib.request.urlopen(req, timeout=120).read()
text = app3._sec_doc_to_text(raw, name)
print(f"raw bytes: {len(raw):,}   text chars: {len(text):,}   newlines in text: {text.count(chr(10)):,}")

head = text[:9000]
print("\n== cover markers inside the first 9,000 chars ==")
for pat in (r'rule\s*101\(b\)\(7\)', r'form\s*40-f'):
    pos = [m.start() for m in re.finditer(pat, head, re.I)]
    print(f"  /{pat}/ at {pos[:12]}{' ...' if len(pos) > 12 else ''}")

stripped = app3._strip_6k_cover(text)
cut = len(text) - len(stripped)
print(f"\n== _strip_6k_cover cut {cut:,} chars; stripped text is {len(stripped):,} chars ==")
print("starts with:")
print("  " + repr(stripped[:500]))

top = stripped[:600].lower()
pos_re = r'results|earnings|operating profit|net (income|profit)|financial report|interim|quarter'
neg_re = r'annual report|sustainability|climate|form 20-f|integrated report'
mp, mn = re.search(pos_re, top), re.search(neg_re, top)
print("\n== headline test on the first 600 chars of the stripped text (old rule) ==")
print(f"  positive match: {mp.group() if mp else None!r}   negative match: {mn.group() if mn else None!r}")
verdict = 'STRONG' if (mp and not mn) else 'WEAK (this is the "no results headline" case)'
print("  => " + verdict)
top15 = stripped[:1500].lower()
mp15 = re.search(pos_re, top15)
print(f"  positive match in first 1,500 chars (new rule): {mp15.group() if mp15 else None!r}"
      + (f" at offset {mp15.start()}" if mp15 else ""))
