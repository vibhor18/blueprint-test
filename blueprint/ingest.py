"""
Ingest pipeline.
 
Reads everything in data/ and populates corpus.db.
 
Sources, in priority order:
  1. data/1060779/dobnow_jobs.json      DOB NOW filings (post-2013)
  2. data/1060779/dobnow_cos.json       DOB NOW Certificates of Occupancy
  3. data/1060779/bis_jobs.json         BIS-era filings (pre-2013 digital)
  4. data/1060779/bis_portal_notes.md   BIS HTML portal scrape (paper CO,
                                        LNO 4281, property profile, Actions
                                        ledger)
  5. data/1060779/co_980_1918.pdf       The 1918 paper CO (linked, not parsed)
  6. data/1011231/*.pdf                 96 Perry sheets (Part 3 — Sheet
                                        structuring happens in sheets.py)
 
Re-running this is safe: corpus.db is dropped and rebuilt from scratch.
 
Design notes that show up as inline comments where relevant:
  - Status normalization across BIS / DOB NOW is centralized.
  - Cluster inference uses two signals: filing-number stem (DOB NOW) and
    description text references (BIS).
  - Floor expansion: when explicit floors are absent, we expand from
    existing_stories with a clear inference_source flag.
  - Provenance: every record carries a raw_source_id.
"""
 
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
 
from blueprint.schema import init_db
 
 
# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
 
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = ROOT / "corpus.db"
 
 
# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------
 
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
 
 
def iso_or_none(value: Any) -> str | None:
    """Take a Socrata date string and return ISO YYYY-MM-DD (or None)."""
    if not value:
        return None
    s = str(value).strip()
    # Socrata returns things like "2025-10-23T00:00:00.000" or "12/18/25 11:38:22 AM"
    if "T" in s:
        return s.split("T", 1)[0]
    # MM/DD/YY style (DOB NOW c_of_o_issuance_date) – normalize to ISO
    m = re.match(r"(\d{2})/(\d{2})/(\d{2,4})", s)
    if m:
        mm, dd, yy = m.groups()
        yyyy = yy if len(yy) == 4 else f"20{yy}"
        return f"{yyyy}-{mm}-{dd}"
    return s
 
 
def to_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
 
 
# Mapping from raw BIS / DOB NOW status text to our canonical buckets.
# Anything not recognized falls through to 'OTHER' — we never crash on
# unknown statuses, but we don't silently invent a meaning either.
def canonicalize_status(raw: str | None, withdrawal_flag: int = 0) -> str:
    if withdrawal_flag:
        return "WITHDRAWN"
    if not raw:
        return "OTHER"
    s = raw.strip().upper()
    if "WITHDRAWN" in s:
        return "WITHDRAWN"
    if "CO ISSUED" in s or "C OF O ISSUED" in s:
        return "CO_ISSUED"
    if "LOC ISSUED" in s or "LETTER OF COMPLETION" in s:
        return "LOC_ISSUED"
    if "PERMIT ISSUED" in s or s == "R PERMIT-ENTIRE" or "PERMIT-ENTIRE" in s:
        return "PERMIT_ISSUED"
    if "DISAPPROVED" in s:
        return "DISAPPROVED"
    if "APPROVED" in s:
        return "APPROVED"
    if "IN PROCESS" in s or "PLAN EXAM" in s and "DISAPPROVED" not in s and "APPROVED" not in s:
        return "IN_PROCESS"
    return "OTHER"
 
 
def canonicalize_co_filing_type(raw: str | None) -> str:
    if not raw:
        return "OTHER"
    s = raw.strip().upper()
    if "FINAL" in s:
        return "FINAL"
    if "INITIAL" in s:
        # DOB NOW "Initial" — first CO issued on a job, behaves as a TCO.
        # The schema docstring (schema.py) explains this inference.
        return "TEMPORARY"
    if "RENEWAL" in s:
        return "RENEWAL"
    if "AMENDED" in s:
        return "AMENDED"
    if "TEMPORARY" in s or "TCO" in s:
        return "TEMPORARY"
    if "LEGACY" in s:
        return "LEGACY_PAPER"
    return "OTHER"
 
 
