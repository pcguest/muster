"""The bundled demo: three synthetic grain-receival spreadsheets.

Three sites — Karrilong, Mundawarra and Bellandry — record the same grain
receivals business with different headings, date formats, number styles and
boolean spellings, plus the classics: a cross-site conflict on one ticket,
an agreeing duplicate, uncoercible cells, an out-of-range load, an
unexpected commodity and a column nobody declared. Every value is invented;
no real growers, sites or organisations appear.

``muster demo`` writes the files and a ready-confirmed muster.yaml into a
folder and runs the full pipeline over them, so the report, exceptions file
and manifest can be explored without touching real data.
"""

from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

DEMO_CONFIG = """\
# muster.yaml — configuration for the bundled Muster demo.
#
# Three invented grain sites record the same receivals with different
# headings, date formats and conventions, and a few deliberate problems.
# See the generated report for what Muster makes of them.

fields:
  - name: receival_id
    type: string
    required: true
    synonyms: ["ticket no", "ticket number", "docket"]
  - name: grower
    type: string
    required: true
    synonyms: ["grower name"]
  - name: commodity
    type: string
    required: true
    synonyms: ["grain"]
    rules:
      - rule: allowed_values
        values: ["wheat", "barley", "canola", "oats"]
        severity: error
  - name: tonnes
    type: float
    required: true
    synonyms: ["net weight (t)", "net tonnes"]
    rules:
      - rule: range          # a single road load; outside this is suspect
        min: 0
        max: 60
        severity: warning
  - name: receival_date
    type: date
    required: true
    synonyms: ["date received"]
  - name: moisture_pct
    type: float
    synonyms: ["moisture", "moisture %"]
    rules:
      - rule: range
        min: 0
        max: 30
        severity: warning
  - name: paid
    type: boolean

sources:
  - "sources/*.csv"
  - "sources/*.xlsx"

matching:
  fuzzy_threshold: 90

validation:
  keys: ["receival_id"]
  cross_field: []
  # No survivorship strategy: cross-site conflicts are held for review,
  # which the demo deliberately provokes on one ticket.

limits:
  max_file_size_mb: 100
  chunk_rows: 100000

output:
  directory: output
  dataset_name: receivals

# One publish target so 'muster publish' can be tried end to end: it upserts
# the governed dataset into a SQLite file next to this configuration. The
# demo run records deliberate errors, so the publish refuses without --force
# — exactly what a real incomplete dataset should do.
targets:
  warehouse:
    type: sqlite
    path: warehouse.db
    table: receivals
"""

# Karrilong writes tidy, canonical headings and ISO dates.
_KARRILONG = """\
receival_id,grower,commodity,tonnes,receival_date,moisture_pct,paid
R-1001,Marram Downs,wheat,32.40,2024-01-05,11.2,true
R-1002,T. Halloway,barley,28.15,2024-01-06,9.8,true
R-1003,Stone Creek Pastoral,canola,18.60,2024-01-08,8.4,false
R-1004,Windmill Flat Co-op,wheat,41.90,2024-01-09,12.1,true
R-1005,R. & J. Ellery,oats,22.05,2024-01-10,10.6,false
R-1006,Karrigan Bros,wheat,35.75,2024-01-12,11.9,true
"""

# Mundawarra: different headings, day-first dates, thousands separators,
# an uncoercible weight, a negative weight, an unexpected commodity — and
# ticket R-1004 again with a different weight than Karrilong recorded.
_MUNDAWARRA = """\
Ticket No.,Grower Name,Grain,Net Weight (t),Date Received,Moisture %,Paid
R-2001,Denehurst Farming,wheat,"1,204.50",12/01/2024,11.0,yes
R-2002,Marram Downs,barley,27.30,15/01/2024,10.4,yes
R-2003,T. Halloway,sorghum,24.80,16/01/2024,9.1,no
R-2004,Windmill Flat Co-op,wheat,n/a,17/01/2024,10.9,yes
R-2005,Stone Creek Pastoral,canola,-2.50,18/01/2024,7.9,no
R-1004,Windmill Flat Co-op,wheat,44.20,09/01/2024,12.1,yes
"""

# Bellandry: shouty headings, month-name dates, no paid column, an extra
# operator column nobody declared, one unreadable date — and ticket R-1006
# again, agreeing with Karrilong on every shared value.
_BELLANDRY = {
    "RECEIVAL_ID": ["R-3001", "R-3002", "R-3003", "R-1006", "R-3004"],
    "GROWER": [
        "Karrigan Bros",
        "R. & J. Ellery",
        "Marram Downs",
        "Karrigan Bros",
        "Windmill Flat Co-op",
    ],
    "COMMODITY": ["wheat", "oats", "wheat", "wheat", "barley"],
    "TONNES": ["30.90", "21.70", "33.10", "35.75", "26.40"],
    "RECEIVAL DATE": [
        "05 Feb 2024",
        "06 Feb 2024",
        "mid February",
        "12 Jan 2024",
        "08 Feb 2024",
    ],
    "MOISTURE": ["10.2", "9.9", "11.5", "11.9", "10.0"],
    "WEIGHBRIDGE OPERATOR": ["B. Sutter", "B. Sutter", "C. Ngata", "B. Sutter", "C. Ngata"],
}


def write_demo(target: Path) -> list[Path]:
    """Create the demo project under ``target``; return the files written."""
    sources = target / "sources"
    sources.mkdir(parents=True)
    written = [target / "muster.yaml"]
    written[0].write_text(DEMO_CONFIG, encoding="utf-8")

    karrilong = sources / "receivals_karrilong.csv"
    karrilong.write_text(_KARRILONG, encoding="utf-8")
    mundawarra = sources / "grain_intake_mundawarra.csv"
    mundawarra.write_text(_MUNDAWARRA, encoding="utf-8")
    bellandry = sources / "bellandry_receivals.xlsx"
    pl.DataFrame(_BELLANDRY).write_excel(bellandry)
    written += [karrilong, mundawarra, bellandry]

    logger.info("demo written target=%s files=%d", target, len(written))
    return written
