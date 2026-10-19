"""Refresh data/bonds.json from grenke.com and data/fx.json from the ECB.

Run by the GitHub Action every weekday. It never overwrites good data with a
broken read: if the page can't be parsed (or suddenly lists far fewer bonds),
it exits with an error, the Action fails, and GitHub emails you.
"""
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BONDS_URL = "https://www.grenke.com/en/investor-relations/debt-capital/issued-bonds/"
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
DATA = Path(__file__).resolve().parent.parent / "data"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Accept-Language": "en",
}

# Known issuing entities: (match text, issuer, short name, country, programme, instrument)
ISSUERS = [
    ("at1", "grenke AG", "grenke AG", "Germany", "AT1 Prospectus 2025", "AT1 perpetual"),
    ("grenke finance plc", "grenke Finance Plc.", "grenke Finance", "Ireland", "EUR Debt Issuance Programme", "Senior bond"),
    ("grenke pty ltd", "grenke Pty Ltd", "grenke Pty", "Australia", "AUD Debt Issuance Programme", "Senior bond"),
    ("crédit-bail québec", "GC Crédit-Bail Québec Inc.", "GC Crédit-Bail Québec", "Canada", "CAD Debt Issuance Programme", "Senior bond"),
    ("grenkeleasing ag", "Grenkeleasing AG", "Grenkeleasing CH", "Switzerland", "CHF Debt Issuance Programme", "Senior bond"),
]

AMOUNT = re.compile(r"^([A-Z]{3})\s+([\d.,]+)\s*(?:Mio|Mn|million|m)\.?$", re.I)
COUPON = re.compile(r"^(\d+(?:[.,]\d+)?)\s*%$")
TERM = re.compile(r"^(\d{2}\.\d{2}\.\d{4})\s*[–—-]\s*(\d{2}\.\d{2}\.\d{4})$")
DATE = re.compile(r"^(\d{2}\.\d{2}\.\d{4})$")
ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
ESG = {"green bond": "Green", "social bond": "Social", "sustainable bond": "Sustainable", "sustainability bond": "Sustainable"}


def iso(d):
    return datetime.strptime(d, "%d.%m.%Y").strftime("%Y-%m-%d")


def num(s):
    s = s.replace(",", "") if re.search(r",\d{3}(\D|$)", s) else s.replace(",", ".")
    v = float(s)
    return int(v) if v.is_integer() else v


def known_issuer(line):
    low = line.lower()
    if len(line) > 90:
        return None
    for key, *rest in ISSUERS:
        if key == "at1":
            if "at1" in low and "grenke ag" in low and "prospectus" not in low:
                return rest
        elif key in low:
            return rest
    return None


def doc_links(html):
    """ISIN -> link of the official document grenke lists for it (Final Terms, prospectus, pricing supplement)."""
    soup = BeautifulSoup(html, "html.parser")
    links = {}
    for a in soup.find_all("a", href=True):
        text = re.sub(r"\s+", " ", a.get_text(" ")).strip()
        href = a["href"]
        if len(text) > 160 or ".pdf" not in href.lower():
            continue
        for isin in re.findall(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b", text):
            links.setdefault(isin, requests.compat.urljoin(BONDS_URL, href))
    return links


def page_lines(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "nav", "header", "footer"]):
        t.decompose()
    lines = [re.sub(r"\s+", " ", l).strip() for l in soup.get_text("\n").split("\n")]
    return [l for l in lines if l]


