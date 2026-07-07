# darfin-company-analysis

DART **정기공시**(사업/반기/분기보고서) pipeline worker. Collects → parses → diffs → LLM summarizes → writes to MySQL. No HTTP server.

## Commands

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Per-stock pipeline (manual)
python scripts/ingest_filings.py --stock 005930     # RAW
python scripts/parse_filings.py --stock 005930      # PARSED
python scripts/fetch_metrics.py --stock 005930      # Financial metrics
python scripts/diff_filings.py --stock 005930       # DIFFED
python scripts/build_overview.py --stock 005930     # Deterministic overview
python scripts/extract_findings.py --stock 005930   # LLM findings + scores

# Cron equivalents
python scripts/run_daily_scan.py    # Daily: ingest → diff (no Gemini)
python scripts/run_llm_worker.py    # Every minute: on-demand LLM queue
```

Requires `.env` with `GEMINI_API_KEY`, `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`.

## Structure

```
dart_pipeline/      # Pipeline stage logic (ingest, parse, diff, LLM)
dart_parser/        # DART XML → structured data
scripts/            # CLI entry points
data/               # DART XML/ZIP cache (gitignored)
IMPLEMENTATION_PLAN.md  # Design doc — read before any pipeline work
```

## Read before changing

**Always read [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) first** — pipeline stages, storage strategy, schema, frontend data contract, state machine.

| Contract | Location |
|----------|----------|
| Frontend API shapes | [../darfin-front/src/mocks/companyAnalysis/types.js](../darfin-front/src/mocks/companyAnalysis/types.js) |
| DB schema | [../darfin-main/ddl.sql](../darfin-main/ddl.sql) |

Update `IMPLEMENTATION_PLAN.md` when design changes.

## Pipeline state machine

`RAW → PARSED → DIFFED → SUMMARIZED`

Each stage is idempotent per `rcept_no` — re-run replaces prior output.

## Conventions

- Python 3.11+, Gemini via `google-genai`
- **Never store raw XML in MySQL** — filesystem cache in `data/`
- **LLM sees diffs only**, not full filing text
- All outputs must cite source sections mechanically (`FilingExcerptRef`)
- QoQ baseline: prior filing; YoY: same quarter prior year
- Record model name / tokens / cost per LLM call

## Do not

- Add an HTTP server — read API lives in `darfin-main`
- Touch 수시공시 (`disclosure` tables) — that's `darfin-disclosure`
- Change API response shapes without updating `types.js`
- Commit `.env` or `data/` cache files

## Related repos

- `../darfin-main` — Spring API reads pipeline output from MySQL
- `../darfin-front` — UI consumer, `types.js` is the contract
- `../darfin-disclosure` — separate 수시공시 pipeline (not this repo)