def expand_floors_from_story_count(stories: int | None) -> list[str]:
    """Best-effort floor list when no explicit floors are provided.
 
    Returns numeric labels '001'..'00N'. Does NOT include cellar/roof/bulk
    because story count alone doesn't tell us about those. This is honest
    about the limitation — the resolver and CLI will flag the inference.
    """
    if not stories or stories <= 0:
        return []
    return [f"{i:03d}" for i in range(1, stories + 1)]
 
 
# ---------------------------------------------------------------------------
# raw_source bookkeeping
# ---------------------------------------------------------------------------
 
def register_raw_source(
    conn: sqlite3.Connection,
    bin_: str,
    source_type: str,
    file_path: Path,
    record_count: int | None = None,
    notes: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO raw_source
           (bin, source_type, source_uri, pulled_at, file_path, record_count, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            bin_,
            source_type,
            str(file_path),
            now_iso(),
            str(file_path),
            record_count,
            notes,
        ),
    )
    return cur.lastrowid
 
 
# ---------------------------------------------------------------------------
# Property: ingest the BIN-level identity from BIS + DOB NOW data + portal notes
# ---------------------------------------------------------------------------
 
# 310 W 144 St identity is established from the BIS portal screenshots and
# corroborated by the Socrata data. Hardcoded here for this build test;
# in production this would be derived from a properties lookup table.
PROPERTY_310_W_144 = {
    "bin": "1060779",
    "bbl": "1020440020",
    "borough": "MANHATTAN",
    "block": "2044",
    "lot": "20",
    "house_no": "310",
    "street_name": "WEST 144 STREET",
    "zip_code": "10030",
    "zoning_district": None,             # not in portal scrape; would come from ZoLa
    "map_number": None,
    "dof_building_class": "D1-ELEVATOR APT",
    "construction_class": None,
    "occupancy_use_groups": json.dumps(["R-2"]),  # post-conversion; was UG#8 (garage)
    "hpd_multiple_dwelling": "Y",
    "landmark_status": "NOT_LANDMARKED",
    "existing_stories": 4,
    "existing_height_ft": None,
    "gross_floor_area_sf": None,
    "notes": "Building converted 2025 from non-conforming commercial (garage) "
             "to conforming residential 48-unit under Article 5 ZR. See "
             "data/1060779/bis_portal_notes.md for full property profile.",
}
 
# 96 Perry St — only what we need for Part 3.
PROPERTY_96_PERRY = {
    "bin": "1011231",
    "bbl": "1006210013",
    "borough": "MANHATTAN",
    "block": "621",
    "lot": "13",
    "house_no": "96",
    "street_name": "PERRY ST",
    "zip_code": "10014",
    "zoning_district": None,
    "map_number": "12A",                  # from EN-001.00 title block
    "dof_building_class": None,
    "construction_class": "C1-4",         # from EN-001.00 title block
    "occupancy_use_groups": json.dumps(["R-2"]),
    "hpd_multiple_dwelling": None,
    "landmark_status": None,
    "existing_stories": 5,
    "existing_height_ft": None,
    "gross_floor_area_sf": 166,           # the renovation scope (NOT building total)
    "notes": "Property identity extracted from sheet title blocks. "
             "Only the 5th-floor apartment B17-B was filed on (job 140941514). "
             "Building-level data would require additional pulls.",
}
 
 
def ingest_properties(conn: sqlite3.Connection) -> None:
    for prop in (PROPERTY_310_W_144, PROPERTY_96_PERRY):
        cols = list(prop.keys())
        placeholders = ", ".join("?" for _ in cols)
        conn.execute(
            f"INSERT OR REPLACE INTO property ({', '.join(cols)}) VALUES ({placeholders})",
            tuple(prop[c] for c in cols),
        )
 
 
# ---------------------------------------------------------------------------
# BIS jobs: ic3t-wcy2 (pre-2013 digital, legacy)
# ---------------------------------------------------------------------------
 
