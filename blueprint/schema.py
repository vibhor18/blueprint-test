"""
Blueprint corpus schema — SQLite.

Design philosophy:
- Every record carries its provenance. We never silently lose where data came from.
- Every record carries a confidence level. Inferences are flagged as such.
- Coverage is a first-class concept, not an afterthought. We can always answer
  "what does the system have, and what is it missing?" for any BIN.
- The schema is honest about NYC's actual data shape: two systems (BIS and
  DOB NOW), paper records that predate both, and filings that cluster around
  primaries with no explicit foreign key.

Hierarchy (top down):
    property (BBL+BIN)
      ├── filing (job/permit/CO-job)                  many per property
      │     ├── filing_floor                          many per filing
      │     └── sheet (Part 3)                        many per filing
      │           └── sheet_region                    many per sheet
      │                 └── finding                   many per region
      ├── certificate_of_occupancy                    many per property
      │     └── co_floor                              many per CO
      └── interim_use_record (LNOs, etc.)             many per property

Provenance/coverage is tracked across a sidecar `raw_source` table that every
ingested record points to, plus a `bin_coverage` table that summarises what
data we have for each BIN.
"""

import sqlite3
from pathlib import Path


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
-- Provenance: every raw input file or API response we've ingested.
-- Other tables reference this so we can always trace a value back to its source.
CREATE TABLE IF NOT EXISTS raw_source (
    source_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    bin                 TEXT NOT NULL,
    source_type         TEXT NOT NULL,    -- 'BIS_SOCRATA_JOBS' | 'DOBNOW_SOCRATA_JOBS'
                                          --   | 'DOBNOW_SOCRATA_COS' | 'BIS_SOCRATA_COS'
                                          --   | 'BIS_PORTAL_HTML' | 'BIS_PAPER_PDF'
                                          --   | 'DOB_DRAWING_PDF'
    source_uri          TEXT,             -- original URL or file path
    pulled_at           TEXT NOT NULL,    -- ISO timestamp
    file_path           TEXT,             -- local file path (if applicable)
    notes               TEXT
);

-- The top-level real-world entity: a building on a lot.
-- One row per BIN. BBL is denormalized here because most queries enter via BIN.
CREATE TABLE IF NOT EXISTS property (
    bin                     TEXT PRIMARY KEY,
    bbl                     TEXT NOT NULL,
    borough                 TEXT NOT NULL,
    block                   TEXT NOT NULL,
    lot                     TEXT NOT NULL,
    house_no                TEXT,
    street_name             TEXT,
    zip_code                TEXT,
    dof_building_class      TEXT,         -- e.g. 'D1-ELEVATOR APT'
    hpd_multiple_dwelling   TEXT,         -- 'Y' / 'N' / NULL
    landmark_status         TEXT,
    existing_stories        INTEGER,
    existing_height_ft      REAL,
    notes                   TEXT
);

