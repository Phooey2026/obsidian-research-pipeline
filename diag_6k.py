#!/usr/bin/env python3
"""
diag_6k.py — show what is inside a foreign filer's recent 6-Ks.

Usage:  python3 diag_6k.py BHP 2026-08-19 2026-08-19   # only 6-Ks filed ON that date
        python3 diag_6k.py VALE 2026-07-25 5           # 6-Ks on/after date, max 5

For each 6-K: filing date, accession, primary document, every file in the
filing with its size, and the first 300 characters of text of the largest
HTML file (so we can see whether the results release is a separate exhibit,
the main document, or a PDF/image we cannot read). Read-only; sec.gov only.
"""
import sys, re, json, urllib.request

UA = {"User-Agent": "Jay jay@example.com"}


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return r.read()


def text_of(raw):
    t = raw.decode("utf-8", "replace")
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def main(tk, since, arg3):
    exact = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(arg3)))
    limit = 99 if exact else int(arg3)
    tk = tk.upper()
    cands = [e for e in json.loads(get("https://www.sec.gov/files/company_tickers.json")).values()
             if e.get("ticker", "").upper() == tk]
    if not cands:
        sys.exit(f"{tk}: not in SEC ticker file")
    cik = str(cands[0]["cik_str"]).zfill(10)
    rec = json.loads(get(f"https://data.sec.gov/submissions/CIK{cik}.json"))["filings"]["recent"]
    shown = 0
    for i, form in enumerate(rec["form"]):
        if form != "6-K" or rec["filingDate"][i] < since:
            continue
        if exact and rec["filingDate"][i] != arg3:
            continue
        accn = rec["accessionNumber"][i]
        base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn.replace('-', '')}/"
        print("=" * 78)
        print(f"{tk} 6-K filed {rec['filingDate'][i]}  accession {accn}  primary {rec['primaryDocument'][i]}")
        try:
            items = json.loads(get(base + "index.json"))["directory"]["item"]
        except Exception as e:
            print(f"  index.json failed: {e}")
            continue
        files = [(it["name"], int(it.get("size") or 0)) for it in items
                 if not it["name"].endswith(("-index.html", "-index-headers.html"))
                 and not it["name"].endswith(".txt")]
        for n, sz in files:
            print(f"  {sz:>9,}  {n}")
        htm = sorted([f for f in files if f[0].lower().endswith((".htm", ".html"))],
                     key=lambda f: -f[1])
        if htm:
            try:
                t = text_of(get(base + htm[0][0]))
                print(f"  largest html text, {len(t):,} chars total")
                print("    [0:250]    ", t[:250])
                print("    [600:1700] ", t[600:1700])
                print("    [1700:2800]", t[1700:2800])
                m = re.search(r"rule\s*101\(b\)\(7\)", t, re.I)
                print("    101(b)(7) at", m.start() if m else None)
            except Exception as e:
                print(f"  could not read {htm[0][0]}: {e}")
        shown += 1
        if shown >= limit:
            break


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else 6)
