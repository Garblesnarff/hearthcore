-- Migration 002: Phase II Living Guild persona registry & opportunity tables (Issue #64)
-- Exact column parity with canonical ops/sql/companion_schema.sql.

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
  PRIMARY KEY (`id`),
  KEY `idx_co_state` (`state`,`is_shadow`,`recruit_until`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

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
