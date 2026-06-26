"""
Thin CLI — Part 4 of the build test.
 
Three subcommands:
 
  ask        Run a canned demo question end-to-end (cited, confidence-rated,
             coverage-explicit). Two questions are bundled. Both query the
             same floor of 310 W 144 St but at different points in time —
             the side-by-side is the demonstration that the controlling
             record changes over the building's history.
 
  resolve    Direct resolver access. Pass --bin --floor --as-of and get
             the full Resolution. Same engine as `ask`, no preamble.
 
  coverage   What the corpus has for a BIN, what it doesn't, where the
             gaps are. The honesty layer.
 
Every answer follows the same contract:
  - cite       every claim links back to a source row (co_id / filing_id /
               record_id) or to a coverage entry
  - confidence HIGH / MEDIUM / LOW, derived from the resolver's roll-up
  - coverage   what sources are in the corpus and what's known to be
               missing for the queried BIN
"""
 
from __future__ import annotations
 
import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
 
from blueprint.controlling_job import (
    Resolution,
    format_resolution,
    resolve_controlling_job,
)
from blueprint.extraction import extract, format_extraction
 
 
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "corpus.db"
 
 
# ---------------------------------------------------------------------------
# Canned demo questions
# ---------------------------------------------------------------------------
 
@dataclass
class DemoQuestion:
    qid: str
    prompt: str
    bin: str
    floor: str
    as_of: str
    rationale: str          # one line of why this question is interesting
    expected_signal: str    # one line of what the answer should demonstrate
 
 
DEMO_QUESTIONS: dict[str, DemoQuestion] = {
    "q1": DemoQuestion(
        qid="q1",
        prompt="What is the current legal use of floor 3 of 310 W 144 Street?",
        bin="1060779",
        floor="003",
        as_of="2026-06-24",
        rationale="Resolves against the most recent Final CO. Tests the "
                  "happy path: a clean Final CO with a fully-clustered "
                  "originating job.",
        expected_signal="Controlling: CO 1060779-0000003 (FINAL, 2025-12-18); "
                        "cluster: M01118538-I1 + 14 children; confidence: HIGH; "
                        "1918 paper CO and 2017 LNO listed as superseded.",
    ),
    "q2": DemoQuestion(
        qid="q2",
        prompt="What was the legal use of floor 3 of 310 W 144 Street on "
               "August 1, 2025 — between the issuance of the initial CO "
               "and the final?",
        bin="1060779",
        floor="003",
        as_of="2025-08-01",
        rationale="Same floor, same building, 11 months earlier. The "
                  "controlling record is a TEMPORARY CO with a renewal still "
                  "pending. Same residential 48-unit use, but a categorically "
                  "different legal posture from a Final CO. Demonstrates that "
                  "the resolver handles the TCO-to-Final lifecycle.",
        expected_signal="Controlling: CO 1060779-0000001 (TEMPORARY/Initial, "
                        "2025-06-16); cluster: same M01118538-I1; confidence: "
                        "MEDIUM (downgraded for TCO); transitional caveat "
                        "fires; later renewal and final COs listed as "
                        "future-of-as-of-date.",
    ),
}
 
 
# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------
 
def render_ask(question: DemoQuestion, resolution: Resolution) -> str:
    """Render a demo question + its answer with a clean preamble."""
    lines: list[str] = []
    lines.append("")
    lines.append("#" * 72)
    lines.append(f"#  QUESTION {question.qid.upper()}")
    lines.append("#" * 72)
    lines.append(f"#  Prompt:   {question.prompt}")
    lines.append(f"#  Why:      {question.rationale}")
    lines.append("#" * 72)
    lines.append("")
    lines.append(format_resolution(resolution))
    lines.append("")
    lines.append(f"  Plain-language answer:")
    lines.append(f"    {_one_line_answer(question, resolution)}")
    lines.append("")
    return "\n".join(lines)
 
 
