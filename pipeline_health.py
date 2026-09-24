#!/usr/bin/env python3
"""
pipeline_health.py — Obsidian Capital Weekly Pipeline Health Check
Jansky Recommendation #5: Automated pipeline health telemetry

Checks:
  1. API call success rates (from agent JSON outputs)
  2. Data staleness by agent (Nova records, Mercury data dates)
  3. FRED series validation (spot-check key series for current data)
  4. Atlas YAML schema compliance
  5. Jupiter summary completeness (verdict present, min length)
  6. Mercury data gap count

Outputs:
  pipeline_health.json    — machine-readable health report
  pipeline_health.md      — human-readable summary

Run manually or add to pipeline after jansky_review.py:
  python3 pipeline_health.py
"""

import json
import os
import datetime
import yaml
import re

BASE_DIR   = os.environ.get("STOCK_BASE_DIR", "/home/jay/stock_dashboard")
DATA_DIR   = f"{BASE_DIR}/data"
REPORT_DIR = f"{BASE_DIR}/reports"

# ─── Load Config ──────────────────────────────────────────────────────────────

def _load_config() -> dict:
    """Load obsidian_config.json for pipeline-wide constants."""
    config_path = os.path.join(BASE_DIR, "obsidian_config.json")
    defaults = {
        "staleness_thresholds": {"earnings_stale_days": 180, "legal_stale_days": 90},
        "summary_quality":      {"min_summary_chars": 2000},
    }
    if not os.path.exists(config_path):
        return defaults
    try:
        with open(config_path) as f:
            return json.load(f)
    except Exception:
        return defaults

_config = _load_config()
EARNINGS_STALE_DAYS = _config.get("staleness_thresholds", {}).get("earnings_stale_days", 180)
LEGAL_STALE_DAYS    = _config.get("staleness_thresholds", {}).get("legal_stale_days", 90)
MIN_SUMMARY_CHARS   = _config.get("summary_quality", {}).get("min_summary_chars", 2000)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _today() -> str:
    return datetime.datetime.now().strftime('%Y-%m-%d')

def _stamp() -> str:
    return datetime.datetime.now().strftime('%Y%m%d_%H%M')

def _days_ago(date_str: str) -> int:
    try:
        d = datetime.datetime.strptime(date_str[:10], "%Y-%m-%d")
        return (datetime.datetime.now() - d).days
    except Exception:
        return 9999

# Sept 2026: must mirror nova_earnings_call.py's own NO_DATA_RETRY_DAYS
# constant. See _earnings_recently_attempted() below.
NOVA_NO_DATA_RETRY_DAYS = 30

def _earnings_recently_attempted(earnings: dict, window_days: int = 7) -> bool:
    """True if a re-run of nova_earnings_call.py on this ticker right now
    would be a no-op, so this check shouldn't WARN about it.

    Ported from the identical fix in jansky_review.py (Sept 22, 2026) —
    pipeline_health.py's own Nova staleness check had the same gap:
    it only ever looked at call_date age, with zero awareness that
    nova_earnings_call.py tracks and skips tickers it has already
    searched and found nothing new for within the last 30 days
    (NO_DATA_RETRY_DAYS). Roughly half the watchlist has no findable
    wire-service earnings source at all (structurally, not from lack of
    trying) — those tickers will always show an old call_date and would
    otherwise WARN every single day forever, even though Nova is doing
    exactly what it's supposed to (not wasting API calls re-attempting a
    search that just failed 8 days ago).
    """
    last_attempted = earnings.get("last_search_attempted", "")
    if not last_attempted:
        return False
    last_result = earnings.get("last_search_result", "")
    days = _days_ago(last_attempted)
    if last_result == "no_new_data_found":
        return days <= NOVA_NO_DATA_RETRY_DAYS
    return days <= window_days

