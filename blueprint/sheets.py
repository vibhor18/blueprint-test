
"""
Sheet structuring — Part 3.
 
Generic, file-driven sheet ingest. Walks `data/{BIN}/` directories, finds
every PDF, and runs the same extractor pipeline on each one. No values
are hardcoded to specific sheets or jobs. The same code processes the
96 Perry filing today and any other approved drawing set tomorrow.
 
Pipeline per page (implemented in blueprint/extractors.py):
  1. read_pdf_metadata        → creation/mod date, producer, author, etc.
  2. extract_title_block_fields → scan code, sheet#, dates, audit stamp,
                                  firm names, change-Y/N flags
  3. detect_regions           → text-marker pass produces region records
  4. extract_findings         → pattern-based finding extraction
                                (no-substitution flags, code refs,
                                 audit-stamp+pdf-mod-date corroboration,
                                 safety code refs, etc.)
 
What's NOT recoverable from pypdf text:
  - Handwritten or rasterized annotations embedded *inside* drawings
    (room labels, the Kemperol manuscript-style note, etc.). These need
    a vision-language model running on the page image. The hook for
    that integration is documented in the README's automation-boundary
    section.
 
Re-running this module is safe: it deletes any prior sheet/region/finding
records for the BINs being processed before reinserting.
"""
 
from __future__ import annotations
 
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
 