def _one_line_answer(question: DemoQuestion, r: Resolution) -> str:
    """A single-sentence headline answer for the demo question.
 
    Carries enough provenance for someone reading only the headline to know
    where to look next. Confidence and coverage are always stated.
    """
    if not r.controlling_co:
        return (f"NO CONTROLLING RECORD found for floor {r.query['floor']} as "
                f"of {r.query['as_of']}. (confidence: {r.confidence})")
 
    co = r.controlling_co
    use = r.legal_use or "(legal use not certified in source record)"
    citation = f"CO #{co['co_number']} ({co['filing_type_canonical']}, "\
               f"issued {co['issuance_date']}, source: {co['source']})"
 
    if r.controlling_job_cluster:
        cluster = r.controlling_job_cluster
        cluster_note = (f", with approved scope per cluster {cluster.root_filing_id} "
                        f"({cluster.root_job_type})")
    else:
        cluster_note = ""
 
    return (f"{use}. Cited to {citation}{cluster_note}. "
            f"Confidence: {r.confidence}. "
            f"Coverage: {len(r.coverage.get('known_gaps', []))} known gap(s) "
            f"flagged below.")
 
 
def render_coverage(bin_: str) -> str:
    """Inspect what the corpus has for a BIN, what's missing, and where to look next."""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
 
    prop = conn.execute(
        "SELECT * FROM property WHERE bin = ?", (bin_,)
    ).fetchone()
    if not prop:
        return f"BIN {bin_} not found in corpus."
 
    cov = conn.execute(
        "SELECT * FROM bin_coverage WHERE bin = ?", (bin_,)
    ).fetchone()
 
    sources = conn.execute(
        "SELECT source_type, file_path, record_count, pulled_at "
        "FROM raw_source WHERE bin = ? ORDER BY source_type", (bin_,)
    ).fetchall()
 
    n_filings = conn.execute(
        "SELECT COUNT(*) AS c FROM filing WHERE bin = ?", (bin_,)
    ).fetchone()["c"]
    n_cos = conn.execute(
        "SELECT COUNT(*) AS c FROM certificate_of_occupancy WHERE bin = ?", (bin_,)
    ).fetchone()["c"]
    n_lnos = conn.execute(
        "SELECT COUNT(*) AS c FROM interim_use_record WHERE bin = ?", (bin_,)
    ).fetchone()["c"]
    n_sheets = conn.execute(
        "SELECT COUNT(*) AS c FROM sheet WHERE bin = ?", (bin_,)
    ).fetchone()["c"]
 
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f"COVERAGE  BIN {bin_}")
    lines.append("=" * 72)
    lines.append(f"  Address       : {prop['house_no']} {prop['street_name']}, {prop['borough']}")
    lines.append(f"  BBL           : {prop['bbl']}  (Block {prop['block']}, Lot {prop['lot']})")
    lines.append(f"  Class         : {prop['dof_building_class'] or '(n/a)'}")
    lines.append(f"  Stories       : {prop['existing_stories'] or '(n/a)'}")
    lines.append("")
    lines.append("RECORDS IN CORPUS")
    lines.append(f"  Filings           : {n_filings}")
    lines.append(f"  Certificates of O : {n_cos}")
    lines.append(f"  Interim use (LNO) : {n_lnos}")
    lines.append(f"  Drawing sheets    : {n_sheets}")
    lines.append("")
    lines.append("SOURCES INGESTED")
    for s in sources:
        rc = f"  ({s['record_count']} records)" if s["record_count"] else ""
        lines.append(f"  - {s['source_type']:<24} {Path(s['file_path']).name}{rc}")
    lines.append("")
    if cov and cov["notes"]:
        lines.append("KNOWN GAPS")
        for line in cov["notes"].split(". "):
            line = line.strip()
            if line:
                lines.append(f"  ! {line}{'.' if not line.endswith('.') else ''}")
        lines.append("")
    if cov:
        if cov["pre_1985_paper_records"] and not cov["pre_1985_paper_in_corpus"]:
            lines.append(f"OUT-OF-CORPUS RECORDS (visible in BIS portal, not ingested)")
            lines.append(f"  ! {cov['pre_1985_paper_records']} pre-1985 paper records "
                         f"in BIS Actions ledger — physical microfilm at 280 Broadway")
            lines.append("")
    lines.append("=" * 72)
    conn.close()
    return "\n".join(lines)
 
 
# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------
 