def parse_bonds(lines):
    bonds, cur, prev, pending, last = [], None, "", None, None
    for line in lines:
        k = known_issuer(line)
        if k and not AMOUNT.match(line):
            cur, pending, last = k, None, None
        elif line.lower().startswith("(guaranteed by") and not known_issuer(prev):
            # a section heading we don't know yet: use it as the issuer name
            cur, pending, last = [prev, prev, "", None, "Senior bond"], None, None
        elif (m := AMOUNT.match(line)) and cur:
            pending, last = {"currency": m.group(1).upper(), "amount_m": num(m.group(2))}, None
        elif pending is not None and "coupon_pct" not in pending and (m := COUPON.match(line)):
            pending["coupon_pct"] = num(m.group(1))
        elif pending is not None and (m := TERM.match(line)):
            pending["issue_date"], pending["maturity_date"] = iso(m.group(1)), iso(m.group(2))
        elif pending is not None and (m := DATE.match(line)):
            pending["issue_date"], pending["maturity_date"] = iso(m.group(1)), ""
        elif pending is not None and ISIN.match(line):
            issuer, short, country, programme, instrument = cur
            if not all(k in pending for k in ("coupon_pct", "issue_date")):
                raise ValueError(f"incomplete row before {line}: {pending}")
            last = {
                "isin": line, "issuer": issuer, "issuer_short": short, "country": country,
                "programme": programme or f"{pending['currency']} Debt Issuance Programme",
                "instrument": instrument, "currency": pending["currency"], "amount_m": pending["amount_m"],
                "coupon_pct": pending["coupon_pct"], "issue_date": pending["issue_date"],
                "maturity_date": pending["maturity_date"], "esg": "Conventional",
            }
            bonds.append(last)
            pending = None
        elif last is not None and line.lower() in ESG:
            last["esg"] = ESG[line.lower()]
            last = None
        prev = line
    # one row per ISIN, in page order
    seen, out = set(), []
    for b in bonds:
        if b["isin"] not in seen:
            seen.add(b["isin"])
            out.append(b)
    return out


def parse_fx(xml_text, currencies):
    root = ET.fromstring(xml_text)
    day = next(e for e in root.iter() if e.tag.endswith("Cube") and "time" in e.attrib)
    rates = {e.attrib["currency"]: float(e.attrib["rate"]) for e in day if "currency" in e.attrib}
    missing = [c for c in currencies if c != "EUR" and c not in rates]
    if missing:
        raise ValueError(f"ECB has no rate for {missing}")
    date = day.attrib["time"]
    return [{"currency": "EUR", "per_eur": 1, "rate_date": date}] + [
        {"currency": c, "per_eur": rates[c], "rate_date": date} for c in currencies if c != "EUR"
    ]


def main():
    old = json.loads((DATA / "bonds.json").read_text(encoding="utf-8"))
    meta = json.loads((DATA / "meta.json").read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    r = requests.get(BONDS_URL, headers=HEADERS, timeout=60)
    r.raise_for_status()
    bonds = parse_bonds(page_lines(r.text))
    docs = doc_links(r.text)
    for b in bonds:
        b["doc_url"] = docs.get(b["isin"], "")
        b["doc_type"] = (
            "Not linked" if not b["doc_url"]
            else "Prospectus" if b["instrument"].startswith("AT1")
            else "Final terms" if b["issuer"].lower().startswith("grenke finance")
            else "Pricing supplement"
        )
    if len(bonds) < max(5, int(len(old) * 0.7)):
        sys.exit(f"Only {len(bonds)} bonds parsed (had {len(old)}); page layout may have changed. Keeping old data.")

    if bonds != old:
        added = sorted({b["isin"] for b in bonds} - {b["isin"] for b in old})
        removed = sorted({b["isin"] for b in old} - {b["isin"] for b in bonds})
        print(f"Bonds changed. Added: {added or 'none'}. Removed: {removed or 'none'}.")
        (DATA / "bonds.json").write_text(json.dumps(bonds, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        meta["bonds_changed_at"] = now
    else:
        print(f"No change in {len(bonds)} bonds.")

    currencies = ["EUR"] + sorted({b["currency"] for b in bonds} - {"EUR"})
    fx = parse_fx(requests.get(ECB_URL, headers=HEADERS, timeout=60).text, currencies)
    (DATA / "fx.json").write_text(json.dumps(fx, indent=1) + "\n", encoding="utf-8")
    print("FX", fx[0]["rate_date"], {f["currency"]: f["per_eur"] for f in fx})

    meta["checked_at"] = now
    (DATA / "meta.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
