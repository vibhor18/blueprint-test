# Blueprint — build test

An end-to-end slice of a building-records retrieval system for two NYC buildings.
The corpus is a SQLite database, the pipeline is six Python modules, and every
answer the CLI returns is cited to a source record, carries a confidence
indicator, and is explicit about what's in the corpus and what isn't.

The two buildings:

- **310 West 144th Street** (BIN 1060779, BBL 1020440020) — used for Parts 1 and 2.
  A non-fireproof garage built around 1902. CO 980 (1918) was the legal answer
  for 107 years. A 2009 conversion was approved but never finalized; a 2017
  Letter of No Objection re-confirmed the garage at 130 cars; a 2024 filing
  (DOB NOW M01118538-I1) was approved December 2024 and a chain of three COs
  through 2025 settled the legal use as residential, 48 dwelling units.
- **96 Perry Street** (BIN 1011231, BBL 1006210013) — used for Part 3. Interior
  renovation of unit B17-B (166 sf, 5th floor partial), job 140941514, audit
  accepted April 23, 2025.

## Quickstart

```bash
pip install -r requirements.txt

python -m blueprint.ingest       # build corpus.db from data/
python -m blueprint.sheets       # process drawing PDFs into sheet/region/finding rows
python -m blueprint.cli ask      # answer the two demo questions for Part 4
```

That's the whole flow. Three other CLI subcommands are available for direct
inspection:

```bash
# Part 2 — controlling-job resolution for any (BIN, floor, as-of-date)
python -m blueprint.cli resolve --bin 1060779 --floor 003 --as-of 2026-06-24
python -m blueprint.cli resolve --bin 1060779 --floor 003 --as-of 2025-08-01
python -m blueprint.cli resolve --bin 1060779 --floor 003 --as-of 2020-06-01

# Part 3 — six-bucket field extraction for any (BIN, job)
python -m blueprint.cli extract --bin 1011231 --job 140941514

# Coverage report — what the corpus has and what it's missing
python -m blueprint.cli coverage --bin 1060779
```

All commands accept `--json` for machine-readable output.

## What this delivers, by part

**Part 1 — Ingest and identity.** `blueprint/ingest.py` and `blueprint/schema.py`.
A 13-table SQLite schema that normalizes BIS-era (pre-2013) and DOB NOW
(post-2013) records into a single shape, with two-system ID namespacing so
filings from the two systems never collide. Status values are mapped to a
canonical set (APPROVED, DISAPPROVED, WITHDRAWN, PERMIT_ISSUED, CO_ISSUED,
LOC_ISSUED, IN_PROCESS, OTHER) and DOB NOW CO filing types are normalized
(`Initial` → TEMPORARY, `Renewal Without Change` → RENEWAL, `Final` → FINAL,
paper → LEGACY_PAPER). Every BIN lives in its own `data/<BIN>/` directory with
a `property.json` and any of the optional source files (`bis_jobs.json`,
`dobnow_jobs.json`, `dobnow_cos.json`, `bis_portal_notes.md`, `legacy_co.json`,
`interim_use.json`, drawing PDFs). The ingester walks every directory that has
a `property.json` and applies the same extractors to whatever it finds, so
adding a third building is dropping a directory — no code change required.

**Part 2 — Controlling-job resolution.** `blueprint/controlling_job.py`. Given a
(BIN, floor, as-of-date), the resolver returns the CO that governs that floor
on that date, the originating job cluster, every superseded record (listed
explicitly for transparency), any historical contradictions surfaced as
caveats, and a confidence rollup (HIGH / MEDIUM / LOW). The same code path
correctly handles all three of the eras present in this building's history:
the 2025 Final CO, the 2025 Temporary CO that preceded it, and the 1918
garage CO that governed for 107 years before any of it. The reasoning trace
is printed alongside the answer.

**Part 3 — Sheet structuring.** `blueprint/sheets.py`, `blueprint/extractors.py`,
`blueprint/extraction.py`. A generic, file-driven pipeline that walks every
PDF under `data/<BIN>/`, filters to actual DOB drawings via marker detection
(scan code + Department of Buildings header), and runs the same extractor
pipeline on each: PDF metadata read (creation date, modification date,
producer, embedded title), title-block field extraction (job number, sheet
number, dates, audit stamps, firm names, change-Y/N flags, NYCECC, climate
zone), region detection (19 region-marker patterns including Scope of Work,
Demolition Notes, Tenant Safety Notes, Tile Detail, Door Saddle Detail,
Legend, Index of Drawings, etc.), and pattern-based finding extraction
(no-substitution flags, audit-stamp/PDF-mod-date corroboration, energy code
references, TR-1/TR-8 inspection markers, safety code section references).
The six-bucket extraction renderer in `extraction.py` walks the evaluator's
target field list and marks each field as either populated (✓ with value,
source, confidence, and notes) or NOT ON SHEET (✗ with an honest rationale).

