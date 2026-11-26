-- Least-privilege MySQL user provisioning template (Issues #62 & #75)
--
-- SAFETY:
--   - Contains NO hardcoded passwords; requires @brain_pw and @watch_pw session variables (>=16 chars).
--   - Restricts accounts strictly to localhost / 127.0.0.1 (NEVER wildcard '%').
--   - Restricts acore_auth.account SELECT to (id, username) ONLY (never exposes salt/verifier/email).
--
-- Usage:
--   Run via ops/provision-least-priv-db.sh or as root with session variables:
--   SET @brain_pw='...'; SET @watch_pw='...'; SOURCE ops/sql/create_least_priv_users.sql;

SET @s_brain_lo = IF(
  @brain_pw IS NULL OR CHAR_LENGTH(@brain_pw) < 16,
  'SELECT FAIL_MISSING_BRAIN_PW()',
  CONCAT('CREATE USER IF NOT EXISTS ''hearthcore_brain''@''localhost'' IDENTIFIED BY ', QUOTE(@brain_pw))
);
PREPARE stmt FROM @s_brain_lo; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @s_brain_lo_alt = IF(
  @brain_pw IS NULL OR CHAR_LENGTH(@brain_pw) < 16,
  'SELECT FAIL_MISSING_BRAIN_PW()',
  CONCAT('ALTER USER ''hearthcore_brain''@''localhost'' IDENTIFIED BY ', QUOTE(@brain_pw))
);
PREPARE stmt FROM @s_brain_lo_alt; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @s_brain_ip = IF(
  @brain_pw IS NULL OR CHAR_LENGTH(@brain_pw) < 16,
  'SELECT FAIL_MISSING_BRAIN_PW()',
  CONCAT('CREATE USER IF NOT EXISTS ''hearthcore_brain''@''127.0.0.1'' IDENTIFIED BY ', QUOTE(@brain_pw))
);
PREPARE stmt FROM @s_brain_ip; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @s_brain_ip_alt = IF(
  @brain_pw IS NULL OR CHAR_LENGTH(@brain_pw) < 16,
  'SELECT FAIL_MISSING_BRAIN_PW()',
  CONCAT('ALTER USER ''hearthcore_brain''@''127.0.0.1'' IDENTIFIED BY ', QUOTE(@brain_pw))
);
PREPARE stmt FROM @s_brain_ip_alt; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @s_watch_lo = IF(
  @watch_pw IS NULL OR CHAR_LENGTH(@watch_pw) < 16,
  'SELECT FAIL_MISSING_WATCH_PW()',
  CONCAT('CREATE USER IF NOT EXISTS ''azeroth_watch''@''localhost'' IDENTIFIED BY ', QUOTE(@watch_pw))
);
PREPARE stmt FROM @s_watch_lo; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @s_watch_lo_alt = IF(
  @watch_pw IS NULL OR CHAR_LENGTH(@watch_pw) < 16,
  'SELECT FAIL_MISSING_WATCH_PW()',
  CONCAT('ALTER USER ''azeroth_watch''@''localhost'' IDENTIFIED BY ', QUOTE(@watch_pw))
);
PREPARE stmt FROM @s_watch_lo_alt; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @s_watch_ip = IF(
  @watch_pw IS NULL OR CHAR_LENGTH(@watch_pw) < 16,
  'SELECT FAIL_MISSING_WATCH_PW()',
  CONCAT('CREATE USER IF NOT EXISTS ''azeroth_watch''@''127.0.0.1'' IDENTIFIED BY ', QUOTE(@watch_pw))
);
PREPARE stmt FROM @s_watch_ip; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @s_watch_ip_alt = IF(
  @watch_pw IS NULL OR CHAR_LENGTH(@watch_pw) < 16,
  'SELECT FAIL_MISSING_WATCH_PW()',
  CONCAT('ALTER USER ''azeroth_watch''@''127.0.0.1'' IDENTIFIED BY ', QUOTE(@watch_pw))
);
PREPARE stmt FROM @s_watch_ip_alt; EXECUTE stmt; DEALLOCATE PREPARE stmt;

REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'hearthcore_brain'@'localhost';
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'hearthcore_brain'@'127.0.0.1';

-- Read-only access to authoritative game state (column-scoped on acore_auth.account!)
GRANT SELECT ON acore_characters.characters TO 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT ON acore_characters.character_skills TO 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT ON acore_characters.character_queststatus_rewarded TO 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT ON acore_characters.guild_bank_item TO 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT ON acore_characters.guild_member TO 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT ON acore_characters.item_instance TO 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT ON acore_characters.companion_schedule TO 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT ON acore_characters.companion_events TO 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT ON acore_characters.companion_relationships TO 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT ON acore_characters.companion_ledger TO 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT ON acore_characters.companion_weights TO 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT (id, username) ON acore_auth.account TO 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';

-- Write access strictly limited to planner/Quartermaster/Phase-II operational tables
GRANT SELECT, INSERT, UPDATE, DELETE ON acore_characters.companion_weight_overrides TO 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT, INSERT, UPDATE, DELETE ON acore_characters.companion_proposals TO 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT, INSERT, UPDATE, DELETE ON acore_characters.companion_work_orders TO 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT, INSERT, UPDATE, DELETE ON acore_characters.companion_bank_requests TO 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT, INSERT, UPDATE ON acore_characters.companion_personas TO 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT, INSERT, UPDATE ON acore_characters.companion_persona_characters TO 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT, INSERT, UPDATE ON acore_characters.companion_opportunities TO 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT, INSERT, UPDATE ON acore_characters.companion_opportunity_interests TO 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';

-- Dashboard read-only user (strictly SELECT, localhost/127.0.0.1 only)
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'azeroth_watch'@'localhost';
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'azeroth_watch'@'127.0.0.1';

GRANT SELECT ON acore_characters.* TO 'azeroth_watch'@'localhost', 'azeroth_watch'@'127.0.0.1';
GRANT SELECT ON acore_world.item_template TO 'azeroth_watch'@'localhost', 'azeroth_watch'@'127.0.0.1';
GRANT SELECT ON acore_world.player_xp_for_level TO 'azeroth_watch'@'localhost', 'azeroth_watch'@'127.0.0.1';
GRANT SELECT ON acore_auth.realmlist TO 'azeroth_watch'@'localhost', 'azeroth_watch'@'127.0.0.1';
GRANT SELECT (id, username) ON acore_auth.account TO 'azeroth_watch'@'localhost', 'azeroth_watch'@'127.0.0.1';

FLUSH PRIVILEGES;
