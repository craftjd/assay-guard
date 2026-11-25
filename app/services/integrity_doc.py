"""Structured copy for /docs (keep in sync with README «Data integrity»)."""


def doc_blocks() -> list[dict]:
    return [
        {"type": "p", "text": "Assay Guard is a reference implementation of safe lab CSV imports. The UI is minimal; the rules below are what matter for production data."},
        {"type": "h2", "text": "Three layers"},
        {
            "type": "table",
            "headers": ["Layer", "Holds", "Rule"],
            "rows": [
                ["Raw", "Exact file evidence", "Same file bytes stored once (SHA-256)."],
                ["Processing", "Per-row check results", "Rerun when config changes; runs stored only when outcomes change."],
                ["Main", "Trusted assays", "Add or update only after confirm; never in-place edit."],
            ],
        },
        {"type": "p", "text": "Archive keeps retired values so you can audit replacements, removals, voids, and rollbacks."},
        {"type": "h2", "text": "Row outcomes (per run)"},
        {
            "type": "table",
            "headers": ["Outcome", "Meaning", "Main"],
            "rows": [
                ["New", "Valid row, key not in main", "May add after confirm"],
                ["Same as main", "Values match", "Skip"],
                ["Different", "Values differ", "Archive old, then insert"],
                ["Fails config", "Breaks loaded config", "Do not add; may remove after confirm"],
            ],
        },
        {"type": "p", "text": "Config is a named file. CSV headers match by name, not column position."},
        {"type": "h2", "text": "Two compares"},
        {
            "type": "ul",
            "items": [
                "Run vs run — what changed when you reprocessed or the config changed.",
                "File vs main — what would add, update, remove, or stay dismissed.",
            ],
        },
        {"type": "p", "text": "Confirmed writes require the latest run and a matching digest so the list cannot drift."},
        {"type": "h2", "text": "Replace, void, roll back"},
        {
            "type": "ul",
            "items": [
                "Replace — new file version; old version read-only until you save changes.",
                "Void — rows leave main; copies stay in archive.",
                "Roll back — undo the current version and restore main from the prior one.",
            ],
        },
        {"type": "h2", "text": "Global checks"},
        {"type": "p", "text": "SQL checks on Main run against the full curated table (overlap, required fields, numerics)."},
        {"type": "h2", "text": "Out of scope here"},
        {
            "type": "ul",
            "items": [
                "Column-mapping studio or synonym UI",
                "Multi-user auth or locking",
                "30M-row production scale",
            ],
        },
    ]