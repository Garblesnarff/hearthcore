-- Canonical Hearthguard companion_* table DDL (Issue #64)
-- Generated from live acore_characters MySQL 8.4 schema (AUTO_INCREMENT stripped for clean CI fixtures).

-- Table: companion_bank_requests
CREATE TABLE IF NOT EXISTS `companion_bank_requests` (
  `item_entry` int unsigned NOT NULL,
  `requested_by` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '',
  `min_count` int unsigned NOT NULL DEFAULT '20',
  `note` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`item_entry`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: companion_events
CREATE TABLE IF NOT EXISTS `companion_events` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `guid` int unsigned NOT NULL,
  `event_type` varchar(24) NOT NULL,
  `summary` varchar(200) NOT NULL,
  `partner_guid` int unsigned NOT NULL DEFAULT '0',
  `zone_id` int unsigned NOT NULL DEFAULT '0',
  `created_at` int unsigned NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_comp_events_guid_time` (`guid`,`created_at` DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Table: companion_ledger
CREATE TABLE IF NOT EXISTS `companion_ledger` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `time` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `guid` int unsigned NOT NULL,
  `action` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL,
  `item` int unsigned NOT NULL DEFAULT '0',
  `count` int unsigned NOT NULL DEFAULT '0',
  `tab` tinyint DEFAULT NULL,
  `note` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '',
  PRIMARY KEY (`id`),
  KEY `idx_companion_ledger_guid_time` (`guid`,`time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Companion bank, vendor, trainer and repair ledger';

-- Table: companion_opportunities
CREATE TABLE IF NOT EXISTS `companion_opportunities` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `schema_version` tinyint unsigned NOT NULL DEFAULT '1',
  `revision` int unsigned NOT NULL DEFAULT '1',
  `kind` varchar(24) NOT NULL DEFAULT 'dungeon_run',
  `creator_guid` int unsigned NOT NULL DEFAULT '0',
  `faction` varchar(16) NOT NULL DEFAULT 'alliance',
  `guild_id` int unsigned NOT NULL DEFAULT '6',
  `dungeon_map_id` int unsigned NOT NULL DEFAULT '0',
  `difficulty` tinyint unsigned NOT NULL DEFAULT '0',
  `roles_json` varchar(160) NOT NULL DEFAULT '{"tank":1,"healer":1,"damage":3}',
  `state` varchar(24) NOT NULL DEFAULT 'recruiting',
  `created_at` int unsigned NOT NULL,
  `recruit_until` int unsigned NOT NULL,
  `estimated_minutes` smallint unsigned NOT NULL DEFAULT '45',
  `group_guid` int unsigned NOT NULL DEFAULT '0',
  `outcome_reason` varchar(120) NOT NULL DEFAULT '',
  `is_shadow` tinyint unsigned NOT NULL DEFAULT '1',
  `lfg_dungeon_id` int unsigned NOT NULL DEFAULT '0',
  `native_queue_id` varchar(64) NOT NULL DEFAULT '',
  `native_proposal_id` int unsigned NOT NULL DEFAULT '0',
  `native_run_id` varchar(64) NOT NULL DEFAULT '',
  `correlation_key` varchar(96) NOT NULL DEFAULT '',
  PRIMARY KEY (`id`),
  KEY `idx_co_state` (`state`,`is_shadow`,`recruit_until`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Table: companion_opportunity_interests
CREATE TABLE IF NOT EXISTS `companion_opportunity_interests` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `opportunity_id` bigint unsigned NOT NULL,
  `opportunity_revision` int unsigned NOT NULL DEFAULT '1',
  `persona_id` varchar(64) NOT NULL,
  `character_guid` int unsigned NOT NULL,
  `role` varchar(16) NOT NULL,
  `state` varchar(24) NOT NULL DEFAULT 'interested',
  `reason_code` varchar(64) NOT NULL DEFAULT '',
  `available_until` int unsigned NOT NULL,
  `reserved_until` int unsigned NOT NULL DEFAULT '0',
  `created_at` int unsigned NOT NULL,
  `updated_at` int unsigned NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_coi_opp_char` (`opportunity_id`,`character_guid`),
  KEY `idx_coi_persona_state` (`persona_id`,`state`,`reserved_until`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Table: companion_persona_characters
CREATE TABLE IF NOT EXISTS `companion_persona_characters` (
  `persona_id` varchar(64) NOT NULL,
  `realm_id` int unsigned NOT NULL DEFAULT '1',
  `character_guid` int unsigned NOT NULL,
  `incarnation` int unsigned NOT NULL DEFAULT '1',
  `slot_kind` varchar(16) NOT NULL DEFAULT 'main',
  `active` tinyint unsigned NOT NULL DEFAULT '1',
  `created_at` int unsigned NOT NULL,
  PRIMARY KEY (`realm_id`,`character_guid`,`incarnation`),
  KEY `idx_cpc_persona` (`persona_id`,`active`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Table: companion_personas
CREATE TABLE IF NOT EXISTS `companion_personas` (
  `persona_id` varchar(64) NOT NULL,
  `slug` varchar(32) NOT NULL,
  `faction` varchar(16) NOT NULL DEFAULT 'alliance',
  `owner_group` varchar(32) NOT NULL DEFAULT 'guild',
  `profile_rev` int unsigned NOT NULL DEFAULT '1',
  `state` varchar(16) NOT NULL DEFAULT 'active',
  `enrolled_coop` tinyint unsigned NOT NULL DEFAULT '0',
  `created_at` int unsigned NOT NULL,
  `updated_at` int unsigned NOT NULL,
  PRIMARY KEY (`persona_id`),
  UNIQUE KEY `uk_cp_slug` (`slug`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Table: companion_profiles
CREATE TABLE IF NOT EXISTS `companion_profiles` (
  `guid` int unsigned NOT NULL,
  `name` varchar(32) NOT NULL,
  `archetype` varchar(32) NOT NULL,
  `culture` varchar(24) NOT NULL DEFAULT 'social',
  `flaw` varchar(128) NOT NULL DEFAULT '',
  `bio` varchar(255) NOT NULL DEFAULT '',
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`guid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Table: companion_proposals
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

-- Table: companion_relationships
CREATE TABLE IF NOT EXISTS `companion_relationships` (
  `guid_a` int unsigned NOT NULL,
  `guid_b` int unsigned NOT NULL,
  `runs_together` int unsigned NOT NULL DEFAULT '0',
  `wipes` int unsigned NOT NULL DEFAULT '0',
  `help_given` int unsigned NOT NULL DEFAULT '0',
  `loot_contests` int unsigned NOT NULL DEFAULT '0',
  `affinity` int NOT NULL DEFAULT '0',
  `last_event` varchar(160) NOT NULL DEFAULT '',
  `updated_at` int unsigned NOT NULL DEFAULT '0',
  PRIMARY KEY (`guid_a`,`guid_b`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Table: companion_rep_goals
CREATE TABLE IF NOT EXISTS `companion_rep_goals` (
  `guid` int unsigned NOT NULL,
  `faction` int unsigned NOT NULL,
  `target_rank` int unsigned NOT NULL DEFAULT '7',
  `note` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '',
  PRIMARY KEY (`guid`,`faction`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Per-companion reputation goals';

-- Table: companion_schedule
CREATE TABLE IF NOT EXISTS `companion_schedule` (
  `name` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `shift_group` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'always',
  `start_hour` tinyint unsigned NOT NULL DEFAULT '0',
  `end_hour` tinyint unsigned NOT NULL DEFAULT '24',
  `days_mask` tinyint unsigned NOT NULL DEFAULT '127',
  `always_online` tinyint unsigned NOT NULL DEFAULT '0',
  `note` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '',
  PRIMARY KEY (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: companion_weight_overrides
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

-- Table: companion_weights
CREATE TABLE IF NOT EXISTS `companion_weights` (
  `guid` int unsigned NOT NULL,
  `key` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `value` float NOT NULL DEFAULT '0',
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `source` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '',
  PRIMARY KEY (`guid`,`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Per-companion weights (RPG status and duty)';

-- Table: companion_work_orders
CREATE TABLE IF NOT EXISTS `companion_work_orders` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `requester_guid` int unsigned NOT NULL,
  `requester_name` varchar(32) NOT NULL,
  `crafter_guid` int unsigned NOT NULL DEFAULT '0',
  `crafter_name` varchar(32) NOT NULL DEFAULT '',
  `attempt_id` bigint unsigned NOT NULL DEFAULT '0',
  `item_entry` int unsigned NOT NULL,
  `item_name` varchar(80) NOT NULL DEFAULT '',
  `quantity` int unsigned NOT NULL DEFAULT '1',
  `produced_qty` int unsigned NOT NULL DEFAULT '0',
  `transferred_qty` int unsigned NOT NULL DEFAULT '0',
  `received_qty` int unsigned NOT NULL DEFAULT '0',
  `delivery_tab` tinyint unsigned NOT NULL DEFAULT '255',
  `story` varchar(220) NOT NULL DEFAULT '',
  `status` varchar(16) NOT NULL DEFAULT 'open',
  `revision` int unsigned NOT NULL DEFAULT '1',
  `created_at` int unsigned NOT NULL,
  `updated_at` int unsigned NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_comp_orders_status` (`status`,`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

