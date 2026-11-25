# Execution phase — Windows + Python + Laragon

## Stack (locked for speed)

| Piece | Choice | Why |
|-------|--------|-----|
| Language | Python 3 | Already on your machine |
| Database | MySQL via Laragon | Real SQL; matches owner’s world |
| Web | FastAPI + Jinja2 templates | One process, simple screens, no Node/React |
| MySQL driver | PyMySQL | Pure Python; avoids Windows compile pain |
| CSV | stdlib `csv` | No pandas |
| Config | `.env` + python-dotenv | Local Laragon credentials only |

**Not using:** React/Next, Docker, SQLAlchemy ORM, pandas, Celery, mapping studio.

## Dependencies (only these)

```
fastapi
uvicorn[standard]
jinja2
pymysql
python-dotenv
```

Install once:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Laragon setup (once)

1. Start Laragon → MySQL running.
2. Create empty database, e.g. `assay_import`.
3. Copy `.env.example` → `.env` and set host/user/password/db (Laragon defaults are often `root` with empty password, host `127.0.0.1`, port `3306`).
4. Apply schema: run `sql/schema.sql` in HeidiSQL / MySQL console.

## Project folder

```
/
  README.md
  requirements.txt
  .env.example
  .gitignore
  docs/
    IMPORT_DEMO_PLAN.md      # product rules
    EXECUTION_PHASE.md       # this file
  app/
    main.py                  # routes + pages
    db.py                    # MySQL helpers
    settings.py              # env loader
    services/                # import / process / apply logic (add as slices land)
  sql/
    schema.sql
    views.sql
  fixtures/                  # sample CSVs
  templates/                 # Jinja screens
  static/                    # minimal CSS
```

## Build order (same product slices)

| Slice | Deliver |
|-------|---------|
| 1 | Schema + fixture CSVs + DB connects from app |
| 2 | Register import → load raw (idempotent by fingerprint) |
| 3 | Process → outcomes: good / already_same / changed / invalid |
| 4 | Save good rows to main (interrupt-safe) |
| 5 | Rerun processing + two compares |
| 6 | Reimport + archive for changed / header mismatch |
| 7 | Thin UI walkthrough of the three flows |

## Run (after slice 1+)

```bat
.venv\Scripts\activate
uvicorn app.main:app --reload --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765`.

## Rules while building

- Prefer SQL in `sql/` + short Python; keep logic readable in HeidiSQL.
- Header-name column discovery only; no index binding; no mapping UI.
- No lasting reject-tag product — per-run outcomes + summary only.
- Do not add packages unless a slice is blocked without them.