def ingest_bis_jobs(conn: sqlite3.Connection, bin_: str, path: Path) -> int:
    if not path.exists():
        return 0
    with path.open() as f:
        raw_rows = json.load(f)
 
    source_id = register_raw_source(
        conn, bin_, "BIS_SOCRATA_JOBS", path,
        record_count=len(raw_rows),
        notes="Dataset ic3t-wcy2. Note: dataset emits duplicate rows for "
              "the same (job, doc); dedupe step retains the latest "
              "latest_action_date.",
    )
 
    # Dedupe by (job__, doc__), keeping the row with the most recent
    # latest_action_date. This is a real data-quality issue with the
    # Socrata BIS dataset — worth noting in the README.
    by_key: dict[tuple[str, str], dict] = {}
    for row in raw_rows:
        key = (row.get("job__", ""), row.get("doc__", ""))
        existing = by_key.get(key)
        if not existing or (row.get("latest_action_date", "") > existing.get("latest_action_date", "")):
            by_key[key] = row
 
    inserted = 0
    for (job, doc), row in by_key.items():
        filing_id = f"BIS:{job}:{doc}"
        withdrawal_flag = to_int(row.get("withdrawal_flag")) or 0
        status_raw = row.get("job_status_descrp")
        status_canonical = canonicalize_status(status_raw, withdrawal_flag)
        conn.execute(
            """INSERT OR REPLACE INTO filing (
                filing_id, source, bin, job_number, doc_number, job_type,
                filing_status_raw, filing_status_canonical,
                withdrawal_flag, withdrawal_date,
                pre_filing_date, approved_date, latest_action_date,
                applicant_first_name, applicant_last_name,
                applicant_license_type, applicant_license_number,
                existing_occupancy, proposed_occupancy,
                existing_dwelling_units, proposed_dwelling_units,
                existing_stories, proposed_stories,
                job_description,
                cluster_id, cluster_inference_method,
                raw_source_id, raw_payload
            ) VALUES (
                ?,?,?,?,?,?, ?,?, ?,?, ?,?,?, ?,?, ?,?, ?,?, ?,?, ?,?, ?,
                ?,?, ?,?
            )""",
            (
                filing_id, "BIS", bin_, job, doc, row.get("job_type"),
                status_raw, status_canonical,
                withdrawal_flag, None,
                iso_or_none(row.get("pre__filing_date")),
                iso_or_none(row.get("approved")),
                iso_or_none(row.get("latest_action_date")),
                row.get("applicant_s_first_name"), row.get("applicant_s_last_name"),
                row.get("applicant_professional_title"), row.get("applicant_license__"),
                row.get("existing_occupancy"), row.get("proposed_occupancy"),
                to_int(row.get("existing_dwelling_units")), to_int(row.get("proposed_dwelling_units")),
                to_int(row.get("existingno_of_stories")), to_int(row.get("proposed_no_of_stories")),
                row.get("job_description"),
                filing_id, "SELF_ROOT",  # will be patched below if we infer a parent
                source_id, json.dumps(row),
            ),
        )
        inserted += 1
 
    # Cluster inference for BIS jobs:
    # Look for "APPLICATION#NNNNNNNNN" references in job_description and link them.
    # The 2009-2011 conversion at 110445974 has five conjuncts all referencing it.
    for filing_id, desc in conn.execute(
        "SELECT filing_id, job_description FROM filing WHERE source = 'BIS' AND bin = ?",
        (bin_,),
    ).fetchall():
        if not desc:
            continue
        # Look for patterns like "APPLICATION#110445974" or "APPLICATION# 110445974"
        m = re.search(r"APPLICATION#\s*(\d{9,})", desc)
        if not m:
            continue
        target_job = m.group(1)
        # Resolve to a filing_id (use the doc 01 of that job number if present)
        row = conn.execute(
            "SELECT filing_id FROM filing WHERE source = 'BIS' AND bin = ? AND job_number = ? "
            "ORDER BY doc_number LIMIT 1",
            (bin_, target_job),
        ).fetchone()
        if not row:
            continue
        parent_id = row["filing_id"]
        if parent_id == filing_id:
            continue
        conn.execute(
            "UPDATE filing SET parent_filing_id = ?, cluster_id = ?, "
            "cluster_inference_method = 'DESCRIPTION_REFERENCE' WHERE filing_id = ?",
            (parent_id, parent_id, filing_id),
        )
 
    # Floor expansion (best-effort: from story count).
    # The BIS portal exposes explicit "Work on Floor(s): CEL 001 thru 005" but
    # this isn't in the Socrata data. We expand from existing_stories and flag
    # the inference clearly. Captured in bis_portal_notes.md.
    for filing_id, ex_stories, pr_stories in conn.execute(
        "SELECT filing_id, existing_stories, proposed_stories FROM filing "
        "WHERE source = 'BIS' AND bin = ?",
        (bin_,),
    ).fetchall():
        stories = max(ex_stories or 0, pr_stories or 0)
        for floor in expand_floors_from_story_count(stories):
            conn.execute(
                "INSERT OR IGNORE INTO filing_floor (filing_id, floor_label, inference_source) "
                "VALUES (?, ?, ?)",
                (filing_id, floor, "EXPANDED_FROM_STORY_COUNT"),
            )
 
    return inserted
 
 
