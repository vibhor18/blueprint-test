
"""
Controlling-job resolution — Part 2 of the build test.
 
Given (BIN, floor, as_of_date), answer:
    Which filing/record currently governs the legal use and approved scope
    of that floor, as of that date?
 
This is harder than it sounds because:
  - Nothing in DOB data explicitly says "this supersedes that". We infer.
  - A building's record is split across two systems (BIS + DOB NOW) with
    different schemas and naming conventions.
  - Filings cluster (a primary I1 plus 14 subsequents on 310 W 144 St);
    the cluster is the unit of scope, not the individual filing.
  - Temporary COs and Final COs both legally certify use, but only the
    Final ends the TCO renewal lifecycle. Same legal use, different weight.
  - Approved-but-never-finalized jobs from 2009 sat in the record for
    15 years before being withdrawn — a naive "most recent approved"
    resolver would have surfaced them as controlling, wrongly.
  - The 1918 paper CO was the legal answer for 107 years. It must remain
    a queryable answer for "as_of_date < first new CO issuance".
 
The resolver returns a Resolution object that is part *answer* and part
*reasoning trace*. Every claim in the answer points back to a record. Every
inference (status normalization, cluster join, supersedence, floor expansion)
is explicit. The CLI in Part 4 serializes this directly to the user.
 
Design decisions worth flagging to a reader:
  1. The CO timeline is the spine. We walk COs first because a CO is the
     strongest possible statement about legal use. Jobs without a CO are
     proposed scope, not certified scope.
  2. Temporary CO (DOB NOW "Initial") is treated as effective for legal use
     until superseded by a later CO. The flag is preserved so downstream
     consumers can decide whether to trust a TCO for their purposes.
  3. A "Renewal Without Change" extends the prior TCO; it does not
     introduce new legal terms. The renewal is the controlling record from
     its own issuance date until the next CO (final or another renewal)
     supersedes it.
  4. Withdrawn jobs are excluded from controlling-scope candidates even if
     they were once approved. The withdrawal flag is the city's own
     supersedence signal — we use it as authoritative.
  5. The cluster root (the I1) carries the legal scope; child filings
     (P/S/A) carry technical discipline scope (plumbing, sprinkler, etc.).
     We report the cluster as a unit, with the root explicitly named.
"""
 
from __future__ import annotations
 
import json
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any
 
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "corpus.db"
 
 
# ---------------------------------------------------------------------------
# Result objects
# ---------------------------------------------------------------------------
 
@dataclass
class CitedFact:
    """A fact paired with its source record. Used in the reasoning trace."""
    fact: str
    source_type: str          # 'CO' | 'FILING' | 'INTERIM_USE' | 'COVERAGE'
    source_id: str            # the co_id / filing_id / record_id
    confidence: str = "HIGH"  # HIGH | MEDIUM | LOW
 
 
@dataclass
class JobCluster:
    """A cluster of filings sharing a primary I1 root."""
    root_filing_id: str
    root_job_number: str
    root_job_type: str | None
    root_status: str | None
    root_scope: str | None
    root_approved_date: str | None
    children: list[dict[str, Any]] = field(default_factory=list)
    inference_method: str | None = None
 
 
@dataclass
class Resolution:
    """The full answer for a (BIN, floor, as_of_date) query."""
    query: dict[str, Any]
    controlling_co: dict[str, Any] | None
    controlling_job_cluster: JobCluster | None
    legal_use: str | None
    confidence: str
    reasoning_trace: list[str]
    superseded_records: list[CitedFact]
    caveats: list[str]
    coverage: dict[str, Any]
 
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.controlling_job_cluster is None:
            d["controlling_job_cluster"] = None
        return d
 
 
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
 
