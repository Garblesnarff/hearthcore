-- Least-privilege MySQL user provisioning template (Issues #62 & #75)
--
-- SAFETY:
--   - Contains NO hardcoded passwords; requires @brain_pw and @watch_pw session variables (>=16 chars).
--   - Restricts accounts strictly to localhost and @client_host (default '127.0.0.1'; NEVER wildcard '%').
--   - Restricts acore_auth.account SELECT to (id, username) ONLY (never exposes salt/verifier/email).
--
-- Usage:
--   Run via ops/provision-least-priv-db.sh or as root with session variables:
--   SET @brain_pw='...'; SET @watch_pw='...'; SOURCE ops/sql/create_least_priv_users.sql;

SET @client_host = IF(@client_host IS NULL OR @client_host = '' OR @client_host = '%', '127.0.0.1', @client_host);

-- Helper macro via prepared statements (fail loudly if passwords missing or <16 chars)
SET @s = IF(@brain_pw IS NULL OR CHAR_LENGTH(@brain_pw) < 16, 'SELECT FAIL_MISSING_BRAIN_PW()', CONCAT('CREATE USER IF NOT EXISTS ''hearthcore_brain''@''localhost'' IDENTIFIED BY ', QUOTE(@brain_pw)));
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = IF(@brain_pw IS NULL OR CHAR_LENGTH(@brain_pw) < 16, 'SELECT FAIL_MISSING_BRAIN_PW()', CONCAT('ALTER USER ''hearthcore_brain''@''localhost'' IDENTIFIED BY ', QUOTE(@brain_pw)));
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @s = IF(@brain_pw IS NULL OR CHAR_LENGTH(@brain_pw) < 16, 'SELECT FAIL_MISSING_BRAIN_PW()', CONCAT('CREATE USER IF NOT EXISTS ''hearthcore_brain''@', QUOTE(@client_host), ' IDENTIFIED BY ', QUOTE(@brain_pw)));
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = IF(@brain_pw IS NULL OR CHAR_LENGTH(@brain_pw) < 16, 'SELECT FAIL_MISSING_BRAIN_PW()', CONCAT('ALTER USER ''hearthcore_brain''@', QUOTE(@client_host), ' IDENTIFIED BY ', QUOTE(@brain_pw)));
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @s = IF(@watch_pw IS NULL OR CHAR_LENGTH(@watch_pw) < 16, 'SELECT FAIL_MISSING_WATCH_PW()', CONCAT('CREATE USER IF NOT EXISTS ''azeroth_watch''@''localhost'' IDENTIFIED BY ', QUOTE(@watch_pw)));
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = IF(@watch_pw IS NULL OR CHAR_LENGTH(@watch_pw) < 16, 'SELECT FAIL_MISSING_WATCH_PW()', CONCAT('ALTER USER ''azeroth_watch''@''localhost'' IDENTIFIED BY ', QUOTE(@watch_pw)));
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @s = IF(@watch_pw IS NULL OR CHAR_LENGTH(@watch_pw) < 16, 'SELECT FAIL_MISSING_WATCH_PW()', CONCAT('CREATE USER IF NOT EXISTS ''azeroth_watch''@', QUOTE(@client_host), ' IDENTIFIED BY ', QUOTE(@watch_pw)));
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = IF(@watch_pw IS NULL OR CHAR_LENGTH(@watch_pw) < 16, 'SELECT FAIL_MISSING_WATCH_PW()', CONCAT('ALTER USER ''azeroth_watch''@', QUOTE(@client_host), ' IDENTIFIED BY ', QUOTE(@watch_pw)));
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @bh = CONCAT('''hearthcore_brain''@''localhost'', ''hearthcore_brain''@', QUOTE(@client_host));
SET @wh = CONCAT('''azeroth_watch''@''localhost'', ''azeroth_watch''@', QUOTE(@client_host));

SET @s = CONCAT('REVOKE ALL PRIVILEGES, GRANT OPTION FROM ''hearthcore_brain''@''localhost''');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('REVOKE ALL PRIVILEGES, GRANT OPTION FROM ''hearthcore_brain''@', QUOTE(@client_host));
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- Read-only access to authoritative game state (column-scoped on acore_auth.account!)
SET @s = CONCAT('GRANT SELECT ON acore_characters.characters TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT ON acore_characters.character_skills TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT ON acore_characters.character_queststatus_rewarded TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT ON acore_characters.guild_bank_item TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT ON acore_characters.guild_member TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT ON acore_characters.item_instance TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT ON acore_characters.companion_schedule TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT ON acore_characters.companion_events TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT ON acore_characters.companion_relationships TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT ON acore_characters.companion_ledger TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT ON acore_characters.companion_weights TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT ON acore_characters.character_instance TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT ON acore_characters.instance TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT ON acore_characters.group_member TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT ON acore_characters.character_talent TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT ON acore_characters.character_spell TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT ON acore_characters.character_inventory TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT ON acore_world.item_template TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT (id, username) ON acore_auth.account TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- Write access strictly limited to planner/Quartermaster/Phase-II operational tables
SET @s = CONCAT('GRANT SELECT, INSERT, UPDATE, DELETE ON acore_characters.companion_weight_overrides TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT, INSERT, UPDATE, DELETE ON acore_characters.companion_proposals TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT, INSERT, UPDATE, DELETE ON acore_characters.companion_work_orders TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT, INSERT, UPDATE, DELETE ON acore_characters.companion_bank_requests TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT, INSERT, UPDATE ON acore_characters.companion_personas TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT, INSERT, UPDATE ON acore_characters.companion_persona_characters TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT, INSERT, UPDATE ON acore_characters.companion_opportunities TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT, INSERT, UPDATE ON acore_characters.companion_opportunity_interests TO ', @bh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- Dashboard read-only user (strictly SELECT)
SET @s = CONCAT('REVOKE ALL PRIVILEGES, GRANT OPTION FROM ''azeroth_watch''@''localhost''');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('REVOKE ALL PRIVILEGES, GRANT OPTION FROM ''azeroth_watch''@', QUOTE(@client_host));
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @s = CONCAT('GRANT SELECT ON acore_characters.* TO ', @wh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT ON acore_world.item_template TO ', @wh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT ON acore_world.quest_template TO ', @wh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT ON acore_world.player_xp_for_level TO ', @wh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT ON acore_auth.realmlist TO ', @wh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = CONCAT('GRANT SELECT (id, username) ON acore_auth.account TO ', @wh); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- Optional human operator dashboard user (narrowly scoped ONLY to companion direction tables; never characters/auth/world writes!)
SET @op_enabled = IF(@operator_pw IS NOT NULL AND CHAR_LENGTH(@operator_pw) >= 16, 1, 0);
SET @s = IF(@op_enabled = 1, CONCAT('CREATE USER IF NOT EXISTS ''hearthcore_operator''@''localhost'' IDENTIFIED BY ', QUOTE(@operator_pw)), 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = IF(@op_enabled = 1, CONCAT('ALTER USER ''hearthcore_operator''@''localhost'' IDENTIFIED BY ', QUOTE(@operator_pw)), 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = IF(@op_enabled = 1, CONCAT('CREATE USER IF NOT EXISTS ''hearthcore_operator''@', QUOTE(@client_host), ' IDENTIFIED BY ', QUOTE(@operator_pw)), 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = IF(@op_enabled = 1, CONCAT('ALTER USER ''hearthcore_operator''@', QUOTE(@client_host), ' IDENTIFIED BY ', QUOTE(@operator_pw)), 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @oh = CONCAT('''hearthcore_operator''@''localhost'', ''hearthcore_operator''@', QUOTE(@client_host));
SET @s = IF(@op_enabled = 1, 'REVOKE ALL PRIVILEGES, GRANT OPTION FROM ''hearthcore_operator''@''localhost''', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = IF(@op_enabled = 1, CONCAT('REVOKE ALL PRIVILEGES, GRANT OPTION FROM ''hearthcore_operator''@', QUOTE(@client_host)), 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @s = IF(@op_enabled = 1, CONCAT('GRANT SELECT ON acore_characters.characters TO ', @oh), 'SELECT 1'); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = IF(@op_enabled = 1, CONCAT('GRANT SELECT ON acore_characters.companion_proposals TO ', @oh), 'SELECT 1'); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = IF(@op_enabled = 1, CONCAT('GRANT SELECT ON acore_world.item_template TO ', @oh), 'SELECT 1'); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = IF(@op_enabled = 1, CONCAT('GRANT SELECT, INSERT, UPDATE, DELETE ON acore_characters.companion_weights TO ', @oh), 'SELECT 1'); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = IF(@op_enabled = 1, CONCAT('GRANT SELECT, INSERT, UPDATE, DELETE ON acore_characters.companion_weight_overrides TO ', @oh), 'SELECT 1'); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = IF(@op_enabled = 1, CONCAT('GRANT SELECT, INSERT, UPDATE ON acore_characters.companion_work_orders TO ', @oh), 'SELECT 1'); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @s = IF(@op_enabled = 1, CONCAT('GRANT SELECT, UPDATE ON acore_characters.companion_schedule TO ', @oh), 'SELECT 1'); PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

FLUSH PRIVILEGES;
