# Obsidian Capital — Pipeline Health Report
**2026-09-12** | Overall: ⚠ WARN

## ✓ Atlas YAML Schema
- All checks passed

**Metrics:**
- backdrop_bytes: 3673
- yaml_keys_found: 9
- generated_date: 2026-09-12
- summary_bytes: 5564

## ✓ Mercury CCC Coverage
- All checks passed

**Metrics:**
- run_date: 2026-09-12
- summary_chars: 12254
- age_days: 0
- critical_gap_count: 0

## ⚠ Nova Coverage Staleness
- Stale earnings records (>180d): ADBE(184d), ABBV(953d), HUBG(219d), PGR(788d), RIO(206d), CRM(199d), BHP(208d), HON(324d)
- Stale legal records (>90d): CZR(92d), ALL(92d), NEM(92d), MCD(92d), TGT(92d), SO(91d), HUBG(91d), LOW(91d)
- Missing earnings records: ODFL

**Metrics:**
- total_tickers: 129
- stale_earnings: 8
- stale_legal: 8
- missing_earnings: 1
- missing_legal: 45
- high_risk_count: 25
- avg_earnings_age_days: 99
- avg_legal_age_days: 40

## ✓ Jupiter Summary Completeness
- Missing section 13 (3): LSTR, COST, MSFT

**Metrics:**
- research_file: research_20260912_1154.json
- age_days: 0
- total_tickers: 128
- missing_verdict: 0
- missing_section_13: 3
- short_summaries: 0
- avg_summary_chars: 9782
- min_summary_chars: 6713

## ✓ Pipeline File Freshness
- All checks passed

**Metrics:**
- macro_backdrop.yaml: 0d old
- macro_summary.md: 0d old
- mercury_latest.json: 0d old
- mercury_backdrop.yaml: 0d old
- nova_supplemental.json: 0d old
- jansky_latest.json: 0d old
- trade_decisions.json: 0d old
- jansky_trade_feedback.json: 0d old
- latest_research: research_20260912_1154.json (0d old)

## Action Items Before Next Run
- [Nova Coverage Staleness] Stale earnings records (>180d): ADBE(184d), ABBV(953d), HUBG(219d), PGR(788d), RIO(206d), CRM(199d), BHP(208d), HON(324d)
- [Nova Coverage Staleness] Stale legal records (>90d): CZR(92d), ALL(92d), NEM(92d), MCD(92d), TGT(92d), SO(91d), HUBG(91d), LOW(91d)
- [Nova Coverage Staleness] Missing earnings records: ODFL