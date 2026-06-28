"""
Generic extractors that run on any DOB-style drawing PDF.
 
This module contains the actual extraction logic — pypdf-based PDF metadata
reading, regex-based title-block field extraction, text-marker-based region
detection, and pattern-based finding extraction. None of it is bound to a
specific BIN, job, or sheet. The same code processes any PDF dropped into
data/{BIN}/.
 
Limits we're explicit about:
  - pypdf reads PDF *text* only. Architectural drawings render most of
    their content as vector geometry plus rasterized annotations. Anything
    that's not actual text in the PDF stream (handwritten notes, content
    embedded inside floor-plan drawings, room labels positioned over the
    drawing area) is NOT recovered here. The right tool for that is a
    vision-language model running on the rasterized page; this module
    exposes the regions but doesn't fill the deep-content fields when the
    underlying source is non-text.
  - Bounding boxes are not produced from PDF coordinates. pypdf strips
    positional info during text extraction. Region records carry a
    character-position offset within the text stream (`text_position`) so
    downstream code can reconstruct ordering; spatial bboxes would require
    a different PDF backend (pdfplumber, PyMuPDF) and are noted as v2 work.
"""
 
from __future__ import annotations
 
import re
from datetime import datetime
from pathlib import Path
from typing import Any
 
try:
    from pypdf import PdfReader  # type: ignore
    _HAS_PYPDF = True
except ImportError:
    _HAS_PYPDF = False
 
 
# ---------------------------------------------------------------------------
# Title-block field patterns
# ---------------------------------------------------------------------------
 
TITLE_BLOCK_PATTERNS: dict[str, re.Pattern] = {
    # Core anchor identifiers
    "scan_code":       re.compile(r"\b(ES\d{9})\b"),
    "job_number":      re.compile(r"(\d{9})\s*\n+\s*(?:ES\d{9})\s*\n+\s*DEPT\s+OF\s+BLDGS", re.IGNORECASE),
 
    # Sheet identity
    "sheet_number":    re.compile(r"^\s*([A-Z]{1,3})-?\s*(\d{3}\.\d{2})\s*$", re.MULTILINE),
    "sheet_of_total":  re.compile(r"(\d+)\s+OF\s+(\d+)"),
 
    # Dates
    "drawing_date":    re.compile(r"\b(\d{2}/\d{2}/\d{4})\b"),
    "audit_date":      re.compile(r"Date\s*:?\s*\n?\s*(\d{2}/\d{2}/\d{4})"),
 
    # Stamps and roles
    "audit_accepted":  re.compile(r"AUDIT\s+ACCEPTED", re.IGNORECASE),
    "professional_cert": re.compile(r"Professional\s+Certification", re.IGNORECASE),
 
    # Scope flags (these often appear in scope-of-work block)
    "no_change_use":     re.compile(r"NO\s+CHANGE\s+IN\s+USE", re.IGNORECASE),
    "no_change_egress":  re.compile(r"NO\s+CHANGE\s+IN\s+[^.]*EGRESS", re.IGNORECASE),
    "no_change_occupancy": re.compile(r"NO\s+CHANGE\s+IN\s+[^.]*OCCUPANCY", re.IGNORECASE),
    "yes_change_use":     re.compile(r"YES\s+CHANGE\s+IN\s+USE", re.IGNORECASE),
 
    # Code references
    "nycecc_present":  re.compile(r"NYCECC", re.IGNORECASE),
    "climate_zone":    re.compile(r"CLIMATE\s+ZONE\s+(\d+)", re.IGNORECASE),
    "bc_section":      re.compile(r"\bBC\s*(\d{3}\.\d+(?:\.\d+)?)"),
 
    # Material flags (high-value)
    "no_substitution": re.compile(
        r"(no\s+other\s+(?:system|product)\s+(?:is\s+)?permitted"
        r"|no\s+substitution\s+permitted"
        r"|substitution\s+(?:is\s+)?not\s+permitted"
        r"|the\s+only\s+permitted\s+\w+)",
        re.IGNORECASE,
    ),
 
    # Scale
    "scale_pattern":   re.compile(r"SCALE\s*:?\s*([\d/]+\"?\s*=\s*\d+\'-?\d*\"?)", re.IGNORECASE),
}
 
 
# Firm patterns. Detection is intentionally generic: any token sequence that
# looks like an architecture/engineering firm name (ending in INC / P.C. /
# LLC / STUDIO / ASSOCIATES / ARCHITECTS / ENGINEERING) is captured. Role
# assignment (AOR vs Design Firm vs Professional Certifier) is then a text-
# position heuristic — firms appearing near "Professional Certification"
# text are tagged as certifier; the rest are tagged as candidate AOR or
# Design Firm. This means a brand-new firm we've never seen before still
# gets detected and labeled with whatever role its position suggests.
FIRM_PATTERN = re.compile(
    r"\b([A-Z][A-Z0-9\-]*(?:\s+[A-Z][A-Z0-9\-]*)*"
    r"\s+(?:ENGINEERING|ARCHITECTS?|STUDIO|ASSOCIATES?|DESIGN(?:\s+ASSOCIATES)?)"
    r"(?:[\s,]+(?:INC|P\.?C|LLC|LTD)\.?)?)",
    re.IGNORECASE,
)
 
 
# ---------------------------------------------------------------------------
# Region marker patterns
# ---------------------------------------------------------------------------
 