**Part 4 — Answer + provenance + coverage.** `blueprint/cli.py`. A CLI rather
than a service layer (see the scoping section below for why). Every fact the
CLI emits is cited to a source record, every answer carries an explicit
confidence indicator, and every output includes a coverage block that names
the sources ingested and the gaps known to exist but not yet retrieved.

## How I scoped it

Two clarifying questions were sent before the build started, both because the
evaluator's framing left an intentional ambiguity and locking it in early
mattered more than guessing.

**Question 1 — what counts as a floor.** Are you expecting controlling-job
resolution per individual floor as labeled in the records (1st, 2nd, cellar,
roof), or grouped by something else? The answer that came back: floor as
labeled is the right primary unit, but the harder question is how to model
floor vs. use vs. unit, and how that modeling decision is made is part of
what's being evaluated. The section that follows lays out the model I picked
and the alternatives I rejected.

**Question 2 — CLI or HTTP endpoint.** The answer was CLI, with the explicit
note that what matters in Part 4 isn't the transport but that every fact is
traceable to its source, carries a confidence indicator, and the output is
explicit about what the corpus has and hasn't retrieved. Those three
requirements drove the shape of the output format in `cli.py` more than any
ergonomic concern about CLI argument design.

Both responses arrived after the resolver had been sketched but before final
shape was locked in, which is why the resolver's output format matches the
three Part 4 requirements directly rather than being retrofitted to them.

## The floor / use / unit decision

The honest version of this question is: floor labels are the wrong unit of
legal control about half the time. A floor isn't governed by anything on its
own — what's governed is *a use on a floor*, and a building can have multiple
uses on one floor (garage + boiler room, retail + residential) or one use
spanning many floors (residential 48 dwelling units across stories 1-4 + cellar).
Jobs themselves don't respect the floor boundary either: M01118538-I1's scope
text reads "PROPOSED INTERIOR CONVERSION AND EXTERIOR WORK OF NON-CONFORMING
COMMERCIAL BUILDING TO A CONFORMING RESIDENTIAL 48 UNIT BUILDING UNDER ARTICLE
5 OF THE ZONING RESOLUTION." That's one job touching every floor in the
building plus the roof and bulkhead.

I considered three modeling options:

