-- Slice 1: core tables. Apply in Laragon MySQL (HeidiSQL or mysql client).
-- Database should already exist (see .env MYSQL_DATABASE).

CREATE TABLE IF NOT EXISTS import_batch (
  import_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  status VARCHAR(32) NOT NULL DEFAULT 'registered',
  note VARCHAR(255) NULL,
  PRIMARY KEY (import_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS import_file (
  file_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  import_id BIGINT UNSIGNED NOT NULL,
  file_name VARCHAR(255) NOT NULL,
  fingerprint CHAR(64) NOT NULL,
  row_count INT UNSIGNED NULL,
  column_count INT UNSIGNED NULL,
  headers_json JSON NULL,
  PRIMARY KEY (file_id),
  UNIQUE KEY uq_import_fingerprint (import_id, fingerprint),
  KEY ix_import_file_import (import_id),
  CONSTRAINT fk_import_file_batch FOREIGN KEY (import_id) REFERENCES import_batch (import_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Raw evidence (append for an import; idempotent by import + source identity).
CREATE TABLE IF NOT EXISTS raw_row (
  raw_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  import_id BIGINT UNSIGNED NOT NULL,
  file_id BIGINT UNSIGNED NOT NULL,
  source_row_no INT UNSIGNED NOT NULL,
  row_key VARCHAR(191) NULL,
  payload_json JSON NOT NULL,
  PRIMARY KEY (raw_id),
  UNIQUE KEY uq_raw_identity (import_id, file_id, source_row_no),
  KEY ix_raw_import (import_id),
  CONSTRAINT fk_raw_batch FOREIGN KEY (import_id) REFERENCES import_batch (import_id),
  CONSTRAINT fk_raw_file FOREIGN KEY (file_id) REFERENCES import_file (file_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Processing result for one run (replaceable per import when user reruns).
CREATE TABLE IF NOT EXISTS processing_run (
  run_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  import_id BIGINT UNSIGNED NOT NULL,
  config_version VARCHAR(64) NOT NULL,
  started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at TIMESTAMP NULL,
  summary_json JSON NULL,
  PRIMARY KEY (run_id),
  KEY ix_processing_run_import (import_id),
  CONSTRAINT fk_processing_run_batch FOREIGN KEY (import_id) REFERENCES import_batch (import_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS processing_row (
  processing_row_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id BIGINT UNSIGNED NOT NULL,
  import_id BIGINT UNSIGNED NOT NULL,
  raw_id BIGINT UNSIGNED NOT NULL,
  row_key VARCHAR(191) NULL,
  outcome ENUM('good', 'already_same', 'changed', 'invalid') NOT NULL,
  reason VARCHAR(255) NULL,
  payload_json JSON NOT NULL,
  PRIMARY KEY (processing_row_id),
  KEY ix_processing_row_run (run_id),
  KEY ix_processing_row_import (import_id),
  CONSTRAINT fk_processing_row_run FOREIGN KEY (run_id) REFERENCES processing_run (run_id),
  CONSTRAINT fk_processing_row_batch FOREIGN KEY (import_id) REFERENCES import_batch (import_id),
  CONSTRAINT fk_processing_row_raw FOREIGN KEY (raw_id) REFERENCES raw_row (raw_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Trusted main data (insert only; archive to change).
CREATE TABLE IF NOT EXISTS main_assay (
  row_key VARCHAR(191) NOT NULL,
  import_id BIGINT UNSIGNED NOT NULL,
  payload_json JSON NOT NULL,
  loaded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (row_key),
  KEY ix_main_import (import_id),
  CONSTRAINT fk_main_batch FOREIGN KEY (import_id) REFERENCES import_batch (import_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS archive_assay (
  archive_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  row_key VARCHAR(191) NOT NULL,
  from_import_id BIGINT UNSIGNED NOT NULL,
  payload_json JSON NOT NULL,
  archived_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  reason VARCHAR(255) NULL,
  PRIMARY KEY (archive_id),
  KEY ix_archive_key (row_key),
  KEY ix_archive_import (from_import_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