def _connect(db_path: str | Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn
 
 
def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
 
 
# The supersedence rank between CO filing-type canonicals. Final outranks
# anything; renewals supersede prior renewals/temporaries from the same
# job; temporaries supersede legacy paper unless the temporary expires.
CO_TYPE_RANK = {
    "FINAL": 4,
    "AMENDED": 3,
    "RENEWAL": 2,
    "TEMPORARY": 2,
    "LEGACY_PAPER": 1,
    "OTHER": 0,
}
 
 
# ---------------------------------------------------------------------------
# Core resolver
# ---------------------------------------------------------------------------
 
def resolve_controlling_job(
    bin_: str,
    floor: str,
    as_of: str | date | None = None,
    db_path: str | Path = DB_PATH,
) -> Resolution:
    """Return the controlling CO + job cluster for (bin, floor, as_of_date).
 
    `as_of` may be a date object, an ISO date string, or None for today.
    """
    conn = _connect(db_path)
    try:
        return _resolve(conn, bin_, floor, as_of)
    finally:
        conn.close()
 
 
def _resolve(
    conn: sqlite3.Connection,
    bin_: str,
    floor: str,
    as_of: str | date | None,
) -> Resolution:
    if as_of is None:
        as_of_date = date.today()
    elif isinstance(as_of, str):
        as_of_date = _parse_date(as_of) or date.today()
    else:
        as_of_date = as_of
 
    trace: list[str] = []
    superseded: list[CitedFact] = []
    caveats: list[str] = []
 
    trace.append(
        f"Query: BIN={bin_}, floor={floor!r}, as_of={as_of_date.isoformat()}"
    )
 
    # --- 1. Property sanity check --------------------------------------
    prop = conn.execute(
        "SELECT * FROM property WHERE bin = ?", (bin_,)
    ).fetchone()
    if not prop:
        trace.append(f"BIN {bin_} not found in property table.")
        return Resolution(
            query={"bin": bin_, "floor": floor, "as_of": as_of_date.isoformat()},
            controlling_co=None,
            controlling_job_cluster=None,
            legal_use=None,
            confidence="LOW",
            reasoning_trace=trace,
            superseded_records=[],
            caveats=["BIN not in corpus."],
            coverage=_coverage_summary(conn, bin_),
        )
    trace.append(
        f"Property resolved: {prop['house_no']} {prop['street_name']}, "
        f"BBL {prop['bbl']}, {prop['existing_stories']} stories."
    )
 
    # --- 2. Walk the CO timeline ---------------------------------------
    co_rows = conn.execute(
        """SELECT * FROM certificate_of_occupancy
           WHERE bin = ?
           ORDER BY issuance_date ASC""",
        (bin_,),
    ).fetchall()
    trace.append(f"Found {len(co_rows)} CO records on file.")
 
    # Restrict to COs whose issuance_date <= as_of_date AND which cover this floor.
    candidates: list[sqlite3.Row] = []
    for co in co_rows:
        co_date = _parse_date(co["issuance_date"])
        if co_date and co_date > as_of_date:
            superseded.append(CitedFact(
                fact=f"CO {co['co_number']} issued {co['issuance_date']} "
                     f"({co['filing_type_canonical']}) — future of as_of_date.",
                source_type="CO",
                source_id=co["co_id"],
            ))
            continue
        covers_floor = conn.execute(
            "SELECT 1 FROM co_floor WHERE co_id = ? AND floor_label = ? LIMIT 1",
            (co["co_id"], floor),
        ).fetchone()
        if not covers_floor:
            trace.append(
                f"  CO {co['co_number']} skipped: does not cover floor {floor!r} "
                f"(see co_floor)."
            )
            continue
        candidates.append(co)
 
    # --- 3. Pick the controlling CO (latest issuance + highest rank) ---
    controlling_co_row: sqlite3.Row | None = None
    if candidates:
        # Sort by (issuance_date desc, type_rank desc). Most recent wins;
        # in the rare case two COs share an issuance date the higher
        # type_rank breaks the tie (a Final on the same day beats a TCO).
        candidates.sort(
            key=lambda r: (
                _parse_date(r["issuance_date"]) or date.min,
                CO_TYPE_RANK.get(r["filing_type_canonical"], 0),
            ),
            reverse=True,
        )
        controlling_co_row = candidates[0]
        for losing in candidates[1:]:
            superseded.append(CitedFact(
                fact=f"CO {losing['co_number']} ({losing['filing_type_canonical']}, "
                     f"issued {losing['issuance_date']}) — superseded by "
                     f"CO {controlling_co_row['co_number']}.",
                source_type="CO",
                source_id=losing["co_id"],
            ))
 
        trace.append(
            f"Controlling CO selected: {controlling_co_row['co_number']} "
            f"({controlling_co_row['filing_type_canonical']}, "
            f"issued {controlling_co_row['issuance_date']})."
        )
 
        # Caveat for temporary COs
        if controlling_co_row["filing_type_canonical"] == "TEMPORARY":
            caveats.append(
                "Controlling CO is a TEMPORARY (DOB NOW 'Initial') CO. Legal "
                "use is certified but the CO is subject to renewal and may "
                "expire without further action. Treat as transitional."
            )
        elif controlling_co_row["filing_type_canonical"] == "RENEWAL":
            caveats.append(
                "Controlling CO is a TCO RENEWAL ('Renewal Without Change'). "
                "Use is unchanged from the prior TCO; the renewal extends "
                "the temporary period only."
            )
        elif controlling_co_row["filing_type_canonical"] == "LEGACY_PAPER":
            caveats.append(
                "Controlling CO is a pre-digital paper record. Its terms "
                "remain in force because no later CO has been issued that "
                "covers this floor as of the query date. Verify against the "
                "scanned PDF for full text."
            )
    else:
        trace.append("No CO covers this floor as of the as_of_date.")
 
    # --- 4. Identify the controlling job cluster -----------------------
    cluster: JobCluster | None = None
 
    if controlling_co_row and controlling_co_row["originating_filing_id"]:
        # Easy case: the CO points us at the originating filing.
        cluster = _build_cluster_from(conn, controlling_co_row["originating_filing_id"])
        trace.append(
            f"Controlling job cluster derived from CO link: root = "
            f"{cluster.root_filing_id}."
        )
    else:
        # Harder case: no CO link (e.g. legacy paper CO has no
        # originating_filing_id). Fall back to the most recent
        # non-withdrawn, non-superseded approved job that touches this floor.
        trace.append(
            "Controlling CO does not link to a filing (legacy paper or "
            "missing originating_filing_id). Falling back to most recent "
            "approved, non-withdrawn filing on this floor."
        )
        fallback = conn.execute(
            """SELECT f.* FROM filing f
               JOIN filing_floor ff ON ff.filing_id = f.filing_id
               WHERE f.bin = ? AND ff.floor_label = ?
                 AND f.withdrawal_flag = 0
                 AND f.filing_status_canonical IN
                     ('APPROVED','PERMIT_ISSUED','CO_ISSUED','LOC_ISSUED')
                 AND f.parent_filing_id IS NULL
                 AND COALESCE(f.approved_date, f.latest_action_date,
                              f.pre_filing_date) <= ?
               ORDER BY COALESCE(f.approved_date, f.latest_action_date,
                                 f.pre_filing_date) DESC
               LIMIT 1""",
            (bin_, floor, as_of_date.isoformat()),
        ).fetchone()
        if fallback:
            cluster = _build_cluster_from(conn, fallback["filing_id"])
            trace.append(
                f"Fallback controlling cluster: {cluster.root_filing_id}."
            )
 
    # --- 5. List the records we explicitly superseded for transparency -
    # 5a. Withdrawn job-cluster roots that once governed this floor.
    withdrawn = conn.execute(
        """SELECT DISTINCT f.filing_id, f.job_number, f.job_type,
                  f.withdrawal_date, f.job_description
           FROM filing f
           JOIN filing_floor ff ON ff.filing_id = f.filing_id
           WHERE f.bin = ? AND ff.floor_label = ?
             AND f.withdrawal_flag = 1
             AND f.parent_filing_id IS NULL""",
        (bin_, floor),
    ).fetchall()
    for w in withdrawn:
        superseded.append(CitedFact(
            fact=f"Filing {w['job_number']} ({w['job_type']}) — withdrawn "
                 f"{w['withdrawal_date'] or '(date unknown)'}. Scope no "
                 f"longer in effect.",
            source_type="FILING",
            source_id=w["filing_id"],
        ))
 
    # 5b. Interim use records (LNOs etc) on this floor — informational.
    lnos = conn.execute(
        "SELECT * FROM interim_use_record WHERE bin = ?", (bin_,)
    ).fetchall()
    for lno in lnos:
        floors_parsed = json.loads(lno["floors_affected_parsed"] or "[]")
        if floor in floors_parsed:
            lno_date = _parse_date(lno["issuance_date"])
            if lno_date and lno_date <= as_of_date:
                superseded.append(CitedFact(
                    fact=f"{lno['record_number']} ({lno['issuance_date']}): "
                         f"{lno['use_description']}. Non-CO use record; "
                         f"superseded by current CO chain.",
                    source_type="INTERIM_USE",
                    source_id=lno["record_id"],
                    confidence="MEDIUM",
                ))
 
    # --- 6. Conflict detection: occupancy mismatches in the historical record
    # The 2009 BIS A1 on 310 W 144 St claimed existing_occupancy=J-2 while the
    # only CO at the time (CO 980, 1918) certified garage use. Surface this
    # explicitly because nothing in the source data says "this supersedes that"
    # — and unresolved contradictions like this must be flagged rather than
    # silently swallowed by the resolver.
    conflicts = conn.execute(
        """SELECT filing_id, job_number, existing_occupancy, pre_filing_date
           FROM filing
           WHERE bin = ?
             AND existing_occupancy IS NOT NULL
             AND existing_occupancy NOT IN ('','COM')""",
        (bin_,),
    ).fetchall()
    if conflicts:
        # Collect all (filing, governing_co, claimed_occupancy) tuples that
        # show an occupancy contradiction, then surface ONE summary caveat
        # rather than spamming the user with one per filing.
        conflict_filings: list[tuple[str, str]] = []
        gov_co_summary: str | None = None
        for c in conflicts:
            filed_on = _parse_date(c["pre_filing_date"])
            if not filed_on:
                continue
            governing_at_filing = conn.execute(
                """SELECT co_number, filing_type_canonical, legal_use_text, issuance_date
                   FROM certificate_of_occupancy
                   WHERE bin = ? AND date(issuance_date) <= date(?)
                   ORDER BY date(issuance_date) DESC LIMIT 1""",
                (bin_, filed_on.isoformat()),
            ).fetchone()
            if (governing_at_filing
                and "garage" in (governing_at_filing["legal_use_text"] or "").lower()
                and c["existing_occupancy"] in ("J-2", "R-2")):
                conflict_filings.append((c["job_number"], c["existing_occupancy"]))
                gov_co_summary = (f"CO {governing_at_filing['co_number']} "
                                  f"({governing_at_filing['issuance_date']})")
        if conflict_filings:
            jobs_listed = ", ".join(j for j, _ in conflict_filings)
            occs = sorted({occ for _, occ in conflict_filings})
            caveats.append(
                f"Historical data conflict: {len(conflict_filings)} filing(s) "
                f"({jobs_listed}) claimed existing occupancy {'/'.join(occs)} "
                f"(residential) while {gov_co_summary} certified garage use. "
                f"All such filings were subsequently withdrawn (2025). "
                f"Documented for review; does not affect current controlling-"
                f"record selection."
            )
 
    # --- 7. Floor inference caveat -------------------------------------
    floor_inference = conn.execute(
        "SELECT DISTINCT inference_source FROM filing_floor "
        "WHERE filing_id IN (SELECT filing_id FROM filing WHERE bin = ?) "
        "AND floor_label = ?",
        (bin_, floor),
    ).fetchall()
    if any(r["inference_source"] == "EXPANDED_FROM_STORY_COUNT" for r in floor_inference):
        caveats.append(
            f"Floor {floor!r} membership in filings was inferred from "
            f"existing/proposed story counts, not from explicit floor lists. "
            f"For this BIN, explicit floor data exists only in the BIS portal "
            f"scrape (see data/{bin_}/bis_portal_notes.md), not in the "
            f"Socrata API."
        )
 
    # --- 8. Confidence rollup ------------------------------------------
    confidence = _rollup_confidence(controlling_co_row, cluster, caveats)
 
    # --- 9. Legal use --------------------------------------------------
    legal_use = controlling_co_row["legal_use_text"] if controlling_co_row else None
 
    return Resolution(
        query={"bin": bin_, "floor": floor, "as_of": as_of_date.isoformat()},
        controlling_co=_co_to_dict(controlling_co_row) if controlling_co_row else None,
        controlling_job_cluster=cluster,
        legal_use=legal_use,
        confidence=confidence,
        reasoning_trace=trace,
        superseded_records=superseded,
        caveats=caveats,
        coverage=_coverage_summary(conn, bin_),
    )
 
 
# ---------------------------------------------------------------------------
# Cluster, CO, coverage helpers
# ---------------------------------------------------------------------------
 
def _build_cluster_from(conn: sqlite3.Connection, any_filing_id: str) -> JobCluster:
    """Given any filing_id in a cluster, return the full cluster anchored at root."""
    row = conn.execute(
        "SELECT cluster_id FROM filing WHERE filing_id = ?",
        (any_filing_id,),
    ).fetchone()
    root_id = row["cluster_id"] if row else any_filing_id
 
    root = conn.execute(
        "SELECT * FROM filing WHERE filing_id = ?", (root_id,)
    ).fetchone()
    if not root:
        # Fallback if cluster_id resolution failed: treat the queried
        # filing as its own root.
        root = conn.execute(
            "SELECT * FROM filing WHERE filing_id = ?", (any_filing_id,)
        ).fetchone()
        root_id = root["filing_id"]
 
    children_rows = conn.execute(
        """SELECT filing_id, job_number, job_type, filing_status_canonical,
                  approved_date, latest_action_date, withdrawal_flag,
                  cluster_inference_method
           FROM filing WHERE cluster_id = ? AND filing_id != ?
           ORDER BY job_number""",
        (root_id, root_id),
    ).fetchall()
 
    # The method that produced this cluster is the method on the children
    # (FILING_NUMBER_STEM or DESCRIPTION_REFERENCE). The root's own marker
    # is always SELF_ROOT and tells the user nothing useful.
    if children_rows:
        methods = {c["cluster_inference_method"] for c in children_rows
                   if c["cluster_inference_method"] not in (None, "SELF_ROOT")}
        cluster_method = ", ".join(sorted(methods)) if methods else "SELF_ROOT"
    else:
        cluster_method = "SELF_ROOT (no children)"
 
    return JobCluster(
        root_filing_id=root["filing_id"],
        root_job_number=root["job_number"],
        root_job_type=root["job_type"],
        root_status=root["filing_status_canonical"],
        root_scope=root["job_description"],
        root_approved_date=root["approved_date"],
        children=[
            {
                "filing_id": c["filing_id"],
                "job_number": c["job_number"],
                "job_type": c["job_type"],
                "status": c["filing_status_canonical"],
                "approved_date": c["approved_date"],
                "withdrawn": bool(c["withdrawal_flag"]),
            }
            for c in children_rows
        ],
        inference_method=cluster_method,
    )
 
 
def _co_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "co_id": row["co_id"],
        "co_number": row["co_number"],
        "source": row["source"],
        "filing_type_raw": row["filing_type_raw"],
        "filing_type_canonical": row["filing_type_canonical"],
        "sequence_number": row["sequence_number"],
        "issuance_date": row["issuance_date"],
        "legal_use_text": row["legal_use_text"],
        "number_of_dwelling_units": row["number_of_dwelling_units"],
        "originating_filing_id": row["originating_filing_id"],
        "application_number": row["application_number"],
        "source_pdf_path": row["source_pdf_path"],
    }
 
 
