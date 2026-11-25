-- Handy views for the SQL-first owner. Apply after schema.sql.

CREATE OR REPLACE VIEW v_import_summary AS
SELECT
  b.import_id,
  b.status,
  b.created_at,
  COUNT(DISTINCT f.file_id) AS file_count,
  COALESCE(SUM(f.row_count), 0) AS reported_rows
FROM import_batch b
LEFT JOIN import_file f ON f.import_id = b.import_id
GROUP BY b.import_id, b.status, b.created_at;

CREATE OR REPLACE VIEW v_latest_processing_outcomes AS
SELECT
  pr.import_id,
  pr.run_id,
  pr.config_version,
  prow.outcome,
  COUNT(*) AS row_count
FROM processing_run pr
INNER JOIN (
  SELECT import_id, MAX(run_id) AS run_id
  FROM processing_run
  GROUP BY import_id
) latest ON latest.run_id = pr.run_id
INNER JOIN processing_row prow ON prow.run_id = pr.run_id
GROUP BY pr.import_id, pr.run_id, pr.config_version, prow.outcome;

CREATE OR REPLACE VIEW v_archive_vs_main AS
SELECT
  a.archive_id,
  a.row_key,
  a.from_import_id,
  a.payload_json AS archived_payload,
  a.archived_at,
  a.reason,
  a.run_id,
  a.replaced_by_import_id,
  a.payload_sha,
  m.import_id AS main_import_id,
  m.payload_json AS main_payload
FROM archive_assay a
LEFT JOIN main_assay m ON m.row_key = a.row_key;
