-- Issue #20: Add authoritative native WotLK RDF/LFG tracking columns to companion_opportunities.
-- Standard MySQL 8.0+ idempotent migration using information_schema.columns + PREPARE/EXECUTE
-- (safe to run multiple times; strictly compatible with MySQL 8.0+ without MariaDB extensions).

SET @hc_db := DATABASE();

SET @hc_sql := (
  SELECT IF(
    EXISTS(
      SELECT 1 FROM information_schema.columns
      WHERE table_schema = @hc_db AND table_name = 'companion_opportunities' AND column_name = 'lfg_dungeon_id'
    ),
    'SELECT 1',
    'ALTER TABLE `companion_opportunities` ADD COLUMN `lfg_dungeon_id` int unsigned NOT NULL DEFAULT 0'
  )
);
PREPARE hc_stmt FROM @hc_sql;
EXECUTE hc_stmt;
DEALLOCATE PREPARE hc_stmt;

SET @hc_sql := (
  SELECT IF(
    EXISTS(
      SELECT 1 FROM information_schema.columns
      WHERE table_schema = @hc_db AND table_name = 'companion_opportunities' AND column_name = 'native_queue_id'
    ),
    'SELECT 1',
    'ALTER TABLE `companion_opportunities` ADD COLUMN `native_queue_id` varchar(64) NOT NULL DEFAULT '''''
  )
);
PREPARE hc_stmt FROM @hc_sql;
EXECUTE hc_stmt;
DEALLOCATE PREPARE hc_stmt;

SET @hc_sql := (
  SELECT IF(
    EXISTS(
      SELECT 1 FROM information_schema.columns
      WHERE table_schema = @hc_db AND table_name = 'companion_opportunities' AND column_name = 'native_proposal_id'
    ),
    'SELECT 1',
    'ALTER TABLE `companion_opportunities` ADD COLUMN `native_proposal_id` int unsigned NOT NULL DEFAULT 0'
  )
);
PREPARE hc_stmt FROM @hc_sql;
EXECUTE hc_stmt;
DEALLOCATE PREPARE hc_stmt;

SET @hc_sql := (
  SELECT IF(
    EXISTS(
      SELECT 1 FROM information_schema.columns
      WHERE table_schema = @hc_db AND table_name = 'companion_opportunities' AND column_name = 'native_run_id'
    ),
    'SELECT 1',
    'ALTER TABLE `companion_opportunities` ADD COLUMN `native_run_id` varchar(64) NOT NULL DEFAULT '''''
  )
);
PREPARE hc_stmt FROM @hc_sql;
EXECUTE hc_stmt;
DEALLOCATE PREPARE hc_stmt;

SET @hc_sql := (
  SELECT IF(
    EXISTS(
      SELECT 1 FROM information_schema.columns
      WHERE table_schema = @hc_db AND table_name = 'companion_opportunities' AND column_name = 'correlation_key'
    ),
    'SELECT 1',
    'ALTER TABLE `companion_opportunities` ADD COLUMN `correlation_key` varchar(96) NOT NULL DEFAULT '''''
  )
);
PREPARE hc_stmt FROM @hc_sql;
EXECUTE hc_stmt;
DEALLOCATE PREPARE hc_stmt;