def _find_latest(prefix: str) -> str | None:
    try:
        files = sorted([f for f in os.listdir(DATA_DIR)
                        if f.startswith(prefix) and f.endswith('.json')])
        return os.path.join(DATA_DIR, files[-1]) if files else None
    except Exception:
        return None

def _status(ok: bool, warn: bool = False) -> str:
    if ok:    return "✓ OK"
    if warn:  return "⚠ WARN"
    return "✗ FAIL"

# ─── Check 1: Atlas YAML Schema ───────────────────────────────────────────────

def check_atlas() -> dict:
    """Validate Atlas macro_backdrop.yaml for completeness."""
    result = {
        "name":   "Atlas YAML Schema",
        "status": "OK",
        "issues": [],
        "metrics": {}
    }

    backdrop_path = f"{BASE_DIR}/macro_backdrop.yaml"
    summary_path  = f"{BASE_DIR}/macro_summary.md"

    if not os.path.exists(backdrop_path):
        result["status"] = "FAIL"
        result["issues"].append("macro_backdrop.yaml not found")
        return result

    # Check file sizes
    backdrop_size = os.path.getsize(backdrop_path)
    result["metrics"]["backdrop_bytes"] = backdrop_size

    if backdrop_size < 2000:
        result["status"] = "WARN"
        result["issues"].append(f"macro_backdrop.yaml suspiciously small ({backdrop_size} bytes)")

    # Try parsing as YAML
    try:
        with open(backdrop_path) as f:
            content = f.read()
        data = yaml.safe_load(content)

        # Check required top-level keys
        required_keys = ["monetary_policy", "inflation", "growth",
                         "labor_market", "credit", "ai_guidance"]
        for key in required_keys:
            if key not in data:
                result["status"] = "WARN"
                result["issues"].append(f"Missing YAML key: {key}")
            elif isinstance(data[key], dict):
                # Check for truncated values (ending mid-word)
                # Skip ai_guidance block scalars which end with valid prose
                if key == "ai_guidance":
                    continue
                for k, v in data[key].items():
                    if isinstance(v, str) and len(v) > 10:
                        # Only flag if ends with a character that suggests
                        # genuine mid-word truncation (not valid YAML endings)
                        stripped_v = v.rstrip()
                        last_char = stripped_v[-1] if stripped_v else ''
                        if last_char not in '."%\')-_0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ':
                            result["status"] = "WARN"
                            result["issues"].append(
                                f"Possible truncation in {key}.{k}: "
                                f"'{stripped_v[-20:]}'"
                            )

        result["metrics"]["yaml_keys_found"] = len(data)
        result["metrics"]["generated_date"]  = str(
            data.get("generated_date", "unknown")
        )

    except yaml.YAMLError as e:
        result["status"] = "FAIL"
        result["issues"].append(f"YAML parse error: {str(e)[:100]}")
    except Exception as e:
        result["status"] = "WARN"
        result["issues"].append(f"Validation error: {e}")

    # Check macro summary
    if os.path.exists(summary_path):
        summary_size = os.path.getsize(summary_path)
        result["metrics"]["summary_bytes"] = summary_size
        if summary_size < 1000:
            result["status"] = "WARN"
            result["issues"].append(
                f"macro_summary.md suspiciously small ({summary_size} bytes)"
            )
        # Check it ends with a complete sentence
        with open(summary_path) as f:
            summary_text = f.read().strip()
        if summary_text and summary_text[-1] not in '.!?"\'':
            result["status"] = "WARN"
            result["issues"].append(
                f"macro_summary.md may be truncated (ends: "
                f"'{summary_text[-40:]}')"
            )
    else:
        result["status"] = "WARN"
        result["issues"].append("macro_summary.md not found")

    if not result["issues"]:
        result["issues"] = ["All checks passed"]
    return result

# ─── Check 2: Mercury Data Coverage ───────────────────────────────────────────

