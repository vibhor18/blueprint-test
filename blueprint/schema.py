"""
Blueprint corpus schema — SQLite.
 
Design principles:
  1. Every record carries provenance. We never lose where a value came from.
  2. Every record carries confidence. Inferences are flagged as such.
  3. Coverage is a first-class concept. We can always answer "what is in
     the corpus and what is missing?" for any BIN.
  4. The schema is honest about NYC's two-system reality: BIS (legacy) and
     DOB NOW (post-2013), plus paper records that predate both.
 
Hierarchy:
    property (BBL+BIN)
      ├── filing (BIS jobs and DOB NOW filings)
      │     ├── filing_floor
      │     └── sheet
      │           ├── sheet_region
      │           └── finding
      ├── certificate_of_occupancy
      │     └── co_floor
      ├── interim_use_record  (LNOs etc.)
      └── (sheet_index_entry — what the drawings claim should exist)
 
Provenance flows through raw_source. Coverage is summarised in bin_coverage.
"""
 
import sqlite3
from pathlib import Path
 
 
SCHEMA_SQL = """
 
-- Provenance: one row per raw input we've ingested.
CREATE TABLE IF NOT EXISTS raw_source (
    source_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    bin                 TEXT NOT NULL,
    source_type         TEXT NOT NULL,    -- 'BIS_SOCRATA_JOBS' | 'DOBNOW_SOCRATA_JOBS'
                                          --   | 'DOBNOW_SOCRATA_COS' | 'BIS_SOCRATA_COS'
                                          --   | 'BIS_PORTAL_NOTES' | 'BIS_PAPER_PDF'
                                          --   | 'DOB_DRAWING_PDF'
    source_uri          TEXT,
    pulled_at           TEXT NOT NULL,
    file_path           TEXT,
    record_count        INTEGER,
    notes               TEXT
);
 
 
-- The top-level real-world entity: a building.
CREATE TABLE IF NOT EXISTS property (
    bin                     TEXT PRIMARY KEY,
    bbl                     TEXT NOT NULL,
    borough                 TEXT NOT NULL,
    block                   TEXT NOT NULL,
    lot                     TEXT NOT NULL,
    house_no                TEXT,
    street_name             TEXT,
    zip_code                TEXT,
    zoning_district         TEXT,
    map_number              TEXT,
    dof_building_class      TEXT,
    construction_class      TEXT,
    occupancy_use_groups    TEXT,         -- JSON array, multi-use buildings have several
    hpd_multiple_dwelling   TEXT,
    landmark_status         TEXT,
    existing_stories        INTEGER,
    existing_height_ft      REAL,
    gross_floor_area_sf     INTEGER,
    notes                   TEXT
);
 
 
-- A single filing: a job/permit/CO application as filed with DOB.
-- Covers BIS (job# + doc#) and DOB NOW (filing#) in one schema.
CREATE TABLE IF NOT EXISTS filing (
    filing_id                   TEXT PRIMARY KEY,    -- e.g. 'BIS:110445974:01' | 'DOBNOW:M01118538-I1'
    source                      TEXT NOT NULL,       -- 'BIS' | 'DOBNOW'
    bin                         TEXT NOT NULL REFERENCES property(bin),
    job_number                  TEXT NOT NULL,
    doc_number                  TEXT,                -- BIS only
 
    job_type                    TEXT,                -- 'A1','A2','A3','NB','DM','Alteration CO', etc.
    filing_status_raw           TEXT,
    filing_status_canonical     TEXT,                -- 'APPROVED' | 'DISAPPROVED' | 'WITHDRAWN'
                                                     --   | 'PERMIT_ISSUED' | 'CO_ISSUED'
                                                     --   | 'LOC_ISSUED' | 'IN_PROCESS' | 'OTHER'
    withdrawal_flag             INTEGER DEFAULT 0,
    withdrawal_date             TEXT,
 
    pre_filing_date             TEXT,
    approved_date               TEXT,
    latest_action_date          TEXT,
 
    applicant_first_name        TEXT,
    applicant_last_name         TEXT,
    applicant_business_name     TEXT,
    applicant_license_type      TEXT,
    applicant_license_number    TEXT,
 
    design_firm                 TEXT,
    filing_rep_name             TEXT,
    filing_rep_business         TEXT,
 
    existing_occupancy          TEXT,
    proposed_occupancy          TEXT,
    existing_dwelling_units     INTEGER,
    proposed_dwelling_units     INTEGER,
    existing_stories            INTEGER,
    proposed_stories            INTEGER,
 
    change_in_use               INTEGER,             -- 0/1, NULL if unknown
    change_in_egress            INTEGER,
    change_in_occupancy         INTEGER,
    area_of_work_sf             INTEGER,
    occupant_load               INTEGER,
 
    work_types                  TEXT,                -- JSON array
 
    job_description             TEXT,
 
    -- Cluster relationships (no FK on cluster_id because root rows
    -- self-reference and we want flexible load order).
    parent_filing_id            TEXT,
    cluster_id                  TEXT,                -- root filing_id of the cluster
    cluster_inference_method    TEXT,                -- 'FILING_NUMBER_STEM'
                                                     --   | 'DESCRIPTION_REFERENCE'
                                                     --   | 'EXPLICIT_PARENT'
                                                     --   | 'SELF_ROOT'
 
    raw_source_id               INTEGER REFERENCES raw_source(source_id),
    raw_payload                 TEXT
);
 
CREATE INDEX IF NOT EXISTS idx_filing_bin ON filing(bin);
CREATE INDEX IF NOT EXISTS idx_filing_cluster ON filing(cluster_id);
CREATE INDEX IF NOT EXISTS idx_filing_status ON filing(filing_status_canonical);
 
 
-- Which floors a filing's scope touches.
CREATE TABLE IF NOT EXISTS filing_floor (
    filing_id           TEXT NOT NULL REFERENCES filing(filing_id),
    floor_label         TEXT NOT NULL,               -- 'CEL','001','002',...,'ROOF','BULK'
    inference_source    TEXT NOT NULL,               -- 'EXPLICIT_FROM_BIS'
                                                     --   | 'EXPANDED_FROM_STORY_COUNT'
                                                     --   | 'PARSED_FROM_DESCRIPTION'
    PRIMARY KEY (filing_id, floor_label)
);
 
 
-- Certificates of Occupancy. First-class entity. Spans paper, BIS-digital, DOB NOW.
CREATE TABLE IF NOT EXISTS certificate_of_occupancy (
    co_id                       TEXT PRIMARY KEY,
    bin                         TEXT NOT NULL REFERENCES property(bin),
    source                      TEXT NOT NULL,       -- 'BIS_PAPER' | 'BIS_DIGITAL' | 'DOBNOW'
    co_number                   TEXT NOT NULL,       -- '980' or '1060779-0000003'
    sequence_number             TEXT,
    filing_type_raw             TEXT,                -- 'Initial' | 'Final' | 'Renewal Without Change'
    filing_type_canonical       TEXT,                -- 'TEMPORARY' | 'RENEWAL' | 'FINAL'
                                                     --   | 'AMENDED' | 'LEGACY_PAPER'
    status                      TEXT,
    issuance_date               TEXT NOT NULL,
    submitted_date              TEXT,
    originating_filing_id       TEXT REFERENCES filing(filing_id),
    legal_use_text              TEXT,
    number_of_dwelling_units    INTEGER,
    application_number          TEXT,
    source_pdf_path             TEXT,
    raw_source_id               INTEGER REFERENCES raw_source(source_id),
    raw_payload                 TEXT
);
 
CREATE INDEX IF NOT EXISTS idx_co_bin ON certificate_of_occupancy(bin);
CREATE INDEX IF NOT EXISTS idx_co_issuance ON certificate_of_occupancy(issuance_date);
 
 
-- Per-floor occupancy as certified by a CO.
CREATE TABLE IF NOT EXISTS co_floor (
    co_id                       TEXT NOT NULL REFERENCES certificate_of_occupancy(co_id),
    floor_label                 TEXT NOT NULL,
    occupancy_description       TEXT,
    PRIMARY KEY (co_id, floor_label)
);
 
 
-- Non-CO legal-use records (Letters of No Objection, Letters of Completion-for-use, etc).
CREATE TABLE IF NOT EXISTS interim_use_record (
    record_id                   TEXT PRIMARY KEY,
    bin                         TEXT NOT NULL REFERENCES property(bin),
    record_type                 TEXT NOT NULL,       -- 'LNO' | 'LOC' | 'OTHER'
    record_number               TEXT,
    issuance_date               TEXT,
    use_description             TEXT,
    floors_affected_raw         TEXT,                -- raw text from source
    floors_affected_parsed      TEXT,                -- JSON array of normalized floor labels
    notes                       TEXT,
    raw_source_id               INTEGER REFERENCES raw_source(source_id)
);
 
 
-- -----------------------------------------------------------------------
-- Part 3: drawing sheets
-- -----------------------------------------------------------------------
 
CREATE TABLE IF NOT EXISTS sheet (
    sheet_id                    TEXT PRIMARY KEY,
    bin                         TEXT NOT NULL REFERENCES property(bin),
    filing_id                   TEXT REFERENCES filing(filing_id),
    sheet_number                TEXT,                -- 'EN-001.00' | 'A-001.00'
    drawing_title               TEXT,
    scale                       TEXT,
    architect_of_record         TEXT,
    design_firm                 TEXT,
    dob_scan_code               TEXT,
    dob_stamp_date              TEXT,
    dob_audit_accepted          INTEGER DEFAULT 0,
    pdf_path                    TEXT NOT NULL,
    pdf_page_number             INTEGER NOT NULL,
    pdf_creation_date           TEXT,
    pdf_modification_date       TEXT,
    pdf_producer                TEXT,
    pdf_embedded_title          TEXT,
    source_format               TEXT,                -- 'BORN_DIGITAL' | 'SCANNED_UPLOAD' | 'MICROFILM'
    legibility_score            TEXT,                -- 'HIGH' | 'MEDIUM' | 'LOW'
    legibility_notes            TEXT,
    professional_certifier      TEXT,                -- e.g. 'QWA Studio' if Prof. Cert. used.
                                                     --   Distinct from architect_of_record:
                                                     --   the AOR signs the drawings; the
                                                     --   certifier signs the audit-acceptance.
    professional_certifier_date TEXT,                -- ISO date of certification
    is_canonical                INTEGER,             -- NULL = unresolved
    canonical_reasoning         TEXT,
    raw_source_id               INTEGER REFERENCES raw_source(source_id)
);
 
CREATE INDEX IF NOT EXISTS idx_sheet_filing ON sheet(filing_id);
 
 
-- Functional regions on a sheet: a single page may carry many regions
-- (floor plan + demo + detail + legend on one A-001 sheet, e.g.).
CREATE TABLE IF NOT EXISTS sheet_region (
    region_id                   TEXT PRIMARY KEY,
    sheet_id                    TEXT NOT NULL REFERENCES sheet(sheet_id),
    region_type                 TEXT NOT NULL,       -- 'title_block' | 'plot_plan' | 'floor_plan'
                                                     --   | 'demolition_plan' | 'general_notes'
                                                     --   | 'scope_of_work' | 'energy_compliance'
                                                     --   | 'index_of_drawings' | 'legend'
                                                     --   | 'detail' | 'section' | 'project_data'
    region_label                TEXT,
    bbox                        TEXT,                -- JSON [x1,y1,x2,y2] in PDF coords
    extracted_text              TEXT,
    extracted_fields            TEXT,                -- JSON structured extraction
    extraction_method           TEXT,                -- 'PDF_TEXT_LAYER' | 'OCR' | 'VISION' | 'MANUAL'
    extraction_confidence       TEXT
);
 
CREATE INDEX IF NOT EXISTS idx_region_sheet ON sheet_region(sheet_id);
 
 
-- What the drawing's own Index of Drawings table claims should exist.
CREATE TABLE IF NOT EXISTS sheet_index_entry (
    entry_id                    TEXT PRIMARY KEY,
    filing_id                   TEXT REFERENCES filing(filing_id),
    sheet_number                TEXT NOT NULL,
    sheet_title                 TEXT,
    discipline                  TEXT,                -- 'AR' | 'MP' | 'SP' | 'EL' | 'EN' | 'STR' | 'SITE'
    source_sheet_id             TEXT REFERENCES sheet(sheet_id),
    found_in_corpus             INTEGER DEFAULT 0
);
 
 
-- Actionable, citable facts pulled from a region.
CREATE TABLE IF NOT EXISTS finding (
    finding_id                  TEXT PRIMARY KEY,
    region_id                   TEXT NOT NULL REFERENCES sheet_region(region_id),
    finding_type                TEXT NOT NULL,       -- 'scope' | 'compliance' | 'inspection'
                                                     --   | 'safety' | 'material_spec' | 'demolition'
                                                     --   | 'no_substitution_flag' | 'conflict_flag'
    finding_text                TEXT NOT NULL,
    is_flag                     INTEGER DEFAULT 0,
    confidence                  TEXT
);
 
 
 
-- Amendment diffs: firm or certifier changes detected across sheets
-- belonging to the same job. One row per detected change event.
-- The temporal sequence is the value: who held the role at filing,
-- who holds it at audit-acceptance, and when each event occurred.
CREATE TABLE IF NOT EXISTS filing_amendment_diff (
    diff_id             TEXT PRIMARY KEY,
    bin                 TEXT NOT NULL REFERENCES property(bin),
    job_number          TEXT NOT NULL,
    field_name          TEXT NOT NULL,
    prior_value         TEXT,
    new_value           TEXT NOT NULL,
    prior_date          TEXT,
    new_date            TEXT NOT NULL,
    change_type         TEXT,
    notes               TEXT,
    raw_source_id       INTEGER REFERENCES raw_source(source_id)
);
 
-- -----------------------------------------------------------------------
-- Coverage: what we have and what we know is missing.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bin_coverage (
    bin                         TEXT PRIMARY KEY REFERENCES property(bin),
    last_pulled_at              TEXT,
    has_bis_jobs_digital        INTEGER DEFAULT 0,
    has_dobnow_jobs             INTEGER DEFAULT 0,
    has_bis_cos_digital         INTEGER DEFAULT 0,
    has_dobnow_cos              INTEGER DEFAULT 0,
    has_legacy_paper_co         INTEGER DEFAULT 0,
    has_bis_portal_scrape       INTEGER DEFAULT 0,
    has_sheets                  INTEGER DEFAULT 0,
    known_filings_count         INTEGER,
    filings_with_sheets_count   INTEGER,
    pre_1985_paper_records      INTEGER,
    pre_1985_paper_in_corpus    INTEGER DEFAULT 0,
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
    print(f"Tables ({len(tables)}):")
    for t in tables:
        print(f"  - {t}")
    conn.close()
 