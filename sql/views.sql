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
