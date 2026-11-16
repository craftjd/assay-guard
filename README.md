# Assay import demo

Safer lab CSV → MySQL import PoC (raw / processing / main).

## Docs

- [Product plan](docs/IMPORT_DEMO_PLAN.md) — rules, outcomes, test cases  
- [Execution phase](docs/EXECUTION_PHASE.md) — Windows + Python + Laragon build plan  

## Stack

Python + FastAPI + Jinja2 + PyMySQL. MySQL from Laragon. Five pip packages only.

## Windows setup

1. Start **Laragon** and MySQL.
2. Create database `assay_import`.
3. Copy `.env.example` → `.env` (adjust password if needed).
4. Run `sql/schema.sql` and `sql/views.sql` in HeidiSQL.
5. In this folder:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8765
```

6. Open http://127.0.0.1:8765 — home page should show DB connected.

## Status

Slice 1 scaffold: app shell, schema, views. Import/process/apply flows not built yet.