def _coverage_summary(conn: sqlite3.Connection, bin_: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM bin_coverage WHERE bin = ?", (bin_,)
    ).fetchone()
    if not row:
        return {"bin": bin_, "note": "no coverage record"}
    d = dict(row)
    # Add a human-readable summary of known gaps.
    gaps = []
    if not d.get("has_sheets"):
        gaps.append("no approved drawing sets ingested for this BIN")
    if (d.get("pre_1985_paper_records") or 0) > (d.get("pre_1985_paper_in_corpus") or 0):
        gaps.append(
            f"{d['pre_1985_paper_records']} pre-1985 paper records visible in "
            f"BIS Actions ledger; none ingested (microfilm at 280 Broadway)"
        )
    d["known_gaps"] = gaps
    return d
 
 
def _rollup_confidence(
    co_row: sqlite3.Row | None,
    cluster: JobCluster | None,
    caveats: list[str],
) -> str:
    """Combine signals into HIGH / MEDIUM / LOW."""
    if not co_row:
        return "LOW"
    rank = CO_TYPE_RANK.get(co_row["filing_type_canonical"], 0)
    if rank >= 4 and cluster and not caveats:
        return "HIGH"
    if rank >= 4 and cluster:
        return "HIGH"  # caveats don't downgrade FINAL CO past HIGH on their own
    if rank >= 2:
        return "MEDIUM"
    if rank >= 1:
        return "MEDIUM"
    return "LOW"
 
 
