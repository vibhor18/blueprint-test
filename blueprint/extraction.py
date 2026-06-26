
"""
Target-field-list extraction renderer.
 
The assignment lists six buckets of fields that "a useful extraction of a
sheet/filing should produce — pull what's present, flag what isn't". This
module walks those buckets for a given (BIN, job_number), pulls values
from the corpus where present, and explicitly marks "not on sheet" where
the field isn't recorded in any source we have.
 
It reads ONLY from corpus tables — `property`, `filing`, `sheet`,
`sheet_region`, `finding`. No values are bound to specific BINs or jobs.
The same renderer works for any building once its records are in corpus.
"""
 
from __future__ import annotations
 
import json
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any
 
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "corpus.db"
 
 
# ---------------------------------------------------------------------------
# Field model
# ---------------------------------------------------------------------------
 
@dataclass
class Field:
    name: str
    value: Any = None
    present: bool = False
    source: str | None = None
    confidence: str = "HIGH"
    notes: str | None = None
 
 
@dataclass
class Bucket:
    name: str
    fields: list[Field] = field(default_factory=list)
 
    def add(self, name: str, value: Any, source: str | None = None,
            confidence: str = "HIGH", notes: str | None = None) -> None:
        present = value not in (None, "", [], {}, "NOT ON SHEET", False)
        # but explicit False (Y/N) is still "present" semantically
        if value is False:
            present = True
        self.fields.append(Field(
            name=name, value=value, present=present,
            source=source, confidence=confidence, notes=notes,
        ))
 
    def absent(self, name: str, source: str | None = None,
               notes: str | None = None) -> None:
        self.fields.append(Field(
            name=name, value=None, present=False,
            source=source, confidence="HIGH", notes=notes or "Not on sheet.",
        ))
 
 
@dataclass
class Extraction:
    bin: str
    job_number: str
    buckets: list[Bucket]
    findings_flagged: list[dict[str, Any]] = field(default_factory=list)
    findings_all: list[dict[str, Any]] = field(default_factory=list)
    coverage_notes: list[str] = field(default_factory=list)
 
    def to_dict(self) -> dict[str, Any]:
        return {
            "bin": self.bin,
            "job_number": self.job_number,
            "buckets": [
                {"name": b.name, "fields": [asdict(f) for f in b.fields]}
                for b in self.buckets
            ],
            "findings_flagged": self.findings_flagged,
            "findings_all": self.findings_all,
            "coverage_notes": self.coverage_notes,
        }
 
 
# ---------------------------------------------------------------------------
# Corpus lookups
# ---------------------------------------------------------------------------
 