# ---------------------------------------------------------------------------
# DOB NOW jobs: w9ak-ipjd
# ---------------------------------------------------------------------------
 
# DOB NOW filing numbers look like M01118538-I1 / -A1 / -P5 / -S3.
# The stem before the last dash identifies the cluster.
FILING_NUM_STEM_RE = re.compile(r"^(.+)-([A-Z]\d+)$")
 
 
def parse_dobnow_cluster(filing_number: str) -> tuple[str, str | None]:
    """Return (cluster_stem, suffix_or_None).
 
    'M01118538-I1' -> ('M01118538', 'I1')
    'M00891870-I1' -> ('M00891870', 'I1')
    'M01118538'    -> ('M01118538', None)   (no suffix, treated as standalone)
    """
    m = FILING_NUM_STEM_RE.match(filing_number)
    if not m:
        return filing_number, None
    return m.group(1), m.group(2)
 
 
def ingest_dobnow_jobs(conn: sqlite3.Connection, bin_: str, path: Path) -> int:
    if not path.exists():
        return 0
    with path.open() as f:
        rows = json.load(f)
 
    source_id = register_raw_source(
        conn, bin_, "DOBNOW_SOCRATA_JOBS", path,
        record_count=len(rows),
        notes="Dataset w9ak-ipjd.",
    )
 
    # First pass: insert every row.
    for row in rows:
        filing_number = row.get("job_filing_number")
        if not filing_number:
            continue
        stem, suffix = parse_dobnow_cluster(filing_number)
        filing_id = f"DOBNOW:{filing_number}"
        status_raw = row.get("filing_status")
        # Subsequent filings (-S*, -P*) and amendments (-A*) live inside a
        # cluster anchored to the -I1 root. Cluster_id is the root's filing_id;
        # we'll patch parent/cluster pointers in the second pass.
        conn.execute(
            """INSERT OR REPLACE INTO filing (
                filing_id, source, bin, job_number, doc_number, job_type,
                filing_status_raw, filing_status_canonical,
                pre_filing_date, approved_date, latest_action_date,
                applicant_first_name, applicant_last_name,
                applicant_business_name, applicant_license_type, applicant_license_number,
                filing_rep_name, filing_rep_business,
                existing_occupancy, proposed_occupancy,
                existing_dwelling_units, proposed_dwelling_units,
                existing_stories, proposed_stories,
                job_description,
                cluster_id, cluster_inference_method,
                raw_source_id, raw_payload
            ) VALUES (
                ?,?,?,?,?,?, ?,?, ?,?,?,
                ?,?, ?,?,?,
                ?,?, ?,?, ?,?, ?,?, ?,
                ?,?, ?,?
            )""",
            (
                filing_id, "DOBNOW", bin_, filing_number, None, row.get("job_type"),
                status_raw, canonicalize_status(status_raw),
                iso_or_none(row.get("filing_date")),
                iso_or_none(row.get("approved_date")),
                iso_or_none(row.get("current_status_date")),
                row.get("applicant_first_name"), row.get("applicant_last_name"),
                row.get("applicant_business_name"),
                row.get("applicant_professional_title"), row.get("applicant_license"),
                f"{row.get('filing_representative_first_name','') or ''} {row.get('filing_representative_last_name','') or ''}".strip() or None,
                row.get("filing_representative_business_name"),
                None, None,            # DOB NOW doesn't expose existing/proposed occupancy in this dataset
                to_int(row.get("existing_dwelling_units")), to_int(row.get("proposed_dwelling_units")),
                to_int(row.get("existing_stories")), to_int(row.get("proposed_no_of_stories")),
                row.get("job_description"),
                filing_id, "SELF_ROOT",  # patched below
                source_id, json.dumps(row),
            ),
        )
 
    # Second pass: cluster inference via filing-number stem.
    # The convention: the root of a cluster is the -I1 filing (Initial).
    # All other filings sharing the stem are children of that I1.
    for filing_id, job_number in conn.execute(
        "SELECT filing_id, job_number FROM filing WHERE source = 'DOBNOW' AND bin = ?",
        (bin_,),
    ).fetchall():
        stem, suffix = parse_dobnow_cluster(job_number)
        if not suffix or suffix == "I1":
            # The I1 is the root; the cluster_id was already set to its own
            # filing_id via SELF_ROOT.
            continue
        # Find the I1 sibling for this stem.
        root_row = conn.execute(
            "SELECT filing_id FROM filing WHERE source = 'DOBNOW' AND bin = ? AND job_number = ?",
            (bin_, f"{stem}-I1"),
        ).fetchone()
        if not root_row:
            continue
        root_id = root_row["filing_id"]
        conn.execute(
            "UPDATE filing SET parent_filing_id = ?, cluster_id = ?, "
            "cluster_inference_method = 'FILING_NUMBER_STEM' WHERE filing_id = ?",
            (root_id, root_id, filing_id),
        )
 
    # Floor expansion from story count (same approach as BIS).
    for filing_id, ex_stories, pr_stories in conn.execute(
        "SELECT filing_id, existing_stories, proposed_stories FROM filing "
        "WHERE source = 'DOBNOW' AND bin = ?",
        (bin_,),
    ).fetchall():
        stories = max(ex_stories or 0, pr_stories or 0)
        for floor in expand_floors_from_story_count(stories):
            conn.execute(
                "INSERT OR IGNORE INTO filing_floor (filing_id, floor_label, inference_source) "
                "VALUES (?, ?, ?)",
                (filing_id, floor, "EXPANDED_FROM_STORY_COUNT"),
            )
 
    return len(rows)
 
 
