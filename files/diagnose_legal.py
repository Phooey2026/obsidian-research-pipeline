#!/usr/bin/env python3
"""
Legal Proceedings Section Finder
Diagnoses why get_legal_proceedings failed for specific tickers by hunting
through the actual 10-K/20-F filing structure on SEC EDGAR.

Usage:
    python3 diagnose_legal.py                   # test all FLAGGED tickers
    python3 diagnose_legal.py AMD DUK CB        # test specific tickers
    python3 diagnose_legal.py --all             # test all tickers in watchlist
    python3 diagnose_legal.py --failed          # read flagged list from legal_test_latest.json

Output: per-ticker report showing exactly where legal content is (or isn't),
        plus a JSON file with full findings.
"""

import sys
import os
import re
import json
import time
import urllib.request
import urllib.error
from datetime import datetime
from html.parser import HTMLParser

# ─── Configuration ────────────────────────────────────────────────────────────
WATCHLIST    = "/home/shogun/stock_dashboard/watchlist.json"
RESULTS_DIR  = "/home/shogun/stock_dashboard/data"
LOG_FILE     = f"{RESULTS_DIR}/legal_test_latest.json"
OUT_FILE     = f"{RESULTS_DIR}/legal_diagnose_{datetime.now().strftime('%Y%m%d_%H%M')}.json"

# Default tickers to diagnose (the 5 flagged from the last run)
DEFAULT_FLAGGED = ["AMD", "DUK", "CB", "ELV", "RIO"]

EDGAR_HEADERS = {
    "User-Agent": "stock-dashboard-diagnostic jay@example.com",
    "Accept-Encoding": "gzip, deflate",
}

# Anchors and text patterns used to locate legal proceedings sections
LEGAL_ANCHORS = [
    "legal-proceedings",
    "legalproceedings",
    "legal_proceedings",
    "item3",
    "item-3",
    "item_3",
]

LEGAL_TEXT_PATTERNS = [
    r'item\s+3[\.\s]*legal\s+proceedings',
    r'legal\s+proceedings',
    r'note\s+\d+[\.\s]*legal\s+proceedings',
    r'commitments\s+and\s+contingencies',
]