-- A filing = a single job/permit/CO-application as filed with DOB.
-- Captures both BIS-era (job# + doc#) and DOB NOW (filing#) records under one model.
--
-- Why composite filing_id: BIS jobs use (job_number, doc_number) — doc_number is
-- the amendment sequence (01 = initial, 02 = first amendment, etc.). DOB NOW
-- uses a single suffixed filing number like 'M01118538-I1' (I1 = initial, P# =
-- post-approval, S# = subsequent, A# = amendment). We bake the source into the
-- ID so the two never collide.
--
-- Cluster relationships:
--   parent_filing_id: pointer to the controlling primary filing (nullable)
--   cluster_id: the filing_id of the cluster's root. The root's cluster_id =
--               its own filing_id. Children share the root's cluster_id.
--               Two ways we infer this:
--                 (a) DOB NOW filing-number stem (M01118538-* all share stem)
--                 (b) BIS job descriptions that reference another job
--                     ("...IN CONJUNCTION WITH APPLICATION#110445974")
--               cluster_inference_method tracks which signal produced the link.
CREATE TABLE IF NOT EXISTS filing (
    filing_id                   TEXT PRIMARY KEY,    -- 'BIS:110445974:01' | 'DOBNOW:M01118538-I1'
    source                      TEXT NOT NULL,       -- 'BIS' | 'DOBNOW'
    bin                         TEXT NOT NULL REFERENCES property(bin),
    job_number                  TEXT NOT NULL,       -- '110445974' | 'M01118538-I1'
    doc_number                  TEXT,                -- '01' for BIS; NULL for DOB NOW
    job_type                    TEXT,                -- raw: 'A1','A2','A3','NB','DM','Alteration CO', etc.

    -- Status, with both raw and canonical forms
    filing_status_raw           TEXT,
    filing_status_canonical     TEXT,                -- one of:
                                                     --   'APPROVED', 'DISAPPROVED', 'WITHDRAWN',
                                                     --   'PERMIT_ISSUED', 'CO_ISSUED', 'LOC_ISSUED',
                                                     --   'IN_PROCESS', 'OTHER'
    withdrawal_flag             INTEGER DEFAULT 0,
    withdrawal_date             TEXT,

    -- Dates (ISO)
    pre_filing_date             TEXT,
    approved_date               TEXT,
    latest_action_date          TEXT,

    -- People
    applicant_first_name        TEXT,
    applicant_last_name         TEXT,
    applicant_business_name     TEXT,
    applicant_license_type      TEXT,                -- 'PE' | 'RA' | etc.
    applicant_license_number    TEXT,

    -- Occupancy and dwelling unit counts (existing → proposed)
    existing_occupancy          TEXT,                -- 'J-2' (old code) | 'R-2' (new code) | 'COM' | etc.
    proposed_occupancy          TEXT,
    existing_dwelling_units     INTEGER,
    proposed_dwelling_units     INTEGER,
    existing_stories            INTEGER,
    proposed_stories            INTEGER,

    -- Scope
    job_description             TEXT,

    -- Cluster relationships
    parent_filing_id            TEXT REFERENCES filing(filing_id),
    cluster_id                  TEXT,                -- root filing_id of the cluster
    cluster_inference_method    TEXT,                -- 'FILING_NUMBER_STEM' | 'DESCRIPTION_REFERENCE'
                                                     --   | 'EXPLICIT_PARENT' | NULL (= root)

    -- Provenance
    raw_source_id               INTEGER REFERENCES raw_source(source_id),
    raw_payload                 TEXT                 -- original Socrata row, JSON
);

CREATE INDEX IF NOT EXISTS idx_filing_bin ON filing(bin);
CREATE INDEX IF NOT EXISTS idx_filing_cluster ON filing(cluster_id);
CREATE INDEX IF NOT EXISTS idx_filing_status ON filing(filing_status_canonical);

-- Which floors a filing's scope touches.
-- Floor labels are NYC's: 'CEL', 'SUB', '001', '002', ..., 'ROOF', 'BULK'.
-- BIS portal exposes this as "Work on Floor(s): CEL 001 thru 005" — we parse
-- that into individual rows. DOB NOW exposes it only via existing_stories /
-- proposed_stories counts (no per-floor list); for whole-building work we
-- expand to all floors of the building and flag the inference.
CREATE TABLE IF NOT EXISTS filing_floor (
    filing_id           TEXT NOT NULL REFERENCES filing(filing_id),
    floor_label         TEXT NOT NULL,
    inference_source    TEXT NOT NULL,    -- 'EXPLICIT_FROM_BIS' | 'EXPANDED_FROM_STORY_COUNT'
                                          --   | 'PARSED_FROM_DESCRIPTION'
    PRIMARY KEY (filing_id, floor_label)
);

-- Certificates of Occupancy. The most important entity for Part 2.
-- One row per CO issued. Spans paper (CO 980 from 1918), BIS digital (none for
-- this building, but supported), and DOB NOW.
--
-- filing_type_canonical normalizes across systems:
--   'TEMPORARY'  — BIS TCO, or DOB NOW 'Initial' on an active job
--                  (DOB NOW issues an Initial CO on a job that hasn't yet
--                   reached final-CO-eligible state; behaviorally a TCO)
--   'RENEWAL'    — BIS TCO renewal, or DOB NOW 'Renewal Without Change'
--   'FINAL'      — BIS final CO, or DOB NOW 'Final'
--   'AMENDED'    — DOB NOW 'Amended'
--   'LEGACY_PAPER' — pre-digital, treated as final unless superseded
CREATE TABLE IF NOT EXISTS certificate_of_occupancy (
    co_id                       TEXT PRIMARY KEY,    -- our internal ID
    bin                         TEXT NOT NULL REFERENCES property(bin),
    source                      TEXT NOT NULL,       -- 'BIS_PAPER' | 'BIS_DIGITAL' | 'DOBNOW'
    co_number                   TEXT NOT NULL,       -- '980' or '1060779-0000003'
    sequence_number             TEXT,                -- DOB NOW: '83682'
    filing_type_raw             TEXT,                -- 'Initial' | 'Final' | etc.
    filing_type_canonical       TEXT,                -- see comment above
    status                      TEXT,                -- 'CO Issued' / 'In Process' / etc.

    issuance_date               TEXT NOT NULL,       -- ISO
    submitted_date              TEXT,                -- ISO; CO application submitted

    -- The job that produced this CO. NULL for the 1918 paper CO (its
    -- originating ALT predates the digital era).
    originating_filing_id       TEXT REFERENCES filing(filing_id),

    -- The legal use this CO certifies
    legal_use_text              TEXT,                -- e.g. 'non-fireproof, basement & 4 story garage'
                                                     --       or 'residential, 48 dwelling units'
    number_of_dwelling_units    INTEGER,
    application_number          TEXT,                -- DOB NOW: 'CO-000083682'

    -- Provenance
    source_pdf_path             TEXT,
    raw_source_id               INTEGER REFERENCES raw_source(source_id),
    raw_payload                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_co_bin ON certificate_of_occupancy(bin);
CREATE INDEX IF NOT EXISTS idx_co_issuance ON certificate_of_occupancy(issuance_date);

-- Per-floor occupancy as certified by a CO.
-- For the 1918 CO 980: cellar = boiler room, floors 1-4 = garage.
-- For the 2025 CO: floors 1-4 + cellar = residential dwelling units.
CREATE TABLE IF NOT EXISTS co_floor (
    co_id                       TEXT NOT NULL REFERENCES certificate_of_occupancy(co_id),
    floor_label                 TEXT NOT NULL,
    occupancy_description       TEXT,                -- 'garage' | 'boiler room' | 'residential'
    PRIMARY KEY (co_id, floor_label)
);

-- Non-CO legal-use records: Letters of No Objection, Letters of Completion
-- referenced for use, etc. LNO 4281 (2017, garage, 130 cars, floors 1-4) is the
-- canonical example for this BIN. These are not COs but they carry legal
-- weight as use-confirmation evidence in regulatory disputes.
CREATE TABLE IF NOT EXISTS interim_use_record (
    record_id                   TEXT PRIMARY KEY,
    bin                         TEXT NOT NULL REFERENCES property(bin),
    record_type                 TEXT NOT NULL,       -- 'LNO' | 'LOC' | 'OTHER'
    record_number               TEXT,                -- 'LNO 4281'
    issuance_date               TEXT,
    use_description             TEXT,                -- 'PARKING GARAGE UG#8 ...'
    floors_affected             TEXT,                -- raw text: 'ONE THROUGH FOUR'
    notes                       TEXT,
    raw_source_id               INTEGER REFERENCES raw_source(source_id)
);

-- ---------------------------------------------------------------------------
-- Part 3: drawing sheets
-- ---------------------------------------------------------------------------

-- A single drawing sheet (typically one page of a drawing PDF).
-- One row per sheet. Same sheet number across different versions = separate rows.
-- is_canonical is derived after all sheets for a job are ingested.
CREATE TABLE IF NOT EXISTS sheet (
    sheet_id                    TEXT PRIMARY KEY,    -- e.g. '1011231:140941514:EN-001.00:2020'
    bin                         TEXT NOT NULL REFERENCES property(bin),
    filing_id                   TEXT REFERENCES filing(filing_id),
    sheet_number                TEXT,                -- 'EN-001.00' | 'A-001.00'
    drawing_title               TEXT,                -- 'PLOT PLAN, NOTES, SCOPE OF WORK, FLOOR PLAN'
    scale                       TEXT,                -- '1/4 inch = 1 foot' (sheet-level if uniform)
    architect_of_record         TEXT,
    design_firm                 TEXT,
    dob_scan_code               TEXT,                -- 'ES905073356'
    dob_stamp_date              TEXT,                -- ISO
    dob_audit_accepted          INTEGER DEFAULT 0,   -- bool
    pdf_path                    TEXT NOT NULL,
    pdf_page_number             INTEGER NOT NULL,
    pdf_creation_date           TEXT,                -- from PDF metadata
    pdf_modification_date       TEXT,                -- from PDF metadata
    pdf_producer                TEXT,                -- from PDF metadata
    pdf_embedded_title          TEXT,                -- from PDF metadata
    is_canonical                INTEGER,             -- bool; NULL until resolved
    canonical_reasoning         TEXT,
    raw_source_id               INTEGER REFERENCES raw_source(source_id)
);

CREATE INDEX IF NOT EXISTS idx_sheet_filing ON sheet(filing_id);

-- A functional region on a sheet. One sheet can carry multiple regions
-- (the A-001 sheet carries: legend, demolition plan, proposed floor plan,
--  door saddle detail, two tile details, section at shower floor, landlord
--  notes, landlord general notes — all on one page).
--
-- bbox stored as JSON [x1,y1,x2,y2] in PDF coordinate space.
-- extracted_fields stored as JSON; structure depends on region_type.
CREATE TABLE IF NOT EXISTS sheet_region (
    region_id                   TEXT PRIMARY KEY,
    sheet_id                    TEXT NOT NULL REFERENCES sheet(sheet_id),
    region_type                 TEXT NOT NULL,       -- 'title_block' | 'plot_plan' | 'floor_plan'
                                                     --   | 'demolition_plan' | 'general_notes'
                                                     --   | 'scope_of_work' | 'energy_compliance'
                                                     --   | 'index_of_drawings' | 'legend'
                                                     --   | 'detail' | 'section' | 'project_data'
    region_label                TEXT,                -- 'Proposed Partial 5th Floor Plan (Apt#B17-B)'
    bbox                        TEXT,                -- JSON [x1,y1,x2,y2]
    extracted_text              TEXT,                -- raw text in the region
    extracted_fields            TEXT,                -- JSON structured extraction
    extraction_method           TEXT,                -- 'PDF_TEXT_LAYER' | 'OCR' | 'VISION_MODEL' | 'MANUAL'
    extraction_confidence       TEXT                 -- 'HIGH' | 'MEDIUM' | 'LOW'
);

CREATE INDEX IF NOT EXISTS idx_region_sheet ON sheet_region(sheet_id);
CREATE INDEX IF NOT EXISTS idx_region_type ON sheet_region(region_type);

-- An actionable fact extracted from a region, citable back to source.
CREATE TABLE IF NOT EXISTS finding (
    finding_id                  TEXT PRIMARY KEY,
    region_id                   TEXT NOT NULL REFERENCES sheet_region(region_id),
    finding_type                TEXT NOT NULL,       -- 'scope' | 'compliance' | 'inspection'
                                                     --   | 'safety' | 'material_spec' | 'demolition'
                                                     --   | 'no_substitution_flag' | 'conflict_flag'
    finding_text                TEXT NOT NULL,
    is_flag                     INTEGER DEFAULT 0,   -- bool: surface this on top of results
    confidence                  TEXT                 -- 'HIGH' | 'MEDIUM' | 'LOW'
);

-- ---------------------------------------------------------------------------
-- Coverage: what the system has and doesn't have, per BIN.
-- The most important thing the system can tell users honestly.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bin_coverage (
    bin                         TEXT PRIMARY KEY REFERENCES property(bin),
    last_pulled_at              TEXT,
    has_bis_jobs_digital        INTEGER DEFAULT 0,   -- pulled ic3t-wcy2
    has_dobnow_jobs             INTEGER DEFAULT 0,   -- pulled w9ak-ipjd
    has_bis_cos_digital         INTEGER DEFAULT 0,   -- pulled bs8b-p36w (may be empty)
    has_dobnow_cos              INTEGER DEFAULT 0,   -- pulled pkdm-hqz6
    has_legacy_paper_co         INTEGER DEFAULT 0,   -- the 1918 CO 980 PDF
    has_bis_portal_scrape       INTEGER DEFAULT 0,
    has_sheets                  INTEGER DEFAULT 0,
    known_filings_count         INTEGER,
    filings_with_sheets_count   INTEGER,
    pre_1985_paper_records      INTEGER,             -- count from BIS Actions ledger
    pre_1985_paper_in_corpus    INTEGER DEFAULT 0,   -- microfilm not pulled
    notes                       TEXT
);
"""


def init_db(db_path: str = "corpus.db") -> sqlite3.Connection:
    """Create or open the corpus database and ensure schema is applied."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


if __name__ == "__main__":
    here = Path(__file__).resolve().parent.parent
    db_path = here / "corpus.db"
    conn = init_db(str(db_path))
    tables = [row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    print(f"Initialized {db_path}")
    print("Tables:")
    for t in tables:
        print(f"  - {t}")
    conn.close()
