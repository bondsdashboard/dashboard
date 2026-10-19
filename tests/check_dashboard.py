"""Consistency checks for the dashboard: page figures vs an independent Python calculation.

Run:  python tests/check_dashboard.py <path-to-local-d3.min.js>
Serves the repo locally, opens it in headless Chromium (Playwright) and compares
KPIs, table totals, simulator results and concentration figures with values
computed here straight from data/*.json.
"""
import json, math, subprocess, sys, time
from datetime import date, datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
D3 = sys.argv[1]
bonds = json.loads((ROOT / "data/bonds.json").read_text())
fx = {f["currency"]: f["per_eur"] for f in json.loads((ROOT / "data/fx.json").read_text())}
TODAY = datetime.now(timezone.utc).date()

def fmt_eur(m):
    if abs(m) < 0.05:
        return 'EUR 0m'
    if abs(m) >= 1000:
        return f"EUR {m/1000:,.2f}bn"
    return f"EUR {m:,.1f}m" if abs(m) < 100 else f"EUR {round(m):,}m"

def add_months(d, n):
    y, mo = divmod(d.month - 1 + n, 12)
    return date(d.year + y, mo + 1, min(d.day, 28 if mo + 1 == 2 else 30 if mo + 1 in (4, 6, 9, 11) else 31))

for b in bonds:
    b["eur"] = round(b["amount_m"] / fx[b["currency"]], 2)
    b["mat"] = date.fromisoformat(b["maturity_date"]) if b["maturity_date"] else None
live = [b for b in bonds if b["mat"] is None or b["mat"] >= TODAY]
tot = sum(b["eur"] for b in live)
due = lambda months: sum(b["eur"] for b in live if b["mat"] and TODAY <= b["mat"] <= add_months(TODAY, months))
cost = sum(round(b["eur"] * b["coupon_pct"]) / 100 for b in live)
hhi = sum((b["eur"] / tot) ** 2 for b in live)
expected = {"k1": fmt_eur(tot), "k2": fmt_eur(due(12)), "k3": fmt_eur(due(24)), "k4": fmt_eur(due(60)), "k6": fmt_eur(cost)}

results = []
def check(name, ok, detail=""):
    results.append((ok, name, detail))

srv = subprocess.Popen([sys.executable, "-m", "http.server", "8799", "--directory", str(ROOT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1)
errors = []
try:
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(viewport={"width": 1400, "height": 900})
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
        pg.route("**/cdnjs.cloudflare.com/**", lambda r: r.fulfill(path=D3, content_type="application/javascript"))
        pg.route("**/fonts.googleapis.com/**", lambda r: r.fulfill(body="", content_type="text/css"))
        pg.goto("http://localhost:8799/index.html")
        pg.wait_for_timeout(1500)

        # 1. KPIs vs independent calculation
        for k, v in expected.items():
            got = pg.inner_text("#" + k)
            check(f"KPI {k} matches independent calculation", got == v, f"page {got} vs expected {v}")
        # 2. Table totals vs KPI cards (same view)
        rows = pg.eval_on_selector_all("#t-body tr", "rs => rs.map(r => [...r.children].map(c => c.textContent))")
        eur_sum = sum(float(r[4].replace(",", "")) for r in rows)
        cost_sum = sum(float(r[9].replace(",", "")) for r in rows)
        check("Table EUR column sums to issued-nominal KPI", fmt_eur(eur_sum) == pg.inner_text("#k1"), f"{eur_sum:.2f}")
        check("Table coupon column sums to annual-coupon KPI", fmt_eur(cost_sum) == pg.inner_text("#k6"), f"{cost_sum:.2f}")
        check("No duplicate ISINs in table", len({r[0] for r in rows}) == len(rows))
        check("Matured bonds excluded from live view", all(r[-1] != "Matured" for r in rows))
        check("Every non-EUR bond converted (EUR eq. differs from local)", all(r[2] == "EUR" or r[3] != r[4] for r in rows))
        # 3. Concentration
        conc = pg.inner_text("#c-conc")
        check("Effective number of bonds matches 1/HHI", f"{1/hhi:.1f} of {len(live)}" in conc, f"{1/hhi:.1f}")
        # 4. Findings present and each has period/source/limits
        n_find = pg.locator("#c-insights .gb-ins").count()
        n_dl = pg.locator("#c-insights .gb-ins dl dt").count()
        check("Key findings rendered with evidence metadata", n_find >= 5 and n_dl == 3 * n_find, f"{n_find} findings")
        # 5. Simulator reproducible and share=0 refinances nothing
        def setr(sel, v):
            pg.evaluate("([s,v])=>{const e=document.querySelector(s);e.value=v;e.dispatchEvent(new Event('input',{bubbles:true}))}", [sel, str(v)])
            pg.wait_for_timeout(120)
        setr("#s-cpn", 5); setr("#s-share", 100); setr("#s-yr", 2030)
        a = pg.inner_text("#s-out")
        setr("#s-cpn", 7); setr("#s-cpn", 5)
        b = pg.inner_text("#s-out")
        check("Simulator results reproducible for identical inputs", a == b)
        setr("#s-share", 0)
        out = pg.inner_text("#s-out")
        check("Share refinanced 0% → no new coupon", ("COUPON ON THE NEW BONDS\n" + fmt_eur(0).upper()) in out.upper(), out.split("\n")[9:11])
        # simulator: maturing by end-2030 vs independent sum
        mat30 = sum(b["eur"] for b in live if b["mat"] and TODAY <= b["mat"] and b["mat"].year <= 2030)
        check("Simulator 'maturing by end-2030' matches", fmt_eur(mat30) in out, fmt_eur(mat30))
        # 6. Filter consistency: EUR only
        pg.click("#f-ccy .gb-chip >> text=EUR"); pg.wait_for_timeout(200)
        eur_only = sum(b["eur"] for b in live if b["currency"] == "EUR")
        check("Currency filter EUR → KPI equals EUR-only sum", pg.inner_text("#k1") == fmt_eur(eur_only), pg.inner_text("#k1"))
        br.close()
finally:
    srv.terminate()
check("No JavaScript errors", not errors, errors[:3])

for ok, name, detail in results:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  [{detail}]"))
print(f"\n{sum(ok for ok, *_ in results)}/{len(results)} checks passed")
sys.exit(0 if all(ok for ok, *_ in results) else 1)