def check_mercury() -> dict:
    """Check Mercury's data gap count and report freshness."""
    result = {
        "name":   "Mercury CCC Coverage",
        "status": "OK",
        "issues": [],
        "metrics": {}
    }

    mercury_path = f"{BASE_DIR}/mercury_latest.json"
    if not os.path.exists(mercury_path):
        result["status"] = "FAIL"
        result["issues"].append("mercury_latest.json not found")
        return result

    with open(mercury_path) as f:
        mercury = json.load(f)

    run_date = mercury.get("run_date", "unknown")
    summary  = mercury.get("summary", "")
    result["metrics"]["run_date"]      = run_date
    result["metrics"]["summary_chars"] = len(summary)

    # Check staleness
    if run_date != "unknown":
        age = _days_ago(run_date)
        result["metrics"]["age_days"] = age
        if age > 8:
            result["status"] = "WARN"
            result["issues"].append(f"Mercury report is {age} days old")

    # Count data gaps from Mercury's self-report
    gap_count = 0
    critical_gaps = []
    if summary:
        # Mercury self-reports gaps in a "Data gaps noted:" section
        gap_match = re.search(
            r'[Dd]ata gaps?[^\n]*:(.*?)(?=\n---|\n#|\Z)',
            summary, re.DOTALL
        )
        if gap_match:
            gap_text = gap_match.group(1)
            # Count bullet points or comma-separated items
            gap_count = len(re.findall(r'[•\-\*]|\n\d+\.', gap_text))
            result["metrics"]["reported_gap_count"] = gap_count

            # Flag critical gaps
            for critical in ["energy price", "gold", "BDI", "baltic"]:
                if critical.lower() in gap_text.lower():
                    critical_gaps.append(critical)

        # Check confidence rating
        conf_match = re.search(
            r'[Aa]nalysis confidence[:\s]+([A-Z\-]+)',
            summary
        )
        if conf_match:
            confidence = conf_match.group(1)
            result["metrics"]["confidence"] = confidence
            if confidence in ("LOW", "MODERATE-LOW"):
                result["status"] = "WARN"
                result["issues"].append(
                    f"Mercury confidence: {confidence}"
                )

    if critical_gaps:
        result["status"] = "WARN"
        result["issues"].append(
            f"Critical data gaps: {', '.join(critical_gaps)}"
        )

    result["metrics"]["critical_gap_count"] = len(critical_gaps)
    if not result["issues"]:
        result["issues"] = ["All checks passed"]
    return result

# ─── Check 3: Nova Data Staleness ─────────────────────────────────────────────