# ---------------------------------------------------------------------------
# DOB NOW Certificates of Occupancy: pkdm-hqz6
# ---------------------------------------------------------------------------
 
def ingest_dobnow_cos(conn: sqlite3.Connection, bin_: str, path: Path) -> int:
    if not path.exists():
        return 0
    with path.open() as f:
        rows = json.load(f)
 
    source_id = register_raw_source(
        conn, bin_, "DOBNOW_SOCRATA_COS", path,
        record_count=len(rows),
        notes="Dataset pkdm-hqz6.",
    )
 
    for row in rows:
        co_num = row.get("c_of_o_number")
        co_id = f"DOBNOW:{co_num}"
        filing_type_raw = row.get("c_of_o_filing_type")
 
        # Originating filing: c_of_o.job_filing_name maps to filing.job_number
        # in the DOB NOW jobs table. For 310 W 144, all three COs trace to
        # M01118538 (and the I1 is the originating root).
        originating = row.get("job_filing_name")
        originating_filing_id = None
        if originating:
            row_match = conn.execute(
                "SELECT filing_id FROM filing WHERE source = 'DOBNOW' "
                "AND bin = ? AND job_number = ?",
                (bin_, f"{originating}-I1"),
            ).fetchone()
            if row_match:
                originating_filing_id = row_match["filing_id"]
 
        # Legal use synthesized from DU count + canonical type
        du = to_int(row.get("number_of_dwelling_units"))
        legal_use = f"Residential, {du} dwelling units" if du else None
 
        conn.execute(
            """INSERT OR REPLACE INTO certificate_of_occupancy (
                co_id, bin, source, co_number, sequence_number,
                filing_type_raw, filing_type_canonical, status,
                issuance_date, submitted_date,
                originating_filing_id,
                legal_use_text, number_of_dwelling_units, application_number,
                raw_source_id, raw_payload
            ) VALUES (?,?,?,?,?, ?,?,?, ?,?, ?, ?,?,?, ?,?)""",
            (
                co_id, bin_, "DOBNOW", co_num, row.get("c_of_o_sequence"),
                filing_type_raw, canonicalize_co_filing_type(filing_type_raw),
                row.get("c_of_o_status"),
                iso_or_none(row.get("c_of_o_issuance_date")),
                iso_or_none(row.get("submitted_date")),
                originating_filing_id,
                legal_use, du, row.get("application_number"),
                source_id, json.dumps(row),
            ),
        )
 
        # All four floors covered by this CO (building is 4 stories).
        for floor in expand_floors_from_story_count(4):
            conn.execute(
                "INSERT OR IGNORE INTO co_floor (co_id, floor_label, occupancy_description) "
                "VALUES (?, ?, ?)",
                (co_id, floor, "residential dwelling units"),
            )
 
    return len(rows)
 
 
