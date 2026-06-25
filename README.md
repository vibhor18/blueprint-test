# Blueprint — build test

Thin end-to-end slice of a building-records retrieval system.
Two buildings, two pipelines.

## Buildings
- **310 W 144th Street** (BIN 1060779) — Part 2: controlling-job resolution
- **96 Perry Street** (BIN 1011231) — Part 3: sheet structuring

## Quickstart
```bash
pip install -r requirements.txt
python -m blueprint.ingest         # build corpus.db from data/
python -m blueprint.cli ask        # answer demo questions
```

## Structure
- `blueprint/` — code
- `data/` — raw ingested inputs (Socrata JSON + PDFs + portal notes)
- `corpus.db` — generated SQLite corpus

(Full README to follow as code lands.)