def cmd_ask(args: argparse.Namespace) -> int:
    qids = args.question if args.question else list(DEMO_QUESTIONS.keys())
    bad = [q for q in qids if q not in DEMO_QUESTIONS]
    if bad:
        print(f"Unknown question id(s): {', '.join(bad)}", file=sys.stderr)
        print(f"Available: {', '.join(DEMO_QUESTIONS.keys())}", file=sys.stderr)
        return 2
    for qid in qids:
        question = DEMO_QUESTIONS[qid]
        resolution = resolve_controlling_job(
            bin_=question.bin,
            floor=question.floor,
            as_of=question.as_of,
            db_path=DB_PATH,
        )
        if args.json:
            print(json.dumps({
                "question_id": question.qid,
                "prompt": question.prompt,
                "bin": question.bin,
                "floor": question.floor,
                "as_of": question.as_of,
                "answer": resolution.to_dict(),
            }, indent=2, default=str))
        else:
            print(render_ask(question, resolution))
    return 0
 
 
def cmd_resolve(args: argparse.Namespace) -> int:
    resolution = resolve_controlling_job(
        bin_=args.bin,
        floor=args.floor,
        as_of=args.as_of,
        db_path=DB_PATH,
    )
    if args.json:
        print(json.dumps(resolution.to_dict(), indent=2, default=str))
    else:
        print(format_resolution(resolution))
    return 0
 
 
def cmd_coverage(args: argparse.Namespace) -> int:
    print(render_coverage(args.bin))
    return 0
 
 
def cmd_extract(args: argparse.Namespace) -> int:
    """Render the six-bucket target-field-list extraction for (bin, job).
 
    The assignment specifies six buckets of fields a useful extraction
    should produce — pull what's present, flag what isn't. This command
    walks those buckets, pulls corpus values where they exist, and
    explicitly marks 'not on sheet' where the field isn't recorded."""
    e = extract(args.bin, args.job)
    if args.json:
        print(json.dumps(e.to_dict(), indent=2, default=str))
    else:
        print(format_extraction(e))
    return 0
 
 
def cmd_list_questions(args: argparse.Namespace) -> int:
    print("Available demo questions:")
    print()
    for q in DEMO_QUESTIONS.values():
        print(f"  {q.qid}: {q.prompt}")
        print(f"       BIN {q.bin}, floor {q.floor!r}, as_of {q.as_of}")
        print(f"       Why: {q.rationale}")
        print()
    return 0
 
 
# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
 
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blueprint",
        description="Blueprint — thin CLI over the building-records corpus.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python -m blueprint.cli ask                       # run both demo questions
  python -m blueprint.cli ask q1                    # just question 1
  python -m blueprint.cli ask q1 q2 --json          # JSON output
  python -m blueprint.cli resolve --bin 1060779 --floor 003 --as-of 2026-06-24
  python -m blueprint.cli coverage --bin 1060779
  python -m blueprint.cli extract --bin 1011231 --job 140941514
  python -m blueprint.cli list-questions
""",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
 
    p_ask = sub.add_parser("ask", help="Run one or both canned demo questions.")
    p_ask.add_argument("question", nargs="*",
                       help=f"Question id(s) to run. Default: all "
                            f"({', '.join(DEMO_QUESTIONS.keys())}).")
    p_ask.add_argument("--json", action="store_true",
                       help="Emit JSON instead of formatted text.")
    p_ask.set_defaults(func=cmd_ask)
 
    p_res = sub.add_parser("resolve", help="Direct controlling-job resolver.")
    p_res.add_argument("--bin", required=True)
    p_res.add_argument("--floor", required=True,
                       help="Floor label, e.g. '003', 'CEL', 'ROOF'.")
    p_res.add_argument("--as-of", default=None,
                       help="ISO date, defaults to today.")
    p_res.add_argument("--json", action="store_true")
    p_res.set_defaults(func=cmd_resolve)
 
    p_cov = sub.add_parser("coverage", help="What's in the corpus for a BIN.")
    p_cov.add_argument("--bin", required=True)
    p_cov.set_defaults(func=cmd_coverage)
 
    p_ext = sub.add_parser(
        "extract",
        help="Six-bucket target-field-list extraction for a (BIN, job).",
    )
    p_ext.add_argument("--bin", required=True)
    p_ext.add_argument("--job", required=True,
                       help="DOB job number, e.g. 140941514.")
    p_ext.add_argument("--json", action="store_true")
    p_ext.set_defaults(func=cmd_extract)
 
    p_lq = sub.add_parser("list-questions",
                          help="Show the canned demo questions and their rationale.")
    p_lq.set_defaults(func=cmd_list_questions)
 
    return parser
 
 
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
 
 
if __name__ == "__main__":
    raise SystemExit(main())
 