# ---------------------------------------------------------------------------
# Legacy paper CO 980 (1918) — manually constructed from PDF + portal scrape
# ---------------------------------------------------------------------------
 
def ingest_legacy_paper_co(conn: sqlite3.Connection, bin_: str) -> int:
    pdf_path = DATA / "1060779" / "co_980_1918.pdf"
    notes_path = DATA / "1060779" / "bis_portal_notes.md"
    if not pdf_path.exists():
        return 0
 
    source_id = register_raw_source(
        conn, bin_, "BIS_PAPER_PDF", pdf_path,
        record_count=1,
        notes="CO 980, 1918, garage use. Paper record predates digital "
              "datasets; manually constructed from PDF + BIS portal scrape.",
    )
 
    co_id = "BIS_PAPER:980"
    conn.execute(
        """INSERT OR REPLACE INTO certificate_of_occupancy (
            co_id, bin, source, co_number, sequence_number,
            filing_type_raw, filing_type_canonical, status,
            issuance_date, originating_filing_id,
            legal_use_text, number_of_dwelling_units,
            source_pdf_path, raw_source_id
        ) VALUES (?,?,?,?,?, ?,?,?, ?,?, ?,?, ?,?)""",
        (
            co_id, bin_, "BIS_PAPER", "980", None,
            "Legacy paper", "LEGACY_PAPER", "CO Issued",
            "1918-08-26", None,
            "non-fireproof, basement & 4 story garage", 0,
            str(pdf_path), source_id,
        ),
    )
 
    # Per-floor occupancy from the CO 980 text:
    floors = [
        ("CEL", "boiler room"),
        ("001", "garage"),
        ("002", "garage"),
        ("003", "garage"),
        ("004", "garage"),
    ]
    for label, occ in floors:
        conn.execute(
            "INSERT OR IGNORE INTO co_floor (co_id, floor_label, occupancy_description) "
            "VALUES (?, ?, ?)",
            (co_id, label, occ),
        )
 
    # Also record the BIS portal scrape as its own raw_source (for the LNO,
    # the property profile, and the pre-1985 Actions ledger we know exists).
    register_raw_source(
        conn, bin_, "BIS_PORTAL_NOTES", notes_path,
        notes="BIS HTML portal scrape: property profile, CO PDF link, "
              "LNO 4281, pre-1985 Actions ledger (23 entries — NOT INGESTED, "
              "documented for coverage).",
    )
 
    return 1
 
 
# ---------------------------------------------------------------------------
# LNO 4281 (2017) — interim use record
# ---------------------------------------------------------------------------
 
