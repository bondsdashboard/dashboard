# grenke Issued Bonds dashboard

An interactive dashboard of the bonds publicly issued by the grenke group: maturity wall, funding mix, coupon vs. maturity, bond inspector, timeline, issuance by vintage, coupon cost and a full sortable table. Filters for status, issuer, currency and ESG label apply to every chart.

## How it stays up to date

`.github/workflows/update-data.yml` runs every weekday. It runs `scripts/update_data.py`, which:

1. reads grenke's [Issued Bonds page](https://www.grenke.com/en/investor-relations/debt-capital/issued-bonds/) and rewrites `data/bonds.json` if a bond was added or removed;
2. reads the [ECB daily reference rates](https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml) into `data/fx.json` (used for EUR equivalents);
3. stamps `data/meta.json` with the time of the check.

If the page can't be read or suddenly lists far fewer bonds, the script stops without touching the data and GitHub emails you that the run failed.

To refresh by hand: **Actions → Update bond data → Run workflow**.

## Files

| Path | What it is |
|---|---|
| `index.html` | The whole dashboard (HTML, CSS, JS; d3 loaded from cdnjs) |
| `data/bonds.json` | One row per bond, as listed on grenke.com |
| `data/fx.json` | ECB euro reference rates for the currencies in use |
| `data/meta.json` | Last check / last change timestamps |
| `scripts/update_data.py` | The refresh script |

Amounts are nominal, not market value. Indicative analysis, not an official grenke publication.
