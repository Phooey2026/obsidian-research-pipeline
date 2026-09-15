python3 - << 'PYEOF'
import sys, json, os, datetime
sys.path.insert(0, '/home/jay/stock_dashboard')
from weekly_research import generate_dashboard, load_macro_backdrop

BASE_DIR  = '/home/jay/stock_dashboard'
FRAG_DIR  = f'{BASE_DIR}/fragments'
data_dir  = f'{BASE_DIR}/data'

# ── Load latest research JSON ─────────────────────────────────────────────────
latest = sorted([f for f in os.listdir(data_dir) if f.startswith('research_')])[-1]
with open(os.path.join(data_dir, latest)) as f:
    data = json.load(f)
print(f"Research: {latest} ({len(data)} tickers)")

# ── Load Atlas macro backdrop ─────────────────────────────────────────────────
macro_backdrop, macro_summary = load_macro_backdrop()

def load_fragment(name: str, label: str) -> str:
    """Try fragments/ dir first, then root for backward compat."""
    for path in [f'{FRAG_DIR}/{name}', f'{BASE_DIR}/{name}']:
        if os.path.exists(path):
            content = open(path).read()
            print(f"{label}: {len(content):,} chars  ({path})")
            return content
    print(f"{label}: not found")
    return ""

# ── Load all fragments ────────────────────────────────────────────────────────
mercury_fragment       = load_fragment("mercury_dashboard_fragment.html", "Mercury CCC fragment")
jansky_fragment        = load_fragment("jansky_dashboard_fragment.html",  "Jansky fragment")
sectors_delta_fragment = load_fragment("sectors_delta_fragment.html",     "Sectors+Delta fragment")
trades_fragment        = load_fragment("trades_dashboard_fragment.html",  "Trades fragment")
etf_fragment           = load_fragment("etf_dashboard_fragment.html",     "ETF fragment")

# ── Build and write dashboard ─────────────────────────────────────────────────
html = generate_dashboard(data, datetime.datetime.now().strftime('%Y-%m-%d'),
                          macro_backdrop=macro_backdrop,
                          macro_summary=macro_summary,
                          mercury_fragment=mercury_fragment,
                          jansky_fragment=jansky_fragment,
                          sectors_delta_fragment=sectors_delta_fragment,
                          trades_fragment=trades_fragment,
                          etf_fragment=etf_fragment)

with open(f'{BASE_DIR}/dashboard.html', 'w') as f:
    f.write(html)

print(f"\nDashboard rebuilt: {len(html):,} chars")
print(f"Open: http://localhost:8090")
PYEOF