# ─── HTTP Helpers ─────────────────────────────────────────────────────────────
def fetch_url(url: str, retries: int = 3) -> str | None:
    """Fetch a URL with retries and return text content."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=EDGAR_HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                # Handle gzip
                if resp.info().get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 10 * (attempt + 1)
                print(f"      Rate limited — waiting {wait}s...")
                time.sleep(wait)
            elif e.code == 404:
                return None
            else:
                print(f"      HTTP {e.code} on attempt {attempt+1}")
                time.sleep(3)
        except Exception as e:
            print(f"      Fetch error attempt {attempt+1}: {e}")
            time.sleep(3)
    return None


# ─── EDGAR Filing Locator ─────────────────────────────────────────────────────
def get_cik(ticker: str) -> str | None:
    """Resolve ticker to CIK via EDGAR company search."""
    url = f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&startdt=2024-01-01&forms=10-K,20-F"
    # Use the simpler ticker lookup endpoint
    url = f"https://www.sec.gov/cgi-bin/browse-edgar?company=&CIK={ticker}&type=10-K&dateb=&owner=include&count=5&search_text=&action=getcompany&output=atom"
    content = fetch_url(url)
    if not content:
        # Try 20-F
        url = url.replace("10-K", "20-F")
        content = fetch_url(url)
    if not content:
        return None

    # Extract CIK from the atom feed
    m = re.search(r'/cgi-bin/browse-edgar\?action=getcompany&CIK=(\d+)', content)
    if m:
        return m.group(1).lstrip("0")
    return None


def get_latest_filing(ticker: str) -> dict | None:
    """
    Find the most recent 10-K or 20-F for a ticker.
    Returns dict with: cik, form_type, accession_number, filing_date, index_url
    """
    for form_type in ("10-K", "20-F"):
        url = (
            f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22"
            f"&forms={form_type}&dateRange=custom&startdt=2023-01-01"
        )
        # Better: use the submissions API which is reliable
        break

    # Use the EDGAR full-text search submissions approach
    # First get CIK from the company search
    search_url = (
        f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22"
        f"&forms=10-K,20-F&dateRange=custom&startdt=2023-01-01&hits.hits._source=period_of_report"
    )

    # Most reliable: EDGAR company search → submissions JSON
    cik = resolve_cik(ticker)
    if not cik:
        return None

    sub_url = f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
    content = fetch_url(sub_url)
    if not content:
        return None

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None

    filings = data.get("filings", {}).get("recent", {})
    forms   = filings.get("form", [])
    accnums = filings.get("accessionNumber", [])
    dates   = filings.get("filingDate", [])

    # Find the most recent 10-K or 20-F
    for form, acc, date in zip(forms, accnums, dates):
        if form in ("10-K", "20-F", "10-K/A", "20-F/A"):
            acc_clean = acc.replace("-", "")
            return {
                "cik": cik,
                "form_type": form,
                "accession_number": acc,
                "accession_clean": acc_clean,
                "filing_date": date,
                "index_url": (
                    f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                    f"{acc_clean}/{acc}-index.htm"
                ),
                "index_json": (
                    f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
                ),
            }
    return None


def resolve_cik(ticker: str) -> str | None:
    """Resolve ticker → zero-padded CIK using EDGAR company search."""
    # Try the ticker lookup JSON (fastest)
    url = "https://www.sec.gov/files/company_tickers.json"
    content = fetch_url(url)
    if content:
        try:
            data = json.loads(content)
            for entry in data.values():
                if entry.get("ticker", "").upper() == ticker.upper():
                    return str(entry["cik_str"])
        except Exception:
            pass

    # Fallback: EDGAR CIK search
    url = (
        f"https://www.sec.gov/cgi-bin/browse-edgar?company=&CIK={ticker}"
        f"&type=10-K&dateb=&owner=include&count=5&search_text=&action=getcompany"
    )
    content = fetch_url(url)
    if content:
        m = re.search(r'CIK=(\d+)&amp;type', content)
        if m:
            return m.group(1)
    return None


def get_filing_documents(cik: str, acc_clean: str) -> list[dict]:
    """Fetch the filing index and return list of documents."""
    idx_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/"
        f"{acc_clean}/{acc_clean[:10]}-{acc_clean[10:12]}-{acc_clean[12:]}-index.json"
    )
    # Construct accession with dashes
    acc_dashed = f"{acc_clean[:10]}-{acc_clean[10:12]}-{acc_clean[12:]}"
    idx_url = (
        f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
    )
    # Use the filing detail index
    detail_url = (
        f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        f"&CIK={cik}&type=10-K&dateb=&owner=include&count=5&search_text="
    )

    # Fetch the index HTML directly
    acc_dashed = f"{acc_clean[:10]}-{acc_clean[10:12]}-{acc_clean[12:]}"
    index_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/"
        f"{acc_clean}/{acc_dashed}-index.htm"
    )
    content = fetch_url(index_url)
    if not content:
        return []

    # Parse document links
    docs = []
    pattern = re.compile(
        r'<td[^>]*>\s*<a href="(/Archives/edgar/data/[^"]+\.(htm|html|xml))"[^>]*>([^<]+)</a>\s*</td>',
        re.IGNORECASE
    )
    type_pattern = re.compile(
        r'<td[^>]*>(10-K|20-F|10-K/A|20-F/A|EX-\d+|GRAPHIC|XML|[A-Z/-]+)</td>',
        re.IGNORECASE
    )

    # Simpler: find all href links in the table
    links = re.findall(
        r'href="(/Archives/edgar/data/[^"]+\.(?:htm|html))"',
        content, re.IGNORECASE
    )
    for link in links:
        docs.append({
            "url": f"https://www.sec.gov{link}",
            "filename": link.split("/")[-1],
        })

    return docs


# ─── Section Hunter ───────────────────────────────────────────────────────────
class AnchorFinder(HTMLParser):
    """Collect all id= and name= anchor values from an HTML document."""
    def __init__(self):
        super().__init__()
        self.anchors = []
        self.toc_links = []      # hrefs that look like internal TOC links
        self.text_buffer = []
        self.in_body = False

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        anchor_id = attr_dict.get("id") or attr_dict.get("name")
        if anchor_id:
            self.anchors.append(anchor_id.lower().strip())
        href = attr_dict.get("href", "")
        if href.startswith("#"):
            self.toc_links.append(href[1:].lower().strip())


def find_legal_anchors_in_doc(html: str) -> dict:
    """
    Scan an HTML document for legal proceedings anchors and text patterns.
    Returns a findings dict.
    """
    findings = {
        "anchor_matches": [],       # id/name anchors matching LEGAL_ANCHORS
        "toc_link_matches": [],     # TOC links matching LEGAL_ANCHORS
        "text_pattern_matches": [], # regex matches in visible text
        "context_snippets": [],     # 200-char snippets around each match
        "char_count": len(html),
        "is_htm": True,
    }

    # 1. Anchor scan
    parser = AnchorFinder()
    try:
        parser.feed(html)
    except Exception:
        pass

    for anchor in parser.anchors:
        for pattern in LEGAL_ANCHORS:
            if pattern in anchor:
                findings["anchor_matches"].append(anchor)
                break

    for link in parser.toc_links:
        for pattern in LEGAL_ANCHORS:
            if pattern in link:
                findings["toc_link_matches"].append(link)
                break

    # 2. Text pattern scan (on raw HTML — fast and catches most cases)
    html_lower = html.lower()
    for pattern in LEGAL_TEXT_PATTERNS:
        for m in re.finditer(pattern, html_lower):
            start = max(0, m.start() - 100)
            end   = min(len(html), m.end() + 200)
            snippet = html[start:end]
            # Strip tags for readability
            snippet_clean = re.sub(r'<[^>]+>', ' ', snippet)
            snippet_clean = re.sub(r'\s+', ' ', snippet_clean).strip()
            findings["text_pattern_matches"].append({
                "pattern": pattern,
                "position": m.start(),
                "snippet": snippet_clean[:300],
            })

    return findings


def find_legal_in_xbrl(html: str) -> dict:
    """
    For iXBRL inline documents, look for specific XBRL context tags
    that label legal proceedings content.
    """
    findings = {"xbrl_tags": []}
    xbrl_pattern = re.compile(
        r'<ix:[a-z]+[^>]*contextRef="[^"]*"[^>]*name="[^"]*[Ll]egal[^"]*"[^>]*>',
        re.IGNORECASE
    )
    for m in xbrl_pattern.finditer(html):
        findings["xbrl_tags"].append(m.group()[:200])

    # Also look for us-gaap:LegalMatters* context tags
    gaap_pattern = re.compile(
        r'name="us-gaap:(Legal|Commitments|Contingenc)[^"]*"',
        re.IGNORECASE
    )
    for m in gaap_pattern.finditer(html):
        findings["xbrl_tags"].append(m.group()[:200])

    return findings


# ─── Per-Ticker Diagnosis ─────────────────────────────────────────────────────
def diagnose_ticker(ticker: str) -> dict:
    """
    Full diagnosis pipeline for one ticker.
    Returns a structured findings dict.
    """
    print(f"\n  ┌─ {ticker} {'─'*(50-len(ticker))}")
    result = {
        "ticker": ticker,
        "filing": None,
        "documents": [],
        "legal_findings": [],
        "recommendation": "",
        "success": False,
    }

    # 1. Resolve CIK
    print(f"  │  Resolving CIK...", end=" ", flush=True)
    cik = resolve_cik(ticker)
    if not cik:
        print("✗ FAILED")
        result["recommendation"] = "CIK not found — ticker may be foreign or delisted"
        return result
    print(f"✓ CIK={cik}")
    time.sleep(0.5)

    # 2. Get latest 10-K or 20-F
    print(f"  │  Finding latest 10-K/20-F...", end=" ", flush=True)
    filing = get_latest_filing(ticker)
    if not filing:
        print("✗ FAILED")
        result["recommendation"] = "No 10-K or 20-F found in EDGAR submissions"
        return result
    print(f"✓ {filing['form_type']} filed {filing['filing_date']}")
    result["filing"] = {
        "form_type": filing["form_type"],
        "filing_date": filing["filing_date"],
        "accession": filing["accession_number"],
        "index_url": filing["index_url"],
    }
    time.sleep(0.5)

    # 3. Get filing document list
    print(f"  │  Fetching filing index...", end=" ", flush=True)
    docs = get_filing_documents(filing["cik"], filing["accession_clean"])
    print(f"✓ {len(docs)} document(s) found")

    # Filter to main filing documents (skip exhibits, graphics)
    main_docs = [
        d for d in docs
        if not any(skip in d["filename"].lower()
                   for skip in ["ex-", "ex2", "ex3", "ex4", "graphic",
                                "logo", "signature", "r1.", "r2.", "r3."])
    ]
    print(f"  │  Scanning {len(main_docs)} main document(s)...")
    time.sleep(0.5)

    # 4. Scan each main document
    best_match = None
    best_score = 0

    for doc in main_docs[:8]:  # cap at 8 to avoid runaway
        url      = doc["url"]
        filename = doc["filename"]
        print(f"  │    → {filename[:55]:<55}", end=" ", flush=True)

        content = fetch_url(url)
        if not content:
            print("✗ fetch failed")
            continue

        findings  = find_legal_anchors_in_doc(content)
        xbrl_info = find_legal_in_xbrl(content)

        n_anchors  = len(findings["anchor_matches"])
        n_toc      = len(findings["toc_link_matches"])
        n_text     = len(findings["text_pattern_matches"])
        n_xbrl     = len(xbrl_info["xbrl_tags"])
        chars      = findings["char_count"]

        score = n_anchors * 10 + n_toc * 5 + n_text * 3 + n_xbrl * 2
        status_parts = []
        if n_anchors: status_parts.append(f"{n_anchors} anchors")
        if n_toc:     status_parts.append(f"{n_toc} TOC links")
        if n_text:    status_parts.append(f"{n_text} text hits")
        if n_xbrl:    status_parts.append(f"{n_xbrl} XBRL tags")
        status = ", ".join(status_parts) if status_parts else "no legal markers"
        print(f"{chars:>9,} chars  {status}")

        doc_result = {
            "url": url,
            "filename": filename,
            "char_count": chars,
            "score": score,
            "anchor_matches": findings["anchor_matches"],
            "toc_link_matches": findings["toc_link_matches"],
            "text_pattern_count": n_text,
            "text_patterns": findings["text_pattern_matches"][:5],
            "xbrl_tags": xbrl_info["xbrl_tags"][:5],
        }
        result["documents"].append(doc_result)
        result["legal_findings"].append(doc_result)

        if score > best_score:
            best_score = score
            best_match = doc_result

        time.sleep(1.5)  # be polite to EDGAR

    # 5. Recommend a fix
    if best_match and best_score > 0:
        result["success"] = True
        anchors = best_match["anchor_matches"] or best_match["toc_link_matches"]
        anchor_hint = f" — try anchor: #{anchors[0]}" if anchors else ""
        result["recommendation"] = (
            f"Legal content found in {best_match['filename']}"
            f" (score={best_score}){anchor_hint}. "
            f"Text patterns: {best_match['text_pattern_count']} hits."
        )
        print(f"  │")
        print(f"  │  ✓ FOUND in: {best_match['filename']}")
        if anchors:
            print(f"  │    Anchors: {', '.join(anchors[:5])}")
        if best_match["text_patterns"]:
            print(f"  │    First text hit: {best_match['text_patterns'][0]['snippet'][:120]}")
    else:
        result["recommendation"] = (
            "No legal proceedings section found in any scanned document. "
            "Filing may use a non-standard structure, be a 20-F with different layout, "
            "or content may be in an exhibit."
        )
        print(f"  │")
        print(f"  │  ⚠️  No legal proceedings markers found in any document")

    print(f"  └{'─'*52}")
    return result


# ─── Summary Reporter ─────────────────────────────────────────────────────────
def print_diagnosis_summary(all_results: list):
    print(f"\n{'═'*60}")
    print(f"  Legal Section Diagnosis Summary")
    print(f"{'═'*60}")

    found    = [r for r in all_results if r["success"]]
    notfound = [r for r in all_results if not r["success"]]

    print(f"  Total diagnosed: {len(all_results)}")
    print(f"  ✓  Section found:     {len(found)}")
    print(f"  ⚠️  Section not found: {len(notfound)}")

    if found:
        print(f"\n{'─'*60}")
        print(f"  ✓  FOUND — Recommended fix:")
        print(f"{'─'*60}")
        for r in found:
            filing = r.get("filing", {})
            print(f"\n  {r['ticker']:<6}  {filing.get('form_type','?')} "
                  f"({filing.get('filing_date','?')})")
            print(f"    {r['recommendation']}")
            # Show top anchors
            best = max(r["documents"], key=lambda d: d["score"], default=None)
            if best:
                anchors = best["anchor_matches"] or best["toc_link_matches"]
                if anchors:
                    print(f"    Anchors: {anchors}")
                print(f"    URL: {best['url']}")
                for tp in best["text_patterns"][:2]:
                    print(f"    Pattern '{tp['pattern']}' at pos {tp['position']:,}")
                    print(f"      → {tp['snippet'][:160]}")

    if notfound:
        print(f"\n{'─'*60}")
        print(f"  ⚠️  NOT FOUND — Needs manual investigation:")
        print(f"{'─'*60}")
        for r in notfound:
            filing = r.get("filing", {})
            print(f"\n  {r['ticker']:<6}  {filing.get('form_type','?') if filing else 'NO FILING'} "
                  f"({filing.get('filing_date','?') if filing else 'N/A'})")
            print(f"    {r['recommendation']}")
            if filing:
                print(f"    Index: {filing.get('index_url','')}")

    print(f"\n  Results saved to: {OUT_FILE}")
    print(f"{'═'*60}\n")


# ─── Entry Point ──────────────────────────────────────────────────────────────
def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)

    args = sys.argv[1:]

    # Determine tickers to diagnose
    if "--failed" in args:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE) as f:
                prev = json.load(f)
            tickers = prev.get("flagged", DEFAULT_FLAGGED)
            print(f"Loaded {len(tickers)} flagged tickers from {LOG_FILE}")
        else:
            print(f"No log file found at {LOG_FILE}, using defaults")
            tickers = DEFAULT_FLAGGED

    elif "--all" in args:
        with open(WATCHLIST) as f:
            wl = json.load(f)
        tickers = []
        for sector_tickers in wl.get("sectors", {}).values():
            tickers.extend(sector_tickers)
        print(f"Testing all {len(tickers)} tickers from watchlist")

    elif args:
        tickers = [t.upper() for t in args if not t.startswith("--")]

    else:
        tickers = DEFAULT_FLAGGED
        print(f"No args — diagnosing default flagged tickers: {tickers}")

    print(f"\n{'═'*60}")
    print(f"  Legal Proceedings Section Finder")
    print(f"  Diagnosing {len(tickers)} ticker(s)")
    print(f"{'═'*60}")

    all_results = []
    for ticker in tickers:
        result = diagnose_ticker(ticker)
        all_results.append(result)
        time.sleep(2)  # inter-ticker pause

    print_diagnosis_summary(all_results)

    # Save results
    with open(OUT_FILE, "w") as f:
        json.dump({
            "run_date": datetime.now().isoformat(),
            "tickers": tickers,
            "results": all_results,
        }, f, indent=2)


if __name__ == "__main__":
    main()