REGION_MARKERS: dict[str, re.Pattern] = {
    "scope_of_work":     re.compile(r"\bSCOPE\s+OF\s+WORK\b", re.IGNORECASE),
    "index_of_drawings": re.compile(r"\bINDEX\s+OF\s+DRAWINGS?\b", re.IGNORECASE),
    "general_notes":     re.compile(r"\bGENERAL\s+NOTES?\b", re.IGNORECASE),
    "demolition_notes":  re.compile(r"\bDEMOLITION\s+NOTES?\b", re.IGNORECASE),
    "demolition_plan":   re.compile(r"\bDEMOLITION\s+PLAN\b", re.IGNORECASE),
    "floor_plan":        re.compile(r"\bFLOOR\s+PLAN\b", re.IGNORECASE),
    "plot_plan":         re.compile(r"\bPLOT\s+PLAN\b", re.IGNORECASE),
    "legend":            re.compile(r"\bLEGEND\b", re.IGNORECASE),
    "energy_compliance": re.compile(r"NYCECC\s+COMPLIANCE|ENERGY\s+ANALYSIS", re.IGNORECASE),
    "tile_detail":       re.compile(r"TILE\s+DETAIL", re.IGNORECASE),
    "door_saddle":       re.compile(r"DOOR\s+SADDLE", re.IGNORECASE),
    "section_at":        re.compile(r"\bSECTION\s+AT\s+\w+", re.IGNORECASE),
    "tenant_safety":     re.compile(r"\bTENANT\s+SAFETY\b", re.IGNORECASE),
    "smoke_detector":    re.compile(r"\bSMOKE\s+(?:DETECTOR|ALARM)\s*NOTES?", re.IGNORECASE),
    "carbon_monoxide":   re.compile(r"\bCARBON\s+MONOXIDE\s*(?:ALARM|DETECTOR)?\s*NOTES?", re.IGNORECASE),
    "tr1_inspection":    re.compile(r"\bTR-?\s*1\s+(?:SPECIAL|PROGRESS)", re.IGNORECASE),
    "tr8_inspection":    re.compile(r"\bTR-?\s*8\s+", re.IGNORECASE),
    "audit_stamp":       re.compile(r"AUDIT\s+ACCEPTED", re.IGNORECASE),
    "project_data":      re.compile(r"PROJECT\s+DATA", re.IGNORECASE),
}
 
 
# ---------------------------------------------------------------------------
# PDF metadata
# ---------------------------------------------------------------------------
 
def read_pdf_metadata(pdf_path: Path) -> dict[str, Any]:
    """Return embedded PDF metadata + page count, or read_method='UNAVAILABLE' on failure."""
    if not pdf_path.exists():
        return {"read_method": "UNAVAILABLE", "error": "file not found"}
    if not _HAS_PYPDF:
        return {"read_method": "UNAVAILABLE", "error": "pypdf not installed"}
    try:
        reader = PdfReader(str(pdf_path))
        meta = reader.metadata or {}
        def _date(d: Any) -> str | None:
            if not d:
                return None
            s = str(d)
            if s.startswith("D:") and len(s) >= 10:
                try:
                    return f"{int(s[2:6]):04d}-{int(s[6:8]):02d}-{int(s[8:10]):02d}"
                except ValueError:
                    return s
            return s
        return {
            "read_method": "PYPDF",
            "embedded_title": str(meta.get("/Title") or "") or None,
            "creation_date": _date(meta.get("/CreationDate")),
            "modification_date": _date(meta.get("/ModDate")),
            "producer": str(meta.get("/Producer") or "") or None,
            "creator": str(meta.get("/Creator") or "") or None,
            "author": str(meta.get("/Author") or "") or None,
            "num_pages": len(reader.pages),
        }
    except Exception as exc:
        return {"read_method": "UNAVAILABLE", "error": str(exc)}
 
 