from blueprint.extractors import (
    extract_page,
    read_pdf_metadata,
)
 
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = ROOT / "corpus.db"
 
 
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
 
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
 
 
def _register_raw_source(
    conn: sqlite3.Connection,
    bin_: str,
    file_path: Path,
    notes: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO raw_source
           (bin, source_type, source_uri, pulled_at, file_path, record_count, notes)
           VALUES (?, 'DOB_DRAWING_PDF', ?, ?, ?, ?, ?)""",
        (bin_, str(file_path), _now_iso(), str(file_path), None, notes),
    )
    return cur.lastrowid
 
 
def _label_for_region(region_type: str, marker_text: str) -> str:
    """A human label for a detected region. Uses the matched marker text
    as the label so the rendered output reads naturally."""
    return marker_text.title() if marker_text else region_type.replace("_", " ").title()
 
 
def _clear_sheet_data_for_bin(conn: sqlite3.Connection, bin_: str) -> None:
    """Wipe sheet/region/finding/index/source rows for a BIN before re-ingest."""
    conn.execute(
        "DELETE FROM finding WHERE region_id IN "
        "(SELECT region_id FROM sheet_region WHERE sheet_id IN "
        " (SELECT sheet_id FROM sheet WHERE bin = ?))", (bin_,),
    )
    conn.execute(
        "DELETE FROM sheet_region WHERE sheet_id IN "
        "(SELECT sheet_id FROM sheet WHERE bin = ?)", (bin_,),
    )
    conn.execute(
        "DELETE FROM sheet_index_entry WHERE source_sheet_id IN "
        "(SELECT sheet_id FROM sheet WHERE bin = ?)", (bin_,),
    )
    conn.execute("DELETE FROM sheet WHERE bin = ?", (bin_,))
    conn.execute(
        "DELETE FROM raw_source WHERE bin = ? AND source_type = 'DOB_DRAWING_PDF'",
        (bin_,),
    )
 
 
# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------
 
def ingest_sheets(db_path: Path = DB_PATH,
                  data_dir: Path = DATA) -> dict[str, Any]:
    """Walk data/{BIN}/*.pdf and ingest every page of every PDF.
 
    Returns a summary dict with per-PDF and aggregate counts.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
 
    summary: dict[str, Any] = {
        "pdfs_seen": 0,
        "pdfs_skipped": [],
        "sheets": 0,
        "regions": 0,
        "findings": 0,
        "per_pdf": [],
    }
 
    # Discover BIN directories
    bin_dirs: list[Path] = []
    for entry in sorted(data_dir.iterdir()):
        if entry.is_dir() and entry.name.isdigit():
            bin_dirs.append(entry)
 
    # Wipe prior ingest for each BIN
    for bin_dir in bin_dirs:
        _clear_sheet_data_for_bin(conn, bin_dir.name)
 
    # Process each BIN's PDFs
    for bin_dir in bin_dirs:
        bin_ = bin_dir.name
        for pdf_path in sorted(bin_dir.glob("*.pdf")):
            summary["pdfs_seen"] += 1
 
            pdf_meta = read_pdf_metadata(pdf_path)
            if pdf_meta.get("read_method") != "PYPDF":
                summary["pdfs_skipped"].append({
                    "path": str(pdf_path),
                    "reason": pdf_meta.get("error", "unreadable"),
                })
                continue
 
            num_pages = pdf_meta.get("num_pages") or 0
 
            # Skip PDFs that don't look like DOB approved drawing sheets.
            # The signature for a DOB drawing is: at least one page contains
            # both a scan code (ESxxxxxxxxx) and the text "DEPT OF BLDGS",
            # OR a sheet-number pattern. PDFs without either signal are
            # likely other artifacts (paper-CO scans, expedited filings,
            # supporting docs) and the drawing-specific extractor would
            # produce mostly empty rows on them.
            from blueprint.extractors import read_page_text
            import re as _re
            looks_like_drawing = False
            for _p in range(1, num_pages + 1):
                _text = read_page_text(pdf_path, _p)
                if (_re.search(r"ES\d{9}", _text)
                    and _re.search(r"DEPT\s+OF\s+BLDGS", _text, _re.IGNORECASE)):
                    looks_like_drawing = True
                    break
                if _re.search(r"^\s*[A-Z]{1,3}-?\s*\d{3}\.\d{2}\s*$",
                              _text, _re.MULTILINE):
                    looks_like_drawing = True
                    break
            if not looks_like_drawing:
                summary["pdfs_skipped"].append({
                    "path": str(pdf_path),
                    "reason": "no DOB drawing markers detected "
                              "(scan code + DEPT OF BLDGS, or sheet-number "
                              "pattern). Treated as non-drawing PDF.",
                })
                continue
 
            pdf_summary = {
                "path": str(pdf_path), "pages": num_pages,
                "sheets": 0, "regions": 0, "findings": 0,
                "embedded_title": pdf_meta.get("embedded_title"),
                "creation_date": pdf_meta.get("creation_date"),
                "modification_date": pdf_meta.get("modification_date"),
            }
 
            for page_num in range(1, num_pages + 1):
                src_id = _register_raw_source(
                    conn, bin_, pdf_path,
                    notes=f"Page {page_num} of {pdf_path.name}",
                )
 
                result = extract_page(pdf_path, page_num)
                tb = result["title_block_fields"]
                regions = result["regions"]
                findings_for_page = result["findings"]
 
                # Sheet row
                sheet_number = tb.get("sheet_number") or f"page-{page_num}"
                # The job_number value here is what the title block claims; we
                # also fall back to the path stem if needed.
                job_in_tb = tb.get("job_number")
                drawing_title = None  # title block doesn't expose this as a
                                     # clean text run; would need positional
                                     # parsing. Honest leave-as-None.
 
                # Architect of record from firms_detected
                firms = tb.get("firms_detected", [])
                aor = next((f["name"] for f in firms
                            if f["role"] == "architect_of_record"), None)
                cert = next((f for f in firms
                             if f["role"] == "professional_certifier"), None)
                cert_name = cert["name"] if cert else None
                cert_date = tb.get("audit_date") if cert else None
                # Convert MM/DD/YYYY → ISO
                cert_date_iso = None
                if cert_date:
                    try:
                        cert_date_iso = datetime.strptime(
                            cert_date, "%m/%d/%Y").strftime("%Y-%m-%d")
                    except ValueError:
                        cert_date_iso = cert_date
 
                # scale: take the first detected scale pattern if any
                scale = tb.get("scale_pattern")
 
                # Filing link (if a filing record exists for this job + BIN)
                filing_id = None
                if job_in_tb:
                    filing_row = conn.execute(
                        "SELECT filing_id FROM filing WHERE bin = ? AND job_number = ? LIMIT 1",
                        (bin_, job_in_tb),
                    ).fetchone()
                    if filing_row:
                        filing_id = filing_row["filing_id"]
 
                sheet_id = f"{bin_}:{job_in_tb or 'unknown'}:{sheet_number}:p{page_num}"
                conn.execute(
                    """INSERT OR REPLACE INTO sheet (
                        sheet_id, bin, filing_id, sheet_number, drawing_title, scale,
                        architect_of_record, design_firm,
                        dob_scan_code, dob_stamp_date, dob_audit_accepted,
                        pdf_path, pdf_page_number,
                        pdf_creation_date, pdf_modification_date,
                        pdf_producer, pdf_embedded_title,
                        source_format, legibility_score,
                        professional_certifier, professional_certifier_date,
                        raw_source_id
                    ) VALUES (?,?,?,?,?,?, ?,?, ?,?,?, ?,?, ?,?, ?,?, ?,?, ?,?, ?)""",
                    (
                        sheet_id, bin_, filing_id, sheet_number, drawing_title, scale,
                        aor, None,
                        (tb.get("scan_code")[0] if isinstance(tb.get("scan_code"), list)
                         else tb.get("scan_code")),
                        cert_date_iso or tb.get("earliest_drawing_date"),
                        1 if tb.get("audit_accepted") else 0,
                        str(pdf_path), page_num,
                        pdf_meta.get("creation_date"),
                        pdf_meta.get("modification_date"),
                        pdf_meta.get("producer"),
                        pdf_meta.get("embedded_title"),
                        "BORN_DIGITAL"  # default; vision/OCR layer would re-assess
                            if pdf_meta.get("producer") else None,
                        "HIGH" if pdf_meta.get("producer") else None,
                        cert_name, cert_date_iso,
                        src_id,
                    ),
                )
                pdf_summary["sheets"] += 1
                summary["sheets"] += 1
 
                # Persist the title_block field bundle as a region so the
                # bucket-report renderer can find it.
                tb_region_id = f"{sheet_id}:r000:title_block"
                conn.execute(
                    """INSERT OR REPLACE INTO sheet_region (
                        region_id, sheet_id, region_type, region_label,
                        bbox, extracted_text, extracted_fields,
                        extraction_method, extraction_confidence
                    ) VALUES (?,?,?,?, ?,?,?, ?,?)""",
                    (
                        tb_region_id, sheet_id, "title_block",
                        f"Title block (extracted via pypdf text + regex)",
                        None, None,
                        json.dumps(tb, default=str),
                        "PDF_TEXT_REGEX", "HIGH",
                    ),
                )
                pdf_summary["regions"] += 1
                summary["regions"] += 1
 
                # Persist each detected region with its marker text and
                # text-stream position as a proxy for ordering.
                for idx, r in enumerate(regions, start=1):
                    region_id = f"{sheet_id}:r{idx:03d}:{r['region_type']}"
                    region_label = _label_for_region(
                        r["region_type"], r["marker_text"])
                    conn.execute(
                        """INSERT OR REPLACE INTO sheet_region (
                            region_id, sheet_id, region_type, region_label,
                            bbox, extracted_text, extracted_fields,
                            extraction_method, extraction_confidence
                        ) VALUES (?,?,?,?, ?,?,?, ?,?)""",
                        (
                            region_id, sheet_id, r["region_type"], region_label,
                            None,
                            r.get("marker_text"),
                            json.dumps({"text_position": r["text_position"]}),
                            "PDF_TEXT_MARKER_SCAN", "MEDIUM",
                        ),
                    )
                    pdf_summary["regions"] += 1
                    summary["regions"] += 1
 
                # Persist findings (pinned to the title_block region by default,
                # since the pattern extractor returns sheet-level findings
                # without per-region attribution; a more sophisticated future
                # extractor would attribute findings to specific regions).
                for fidx, f in enumerate(findings_for_page):
                    finding_id = f"{sheet_id}:f{fidx:03d}"
                    conn.execute(
                        """INSERT OR REPLACE INTO finding (
                            finding_id, region_id, finding_type, finding_text,
                            is_flag, confidence
                        ) VALUES (?,?,?,?, ?,?)""",
                        (
                            finding_id, tb_region_id,
                            f["finding_type"], f["text"],
                            1 if f["is_flag"] else 0,
                            f.get("confidence", "HIGH"),
                        ),
                    )
                    pdf_summary["findings"] += 1
                    summary["findings"] += 1
 
            summary["per_pdf"].append(pdf_summary)
 
    # Update bin_coverage for any BIN that now has sheets
    for bin_dir in bin_dirs:
        bin_ = bin_dir.name
        n_sheets = conn.execute(
            "SELECT COUNT(*) AS c FROM sheet WHERE bin = ?", (bin_,)
        ).fetchone()["c"]
        if n_sheets:
            conn.execute(
                "UPDATE bin_coverage SET has_sheets = 1, "
                "filings_with_sheets_count = (SELECT COUNT(DISTINCT filing_id) "
                "                              FROM sheet WHERE bin = ? "
                "                              AND filing_id IS NOT NULL), "
                "last_pulled_at = ? "
                "WHERE bin = ?",
                (bin_, _now_iso(), bin_),
            )
 
    conn.commit()
    conn.close()
    return summary
 
 
# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
 
def _print_summary(s: dict[str, Any]) -> None:
    print("Part 3 — sheets.py ingest complete (generic extractor).\n")
    print(f"PDFs seen        : {s['pdfs_seen']}")
    if s["pdfs_skipped"]:
        print(f"PDFs skipped     : {len(s['pdfs_skipped'])}")
        for skip in s["pdfs_skipped"]:
            print(f"  ! {skip['path']}: {skip['reason']}")
    print(f"Sheets ingested  : {s['sheets']}")
    print(f"Regions detected : {s['regions']}")
    print(f"Findings extracted: {s['findings']}")
    print()
    print("Per-PDF breakdown:")
    for p in s["per_pdf"]:
        print(f"  {Path(p['path']).name}")
        print(f"    pages         : {p['pages']}")
        print(f"    sheets        : {p['sheets']}")
        print(f"    regions       : {p['regions']}")
        print(f"    findings      : {p['findings']}")
        print(f"    PDF created   : {p['creation_date']}")
        print(f"    PDF modified  : {p['modification_date']}")
 
 
if __name__ == "__main__":
    _print_summary(ingest_sheets())
 