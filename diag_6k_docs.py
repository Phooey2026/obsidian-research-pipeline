#!/usr/bin/env python3
"""diag_6k_docs.py -- why was every 6-K from a given date rejected?

    python3 diag_6k_docs.py <CIK> <YYYY-MM-DD>
    e.g.  python3 diag_6k_docs.py 811809 2026-08-19      (BHP FY26 results)

Lists every 6-K the company filed on that date, every document in each filing, and for
each .htm/.html document up to 9 MB: size, text length, what _strip_6k_cover cut, the
financial terms / period words the results gate looks for, and the first 400 characters.
"""
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mcp'))
import app3  # noqa: E402

cik, day = sys.argv[1].zfill(10), sys.argv[2]
HDR = {'User-Agent': 'Jay jay@example.com'}


def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=120).read()


sub = json.loads(get("https://data.sec.gov/submissions/CIK" + cik + ".json"))
rec = sub['filings']['recent']
rows = [(rec['accessionNumber'][i], rec['primaryDocument'][i]) for i in range(len(rec['form']))
        if rec['form'][i] == '6-K' and rec['filingDate'][i] == day]
print("6-K filings on " + day + ": " + str(len(rows)))
FIN = ('net income', 'net profit', 'profit', 'ebitda', 'earnings per', 'eps',
       'operating income', 'net sales', 'revenue', 'net earnings')
PERIOD = (r'(first|second|third|fourth|1st|2nd|3rd|4th)[- ]quarter|\bq[1-4]\b|'
          r'\b[1-4]q\s?\d{2,4}\b|\b[12]h\s?\d{2,4}\b|half[- ]year|(three|six|nine|twelve)[- ]months?|'
          r'full[- ]year|(year|quarter|period)s? ended|fiscal (year|quarter)|annual results|interim')
for accn, primary in rows:
    base = "https://www.sec.gov/Archives/edgar/data/" + str(int(cik)) + "/" + accn.replace('-', '') + "/"
    print("\n" + "=" * 70 + "\n" + accn + "  primary=" + primary)
    try:
        items = json.loads(get(base + 'index.json'))['directory']['item']
    except Exception as e:
        print("  index.json failed: " + str(e))
        continue
    for it in items:
        n, sz = it.get('name', ''), int(it.get('size') or 0)
        print("  file " + n + "  " + format(sz, ','))
    for it in items:
        n, sz = it.get('name', ''), int(it.get('size') or 0)
        if not n.lower().endswith(('.htm', '.html')) or 'index' in n.lower() or sz > 9_000_000:
            continue
        raw = get(base + n)
        text = app3._sec_doc_to_text(raw, n)
        stripped = app3._strip_6k_cover(text)
        body = stripped[:8000].lower()
        fin = [t for t in FIN if t in body]
        per = re.search(PERIOD, stripped[:3000].lower())
        print("\n  -- " + n + ": text " + format(len(text), ',') + " chars, cover strip cut "
              + format(len(text) - len(stripped), ',') + ", stripped " + format(len(stripped), ','))
        print("     financial terms in first 8000: " + str(fin))
        print("     period word in first 3000: " + repr(per.group() if per else None))
        print("     starts: " + repr(stripped[:400]))