def read_page_text(pdf_path: Path, page_number: int) -> str:
    """Extract text from a single page (1-indexed). Returns '' on failure."""
    if not _HAS_PYPDF or not pdf_path.exists():
        return ""
    try:
        reader = PdfReader(str(pdf_path))
        if page_number < 1 or page_number > len(reader.pages):
            return ""
        return reader.pages[page_number - 1].extract_text() or ""
    except Exception:
        return ""
 
 
# ---------------------------------------------------------------------------
# Title-block field extraction
# ---------------------------------------------------------------------------
 
def extract_title_block_fields(text: str) -> dict[str, Any]:
    """Apply title-block regexes to `text`. Returns a dict of all matches.
 
    Multi-match patterns (scan_code, drawing_date) return a list of all
    matches in order; single-match patterns return the first match. This
    lets downstream code disambiguate (e.g., the most recent drawing_date
    is the most recent revision).
    """
    fields: dict[str, Any] = {}
 
    # Patterns where multiple matches are meaningful
    multi_match_keys = {"scan_code", "drawing_date", "bc_section"}
 
    for name, pat in TITLE_BLOCK_PATTERNS.items():
        matches = list(pat.finditer(text))
        if not matches:
            continue
        values = [(m.group(1) if m.groups() else m.group(0)) for m in matches]
        if name in multi_match_keys:
            fields[name] = values
        else:
            # Boolean presence patterns (no_change_*, audit_accepted, etc.)
            # store True; capturing patterns store the captured value.
            if pat.groups == 0 and "no_change" not in name and "yes_change" not in name:
                # Patterns like audit_accepted, professional_cert, nycecc_present
                fields[name] = True
            elif "no_change" in name or "yes_change" in name:
                # Presence-only flags
                fields[name] = True
            else:
                fields[name] = values[0]
 
    # Sheet number cleanup ("EN- 001.01" → "EN-001.01")
    sn_match = TITLE_BLOCK_PATTERNS["sheet_number"].search(text)
    if sn_match:
        fields["sheet_number"] = f"{sn_match.group(1).strip()}-{sn_match.group(2).strip()}"
 
    # sheet_of_total → structured
    sot = TITLE_BLOCK_PATTERNS["sheet_of_total"].search(text)
    if sot:
        fields["sheet_of_total"] = {"this": int(sot.group(1)),
                                     "total": int(sot.group(2))}
 
    # Firm detection — generic pattern catches any architecture/engineering
    # firm name in the text. Role assignment is then a text-position
    # heuristic: firms whose match position is within ~250 chars of a
    # "Professional Certification" marker are tagged as professional
    # certifier; the rest are candidate AORs and design firms (with no
    # automatic distinction between those two roles, since positional
    # cues require bbox-aware parsing that pypdf doesn't provide).
    fields["firms_detected"] = []
    cert_marker = TITLE_BLOCK_PATTERNS["professional_cert"].search(text)
    cert_pos = cert_marker.start() if cert_marker else -1
 
    seen_names: set[str] = set()
    # DOB title-block templates include label text like "RESTAURANT CONSULTANT",
    # "AS SHOWN", "SEAL & SIGNATURE:", etc. that gets concatenated with firm
    # names in the flat pypdf text stream. Strip these known prefixes.
    NOISE_PREFIXES = re.compile(
        r"^(RESTAURANT\s+CONSULTANT\s*|AS\s+SHOWN\s*|SEAL\s*&?\s*SIGNATURE\s*:?\s*"
        r"|DOB\s+APPROVAL\s*|DRAWING\s+TITLE\s*:?\s*|SCALE\s*:?\s*"
        r"|DRAWN\s+BY\s*:?\s*|CHECKED\s+BY\s*:?\s*)+",
        re.IGNORECASE,
    )
    for m in FIRM_PATTERN.finditer(text):
        # Normalize whitespace in the captured name, then strip noise prefixes.
        name = re.sub(r"\s+", " ", m.group(1)).strip().rstrip(",")
        name = NOISE_PREFIXES.sub("", name).strip().rstrip(",")
        if not name or name.upper() in seen_names:
            continue
        seen_names.add(name.upper())
 
        # Position-based role guess.
        if cert_pos >= 0 and abs(m.start() - cert_pos) < 250:
            role = "professional_certifier"
            evidence = (f"matched firm pattern at char {m.start()}; "
                        f"co-located with 'Professional Certification' "
                        f"text at char {cert_pos} (distance "
                        f"{abs(m.start() - cert_pos)} chars)")
        else:
            role = "candidate_aor_or_design_firm"
            evidence = (f"matched firm pattern at char {m.start()}; "
                        f"not co-located with certification marker — "
                        f"role between AOR and Design Firm cannot be "
                        f"disambiguated without bbox parsing")
 
        fields["firms_detected"].append({
            "name": name, "role": role, "evidence": evidence,
            "text_position": m.start(),
        })
 
    # Date interpretation:
    #   drawing_date list contains original + revision + audit-acceptance dates.
    #   We don't try to disambiguate which is which without bboxes — we just
    #   report the full list, sorted, and let the renderer present the range.
    if isinstance(fields.get("drawing_date"), list):
        dates = []
        for d in fields["drawing_date"]:
            try:
                dates.append(datetime.strptime(d, "%m/%d/%Y").strftime("%Y-%m-%d"))
            except ValueError:
                pass
        fields["drawing_dates_iso"] = sorted(dates)
        if dates:
            fields["earliest_drawing_date"] = min(dates)
            fields["latest_drawing_date"]   = max(dates)
 
    return fields
 
 