def check_nova() -> dict:
    """Check Nova supplemental for stale records."""
    result = {
        "name":   "Nova Coverage Staleness",
        "status": "OK",
        "issues": [],
        "metrics": {}
    }

    nova_path = f"{BASE_DIR}/nova_supplemental.json"
    if not os.path.exists(nova_path):
        result["status"] = "FAIL"
        result["issues"].append("nova_supplemental.json not found")
        return result

    with open(nova_path) as f:
        nova = json.load(f)

    stale_earnings   = []
    cooldown_earnings= []
    missing_earnings = []
    missing_legal    = []
    high_risk        = []
    ages_earnings    = []
    ages_legal       = []
    stale_legal      = []

    for ticker, record in nova.items():
        # Earnings
        earnings  = record.get("earnings", {})
        call_date = earnings.get("call_date", "")
        if not call_date:
            missing_earnings.append(ticker)
        else:
            age = _days_ago(call_date)
            ages_earnings.append(age)
            if age > EARNINGS_STALE_DAYS:
                if _earnings_recently_attempted(earnings):
                    # Nova already checked recently and found nothing new
                    # (or is within its 30-day no-data cooldown) — an old
                    # call_date here reflects a structurally unfindable
                    # source, not a ticker nobody has looked at. Tracked
                    # separately so it's visible without WARNing on it.
                    cooldown_earnings.append(f"{ticker}({age}d)")
                else:
                    stale_earnings.append(f"{ticker}({age}d)")

        # Legal
        legal      = record.get("legal", {})
        legal_date = legal.get("research_date", "")
        if not legal_date:
            missing_legal.append(ticker)
        else:
            age = _days_ago(legal_date)
            ages_legal.append(age)
            if age > LEGAL_STALE_DAYS:
                stale_legal.append(f"{ticker}({age}d)")

        risk = legal.get("risk_level", "")
        if risk in ("Critical", "High"):
            high_risk.append(f"{ticker}[{risk}]")

    result["metrics"]["total_tickers"]      = len(nova)
    result["metrics"]["stale_earnings"]     = len(stale_earnings)
    result["metrics"]["stale_legal"]        = len(stale_legal)
    result["metrics"]["missing_earnings"]   = len(missing_earnings)
    result["metrics"]["missing_legal"]      = len(missing_legal)
    result["metrics"]["high_risk_count"]    = len(high_risk)
    result["metrics"]["avg_earnings_age_days"] = (
        int(sum(ages_earnings)/len(ages_earnings)) if ages_earnings else 0
    )
    result["metrics"]["avg_legal_age_days"] = (
        int(sum(ages_legal)/len(ages_legal)) if ages_legal else 0
    )

    if stale_earnings:
        result["status"] = "WARN"
        result["issues"].append(
            f"Stale earnings records (>{EARNINGS_STALE_DAYS}d): {', '.join(stale_earnings)}"
        )
    if stale_legal:
        result["status"] = "WARN"
        result["issues"].append(
            f"Stale legal records (>{LEGAL_STALE_DAYS}d): {', '.join(stale_legal)}"
        )
    if missing_earnings:
        result["issues"].append(
            f"Missing earnings records: {', '.join(missing_earnings[:10])}"
        )
    if cooldown_earnings:
        # Informational only — never WARNs, never flips status. These
        # tickers have old call_dates but Nova has genuinely already
        # checked them within its own retry window (see
        # _earnings_recently_attempted above); re-running
        # nova_earnings_call.py on them right now would be a no-op.
        result["issues"].append(
            f"Stale but in Nova's no-data cooldown (not actionable yet): "
            f"{', '.join(cooldown_earnings[:10])}"
        )

    result["metrics"]["stale_earnings_in_cooldown"] = len(cooldown_earnings)
    result["metrics"]["high_risk_tickers"] = high_risk
    if not result["issues"]:
        result["issues"] = ["All checks passed"]
    return result

# ─── Check 4: Jupiter Summary Completeness ────────────────────────────────────

