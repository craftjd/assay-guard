# Mining import demo — plan & brainstorm export

Open this file in Cursor (Windows or cloud). It is the durable export of the brainstorm and execution plan.

Live canvas (same content family): open from the agent chat canvas **Mining import demo — execution plan**.

---

## Problem

Lab assay CSVs are loaded into MySQL with little visibility. Failures force restarts. Validation happens late in SQL. That does not scale to a large (~30M row) project.

## Promise (PoC)

1. Same import does not double **raw** data.
2. **Processing** can be rerun on purpose (including when config/rules change).
3. **Main** data never gets quiet duplicates or in-place edits.

---

## Three tables

| Table | Role | Rerun behavior |
|-------|------|----------------|
| **raw** | Evidence from the file | Same import + same file fingerprint → no duplicates |
| **processing** | Result of applying rules to raw | User can replace this import’s processing result |
| **main** (curated) | Trusted business data | Changes only after review; add only; never edit live rows in place |

**Archive:** preferred over hard delete. When retiring data, keep a copy for diffs. Active main rows are retracted; the archive is the baseline for “what changed.”

---

## Locked choices

- **Keys:** normal acQuire row keys (do not invent a parallel key system).
- **Columns (PoC):** discover by **header name**, not column index/position. Missing required headers → fail clearly before saving to main.
- **Columns (later):** full mapping / synonyms / per-file mapping UI — **out of PoC**.
- **Interrupted save to main:** main unchanged for that import.
- **Finished import replace:** archive first, then load new, diff vs archive, then save.
- **Config change:** rerun processing → compare → user applies to main (not a blind raw reload).

---

## Row outcomes (each processing run)

These are statuses on **that run’s** processing result, not a lasting reject-management product.

| Outcome | Meaning | Main data action |
|---------|---------|------------------|
| **good** | New key, valid row | May add |
| **already_same** | Key in main, values match | Skip (true no-op) |
| **changed** | Key in main, values differ | Do **not** skip quietly. Show in compare. Update only via archive/replace (set aside old → add new) |
| **invalid** | Bad/unusable row | Do not add |

When processing is replaced, keep a short **run summary** (counts + config version). Do not build a tag/reject inbox UI in the PoC.

---

## The two compares

1. **Old processing vs new processing** — what changed in outcomes? (catches rule/config changes and file-driven reprocess results)
2. **Proposed main changes vs main** — what would be added or set aside?

After reprocess, processing and main can disagree until the user applies. Never auto-delete from main when rules newly fail a live row — list it and require confirm.

---

## Demo flows

### 1. First load
Register import → fill raw → run processing → show outcome counts → save good rows to main → rerun same import to prove raw/main stay safe.

### 2. Rerun processing
Same raw → replace processing for that import → show both compares → explicit apply (adds / set-asides).

### 3. Replace finished import / changed rows
Archive (keep copy, retract from main) → load new → diff vs archive → save good rows.

---

## Extra test cases

| Case | Expected |
|------|----------|
| Rerun processing, nothing changed | Compares empty; apply is a no-op |
| Reimport CSV, minor row value edits | New fingerprint; processing marks **changed**; archive/replace required to update main |
| Reimport CSV, major header changes | Stop early on header/config mismatch; no quiet main save |
| After those reimports: reprocess, same config | Compares show file-driven effects; do not blame config |

---

## Design notes that were rejected or deferred

- “Trust above all else” without one falsifiable claim → not useful alone.
- Seven-junction control plane → too much for PoC.
- Lasting skip/reject **tags** as a management system → weak if processing is replaced; use per-run outcomes + summary instead.
- Direct UPDATE of main rows → breaks the no quiet overwrite promise.
- Full mapping studio / reject editing UI / live 30M run → out of PoC.
- Versioned main history forever → nicer long-term; heavier than PoC needs.

### Same files, new rules
Not a normal rerun. Use **recheck / rerun processing**. Newly good may add; newly failed live rows warn only until explicit set-aside.

### How main data may change
Never edit in place. Always: archive baseline → diff → insert. Whole-import archive for replaced lab files; **changed** rows follow the same archive/replace idea for those keys.

---

## Build slices (order)

1. **Sample files + schema** — messy CSVs with named headers; raw / processing / main / archive tables  
2. **Import into raw** — `import_id` + file/row/column counts; same fingerprint does not duplicate  
3. **Processing + outcomes** — header-name discovery; good / already_same / changed / invalid; config version + run summary  
4. **Save to main** — only good rows; interrupt-safe  
5. **Rerun processing + compares** — including no-change proof  
6. **Reimport + archive for changed** — minor edits + major header mismatch  
7. **Thin demo UI** — walk flows without SQL; SQL views still available for the owner  

### Suggested shape
Small web app + real SQL (SQLite fine for demo; MySQL if closer feel). Fixture CSVs. Simple screens for the three flows. SQL views for import summary, latest run outcomes, archive.

---

## Must-show proofs

- Raw idempotent (same fingerprint → no raw duplicates)
- Header discovery (column order can differ; names still bind)
- Main safe on interrupt
- already_same vs changed (unchanged keys skip; edited keys do not)
- No-change reprocess
- Minor CSV edits → changed → archive/replace → main updates
- Major header change fails clearly before main save
- Reprocess after reimport shows file change without blaming config
- Archive replace: diff vs archive; main matches new good rows

---

## Out of this demo

- Full per-CSV mapping studio / synonym engine  
- Reject editing UI / lasting tag management  
- Live 30M-row run  
- Direct edits to main data rows  

---

## Open for build start

When ready to implement: start at **slice 1** (fixtures + schema). Do not expand scope into mapping studio or reject management in the first demo.