def _connect(db_path: str | Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn
 
 
def _region_fields_merged(conn: sqlite3.Connection, bin_: str, job: str,
                          region_type: str) -> dict[str, Any]:
    """Merge `extracted_fields` JSON across all regions of a given type on
    sheets for (bin, job). Earlier-encountered keys win on conflict."""
    rows = conn.execute(
        """SELECT r.extracted_fields, s.sheet_id, r.region_id
           FROM sheet_region r
           JOIN sheet s ON s.sheet_id = r.sheet_id
           WHERE s.bin = ? AND r.region_type = ?
           ORDER BY s.sheet_id, r.region_id""",
        (bin_, region_type),
    ).fetchall()
    merged: dict[str, Any] = {}
    for row in rows:
        try:
            data = json.loads(row["extracted_fields"] or "{}")
        except json.JSONDecodeError:
            continue
        for k, v in data.items():
            merged.setdefault(k, v)
    return merged
 
 
def _all_sheets(conn: sqlite3.Connection, bin_: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sheet WHERE bin = ? ORDER BY sheet_id", (bin_,)
    ).fetchall()
 
 
def _all_findings(conn: sqlite3.Connection, bin_: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT f.finding_id, f.finding_type, f.finding_text, f.is_flag,
                  f.confidence, r.region_type, r.region_label, s.sheet_number
           FROM finding f
           JOIN sheet_region r ON r.region_id = f.region_id
           JOIN sheet s ON s.sheet_id = r.sheet_id
           WHERE s.bin = ?
           ORDER BY f.is_flag DESC, s.sheet_number, f.finding_id""",
        (bin_,),
    ).fetchall()
 
 
def _detected_region_types(conn: sqlite3.Connection, bin_: str) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT r.region_type FROM sheet_region r "
        "JOIN sheet s ON s.sheet_id = r.sheet_id WHERE s.bin = ?", (bin_,)
    ).fetchall()
    return {r["region_type"] for r in rows}
 
 
# ---------------------------------------------------------------------------
# Bucket-by-bucket extraction (works against generic corpus)
# ---------------------------------------------------------------------------
 
def extract(bin_: str, job: str, db_path: str | Path = DB_PATH) -> Extraction:
    conn = _connect(db_path)
    try:
        return _extract(conn, bin_, job)
    finally:
        conn.close()
 
 
def _extract(conn: sqlite3.Connection, bin_: str, job: str) -> Extraction:
    prop = conn.execute("SELECT * FROM property WHERE bin = ?", (bin_,)).fetchone()
    filing_row = conn.execute(
        "SELECT * FROM filing WHERE bin = ? AND job_number = ? LIMIT 1",
        (bin_, job),
    ).fetchone()
    sheets = _all_sheets(conn, bin_)
    findings = _all_findings(conn, bin_)
    detected_regions = _detected_region_types(conn, bin_)
 
    title_fields    = _region_fields_merged(conn, bin_, job, "title_block")
    scope_fields    = _region_fields_merged(conn, bin_, job, "scope_of_work")
    audit_fields    = _region_fields_merged(conn, bin_, job, "audit_stamp")
    energy_fields   = _region_fields_merged(conn, bin_, job, "energy_compliance")
 
    coverage_notes: list[str] = []
 
    # ---- 1. Building identity ----------------------------------------------
    b1 = Bucket("1. Building identity")
    if prop:
        b1.add("BIN", prop["bin"], "property")
        b1.add("BBL (Borough/Block/Lot)", prop["bbl"], "property")
        b1.add("Address",
               f"{prop['house_no']} {prop['street_name']}, {prop['borough']}",
               "property")
        b1.add("Zoning district", prop["zoning_district"], "property")
        b1.add("Map number", prop["map_number"], "property")
        b1.add("Construction class/type", prop["construction_class"]
               or prop["dof_building_class"], "property")
        if prop["occupancy_use_groups"]:
            try:
                ug = json.loads(prop["occupancy_use_groups"])
            except json.JSONDecodeError:
                ug = prop["occupancy_use_groups"]
            b1.add("Occupancy / use group(s)", ug, "property")
        else:
            b1.absent("Occupancy / use group(s)", "property")
        b1.add("Number of stories", prop["existing_stories"], "property")
        b1.add("Building height (ft)", prop["existing_height_ft"], "property")
        b1.add("Gross floor area (building, sf)", prop["gross_floor_area_sf"], "property")
    else:
        coverage_notes.append(
            f"BIN {bin_} is not in the property table. Building-identity "
            f"fields will all read absent."
        )
        for name in ["BIN", "BBL", "Address", "Zoning district", "Map number",
                     "Construction class/type", "Occupancy / use group(s)",
                     "Number of stories", "Building height (ft)",
                     "Gross floor area"]:
            b1.absent(name, "property")
 
    # ---- 2. Filing identity ------------------------------------------------
    b2 = Bucket("2. Filing identity")
    b2.add("Job number", job, "title_block")
    if filing_row:
        b2.add("Filing type", filing_row["job_type"], "filing")
        b2.add("Filing date", filing_row["pre_filing_date"], "filing")
        b2.add("Approval / sign-off / audit status",
               filing_row["filing_status_canonical"], "filing")
        b2.add("Approval date", filing_row["approved_date"], "filing")
        b2.add("Applicant of record",
               f"{filing_row['applicant_first_name'] or ''} "
               f"{filing_row['applicant_last_name'] or ''} "
               f"({filing_row['applicant_business_name'] or 'no business'})".strip(),
               "filing")
        b2.add("Applicant license type", filing_row["applicant_license_type"], "filing")
        b2.add("Design firm", filing_row["design_firm"], "filing")
        b2.add("Filing rep / expediter",
               f"{filing_row['filing_rep_name'] or ''} "
               f"({filing_row['filing_rep_business'] or 'no business'})".strip(),
               "filing")
        b2.add("Work types",
               json.loads(filing_row["work_types"]) if filing_row["work_types"] else None,
               "filing")
    else:
        coverage_notes.append(
            f"No filing row found for job {job} on BIN {bin_}. Below fields "
            f"are inferred from sheet content where possible."
        )
        b2.add("Filing date — earliest drawing date on sheets",
               title_fields.get("earliest_drawing_date"),
               "title_block",
               confidence="MEDIUM",
               notes="Earliest date in title block — may not be the filing date.")
        b2.add("Approval / sign-off / audit status",
               "AUDIT ACCEPTED" if title_fields.get("audit_accepted")
               else (audit_fields.get("audit_accepted") and "AUDIT ACCEPTED") or None,
               "title_block / audit_stamp")
        # Audit date is captured in title_block fields by the regex extractor
        # (Date: MM/DD/YYYY pattern). Convert to ISO if present.
        _ad = title_fields.get("audit_date") or audit_fields.get("audit_date")
        if _ad and "/" in str(_ad):
            try:
                _ad_iso = datetime.strptime(str(_ad), "%m/%d/%Y").strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                _ad_iso = _ad
        else:
            _ad_iso = _ad
        b2.add("Audit acceptance date", _ad_iso, "title_block / audit_stamp")
        b2.absent("Applicant license type",
                  notes="Not extractable from sheet text by pypdf — would "
                        "require OCR of the architect's seal or the filing "
                        "record itself.")
        b2.absent("Filing rep / expediter",
                  notes="Not present on the drawing itself; lives on the "
                        "filing record.")
 
    # Architect of record + design firm + professional certifier from sheets.
    # The generic firm-detection in extractors.py returns roles as:
    #   'professional_certifier' (firm co-located with Prof Cert marker)
    #   'candidate_aor_or_design_firm' (firm without bbox cue — AOR vs Design
    #                                   Firm cannot be disambiguated from
    #                                   flat pypdf text alone)
    firms = title_fields.get("firms_detected") or []
    certs = [f for f in firms if f["role"] == "professional_certifier"]
    candidates = [f for f in firms if f["role"] == "candidate_aor_or_design_firm"]
    if certs:
        b2.add("Professional Certifier (from sheets)",
               [c["name"] for c in certs], "title_block",
               notes=("Detected via generic firm pattern co-located with "
                      "'Professional Certification' text."))
    if candidates:
        b2.add("Other firms on title block (AOR / Design Firm)",
               [c["name"] for c in candidates], "title_block",
               confidence="MEDIUM",
               notes=("Detected via generic firm pattern. pypdf text "
                      "extraction loses positional info, so AOR vs Design "
                      "Firm cannot be auto-distinguished — both appear here "
                      "as candidates. v2 would resolve via bbox-aware "
                      "parsing (pdfplumber / PyMuPDF) or vision model."))
 
    # ---- 3. Scope / regulatory state --------------------------------------
    b3 = Bucket("3. Scope / regulatory state")
    # Y/N change flags: True means "yes change"; False means "no change"
    change_use = scope_fields.get("yes_change_use") if "yes_change_use" in scope_fields \
                 else (False if scope_fields.get("no_change_use") else None)
    change_egress = (False if scope_fields.get("no_change_egress") else None)
    change_occ    = (False if scope_fields.get("no_change_occupancy") else None)
 
    if change_use is not None:
        b3.add("Change in use", change_use, "scope_of_work")
    else:
        b3.absent("Change in use", "scope_of_work")
    if change_egress is not None:
        b3.add("Change in egress", change_egress, "scope_of_work")
    else:
        b3.absent("Change in egress", "scope_of_work")
    if change_occ is not None:
        b3.add("Change in occupancy", change_occ, "scope_of_work")
    else:
        b3.absent("Change in occupancy", "scope_of_work")
 
    if filing_row:
        b3.add("Floor(s) affected (from filing)",
               filing_row["existing_stories"] and "see filing_floor rows",
               "filing")
        b3.add("Area of work (sf)", filing_row["area_of_work_sf"], "filing")
        b3.add("Occupant load", filing_row["occupant_load"], "filing")
    else:
        b3.absent("Floor(s) affected",
                  notes="No filing record in corpus. Sheets typically state "
                        "this in the project-data block; extraction of that "
                        "block requires positional PDF parsing (v2).")
        b3.absent("Area of work (sf)",
                  notes="Sheets typically state this in the project-data "
                        "block; not recoverable from pypdf flat text.")
        b3.absent("Occupant load")
 
    # ---- 4. Sheet inventory ------------------------------------------------
    b4 = Bucket("4. Sheet inventory")
    actual_total = len(sheets)
    b4.add("Total sheets in corpus", actual_total, "sheet")
    # Per-sheet `sheet_of_total.total` value when present is what the sheet
    # itself claims the total should be.
    index_total = None
    for s in sheets:
        # We stored sheet_of_total fields inside the title_block region's JSON
        sot_data = _region_fields_merged(conn, bin_, job, "title_block").get("sheet_of_total")
        if isinstance(sot_data, dict) and sot_data.get("total"):
            index_total = sot_data["total"]
            break
    if index_total is not None:
        b4.add("Total sheets per index/title-block claim", index_total, "title_block")
        if index_total == actual_total:
            b4.add("Sheet count verified", True, "computed",
                   notes=f"Title block claims {index_total} sheets; corpus has "
                         f"{actual_total}. Match.")
        else:
            b4.add("Sheet count discrepancy",
                   f"Claimed {index_total}, corpus has {actual_total}",
                   "computed", confidence="HIGH",
                   notes="The drawing's own title block does not match the "
                         "number of sheets we ingested. Investigate.")
    sheet_list = [{"sheet_number": s["sheet_number"], "title": s["drawing_title"]}
                  for s in sheets]
    b4.add("Sheet list (number + title)", sheet_list, "sheet")
    disciplines = sorted({
        (s["sheet_number"].split("-", 1)[0].upper() if s["sheet_number"] and "-" in s["sheet_number"] else "")
        for s in sheets
    })
    discipline_map = {"A": "AR", "AR": "AR", "S": "STR", "M": "MP",
                      "P": "MP", "SP": "SP", "EL": "EL", "EN": "EN", "T": "SITE"}
    taxonomy = sorted({discipline_map.get(d, d) for d in disciplines if d})
    b4.add("Disciplines present", taxonomy, "computed",
           notes=f"Derived from sheet-number prefixes: {[d for d in disciplines if d]}.")
 
    # ---- 5. System specs --------------------------------------------------
    b5 = Bucket("5. System specs (where shown)")
 
    def _safe_region_finding(keyword: str):
        return next((f for f in findings if keyword in (f["finding_text"] or "").lower()), None)
 
    egress_present = "egress" in detected_regions or any(
        "egress" in (f["finding_text"] or "").lower() for f in findings)
    if egress_present:
        b5.add("Egress (means / count / width)", "Present on sheet — see findings",
               "region/finding")
    else:
        b5.absent("Egress (means / count / width)",
                  notes="No egress region or finding detected. Likely "
                        "absent on this scope (no change in egress).")
 
    if "smoke_detector" in detected_regions:
        f = _safe_region_finding("smoke")
        b5.add("Smoke detectors", f["finding_text"] if f else "Region detected",
               "finding")
    else:
        b5.absent("Smoke detectors")
 
    if "carbon_monoxide" in detected_regions:
        f = _safe_region_finding("carbon monoxide")
        b5.add("CO detectors", f["finding_text"] if f else "Region detected", "finding")
    else:
        b5.absent("CO detectors")
 
    for spec_name, marker in (
        ("Fire protection", "fire_protection"),
        ("Mechanical / HVAC", "mechanical"),
        ("Plumbing fixtures", "plumbing"),
        ("Electrical service / load", "electrical"),
        ("Structural framing", "structural"),
    ):
        # Detect via finding text, since these aren't always tagged as regions
        f = _safe_region_finding(spec_name.split()[0].lower())
        if f:
            b5.add(spec_name, f["finding_text"], "finding")
        else:
            b5.absent(spec_name)
 
    if "energy_compliance" in detected_regions:
        b5.add("Energy code / compliance path",
               {"climate_zone": title_fields.get("climate_zone"),
                "nycecc_present": title_fields.get("nycecc_present", False)},
               "title_block / energy_compliance")
    else:
        b5.absent("Energy code / compliance path")
 
    if "tr1_inspection" in detected_regions:
        b5.add("TR-1 special / progress inspections",
               "Region detected on sheet", "region")
    else:
        b5.absent("TR-1 special / progress inspections")
 
    if "tr8_inspection" in detected_regions:
        b5.add("TR-8 energy progress inspections",
               "Region detected on sheet", "region")
    else:
        b5.absent("TR-8 energy progress inspections")
 
    material_findings = [f for f in findings
                         if f["finding_type"] in ("material_spec",
                                                   "no_substitution_flag")]
    if material_findings:
        b5.add("Notable material specs",
               [f["finding_text"] for f in material_findings],
               "finding",
               notes=f"{len(material_findings)} material spec finding(s); "
                     f"{sum(1 for f in material_findings if f['is_flag'])} "
                     f"carry a high-value flag.")
    else:
        b5.absent("Notable material specs",
                  notes="None detected by pattern extractor. Note: "
                        "handwritten or rasterized notes inside drawings "
                        "are not recoverable from pypdf text; vision-model "
                        "extraction is the v2 path for these.")
 
    # ---- 6. Provenance / quality ------------------------------------------
    b6 = Bucket("6. Provenance / quality")
    src_formats = sorted({s["source_format"] for s in sheets if s["source_format"]})
    b6.add("Source format(s)", src_formats, "sheet")
    legibility = sorted({s["legibility_score"] for s in sheets if s["legibility_score"]})
    b6.add("Legibility", legibility, "sheet")
    if sheets:
        b6.add("PDF embedded title", sheets[0]["pdf_embedded_title"], "pdf_metadata")
        b6.add("PDF creation date", sheets[0]["pdf_creation_date"], "pdf_metadata")
        b6.add("PDF modification date", sheets[0]["pdf_modification_date"],
               "pdf_metadata")
        b6.add("PDF producer", sheets[0]["pdf_producer"], "pdf_metadata")
 
    flagged   = [dict(r) for r in findings if r["is_flag"]]
    all_      = [dict(r) for r in findings]
 
    return Extraction(
        bin=bin_, job_number=job,
        buckets=[b1, b2, b3, b4, b5, b6],
        findings_flagged=flagged,
        findings_all=all_,
        coverage_notes=coverage_notes,
    )
 
 
# ---------------------------------------------------------------------------
# Pretty rendering for the CLI
# ---------------------------------------------------------------------------
 
def format_extraction(e: Extraction) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append(f"TARGET-FIELD-LIST EXTRACTION  BIN={e.bin}, job={e.job_number}")
    lines.append("=" * 78)
 
    for b in e.buckets:
        lines.append("")
        lines.append(b.name)
        lines.append("-" * len(b.name))
        for f in b.fields:
            marker = "✓" if f.present else "✗"
            if isinstance(f.value, (dict, list)):
                value_str = json.dumps(f.value, ensure_ascii=False)
                if len(value_str) > 100:
                    value_str = value_str[:100] + "..."
            elif f.value is None:
                value_str = "NOT ON SHEET"
            else:
                value_str = str(f.value)
            lines.append(f"  [{marker}] {f.name}")
            lines.append(f"        value : {value_str}")
            if f.source:
                lines.append(f"        source: {f.source}")
            if f.notes:
                note = f.notes
                while len(note) > 70:
                    cut = note[:70].rfind(" ")
                    if cut <= 0:
                        cut = 70
                    lines.append(f"        notes : {note[:cut]}")
                    note = note[cut:].lstrip()
                lines.append(f"        notes : {note}")
 
    lines.append("")
    lines.append("FLAGGED FINDINGS")
    lines.append("-" * 16)
    if e.findings_flagged:
        for f in e.findings_flagged:
            lines.append(f"  ! [{f['sheet_number']} / {f['finding_type']}] {f['finding_text']}")
    else:
        lines.append("  (none)")
 
    lines.append("")
    lines.append("ALL FINDINGS (CITED)")
    lines.append("-" * 20)
    for f in e.findings_all:
        flag = "FLAG " if f["is_flag"] else "     "
        lines.append(f"  {flag}[{f['sheet_number']} / {f['finding_type']}]")
        lines.append(f"        {f['finding_text']}")
        lines.append(f"        cited to region: {f['region_label']}")
 
    if e.coverage_notes:
        lines.append("")
        lines.append("COVERAGE NOTES")
        lines.append("-" * 14)
        for n in e.coverage_notes:
            lines.append(f"  ! {n}")
 
    lines.append("")
    lines.append("=" * 78)
    return "\n".join(lines)
 
 
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Render target-field-list extraction.")
    p.add_argument("--bin", required=True)
    p.add_argument("--job", required=True)
    args = p.parse_args()
    print(format_extraction(extract(args.bin, args.job)))
 