def check_jupiter() -> dict:
    """Check Jupiter research JSON for completeness."""
    result = {
        "name":   "Jupiter Summary Completeness",
        "status": "OK",
        "issues": [],
        "metrics": {}
    }

    research_path = _find_latest("research_")
    if not research_path:
        result["status"] = "FAIL"
        result["issues"].append("No research JSON found in data/")
        return result

    result["metrics"]["research_file"] = os.path.basename(research_path)

    with open(research_path) as f:
        research = json.load(f)

    # File age
    fname   = os.path.basename(research_path)
    # Extract date from filename like research_20260614_1356.json
    date_match = re.search(r'research_(\d{8})_', fname)
    if date_match:
        date_str = date_match.group(1)
        file_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        age = _days_ago(file_date)
        result["metrics"]["age_days"] = age
        if age > 8:
            result["status"] = "WARN"
            result["issues"].append(
                f"Research JSON is {age} days old — "
                f"pipeline may not have run this week"
            )

    # Check each ticker
    missing_verdict  = []
    short_summaries  = []
    missing_section13= []
    lengths          = []

    for record in research:
        ticker  = record.get("ticker", "?")
        summary = record.get("summary", "")
        lengths.append(len(summary))

        # Verdict check
        if not any(v in summary for v in ["ACCUMULATE", "WATCH", "AVOID"]):
            missing_verdict.append(ticker)

        # Section 13 check
        if "## 13." not in summary and "Overall Verdict" not in summary:
            missing_section13.append(ticker)

        # Minimum length (complete summaries are typically 10K+ chars)
        if len(summary) < MIN_SUMMARY_CHARS:
            short_summaries.append(f"{ticker}({len(summary)})")

    result["metrics"]["total_tickers"]       = len(research)
    result["metrics"]["missing_verdict"]     = len(missing_verdict)
    result["metrics"]["missing_section_13"]  = len(missing_section13)
    result["metrics"]["short_summaries"]     = len(short_summaries)
    result["metrics"]["avg_summary_chars"]   = (
        int(sum(lengths)/len(lengths)) if lengths else 0
    )
    result["metrics"]["min_summary_chars"]   = min(lengths) if lengths else 0

    if missing_verdict:
        result["status"] = "WARN"
        result["issues"].append(
            f"Missing verdict ({len(missing_verdict)}): "
            f"{', '.join(missing_verdict[:10])}"
        )
    if short_summaries:
        result["status"] = "WARN"
        result["issues"].append(
            f"Suspiciously short summaries (<2000 chars): "
            f"{', '.join(short_summaries[:10])}"
        )
    if missing_section13:
        result["issues"].append(
            f"Missing section 13 ({len(missing_section13)}): "
            f"{', '.join(missing_section13[:10])}"
        )

    if not result["issues"]:
        result["issues"] = ["All checks passed"]
    return result

# ─── Check 5: Pipeline File Freshness ─────────────────────────────────────────

def check_pipeline_files() -> dict:
    """Check that all expected pipeline output files exist and are recent."""
    result = {
        "name":   "Pipeline File Freshness",
        "status": "OK",
        "issues": [],
        "metrics": {}
    }

    expected_files = {
        "macro_backdrop.yaml":              7,   # max age in days
        "macro_summary.md":                 7,
        "mercury_latest.json":              7,
        "mercury_backdrop.yaml":            7,
        "nova_supplemental.json":           7,
        "jansky_latest.json":               7,
        "trade_decisions.json":             7,
        "jansky_trade_feedback.json":       7,
    }

    for fname, max_age in expected_files.items():
        fpath = f"{BASE_DIR}/{fname}"
        if not os.path.exists(fpath):
            result["status"] = "WARN"
            result["issues"].append(f"Missing: {fname}")
            result["metrics"][fname] = "MISSING"
            continue

        # Check modification time
        mtime    = os.path.getmtime(fpath)
        mod_date = datetime.datetime.fromtimestamp(mtime)
        age_days = (datetime.datetime.now() - mod_date).days
        result["metrics"][fname] = f"{age_days}d old"

        if age_days > max_age:
            result["status"] = "WARN"
            result["issues"].append(
                f"{fname} is {age_days} days old (max: {max_age})"
            )

    # Check for dated research files this week
    research_path = _find_latest("research_")
    if research_path:
        age = (datetime.datetime.now() -
               datetime.datetime.fromtimestamp(
                   os.path.getmtime(research_path)
               )).days
        result["metrics"]["latest_research"] = (
            f"{os.path.basename(research_path)} ({age}d old)"
        )
        if age > 8:
            result["status"] = "WARN"
            result["issues"].append(
                f"Latest research file is {age} days old"
            )

    if not result["issues"]:
        result["issues"] = ["All checks passed"]
    return result

# ─── Report Builder ───────────────────────────────────────────────────────────

