-- Migration 002: Phase II Living Guild persona registry & opportunity tables (Issue #64)

CREATE TABLE IF NOT EXISTS `companion_personas` (
  `persona_id` varchar(64) NOT NULL,
  `display_name` varchar(64) NOT NULL,
  `shift_group` varchar(32) NOT NULL DEFAULT '',
  `enrolled_coop` tinyint unsigned NOT NULL DEFAULT '0',
  `created_at` int unsigned NOT NULL DEFAULT '0',
  `updated_at` int unsigned NOT NULL DEFAULT '0',
  PRIMARY KEY (`persona_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `companion_persona_characters` (
  `character_guid` int unsigned NOT NULL,
  `persona_id` varchar(64) NOT NULL,
  `character_slug` varchar(64) NOT NULL,
  `is_primary` tinyint unsigned NOT NULL DEFAULT '1',
  `linked_at` int unsigned NOT NULL DEFAULT '0',
  PRIMARY KEY (`character_guid`),
  KEY `idx_cpc_persona` (`persona_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `companion_opportunities` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `schema_version` int unsigned NOT NULL DEFAULT '1',
  `revision` int unsigned NOT NULL DEFAULT '1',
  `kind` varchar(32) NOT NULL,
  `creator_guid` int unsigned NOT NULL DEFAULT '0',
  `faction` varchar(16) NOT NULL DEFAULT 'alliance',
  `guild_id` int unsigned NOT NULL DEFAULT '6',
  `dungeon_id` varchar(64) NOT NULL,
  `dungeon_map_id` int unsigned NOT NULL DEFAULT '0',
  `difficulty` tinyint unsigned NOT NULL DEFAULT '0',
  `roles_json` varchar(255) NOT NULL,
  `state` varchar(24) NOT NULL,
  `created_at` int unsigned NOT NULL,
  `recruit_until` int unsigned NOT NULL,
  `estimated_minutes` int unsigned NOT NULL DEFAULT '45',
  `group_id` int unsigned NOT NULL DEFAULT '0',
  `outcome_reason` varchar(120) NOT NULL DEFAULT '',
  `is_shadow` tinyint unsigned NOT NULL DEFAULT '1',
  PRIMARY KEY (`id`),
  KEY `idx_co_state` (`state`,`is_shadow`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `companion_opportunity_interests` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `opportunity_id` bigint unsigned NOT NULL,
  `opportunity_revision` int unsigned NOT NULL DEFAULT '1',
  `persona_id` varchar(64) NOT NULL,
  `character_guid` int unsigned NOT NULL,
  `character_slug` varchar(64) NOT NULL,
  `role` varchar(16) NOT NULL,
  `state` varchar(24) NOT NULL,
  `reason_code` varchar(64) NOT NULL DEFAULT '',
  `available_until` int unsigned NOT NULL,
  `reserved_until` int unsigned NOT NULL DEFAULT '0',
  `created_at` int unsigned NOT NULL,
  `updated_at` int unsigned NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_coi_opp_persona` (`opportunity_id`,`persona_id`),
  KEY `idx_coi_persona_state` (`persona_id`,`state`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