1. **Floor-as-primary-key.** Each query returns one CO per floor. Simple, maps
   one-to-one to the prompt. Loses information when a single CO governs many
   floors (it has to be repeated) and when a single floor is split between
   two uses (it can't be expressed).
2. **(Floor, use)-as-primary-key.** Each query returns one CO per (floor, use)
   pair. More expressive, but the caller has to know what "use" means before
   asking — for many queries the use is exactly what the caller wants the
   resolver to tell them.
3. **Floor-as-primary-key with embedded use detail.** The query is keyed on
   floor as labeled in the records (CEL, 001, 002, ..., ROOF, BULK), and the
   answer returns the controlling CO for that floor plus the legal use text
   from the CO, the dwelling unit count if applicable, and a `co_floor` row
   that carries the occupancy_description for that specific floor when the
   CO breaks down by floor.

I chose option 3. The reason is that for both buildings in this build test
and the overwhelming majority of NYC buildings, the floor *is* a coherent
unit because either the same use spans all floors (CO 980's garage on floors
1-4) or the CO records the per-floor occupancy explicitly (`co_floor` table).
The cases where it breaks — split-use floors, vertical condo stacks where
one BBL has multiple BINs each with their own use — are real but they're
the minority case, and the right response is to surface them as caveats
rather than to make the primary query interface harder for everyone.

Two consequences worth flagging:

- The resolver treats *job-to-floor membership* as a separate inference
  problem from *CO-to-floor coverage*. The CO side is fine because COs carry
  per-floor occupancy in the dataset. The job side is harder: BIS jobs
  expose a story count via Socrata but the explicit "Work on Floor(s)" field
  lives only in the BIS HTML portal. When the resolver falls back to story
  count, the caveat is surfaced: "Floor '003' membership in filings was
  inferred from existing/proposed story counts, not from explicit floor
  lists."
- When a building has a unit-level filing (96 Perry's job 140941514 covering
  unit B17-B specifically), I model that as a single job_floor row for
  floor 005 with a scope text that names the unit. The unit identity is
  preserved in the filing's scope_description rather than promoted to its
  own column, because units are not a stable cross-building concept — a
  "B17-B" in one condo means nothing in another building's filing.

## Architecture, briefly

**The schema** (`blueprint/schema.py`, 13 tables) is built around a five-level
hierarchy: property (BBL/BIN), filing (job), filing_floor, certificate of
occupancy (with co_floor for per-floor occupancy breakdowns),
interim_use_record (for LNOs and other non-CO use determinations), sheet,
sheet_region, sheet_index_entry, and finding. A `raw_source` table anchors
provenance — every row in every table carries a `raw_source_id` so any
answer can trace back to which file produced it. A `bin_coverage` table is
the first-class home for what the corpus has versus what it's known to be
missing.

**Two-system reconciliation.** BIS and DOB NOW use overlapping job-number
spaces, so every filing's `filing_id` is namespaced: `BIS:110445974:01` and
`DOBNOW:M01118538-I1` are distinct keys that never collide. Status values
from both systems are normalized to a single canonical set so a query
doesn't have to know which system produced the row.

**Cluster inference uses two signals**, both tracked explicitly in
`filing.cluster_inference_method`:

- `FILING_NUMBER_STEM` for DOB NOW: `M01118538-I1`, `-A1`, `-P2 through -P9`,
  and `-S1 through -S6` all share the stem `M01118538` and form one cluster
  of 15 filings.
- `DESCRIPTION_REFERENCE` for BIS-era: five A2 filings (`120475404`,
  `120475413`, `120475422`, `120475431`, `120720559`) reference the A1
  (`110445974`) in their scope text via "IN CONJUNCTION WITH
  APPLICATION#110445974", which is the only cluster signal available because
  BIS jobs share no filing-number stem.

These are the cleanest cluster signals visible in the data. There are
probably weaker signals (same applicant of record, overlapping filing dates,
overlapping floor scope) that a v2 pipeline would use to surface candidate
clusters for human review.

**Supersedence is always inferred, never declared.** Nothing in the source
data says "this supersedes that." The resolver infers it from: (a) chronology
within the same job cluster, (b) CO chain (a Final CO supersedes the
Temporary that issued first under the same originating job, and any prior CO
on the BIN), and (c) explicit withdrawal status (six 2009-2011 filings on
310 W 144 St were formally withdrawn in 2025). Every superseded record is
listed in the resolver output for transparency, not silently dropped.

**Historical contradictions surface as caveats, not silent corrections.**
The 2009 BIS A1 on 310 W 144 St claimed `existing_occupancy=J-2` while the
only CO at the time was CO 980, which certified garage use. The resolver's
conflict-detection layer fires on any case where a filing's existing
occupancy is residential (J-2 or R-2) while the governing CO at the filing
date is a garage. That layer is general — it isn't keyed to this building —
which means it'll fire automatically on any future building where the same
pattern shows up.

**Coverage is a first-class column on every answer.** The `bin_coverage` table
tracks `known_filings_count`, `has_legacy_paper_co`, and
`pre_1985_paper_records` (the count of pre-1985 Actions ledger entries
visible in the BIS portal but not yet retrieved). Every CLI output includes
a COVERAGE block that names the sources ingested and the gaps known but not
filled.

## Design note 1 — getting 500 buildings into the corpus

The bottleneck is not the API. NYC Open Data (Socrata) is freely queryable
for all of the metadata — jobs, permits, COs, complaints, violations — and
500 BINs is a one-batch pull that takes under an hour. The DOB NOW and BIS
HTTP endpoints are not rate-limited at any meaningful threshold for this
volume.

The bottleneck is physical document retrieval. DOB NOW stakeholder-gates
plan sets: only the owner, applicant, or licensed professional on that
exact filing can download drawings through the portal. For any building you
don't have standing on — which is the entire corpus — the drawings cannot
be retrieved by any script. A human runner has to go to 280 Broadway, request
the folder by BIN, and photograph each page. Pre-2013 records are worse:
they exist only as microfilm in the Records Room, with no Socrata index of
what's in the folder until someone physically pulls it.

So the 500-building shape is:

1. **Metadata pull.** Batched Socrata queries for all 500 BINs land filing
   histories, COs, and ledger metadata in the corpus within hours. This is
   automated and produces a complete picture of *what filings exist*.
2. **Coverage classification.** For each BIN, the corpus knows what filings
   exist but doesn't have the drawings. Each (BIN, job) is queued for
   retrieval with a priority. Priority is set by customer demand — buildings
   that have been queried by paying customers seed first.
3. **Runner trips, batched geographically.** A runner can cover roughly 8-12
   buildings per day at 280 Broadway (request the folder, wait for it to be
   pulled, photograph the contents, organize). 500 buildings is roughly 45-60
   runner-days. The cost is real but bounded, and once a building is in
   corpus the marginal cost of every future query against it is near zero.
4. **Auto-ingest.** Photographed PDFs land in S3, a trigger kicks off the
   sheets pipeline, and the extractors run end-to-end without human
   intervention.

Two operational notes that matter for the cost model:

- Runner batching by geography compresses the retrieval cost meaningfully.
  A runner who pulls 10 buildings in one Manhattan trip is not 10x cheaper
  than 10 runners pulling one each, but it's close enough — the marginal
  cost per building drops to the labor cost of photographing one extra
  folder while you're already in the building.
- Demand-driven seeding inverts the funding model. A customer query for an
  uncached building triggers a runner trip; the customer pays for the
  synthesized output of that trip; the documents enter corpus permanently
  and serve every future query for free. The "first 500 buildings" question
  is therefore really two questions: how many buildings do you proactively
  seed with raise capital, and how many do you let customer demand pull in
  organically? My answer: proactively seed the geographic and use-class
  spread that lets demand-seeded queries hit useful adjacencies (a customer
  asking about 25 Mercer should find that 27 Mercer is already in corpus
  because both were seeded as part of a SoHo block), then let demand handle
  the rest.

The bottleneck that can't be eliminated is the physical retrieval step.
That's also the moat: nobody replicates the corpus without doing the same
human work, building by building.

## Design note 2 — what you serve customers

If a customer asks for "the floor plan," what you serve them is not the raw
PDF. Three reasons:

1. **IP boundary.** The architectural drawings belong to the architect of
   record. Wholesale redistribution creates copyright liability that the
   public-records framework doesn't address. What's licensable is the
   building record (jobs, COs, scope text, extracted findings); the drawing
   itself isn't.
2. **What customers actually need is rarely the full drawing.** A contractor
   bidding interior renovation work needs to know the approved layout,
   demolition scope, material specifications, and any flagged compliance
   constraints. A lender doing diligence needs the legal use, the dwelling
   unit count, and the CO chain. An insurer needs COPE attributes
   (construction, occupancy, protection, exposure). None of these customers
   wants the 36"x24" CAD drawing — they want the answers the drawing
   contains.
3. **What you can serve is a structured rendering anchored to corpus data.**
   For the 96 Perry job, that's the six-bucket extraction output: building
   identity, filing identity, scope and regulatory state, materials and
   notable specs, code compliance fields, and provenance/quality. Plus
   flagged findings — the audit-stamp-matches-PDF-modification-date
   provenance finding, the NYCECC compliance presence, the TR-1/TR-8
   inspection sections, and anything else the pattern extractors caught.
   The customer gets the answers, cited; if they need the raw drawing for
   legal purposes they pull it themselves with their own standing.

The v2 answer is a plan viewer that pins findings to specific drawing
coordinates — but that's geometry work (vision model + bounding box
extraction + spatial indexing), and the right time to build it is after the
text-pipeline corpus has enough buildings in it that the geometry layer has
something to compound against.