def build_health_report(checks: list[dict]) -> tuple[dict, str]:
    """Build JSON report and markdown summary."""
    stamp    = _stamp()
    run_date = _today()

    # Overall status = worst individual status
    status_rank = {"OK": 0, "WARN": 1, "FAIL": 2}
    worst = max(checks, key=lambda c: status_rank.get(c["status"], 0))
    overall = worst["status"]

    report = {
        "run_date":       run_date,
        "generated_at":   datetime.datetime.now().isoformat(),
        "overall_status": overall,
        "checks":         checks,
    }

    # Markdown summary
    status_icons = {"OK": "✓", "WARN": "⚠", "FAIL": "✗"}
    lines = [
        f"# Obsidian Capital — Pipeline Health Report",
        f"**{run_date}** | Overall: {status_icons.get(overall,'?')} {overall}",
        "",
    ]
    for check in checks:
        icon = status_icons.get(check["status"], "?")
        lines.append(f"## {icon} {check['name']}")
        for issue in check["issues"]:
            lines.append(f"- {issue}")
        if check.get("metrics"):
            lines.append("")
            lines.append("**Metrics:**")
            for k, v in check["metrics"].items():
                if not isinstance(v, list):
                    lines.append(f"- {k}: {v}")
        lines.append("")

    # Action items
    action_items = []
    for check in checks:
        if check["status"] != "OK":
            for issue in check["issues"]:
                if issue != "All checks passed":
                    action_items.append(
                        f"[{check['name']}] {issue}"
                    )

    if action_items:
        lines.append("## Action Items Before Next Run")
        for item in action_items:
            lines.append(f"- {item}")

    markdown = "\n".join(lines)
    return report, markdown

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    stamp    = _stamp()
    run_date = _today()

    print(f"\n{'═'*52}")
    print(f"  Pipeline Health Check — {run_date}")
    print(f"{'═'*52}\n")

    checks = []

    print("  Checking Atlas YAML schema...")
    checks.append(check_atlas())
    print(f"    {checks[-1]['status']} — {checks[-1]['issues'][0][:60]}")

    print("  Checking Mercury data coverage...")
    checks.append(check_mercury())
    print(f"    {checks[-1]['status']} — {checks[-1]['issues'][0][:60]}")

    print("  Checking Nova staleness...")
    checks.append(check_nova())
    print(f"    {checks[-1]['status']} — {checks[-1]['issues'][0][:60]}")

    print("  Checking Jupiter completeness...")
    checks.append(check_jupiter())
    print(f"    {checks[-1]['status']} — {checks[-1]['issues'][0][:60]}")

    print("  Checking pipeline file freshness...")
    checks.append(check_pipeline_files())
    print(f"    {checks[-1]['status']} — {checks[-1]['issues'][0][:60]}")

    report, markdown = build_health_report(checks)

    # Save outputs
    os.makedirs(DATA_DIR,   exist_ok=True)
    os.makedirs(REPORT_DIR, exist_ok=True)

    json_path = f"{BASE_DIR}/pipeline_health.json"
    md_path   = f"{BASE_DIR}/pipeline_health.md"
    arch_json = f"{DATA_DIR}/pipeline_health_{stamp}.json"
    arch_md   = f"{REPORT_DIR}/pipeline_health_{stamp}.md"

    with open(json_path, 'w') as f:
        json.dump(report, f, indent=2)
    with open(md_path, 'w') as f:
        f.write(markdown)
    with open(arch_json, 'w') as f:
        json.dump(report, f, indent=2)
    with open(arch_md, 'w') as f:
        f.write(markdown)

    print(f"\n  {'─'*50}")
    print(f"  Overall: {report['overall_status']}")
    print(f"  Saved:   pipeline_health.json")
    print(f"           pipeline_health.md")

    # Print action items
    action_items = [
        issue
        for check in checks
        for issue in check["issues"]
        if check["status"] != "OK" and issue != "All checks passed"
    ]
    if action_items:
        print(f"\n  Action Items ({len(action_items)}):")
        for item in action_items[:10]:
            print(f"    ⚠ {item[:70]}")

    print(f"\n{'═'*52}\n")

if __name__ == "__main__":
    main()