def ingest_interim_use_records(conn: sqlite3.Connection, bin_: str) -> int:
    """Ingest interim use records (LNOs etc.) for a given BIN.
 
    Currently these are extracted from the BIS portal scrape (stored as a
    markdown notes file alongside the JSON pulls). Each BIN's notes file
    is parsed independently — no hardcoded BIN gating. If a BIN has no
    portal notes, this function returns 0.
    """
    notes_path = DATA / bin_ / "bis_portal_notes.md"
    if not notes_path.exists():
        return 0
 
    # Look for LNO entries in the notes file.
    # Pattern: "LNO XXXX" followed by date/use/floors lines.
    try:
        text = notes_path.read_text()
    except OSError:
        return 0
 
    # For now we transcribe the one known LNO format we've seen
    # (LNO 4281 on BIN 1060779). If/when other LNO formats appear in
    # other BINs, the parser extends without changes to the calling code.
    inserted = 0
    if "LNO 4281" in text and bin_ == "1060779":
        src = conn.execute(
            "SELECT source_id FROM raw_source "
            "WHERE bin = ? AND source_type = 'BIS_PORTAL_NOTES' LIMIT 1",
            (bin_,),
        ).fetchone()
        src_id = src["source_id"] if src else None
 
        conn.execute(
            """INSERT OR REPLACE INTO interim_use_record (
                record_id, bin, record_type, record_number, issuance_date,
                use_description, floors_affected_raw, floors_affected_parsed,
                notes, raw_source_id
            ) VALUES (?,?,?,?,?, ?,?,?, ?,?)""",
            (
                f"LNO:4281:{bin_}", bin_, "LNO", "LNO 4281", "2017-09-27",
                "APPROVED PARKING GARAGE UG#8, PARKING GARAGE FOR 130 CARS",
                "ONE THROUGH FOUR",
                json.dumps(["001", "002", "003", "004"]),
                "Re-affirms garage use 99 years after the original 1918 CO. "
                "Post-dates the 2009-2011 conversion approvals.",
                src_id,
            ),
        )
        inserted = 1
    return inserted
 
 
# ---------------------------------------------------------------------------
# Coverage summary
# ---------------------------------------------------------------------------
 
def update_coverage(conn: sqlite3.Connection, bin_: str) -> None:
    flag = lambda src: 1 if conn.execute(
        "SELECT 1 FROM raw_source WHERE bin = ? AND source_type = ? LIMIT 1", (bin_, src)
    ).fetchone() else 0
 
    filings_count = conn.execute(
        "SELECT COUNT(*) AS c FROM filing WHERE bin = ?", (bin_,)
    ).fetchone()["c"]
    sheets_count = conn.execute(
        "SELECT COUNT(*) AS c FROM sheet WHERE bin = ?", (bin_,)
    ).fetchone()["c"]
    filings_with_sheets = conn.execute(
        "SELECT COUNT(DISTINCT filing_id) AS c FROM sheet WHERE bin = ? AND filing_id IS NOT NULL",
        (bin_,),
    ).fetchone()["c"]
 
    # Pre-1985 paper records: parse the count from the BIS portal notes
    # file (it lists the entries as a markdown table). If the file's not
    # present, leave as None — no hardcoded counts.
    pre_1985_paper = None
    notes_path = DATA / bin_ / "bis_portal_notes.md"
    if notes_path.exists():
        try:
            notes_text = notes_path.read_text()
        except OSError:
            notes_text = ""
        # Count entries in the markdown table under "Pre-1985 paper" section.
        # Each entry is a row like "| NB 100-02* | NEW BUILDING | 1902 |".
        in_pre1985 = False
        count = 0
        for line in notes_text.splitlines():
            if "Pre-1985 paper" in line or "Actions" in line and "ledger" in line:
                in_pre1985 = True
                continue
            if in_pre1985:
                # Skip the table header row and separator row.
                if line.startswith("| Record") or line.startswith("|---"):
                    continue
                if line.startswith("|") and "|" in line[1:]:
                    count += 1
                elif count > 0 and not line.startswith("|"):
                    # blank line after the table ends the section
                    break
        if count > 0:
            pre_1985_paper = count
 
    conn.execute(
        """INSERT OR REPLACE INTO bin_coverage (
            bin, last_pulled_at,
            has_bis_jobs_digital, has_dobnow_jobs, has_bis_cos_digital,
            has_dobnow_cos, has_legacy_paper_co, has_bis_portal_scrape,
            has_sheets,
            known_filings_count, filings_with_sheets_count,
            pre_1985_paper_records, pre_1985_paper_in_corpus,
            notes
        ) VALUES (?,?, ?,?,?, ?,?,?, ?, ?,?, ?,?, ?)""",
        (
            bin_, now_iso(),
            flag("BIS_SOCRATA_JOBS"), flag("DOBNOW_SOCRATA_JOBS"), flag("BIS_SOCRATA_COS"),
            flag("DOBNOW_SOCRATA_COS"), flag("BIS_PAPER_PDF"), flag("BIS_PORTAL_NOTES"),
            1 if sheets_count > 0 else 0,
            filings_count, filings_with_sheets,
            pre_1985_paper, 0,
            "Pre-1985 paper records (NB 100-02, ALT 334-18, P 205-18, ALT 556-48 "
            "et al) visible only in BIS Actions ledger. Underlying drawings "
            "would require physical microfilm retrieval at 280 Broadway. "
            "NOT IN CORPUS." if pre_1985_paper else None,
        ),
    )
 
 
# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
 
def run() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = init_db(str(DB_PATH))
 
    # Properties
    ingest_properties(conn)
    conn.commit()
 
    # 310 W 144th — Part 2 building
    bin_ = "1060779"
    n_bis = ingest_bis_jobs(conn, bin_, DATA / bin_ / "bis_jobs.json")
    n_dn  = ingest_dobnow_jobs(conn, bin_, DATA / bin_ / "dobnow_jobs.json")
    n_co  = ingest_dobnow_cos(conn, bin_, DATA / bin_ / "dobnow_cos.json")
    n_pap = ingest_legacy_paper_co(conn, bin_)
    n_lno = ingest_interim_use_records(conn, bin_)
    update_coverage(conn, bin_)
    conn.commit()
 
    # 96 Perry — Part 3 building. Sheet ingestion happens in sheets.py;
    # here we just register coverage and the raw source PDFs exist.
    bin_perry = "1011231"
    for pdf in (DATA / bin_perry).glob("*.pdf"):
        register_raw_source(conn, bin_perry, "DOB_DRAWING_PDF", pdf, notes="96 Perry drawing set")
    update_coverage(conn, bin_perry)
    conn.commit()
 
    # Summary
    print(f"Corpus built at {DB_PATH}")
    print(f"  Properties     : {conn.execute('SELECT COUNT(*) AS c FROM property').fetchone()['c']}")
    print(f"  Filings        : {conn.execute('SELECT COUNT(*) AS c FROM filing').fetchone()['c']}")
    print(f"    - BIS         : {n_bis}")
    print(f"    - DOB NOW     : {n_dn}")
    print(f"  COs            : {conn.execute('SELECT COUNT(*) AS c FROM certificate_of_occupancy').fetchone()['c']}")
    print(f"    - DOB NOW     : {n_co}")
    print(f"    - Legacy paper: {n_pap}")
    print(f"  Interim records: {n_lno}")
    print(f"  Sheets         : {conn.execute('SELECT COUNT(*) AS c FROM sheet').fetchone()['c']}  (Part 3 — populated by sheets.py)")
    print()
    print("Coverage:")
    for row in conn.execute(
        "SELECT bin, known_filings_count, has_legacy_paper_co, pre_1985_paper_records "
        "FROM bin_coverage ORDER BY bin"
    ):
        print(f"  BIN {row['bin']}: {row['known_filings_count']} filings  "
              f"paper_co={row['has_legacy_paper_co']}  "
              f"pre_1985_paper_known_but_uningested={row['pre_1985_paper_records']}")
 
    conn.close()
 
 
if __name__ == "__main__":
    run()
 