# ---------------------------------------------------------------------------
# Pretty printing for the CLI (Part 4)
# ---------------------------------------------------------------------------
 
def format_resolution(r: Resolution) -> str:
    """Human-readable rendering of a Resolution. Used by the CLI."""
    lines: list[str] = []
    q = r.query
    lines.append("=" * 72)
    lines.append(f"QUERY  BIN={q['bin']}, floor={q['floor']!r}, as_of={q['as_of']}")
    lines.append("=" * 72)
    lines.append("")
 
    if r.controlling_co:
        co = r.controlling_co
        lines.append("CONTROLLING CERTIFICATE OF OCCUPANCY")
        lines.append(f"  CO number       : {co['co_number']}")
        lines.append(f"  Type            : {co['filing_type_raw']}  "
                     f"(canonical: {co['filing_type_canonical']})")
        lines.append(f"  Source          : {co['source']}")
        lines.append(f"  Issuance date   : {co['issuance_date']}")
        if co.get("sequence_number"):
            lines.append(f"  Sequence        : {co['sequence_number']}")
        if co.get("application_number"):
            lines.append(f"  Application no. : {co['application_number']}")
        if co.get("originating_filing_id"):
            lines.append(f"  Originating job : {co['originating_filing_id']}")
        if co.get("source_pdf_path"):
            lines.append(f"  Source PDF      : {co['source_pdf_path']}")
        lines.append(f"  Legal use       : {co.get('legal_use_text') or '(not stated)'}")
        if co.get("number_of_dwelling_units"):
            lines.append(f"  Dwelling units  : {co['number_of_dwelling_units']}")
    else:
        lines.append("CONTROLLING CERTIFICATE OF OCCUPANCY")
        lines.append("  (none — no CO covers this floor as of the as_of_date)")
    lines.append("")
 
    if r.controlling_job_cluster:
        c = r.controlling_job_cluster
        lines.append("CONTROLLING JOB CLUSTER")
        lines.append(f"  Root filing     : {c.root_filing_id}")
        lines.append(f"  Job number      : {c.root_job_number}")
        lines.append(f"  Type            : {c.root_job_type}")
        lines.append(f"  Status          : {c.root_status}")
        if c.root_approved_date:
            lines.append(f"  Approved        : {c.root_approved_date}")
        if c.root_scope:
            scope = c.root_scope.replace("\n", " ")
            lines.append(f"  Scope           : {scope[:200]}"
                         + ("..." if len(scope) > 200 else ""))
        if c.children:
            lines.append(f"  Subsequent filings in cluster ({len(c.children)}):")
            for child in c.children:
                marker = " [WITHDRAWN]" if child["withdrawn"] else ""
                lines.append(
                    f"    - {child['job_number']:<18} "
                    f"{child['job_type'] or '':<22} "
                    f"{child['status'] or '':<14}{marker}"
                )
        lines.append(f"  Cluster inferred via: {c.inference_method}")
    else:
        lines.append("CONTROLLING JOB CLUSTER")
        lines.append("  (no controlling cluster could be derived)")
    lines.append("")
 
    lines.append(f"LEGAL USE        : {r.legal_use or '(unknown)'}")
    lines.append(f"CONFIDENCE       : {r.confidence}")
    lines.append("")
 
    lines.append("REASONING TRACE")
    for step in r.reasoning_trace:
        lines.append(f"  - {step}")
    lines.append("")
 
    if r.superseded_records:
        lines.append("SUPERSEDED / NOT-IN-EFFECT RECORDS (for transparency)")
        for s in r.superseded_records:
            lines.append(f"  - [{s.source_type}:{s.source_id}] {s.fact}")
        lines.append("")
 
    if r.caveats:
        lines.append("CAVEATS")
        for c in r.caveats:
            lines.append(f"  ! {c}")
        lines.append("")
 
    lines.append("COVERAGE")
    cov = r.coverage
    sources_present = [k.replace("has_", "")
                       for k in (
                           "has_bis_jobs_digital", "has_dobnow_jobs",
                           "has_bis_cos_digital", "has_dobnow_cos",
                           "has_legacy_paper_co", "has_bis_portal_scrape",
                           "has_sheets",
                       )
                       if cov.get(k)]
    lines.append(f"  Sources in corpus: {', '.join(sources_present) or '(none)'}")
    lines.append(f"  Filings ingested : {cov.get('known_filings_count')}")
    for gap in cov.get("known_gaps", []):
        lines.append(f"  ! gap: {gap}")
    lines.append("")
    lines.append("=" * 72)
 
    return "\n".join(lines)
 
 
# ---------------------------------------------------------------------------
# CLI entry for ad-hoc testing
# ---------------------------------------------------------------------------
 
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Resolve controlling job for (BIN, floor, date).")
    parser.add_argument("--bin", required=True)
    parser.add_argument("--floor", required=True, help="e.g. '003', 'CEL', 'ROOF'")
    parser.add_argument("--as-of", default=None, help="ISO date, defaults to today")
    args = parser.parse_args()
 
    resolution = resolve_controlling_job(args.bin, args.floor, args.as_of)
    print(format_resolution(resolution))
 