## Tradeoffs and what I'd do with more time

**The vision-model gap.** The biggest honest limit in this build is that
pypdf reads only the PDF text layer. Most of the visually rich content in a
DOB drawing — room labels, manuscript notes (the Kemperol no-substitution
clause is one), dimension annotations, handwritten markups — is rendered as
vector geometry or rasterized image and never enters the text stream. Every
"NOT ON SHEET" marker in the extraction output that names this limit is
honest about it. A v2 pipeline would call Claude Vision on each page image,
get back structured JSON with extracted text, region polygons, and
classification labels, and merge that against the pypdf text output to fill
the gaps. The architecture is ready for that — the `sheet_region` table
already has bbox columns waiting to be populated.

**Firm role disambiguation.** The title block carries multiple firm names
(Architect of Record, Design Firm, Professional Certifier). Without spatial
coordinates I can identify the firms by pattern but I can't reliably tell
which is which, except for the Professional Certifier (which co-locates
with the text "Professional Certification" and is therefore distinguishable
from layout alone). The extraction output names both AR-TECH ENGINEERING
P.C. and JD DESIGN ASSOCIATES, INC. as candidates and notes that the
disambiguation requires bbox-aware parsing — that's a one-line code change
once the vision extraction lands.

**The BIS portal HTML scrape.** Explicit "Work on Floor(s)" data lives in
the BIS portal HTML but not in the Socrata API. I expand floors from story
count and flag the inference with `EXPANDED_FROM_STORY_COUNT`. A production
ingester would scrape the portal HTML for the explicit floor list — that's
maybe a day of work but it requires building a polite-scraper layer and
some idempotency around the portal's session handling, which felt out of
scope for a build test.

