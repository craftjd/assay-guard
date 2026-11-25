# Fixture CSVs

Gold project despatch files for the demo loads. The lab column is `DespatchNo`.

- `gold_proj_despatch.csv` — 100 samples, `ACQ00101`–`ACQ00200`, on DDH-26-001 (core), DDH-26-002 (RC), and DDH-26-004 (core). Despatches DSP-2026-041, -042, and -044. Standards every 20th row, blanks every 25th, field duplicates every 15th, and six below-detection gold values (`-0.01`). Intervals on a hole only touch, never overlap.
- `gold_proj_despatch_edited.csv` — the same despatch with three corrections: `ACQ00104` gold `6.40`, `ACQ00137` gold `0.17`, and `ACQ00168` copper `0.88`.
- `gold_proj_bad_rows.csv` — blank sample, reversed interval, non-numeric gold, duplicate sample, and one good row.
- `gold_proj_reordered.csv` — the same assays with headers reordered and recased, plus a `Note` column.
- `gold_proj_overlap.csv` — one valid resample, `ACQ00301` on DDH-26-001 5.0–6.5 m. It saves, then fails the Main overlap check against `ACQ00104` and `ACQ00106`.
- `gold_proj_despatch_8000.csv` — 8,000 rows on DDH-26-101 and DDH-26-102, loaded in batches of 1,000.

`SampleID` is the row key. `STD` and `BLK` rows have no hole or interval. Config `test-config-1` matches these headers by name.
