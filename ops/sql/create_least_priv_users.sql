-- Least-privilege MySQL users for Hearthcore AI Brain and Azeroth Watch (Issues #62 & #75)
-- Apply as root with passwords supplied via environment/session variables:
--   SET @brain_pw = '...'; SET @watch_pw = '...';
--   SOURCE ops/sql/create_least_priv_users.sql;
--
-- 1. hearthcore_brain: SELECT on required character/world/auth lookup tables,
--    INSERT/UPDATE/DELETE ONLY on companion_* operational tables it owns.
-- 2. azeroth_watch: strictly SELECT-only across dashboard read tables (zero write privileges!).

CREATE USER IF NOT EXISTS 'hearthcore_brain'@'%' IDENTIFIED BY 'CHANGE_ME_BRAIN';
CREATE USER IF NOT EXISTS 'hearthcore_brain'@'localhost' IDENTIFIED BY 'CHANGE_ME_BRAIN';
CREATE USER IF NOT EXISTS 'hearthcore_brain'@'127.0.0.1' IDENTIFIED BY 'CHANGE_ME_BRAIN';

REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'hearthcore_brain'@'%';
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'hearthcore_brain'@'localhost';
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'hearthcore_brain'@'127.0.0.1';

-- Read-only access to authoritative game state
GRANT SELECT ON acore_characters.characters TO 'hearthcore_brain'@'%', 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT ON acore_characters.character_skills TO 'hearthcore_brain'@'%', 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT ON acore_characters.character_queststatus_rewarded TO 'hearthcore_brain'@'%', 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT ON acore_characters.guild_bank_item TO 'hearthcore_brain'@'%', 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT ON acore_characters.guild_member TO 'hearthcore_brain'@'%', 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT ON acore_characters.item_instance TO 'hearthcore_brain'@'%', 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT ON acore_characters.companion_schedule TO 'hearthcore_brain'@'%', 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT ON acore_characters.companion_events TO 'hearthcore_brain'@'%', 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT ON acore_characters.companion_relationships TO 'hearthcore_brain'@'%', 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT ON acore_characters.companion_ledger TO 'hearthcore_brain'@'%', 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT ON acore_characters.companion_weights TO 'hearthcore_brain'@'%', 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT ON acore_auth.account TO 'hearthcore_brain'@'%', 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';

-- Write access strictly limited to planner/Quartermaster/Phase-II operational tables
GRANT SELECT, INSERT, UPDATE, DELETE ON acore_characters.companion_weight_overrides TO 'hearthcore_brain'@'%', 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT, INSERT, UPDATE, DELETE ON acore_characters.companion_proposals TO 'hearthcore_brain'@'%', 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT, INSERT, UPDATE, DELETE ON acore_characters.companion_work_orders TO 'hearthcore_brain'@'%', 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT, INSERT, UPDATE, DELETE ON acore_characters.companion_bank_requests TO 'hearthcore_brain'@'%', 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT, INSERT, UPDATE ON acore_characters.companion_personas TO 'hearthcore_brain'@'%', 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT, INSERT, UPDATE ON acore_characters.companion_persona_characters TO 'hearthcore_brain'@'%', 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT, INSERT, UPDATE ON acore_characters.companion_opportunities TO 'hearthcore_brain'@'%', 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';
GRANT SELECT, INSERT, UPDATE ON acore_characters.companion_opportunity_interests TO 'hearthcore_brain'@'%', 'hearthcore_brain'@'localhost', 'hearthcore_brain'@'127.0.0.1';

-- Dashboard read-only user (strictly SELECT)
CREATE USER IF NOT EXISTS 'azeroth_watch'@'%' IDENTIFIED BY 'CHANGE_ME_WATCH';
CREATE USER IF NOT EXISTS 'azeroth_watch'@'localhost' IDENTIFIED BY 'CHANGE_ME_WATCH';
CREATE USER IF NOT EXISTS 'azeroth_watch'@'127.0.0.1' IDENTIFIED BY 'CHANGE_ME_WATCH';

REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'azeroth_watch'@'%';
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'azeroth_watch'@'localhost';
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'azeroth_watch'@'127.0.0.1';

GRANT SELECT ON acore_characters.* TO 'azeroth_watch'@'%', 'azeroth_watch'@'localhost', 'azeroth_watch'@'127.0.0.1';
GRANT SELECT ON acore_world.item_template TO 'azeroth_watch'@'%', 'azeroth_watch'@'localhost', 'azeroth_watch'@'127.0.0.1';
GRANT SELECT ON acore_world.player_xp_for_level TO 'azeroth_watch'@'%', 'azeroth_watch'@'localhost', 'azeroth_watch'@'127.0.0.1';
GRANT SELECT ON acore_auth.realmlist TO 'azeroth_watch'@'%', 'azeroth_watch'@'localhost', 'azeroth_watch'@'127.0.0.1';
GRANT SELECT (id, username) ON acore_auth.account TO 'azeroth_watch'@'%', 'azeroth_watch'@'localhost', 'azeroth_watch'@'127.0.0.1';

FLUSH PRIVILEGES;