**The pre-1985 paper records.** Twenty-three pre-1985 records are visible in
the BIS Actions ledger for 310 W 144 St. None of their underlying drawings
are in corpus because the drawings are on microfilm at 280 Broadway. The
coverage report surfaces this explicitly: "23 pre-1985 paper records in BIS
Actions ledger — physical microfilm at 280 Broadway." Adding them would
require a runner trip plus a separate ingestion pipeline that handles
microfilm scan quality, OCR, and lower confidence thresholds. The schema
already accommodates this (the `sheet` table has `source_format` and
`legibility` columns that distinguish BORN_DIGITAL from microfilm scans).

**A small refactor I didn't do.** The `controlling_job.py` conflict-detection
layer currently checks `existing_occupancy in ('J-2', 'R-2')` and `'garage'
in legal_use_text`. Both checks are general enough to fire on any future
building with the same pattern, but in a corpus of hundreds of buildings the
right shape is probably a `conflict_rules` table that names the
contradiction patterns explicitly and lets them be edited without code
changes. I'd build that the first time the second instance of the pattern
shows up.

## Honest limits on what's in this repo

- The vision-model integration described above isn't here. The architecture
  is ready for it; the code that calls it isn't written. Drawing body content
  not in the PDF text layer is consequently invisible to the extractors.
- Floor data for BIS-era filings is inferred from story count, not parsed
  from the BIS portal HTML. Inferences are flagged.
- 96 Perry's full filing history isn't ingested. The building was used only
  for Part 3 (sheet structuring), so filing-identity fields in the extraction
  output for job 140941514 are inferred from the sheet title block rather
  than pulled from a filing record. This is documented in the extraction
  output's coverage notes.
- Pre-1985 paper records are documented but not retrieved.
- No HTTP service layer. The CLI handles every Part 4 requirement; the
  service shape is explicitly out of scope per the scoping response.

## Stack

- Python 3.11+
- SQLite (stdlib `sqlite3`)
- pypdf (PDF text + metadata extraction)
- No cloud dependencies, no ORMs, no frameworks. Runs locally, ships fast,
  and the same pipeline is deployable as Lambda triggers behind an S3
  ingestion bucket with no architectural changes.

## Repo layout

```
blueprint/
  schema.py           13-table SQLite schema
  ingest.py           Walks data/<BIN>/ and builds corpus.db
  controlling_job.py  Part 2 resolver
  extractors.py       Generic PDF + title-block extractors (no hardcoded sheet data)
  sheets.py           Part 3 orchestrator — walks data/<BIN>/*.pdf
  extraction.py       Six-bucket target-field-list renderer
  cli.py              Part 4 CLI: ask, resolve, coverage, extract
data/
  <BIN>/property.json           Required per BIN
  <BIN>/bis_jobs.json           Optional, Socrata pull
  <BIN>/dobnow_jobs.json        Optional, Socrata pull
  <BIN>/dobnow_cos.json         Optional, Socrata pull
  <BIN>/bis_portal_notes.md     Optional, HTML scrape
  <BIN>/legacy_co.json          Optional, pre-DOB NOW paper CO sidecar
  <BIN>/interim_use.json        Optional, LNOs and other non-CO use records
  <BIN>/*.pdf                   Drawing sets — processed by sheets.py
corpus.db                       Generated, gitignored
requirements.txt                pypdf
```