# ---------------------------------------------------------------------------
# Region detection
# ---------------------------------------------------------------------------
 
def detect_regions(text: str) -> list[dict[str, Any]]:
    """Scan `text` for region marker patterns. Returns a list of region
    descriptors ordered by their position in the text stream.
 
    Each descriptor has:
      region_type     — taxonomy from REGION_MARKERS
      marker_text     — the actual matched text
      text_position   — character offset where the marker starts
                        (used as a coarse spatial ordering signal)
    """
    found: list[dict[str, Any]] = []
    for region_type, pattern in REGION_MARKERS.items():
        for m in pattern.finditer(text):
            found.append({
                "region_type": region_type,
                "marker_text": m.group(0),
                "text_position": m.start(),
            })
    # Dedupe by (region_type, position) so the same marker isn't recorded twice
    seen: set[tuple[str, int]] = set()
    deduped: list[dict[str, Any]] = []
    for r in sorted(found, key=lambda x: x["text_position"]):
        key = (r["region_type"], r["text_position"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    return deduped
 
 
# ---------------------------------------------------------------------------
# Finding extraction
# ---------------------------------------------------------------------------
 
def extract_findings(text: str, title_fields: dict[str, Any],
                     pdf_meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Pattern-based generic finding extraction. Returns a list of findings,
    each with finding_type, text, is_flag, confidence, evidence (the text
    snippet that triggered the finding).
 
    Adding a finding type means adding one pattern to this function. No
    sheet-specific hardcoding.
    """
    findings: list[dict[str, Any]] = []
 
    # 1. No-substitution / "only permitted" material flag
    for m in TITLE_BLOCK_PATTERNS["no_substitution"].finditer(text):
        ctx_start = max(0, m.start() - 200)
        ctx_end = min(len(text), m.end() + 100)
        snippet = text[ctx_start:ctx_end].strip()
        findings.append({
            "finding_type": "no_substitution_flag",
            "text": f"No-substitution / sole-source clause detected: \"{snippet[:300]}\"",
            "is_flag": True,
            "confidence": "HIGH",
            "evidence_pattern": "no_substitution",
        })
 
    # 2. Scope-of-work change flags
    for key, label, polarity in (
        ("no_change_use",       "Change in use",       False),
        ("no_change_egress",    "Change in egress",    False),
        ("no_change_occupancy", "Change in occupancy", False),
        ("yes_change_use",      "Change in use",       True),
    ):
        if title_fields.get(key):
            findings.append({
                "finding_type": "regulatory_state",
                "text": f"{label}: {'YES' if polarity else 'NO'}",
                "is_flag": polarity,
                "confidence": "HIGH",
                "evidence_pattern": key,
            })
 
    # 3. Audit acceptance + PDF modification-date corroboration
    if title_fields.get("audit_accepted") and title_fields.get("audit_date"):
        try:
            audit_iso = datetime.strptime(title_fields["audit_date"],
                                           "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            audit_iso = None
        pdf_mod = pdf_meta.get("modification_date")
        corroborated = audit_iso and pdf_mod and audit_iso == pdf_mod
        findings.append({
            "finding_type": "provenance",
            "text": (
                f"AUDIT ACCEPTED stamp dated {title_fields['audit_date']}"
                + (f"; PDF embedded modification_date is {pdf_mod} (matches — "
                   f"concrete evidence the file was touched at the moment of "
                   f"certification)" if corroborated
                   else (f"; PDF embedded modification_date is {pdf_mod} "
                         f"(does not match audit date — flag for review)" if pdf_mod
                         else "; no PDF modification date available for "
                              "cross-check"))
            ),
            "is_flag": corroborated,
            "confidence": "HIGH" if pdf_mod else "MEDIUM",
            "evidence_pattern": "audit_stamp + pdf_metadata",
        })
 
    # 4. Energy-code reference
    if title_fields.get("nycecc_present"):
        zone = title_fields.get("climate_zone")
        findings.append({
            "finding_type": "compliance",
            "text": f"NYCECC compliance present"
                    + (f"; Climate Zone {zone}" if zone else ""),
            "is_flag": False,
            "confidence": "HIGH",
            "evidence_pattern": "nycecc_present + climate_zone",
        })
 
    # 5. Inspection items (TR-1, TR-8) — present/absent flags
    if "TR" in text.upper():
        if re.search(r"TR-?\s*1\s+SPECIAL\s+INSPECTION", text, re.IGNORECASE):
            findings.append({
                "finding_type": "inspection",
                "text": "TR-1 special inspection section present on sheet.",
                "is_flag": False, "confidence": "HIGH",
                "evidence_pattern": "tr1_inspection",
            })
        if re.search(r"TR-?\s*8\s+ENERGY", text, re.IGNORECASE):
            findings.append({
                "finding_type": "inspection",
                "text": "TR-8 energy progress inspection section present on sheet.",
                "is_flag": False, "confidence": "HIGH",
                "evidence_pattern": "tr8_inspection",
            })
 
    # 6. Safety: smoke / CO detector code references
    for safety_pat, safety_label in (
        (r"SMOKE\s+(?:DETECTOR|ALARM)", "Smoke detector requirement"),
        (r"CARBON\s+MONOXIDE", "Carbon monoxide detector requirement"),
    ):
        m = re.search(safety_pat, text, re.IGNORECASE)
        if m:
            bc_ctx = re.search(r"BC\s*\d{3}\.\d+(?:\.\d+)?",
                                text[max(0, m.start() - 200):m.end() + 200])
            code_ref = bc_ctx.group(0) if bc_ctx else ""
            findings.append({
                "finding_type": "safety",
                "text": f"{safety_label} reference detected"
                        + (f" (cites {code_ref})" if code_ref else ""),
                "is_flag": False, "confidence": "HIGH",
                "evidence_pattern": safety_pat,
            })
 
    return findings
 
 
# ---------------------------------------------------------------------------
# Per-page extraction (the top-level operation)
# ---------------------------------------------------------------------------
 
def extract_page(pdf_path: Path, page_number: int) -> dict[str, Any]:
    """Run the full extraction pipeline on a single page of a PDF."""
    pdf_meta = read_pdf_metadata(pdf_path)
    text = read_page_text(pdf_path, page_number)
    title_fields = extract_title_block_fields(text)
    regions = detect_regions(text)
    findings = extract_findings(text, title_fields, pdf_meta)
    return {
        "pdf_path": str(pdf_path),
        "page_number": page_number,
        "pdf_metadata": pdf_meta,
        "title_block_fields": title_fields,
        "regions": regions,
        "findings": findings,
        "raw_text_length": len(text),
    }
  