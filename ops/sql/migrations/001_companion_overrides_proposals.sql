-- Migration 001: Phase I expiring weight overrides & typed native proposals (Issues #3, #6, #64)

CREATE TABLE IF NOT EXISTS `companion_weight_overrides` (
  `guid` int unsigned NOT NULL,
  `key` varchar(64) NOT NULL,
  `value` float NOT NULL,
  `plan_id` int unsigned NOT NULL DEFAULT '0',
  `revision` int unsigned NOT NULL DEFAULT '1',
  `source` varchar(32) NOT NULL DEFAULT 'hearthcore_ai',
  `enabled` tinyint unsigned NOT NULL DEFAULT '1',
  `created_at` int unsigned NOT NULL,
  `expires_at` int unsigned NOT NULL,
  PRIMARY KEY (`guid`,`key`),
  KEY `idx_cwo_expires` (`enabled`,`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `companion_proposals` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `plan_id` int unsigned NOT NULL,
  `guid` int unsigned NOT NULL,
  `character_name` varchar(32) NOT NULL,
  `goal_id` varchar(96) NOT NULL,
  `goal_type` varchar(24) NOT NULL,
  `target_id` int unsigned NOT NULL DEFAULT '0',
  `advisory_only` tinyint unsigned NOT NULL DEFAULT '0',
  `snapshot_rev` int unsigned NOT NULL DEFAULT '1',
  `status` varchar(24) NOT NULL DEFAULT 'proposed',
  `outcome_reason` varchar(120) NOT NULL DEFAULT '',
  `created_at` int unsigned NOT NULL,
  `expires_at` int unsigned NOT NULL,
  `started_at` int unsigned NOT NULL DEFAULT '0',
  `ended_at` int unsigned NOT NULL DEFAULT '0',
  `active_seconds` int unsigned NOT NULL DEFAULT '0',
  `progress_delta` int unsigned NOT NULL DEFAULT '0',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_cp_plan` (`plan_id`),
  KEY `idx_cp_guid_status` (`guid`,`status`,`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
