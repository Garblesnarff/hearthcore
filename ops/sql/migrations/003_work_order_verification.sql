-- Migration 003: Work-order quantity & delivery verification columns (Issue #27)
-- Idempotent for MySQL 8.0 (checks information_schema.columns before each ALTER TABLE).

SET @db = DATABASE();

SET @sql = IF(
  (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = @db AND table_name = 'companion_work_orders' AND column_name = 'attempt_id') = 0,
  'ALTER TABLE `companion_work_orders` ADD COLUMN `attempt_id` bigint unsigned NOT NULL DEFAULT 0 AFTER `crafter_name`',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = IF(
  (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = @db AND table_name = 'companion_work_orders' AND column_name = 'produced_qty') = 0,
  'ALTER TABLE `companion_work_orders` ADD COLUMN `produced_qty` int unsigned NOT NULL DEFAULT 0 AFTER `quantity`',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = IF(
  (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = @db AND table_name = 'companion_work_orders' AND column_name = 'transferred_qty') = 0,
  'ALTER TABLE `companion_work_orders` ADD COLUMN `transferred_qty` int unsigned NOT NULL DEFAULT 0 AFTER `produced_qty`',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = IF(
  (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = @db AND table_name = 'companion_work_orders' AND column_name = 'received_qty') = 0,
  'ALTER TABLE `companion_work_orders` ADD COLUMN `received_qty` int unsigned NOT NULL DEFAULT 0 AFTER `transferred_qty`',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = IF(
  (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = @db AND table_name = 'companion_work_orders' AND column_name = 'delivery_tab') = 0,
  'ALTER TABLE `companion_work_orders` ADD COLUMN `delivery_tab` tinyint unsigned NOT NULL DEFAULT 255 AFTER `received_qty`',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = IF(
  (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = @db AND table_name = 'companion_work_orders' AND column_name = 'revision') = 0,
  'ALTER TABLE `companion_work_orders` ADD COLUMN `revision` int unsigned NOT NULL DEFAULT 1 AFTER `status`',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
