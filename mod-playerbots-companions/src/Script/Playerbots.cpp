/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#include "CompanionBotMgr.h"
#include "CompanionBrain.h"
#include "CompanionLife.h"
#include "CompanionTrip.h"
#include "DBCStores.h"
#include "Playerbots.h"
#include "BattleGroundTactics.h"
#include "BattlefieldScript.h"
#include "Channel.h"
#include "CheckMountStateAction.h"
#include "Config.h"
#include "BuiltInConfig.h"
#include "DBUpdater.h"
#include "DatabaseEnv.h"
#include "PlayerbotsDatabase.h"
#include <mysqld_error.h>
#include "GuildTaskMgr.h"
#include "PlayerScript.h"
#include "PlayerbotAIConfig.h"
#include "PlayerbotCommandScript.h"
#include "PlayerbotGuildMgr.h"
#include "PlayerbotSpellRepository.h"
#include "PlayerbotWorldThreadProcessor.h"
#include "RandomPlayerbotMgr.h"
#include "ScriptMgr.h"
#include "SpellAuras.h"
#include "UnitScript.h"
#include "WorldSession.h"
#include "WorldSessionMgr.h"
#include "cmath"

class PlayerbotsDatabaseScript : public DatabaseScript
{
public:
    PlayerbotsDatabaseScript() : DatabaseScript("PlayerbotsDatabaseScript") {}

    bool OnModuleDatabasesLoading() override
    {
        std::string const dbString = sConfigMgr->GetOption<std::string>("PlayerbotsDatabaseInfo", "");
        if (dbString.empty())
        {
            LOG_ERROR("server.playerbots", "Playerbots database is not specified in configuration file");
            return false;
        }

        uint8 const synchThreads = sConfigMgr->GetOption<uint8>("PlayerbotsDatabase.SynchThreads", 2);
        PlayerbotsDatabase.SetConnectionInfo(dbString, synchThreads);

        bool const updatesEnabled = sConfigMgr->GetOption<bool>("Playerbots.Updates.EnableDatabases", true);
        if (updatesEnabled && !DBUpdaterUtil::CheckExecutable())
            return false;

        uint32 error = PlayerbotsDatabase.Open();
        if (error == ER_BAD_DB_ERROR && updatesEnabled)
        {
            // Database missing: create it through the mysql CLI and connect again
            if (!ModuleDBUpdater::Create(PlayerbotsDatabase))
                return false;

            error = PlayerbotsDatabase.Open();
        }

        if (error)
        {
            LOG_ERROR("server.playerbots", "Cannot connect to the playerbots database, error {}", error);
            return false;
        }

        if (updatesEnabled)
        {
            DBUpdaterInfo const info = {
                "Playerbots",
                BuiltInConfig::GetSourceDirectory() + "/modules/mod-playerbots",
                BuiltInConfig::GetSourceDirectory() + "/modules/mod-playerbots/data/sql/playerbots/base/",
                "db_playerbot"
            };

            if (!ModuleDBUpdater::Populate(PlayerbotsDatabase, info))
            {
                LOG_ERROR("server.playerbots", "Could not populate the playerbots database, see log for details.");
                return false;
            }

            if (!ModuleDBUpdater::Update(PlayerbotsDatabase, info))
            {
                LOG_ERROR("server.playerbots", "Could not update the playerbots database, see log for details.");
                return false;
            }
        }

        if (!PlayerbotsDatabase.PrepareStatements())
        {
            LOG_ERROR("server.playerbots", "Could not prepare statements of the playerbots database, see log for details.");
            return false;
        }

        return true;
    }

    void OnModuleDatabasesKeepAlive() override { PlayerbotsDatabase.KeepAlive(); }

    void OnModuleDatabasesClosing() override { PlayerbotsDatabase.Close(); }

    void OnDatabaseWarnAboutSyncQueries(bool apply) override { PlayerbotsDatabase.WarnAboutSyncQueries(apply); }

    void OnDatabaseGetDBRevision(std::map<std::string, std::string>& revisions) override
    {
        std::string revision;
        if (QueryResult resultPlayerbot =
                PlayerbotsDatabase.Query("SELECT date FROM version_db_playerbots ORDER BY date DESC LIMIT 1"))
        {
            Field* fields = resultPlayerbot->Fetch();
            revision = fields[0].Get<std::string>();
        }

        if (revision.empty())
            revision = "Unknown Playerbots Database Revision";

        revisions["Playerbots"] = revision;
    }
};

class PlayerbotsPlayerScript : public PlayerScript
{
public:
    PlayerbotsPlayerScript() : PlayerScript("PlayerbotsPlayerScript", {
        PLAYERHOOK_ON_LOGIN,
        PLAYERHOOK_ON_AFTER_UPDATE,
        PLAYERHOOK_ON_BEFORE_CRITERIA_PROGRESS,
        PLAYERHOOK_ON_BEFORE_ACHI_COMPLETE,
        PLAYERHOOK_CAN_PLAYER_USE_PRIVATE_CHAT,
        PLAYERHOOK_CAN_PLAYER_USE_GROUP_CHAT,
        PLAYERHOOK_CAN_PLAYER_USE_GUILD_CHAT,
        PLAYERHOOK_CAN_PLAYER_USE_CHANNEL_CHAT,
        PLAYERHOOK_ON_GIVE_EXP,
        PLAYERHOOK_ON_BEFORE_TELEPORT,
        PLAYERHOOK_ON_PLAYER_QUEST_ACCEPT,
        PLAYERHOOK_ON_PLAYER_COMPLETE_QUEST,
        PLAYERHOOK_ON_LEVEL_CHANGED,
        PLAYERHOOK_ON_UPDATE_SKILL,
        PLAYERHOOK_ON_CREATURE_KILL,
        PLAYERHOOK_ON_PLAYER_KILLED_BY_CREATURE
    }) {}

    // Quest accept and turn-in times for companions, shown by .companion status.
    void OnPlayerQuestAccept(Player* player, Quest const* quest) override
    {
        if (!sCompanionBotMgr.IsCompanion(player))
            return;
        sCompanionBotMgr.NoteQuestAccept(player->GetGUID().GetCounter(), quest->GetQuestId(),
                                         static_cast<uint32>(GameTime::GetGameTime().count()));
        LOG_INFO("playerbots", "Companion {}: quest accept id={} level={} zone={}", player->GetName(),
                 quest->GetQuestId(), quest->GetQuestLevel(), player->GetZoneId());
    }

    void OnPlayerCompleteQuest(Player* player, Quest const* quest) override
    {
        if (sCompanionBotMgr.IsCompanion(player))
        {
            sCompanionBotMgr.NoteQuestTurnIn(player->GetGUID().GetCounter(), quest->GetQuestId(),
                                             static_cast<uint32>(GameTime::GetGameTime().count()));
            CompanionLife::OnQuestComplete(player, quest);
        }
    }

    // Companion milestones (whispered when not grouped, party chat when grouped) + Guild Life real event/cheers.
    void OnPlayerLevelChanged(Player* player, uint8 /*oldLevel*/) override
    {
        CompanionLife::OnLevelUp(player);
        if (!sCompanionBotMgr.IsCompanion(player))
            return;
        if (PlayerbotAI* botAI = GET_PLAYERBOT_AI(player))
            sCompanionBotMgr.SendMilestone(botAI, "level", "I reached level " + std::to_string(player->GetLevel()) + ".");
    }

    void OnPlayerCreatureKill(Player* killer, Creature* killed) override
    {
        CompanionLife::OnCreatureKill(killer, killed);
    }

    void OnPlayerKilledByCreature(Creature* killer, Player* victim) override
    {
        CompanionLife::OnPlayerDeath(victim, killer);
    }

    void OnPlayerUpdateSkill(Player* player, uint32 skillId, uint32 value, uint32 /*max*/, uint32 /*step*/,
                             uint32 newValue) override
    {
        if (!sCompanionBotMgr.IsCompanion(player))
            return;
        SkillLineEntry const* line = sSkillLineStore.LookupEntry(skillId);
        if (!line || line->categoryId != SKILL_CATEGORY_PROFESSION)
            return;
        if (newValue > value)
            CompanionLife::OnSkillMilestone(player, skillId, newValue);
        // A profession rank is learned when its skill crosses 75, 150, 225 or 300.
        for (uint32 tier : { 75u, 150u, 225u, 300u })
        {
            if (value < tier && newValue >= tier)
            {
                if (PlayerbotAI* botAI = GET_PLAYERBOT_AI(player))
                    sCompanionBotMgr.SendMilestone(botAI, "profession",
                                                   "I learned a profession rank: skill " + std::to_string(skillId) +
                                                       " is now " + std::to_string(tier) + ".");
                return;
            }
        }
    }

    void OnPlayerLogin(Player* player) override
    {
        if (!player->GetSession()->IsBot())
        {
            PlayerbotsMgr::instance().AddPlayerbotData(player, false);
            sRandomPlayerbotMgr.OnPlayerLogin(player);

            // Before modifying the following messages, please make sure it does not violate the GNU GPLv2
            // license especially if you are distributing a repack or hosting a public server
            // e.g. you can replace the URL with your own repository,
            // but it should be publicly accessible and include all modifications you've made
            if (sPlayerbotAIConfig.enabled)
            {
                ChatHandler(player->GetSession()).SendSysMessage(
                    "|cff00ff00This server runs with |cff00ccffmod-playerbots|r "
                    "|cffcccccchttps://github.com/mod-playerbots/mod-playerbots|r");
            }

            if (sPlayerbotAIConfig.enabled || sPlayerbotAIConfig.randomBotAutologin)
            {
                std::string maxAllowedBotCount = std::to_string(sRandomPlayerbotMgr.GetMaxAllowedBotCount());

                ChatHandler(player->GetSession()).SendSysMessage(
                    "|cff00ff00Playerbots:|r The server is configured with " + maxAllowedBotCount + " bots.");
            }
        }
    }

    bool OnPlayerBeforeTeleport(Player* /*player*/, uint32 /*mapid*/, float /*x*/, float /*y*/, float /*z*/,
                                float /*orientation*/, uint32 /*options*/, Unit* /*target*/) override
    {
        /* for now commmented out until proven its actually required
        * havent seen any proof CleanVisibilityReferences() is needed

        // If the player is not safe to touch, do nothing
        if (!player)
            return true;

        // If same map or not in world do nothing
        if (!player->IsInWorld() || player->GetMapId() == mapid)
            return true;

        // If this is a selfbot, do nothing
        PlayerbotAI* ai = GET_PLAYERBOT_AI(player);
        if (!ai || IsSelfBot(player))
            return true;

        // Cross-map bot teleport: defer visibility reference cleanup.
        // CleanVisibilityReferences() erases this bot's GUID from other objects' visibility containers.
        // This is intentionally done via the event queue (instead of directly here) because erasing
        // from other players' visibility maps inside the teleport call stack can hit unsafe re-entrancy
        // or iterator invalidation while visibility updates are in progress
        ObjectGuid guid = player->GetGUID();
        player->m_Events.AddEventAtOffset(
            [guid, mapid]()
            {
                // do nothing, if the player is not safe to touch
                Player* p = ObjectAccessor::FindPlayer(guid);
                if (!p || !p->IsInWorld() || p->IsDuringRemoveFromWorld())
                    return;

                // do nothing if we are already on the target map
                if (p->GetMapId() == mapid)
                    return;

                p->GetObjectVisibilityContainer().CleanVisibilityReferences();
            },
            Milliseconds(0));

        */

        return true;
    }

    void OnPlayerAfterUpdate(Player* player, uint32 diff) override
    {
        PlayerbotAI* const botAI = PlayerbotsMgr::instance().GetPlayerbotAI(player);

        if (botAI != nullptr)
        {
            botAI->UpdateAI(diff);
        }

        if (PlayerbotMgr* playerbotMgr = GET_PLAYERBOT_MGR(player))
        {
            playerbotMgr->UpdateAI(diff);
        }
    }

    using PlayerScript::OnPlayerCanUseChat;  // keep the base overloads visible

    bool OnPlayerCanUseChat(Player* player, uint32 type, uint32 /*lang*/, std::string& msg, Player* receiver) override
    {
        if (type != CHAT_MSG_WHISPER)
        {
            return true;
        }

        PlayerbotAI* const botAI = PlayerbotsMgr::instance().GetPlayerbotAI(receiver);

        if (botAI == nullptr)
        {
            return true;
        }

        botAI->HandleCommand(type, msg, player);

        // hotfix; otherwise the server will crash when whispering logout
        // https://github.com/mod-playerbots/mod-playerbots/pull/1838
        // TODO: find the root cause and solve it. (does not happen in party chat)
        if (msg == "logout")
            return false;

        return true;
    }

    bool OnPlayerCanUseChat(Player* player, uint32 type, uint32 /*lang*/, std::string& msg, Group* group) override
    {
        for (GroupReference* itr = group->GetFirstMember(); itr != nullptr; itr = itr->next())
        {
            Player* const member = itr->GetSource();

            if (member == nullptr)
                continue;

            PlayerbotAI* const botAI = PlayerbotsMgr::instance().GetPlayerbotAI(member);

            if (botAI == nullptr)
                continue;

            botAI->HandleCommand(type, msg, player);
        }

        return true;
    }

    bool OnPlayerCanUseChat(Player* player, uint32 type, uint32 /*lang*/, std::string& msg, Guild* /*guild*/) override
    {
        if (type != CHAT_MSG_GUILD)
            return true;

        PlayerbotMgr* playerbotMgr = PlayerbotsMgr::instance().GetPlayerbotMgr(player);

        if (playerbotMgr == nullptr)
            return true;

        for (PlayerBotMap::const_iterator it = playerbotMgr->GetPlayerBotsBegin(); it != playerbotMgr->GetPlayerBotsEnd(); ++it)
        {
            Player* const bot = it->second;

            if (bot == nullptr)
                continue;

            if (bot->GetGuildId() != player->GetGuildId())
                continue;

            PlayerbotsMgr::instance().GetPlayerbotAI(bot)->HandleCommand(type, msg, player);
        }

        return true;
    }

    bool OnPlayerCanUseChat(Player* player, uint32 type, uint32 /*lang*/, std::string& msg, Channel* channel) override
    {
        PlayerbotMgr* const playerbotMgr = PlayerbotsMgr::instance().GetPlayerbotMgr(player);

        if (playerbotMgr != nullptr && channel->GetFlags() & 0x18)
            playerbotMgr->HandleCommand(type, msg);

        sRandomPlayerbotMgr.HandleCommand(type, msg, player);

        return true;
    }

    bool OnPlayerBeforeAchievementComplete(Player* player, AchievementEntry const* achievement) override
    {
        if ((sRandomPlayerbotMgr.IsRandomBot(player) || sRandomPlayerbotMgr.IsAddclassBot(player)) &&
            (achievement->flags & (ACHIEVEMENT_FLAG_REALM_FIRST_REACH | ACHIEVEMENT_FLAG_REALM_FIRST_KILL)))
        {
            return false;
        }

        return true;
    }

    void OnPlayerGiveXP(Player* player, uint32& amount, Unit* /*victim*/, uint8 /*xpSource*/) override
    {
        // companion bots gain no XP at owner level + CompanionLevelLead; quests still complete
        if (sCompanionBotMgr.IsCompanion(player) && sCompanionBotMgr.IsXpCapped(player))
        {
            // Debug proof of the cap: at most one line per companion per 10 minutes (WARN, shown at playerbots=3).
            uint32 now = static_cast<uint32>(GameTime::GetGameTime().count());
            if (sCompanionBotMgr.ShouldLogXpBlock(player->GetGUID().GetCounter(), now))
            {
                uint32 ownerLevel = sCompanionBotMgr.GetOwnerLevel();
                uint32 lead = sPlayerbotAIConfig.companionLevelLead;
                if (!ownerLevel)
                    LOG_WARN("playerbots", "[CompanionXP] blocked: {} level {}, owner level unknown yet, XP {} zeroed",
                             player->GetName(), player->GetLevel(), amount);
                else
                    LOG_WARN("playerbots",
                             "[CompanionXP] blocked: {} level {} >= cap {} (owner {} + lead {}), XP {} zeroed",
                             player->GetName(), player->GetLevel(), ownerLevel + lead, ownerLevel, lead, amount);
            }
            amount = 0;
            return;
        }

        // early return
        if (sPlayerbotAIConfig.randomBotXPRate == 1.0 || !player)
            return;

        // no XP multiplier, when player is no bot.
        if (!player->GetSession()->IsBot() || !sRandomPlayerbotMgr.IsRandomBot(player))
            return;

        // no XP multiplier, when bot is in a group with a real player.
        if (Group* group = player->GetGroup())
        {
            for (GroupReference* gref = group->GetFirstMember(); gref; gref = gref->next())
            {
                Player* member = gref->GetSource();
                if (!member)
                    continue;

                if (!member->GetSession()->IsBot())
                    return;
            }
        }

        // otherwise apply bot XP multiplier.
        amount = static_cast<uint32>(std::round(static_cast<float>(amount) * sPlayerbotAIConfig.randomBotXPRate));
    }
};

class PlayerbotsUnitScript : public UnitScript
{
public:
    // Companion dismount log (AiPlayerbot.Companion.DismountLog) and the damage / aura record for the caught rule.
    PlayerbotsUnitScript() : UnitScript("PlayerbotsUnitScript", true,
        {UNITHOOK_ON_DAMAGE, UNITHOOK_ON_AURA_APPLY, UNITHOOK_ON_AURA_REMOVE}) {}

    void OnDamage(Unit* attacker, Unit* victim, uint32& /*damage*/) override
    {
        CompanionTrip::NoteDamage(attacker, victim);
    }

    void OnAuraApply(Unit* unit, Aura* aura) override { CompanionTrip::NoteAuraApplied(unit, aura); }

    void OnAuraRemove(Unit* unit, AuraApplication* aurApp, AuraRemoveMode mode) override
    {
        if (!unit || !aurApp || !unit->IsPlayer())
            return;
        Aura const* base = aurApp->GetBase();
        SpellInfo const* info = base ? base->GetSpellInfo() : nullptr;
        if (!info || !info->HasAura(SPELL_AURA_MOUNTED))
            return;
        CompanionTrip::NoteMountRemoved(unit, mode);
    }
};

class PlayerbotsMiscScript : public MiscScript
{
public:
    PlayerbotsMiscScript() : MiscScript("PlayerbotsMiscScript", {MISCHOOK_ON_DESTRUCT_PLAYER}) {}

    void OnDestructPlayer(Player* player) override
    {
        PlayerbotAI* botAI = PlayerbotsMgr::instance().GetPlayerbotAI(player);

        if (botAI != nullptr)
            delete botAI;

        if (PlayerbotMgr* playerbotMgr = GET_PLAYERBOT_MGR(player))
            delete playerbotMgr;
    }
};

class PlayerbotsServerScript : public ServerScript
{
public:
    PlayerbotsServerScript() : ServerScript("PlayerbotsServerScript", {
        SERVERHOOK_CAN_PACKET_RECEIVE
    }) {}

    void OnPacketReceived(WorldSession* session, WorldPacket const& packet) override
    {
        if (Player* player = session->GetPlayer())
            if (PlayerbotMgr* playerbotMgr = GET_PLAYERBOT_MGR(player))
                playerbotMgr->HandleMasterIncomingPacket(packet);
    }
};

class PlayerbotsWorldScript : public WorldScript
{
public:
    PlayerbotsWorldScript() : WorldScript("PlayerbotsWorldScript", {
        WORLDHOOK_ON_BEFORE_WORLD_INITIALIZED,
        WORLDHOOK_ON_UPDATE
    }) {}

    void OnBeforeWorldInitialized() override
    {
        // Before modifying the following messages, please make sure it does not violate the GNU GPLv2
        // license especially if you are distributing a repack or hosting a public server
        // e.g. you can replace the URL with your own repository,
        // but it should be publicly accessible and include all modifications you've made
        LOG_INFO("server.loading", "╔══════════════════════════════════════════════════════════╗");
        LOG_INFO("server.loading", "║                                                          ║");
        LOG_INFO("server.loading", "║              AzerothCore Playerbots Module               ║");
        LOG_INFO("server.loading", "║                                                          ║");
        LOG_INFO("server.loading", "╟──────────────────────────────────────────────────────────╢");
        LOG_INFO("server.loading", "║     mod-playerbots is a community-driven open-source     ║");
        LOG_INFO("server.loading", "║  project based on AzerothCore, licensed under GNU GPLv2  ║");
        LOG_INFO("server.loading", "╟──────────────────────────────────────────────────────────╢");
        LOG_INFO("server.loading", "║     https://github.com/mod-playerbots/mod-playerbots     ║");
        LOG_INFO("server.loading", "╚══════════════════════════════════════════════════════════╝");

        uint32 oldMSTime = getMSTime();

        LOG_INFO("server.loading", " ");
        LOG_INFO("server.loading", "Load Playerbots Config...");

        sPlayerbotAIConfig.Initialize();

        LOG_INFO("server.loading", ">> Loaded playerbots config in {} ms", GetMSTimeDiffToNow(oldMSTime));
        LOG_INFO("server.loading", " ");

        PlayerbotSpellRepository::Instance().Initialize();
        CheckMountStateAction::LoadPreferredMounts();

        LOG_INFO("server.loading", "Playerbots World Thread Processor initialized");
    }

    void OnUpdate(uint32 diff) override
    {
        PlayerbotWorldThreadProcessor::instance().Update(diff);
        sCompanionBotMgr.Update(diff);       // World thread only
        sRandomPlayerbotMgr.UpdateAI(diff);  // World thread only
    }
};

class PlayerbotsScript : public PlayerbotScript
{
public:
    PlayerbotsScript() : PlayerbotScript("PlayerbotsScript") {}

    bool OnPlayerbotCheckLFGQueue(lfg::Lfg5Guids const& guidsList) override
    {
        bool nonBotFound = false;
        bool hasTrackedCoopBot = false;
        bool hasSoloCompanion = false;
        std::vector<uint32> botLowGuids;
        botLowGuids.reserve(5);

        for (ObjectGuid const& guid : guidsList.guids)
        {
            if (guid.IsEmpty())
                continue;
            Player* player = ObjectAccessor::FindPlayer(guid);

            if (guid.IsGroup() || IsRealPlayer(player) || IsSelfBot(player))
            {
                nonBotFound = true;
            }
            else if (guid.IsPlayer())
            {
                uint32 const low = guid.GetCounter();
                botLowGuids.push_back(low);
                if (CompanionBrain::IsCoopLfgTrackedBot(low))
                    hasTrackedCoopBot = true;
                if (player && sCompanionBotMgr.IsCompanion(player) && !player->GetGroup())
                    hasSoloCompanion = true;
            }
        }

        // Issue #20 (P1-6 + P1 defence-in-depth): NEVER mix a tracked coop bot OR any solo guild companion
        // into a human/group LFG combo (prevents split rosters and repeated failed pops for real humans!).
        if (hasTrackedCoopBot || (hasSoloCompanion && nonBotFound))
        {
            if (nonBotFound)
                return false;
            return CompanionBrain::AllowBotOnlyLfgProposal(botLowGuids);
        }

        if (nonBotFound)
            return true;

        // Allow native bot-only 5-player WotLK RDF matches ONLY for an exact 5-member confirmed cooperative
        // companion roster under CoopEnabled() == true & Issue #12 admission limits (random bots stay blocked!).
        return CompanionBrain::AllowBotOnlyLfgProposal(botLowGuids);
    }

    void OnPlayerbotCheckKillTask(Player* player, Unit* victim) override
    {
        if (player)
            GuildTaskMgr::instance().CheckKillTask(player, victim);
    }

    void OnPlayerbotCheckPetitionAccount(Player* player, bool& found) override
    {
        if (!found)
            return;

        if (PlayerbotsMgr::instance().GetPlayerbotAI(player) != nullptr)
            found = false;
    }

    bool OnPlayerbotCheckUpdatesToSend(Player* player) override
    {
        PlayerbotAI* botAI = PlayerbotsMgr::instance().GetPlayerbotAI(player);

        if (botAI == nullptr)
            return true;

        return IsSelfBot(player);
    }

    void OnPlayerbotPacketSent(Player* player, WorldPacket const* packet) override
    {
        if (player == nullptr)
            return;

        PlayerbotAI* botAI = PlayerbotsMgr::instance().GetPlayerbotAI(player);

        if (botAI != nullptr)
            botAI->HandleBotOutgoingPacket(*packet);

        if (PlayerbotMgr* playerbotMgr = GET_PLAYERBOT_MGR(player))
            playerbotMgr->HandleMasterOutgoingPacket(*packet);
    }

    void OnPlayerbotUpdate(uint32 /*diff*/) override
    {
        sRandomPlayerbotMgr.UpdateSessions();  // Per-bot updates only
    }

    void OnPlayerbotUpdateSessions(Player* player) override
    {
        if (player)
            if (PlayerbotMgr* playerbotMgr = GET_PLAYERBOT_MGR(player))
                playerbotMgr->UpdateSessions();
    }

    void OnPlayerbotLogout(Player* player) override
    {
        if (PlayerbotMgr* playerbotMgr = GET_PLAYERBOT_MGR(player))
        {
            PlayerbotAI* botAI = PlayerbotsMgr::instance().GetPlayerbotAI(player);

            if (botAI == nullptr || IsSelfBot(player))
                playerbotMgr->LogoutAllBots();
        }

        sRandomPlayerbotMgr.OnPlayerLogout(player);
    }

    void OnPlayerbotLogoutBots() override
    {
        LOG_INFO("playerbots", "Logging out all bots...");

        // Server shutdown (WorldSessionMgr::KickAll). LogoutAllBots skips companions (they stay online with no
        // master), and bot sessions have no socket, so nothing else removes them from their maps before
        // MapMgr::UnloadAll; an instance map with a companion inside then fails ASSERT(!HavePlayers()).
        // AiPlayerbot.Companion.ShutdownLogout (default 1) logs them out (saved) here.
        // Log line: "Companion shutdown: logged out <n> bots (in instances: <k>)", plus one
        // "Companion <Name>: shutdown logout map=<id> instance=<id> instanced=<0|1>" per companion.
        if (!sCompanionBotMgr.CompanionNames().empty() &&
            sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.ShutdownLogout", true))
        {
            uint32 inInstance = 0;
            uint32 count = sRandomPlayerbotMgr.LogoutCompanionBots(inInstance);
            // A companion could also sit in a real player's bot list; those lists only drop non-companions.
            for (auto const& [accountId, session] : sWorldSessionMgr->GetAllSessions())
            {
                Player* player = session ? session->GetPlayer() : nullptr;
                if (!player)
                    continue;
                if (PlayerbotMgr* playerbotMgr = GET_PLAYERBOT_MGR(player))
                    count += playerbotMgr->LogoutCompanionBots(inInstance);
            }
            LOG_INFO("playerbots", "Companion shutdown: logged out {} bots (in instances: {})", count, inInstance);
        }

        sRandomPlayerbotMgr.LogoutAllBots();
    }
};

class PlayerBotsBGScript : public BGScript
{
public:
    PlayerBotsBGScript() : BGScript("PlayerBotsBGScript") {}

    void OnBattlegroundStart(Battleground* bg) override
    {
        BGStrategyData data;

        switch (bg->GetBgTypeID())
        {
            case BATTLEGROUND_WS:
                data.allianceStrategy = urand(0, WS_STRATEGY_MAX - 1);
                data.hordeStrategy = urand(0, WS_STRATEGY_MAX - 1);
                break;
            case BATTLEGROUND_AB:
                data.allianceStrategy = urand(0, AB_STRATEGY_MAX - 1);
                data.hordeStrategy = urand(0, AB_STRATEGY_MAX - 1);
                break;
            case BATTLEGROUND_AV:
                data.allianceStrategy = urand(0, AV_STRATEGY_MAX - 1);
                data.hordeStrategy = urand(0, AV_STRATEGY_MAX - 1);
                break;
            case BATTLEGROUND_EY:
                data.allianceStrategy = urand(0, EY_STRATEGY_MAX - 1);
                data.hordeStrategy = urand(0, EY_STRATEGY_MAX - 1);
                break;
            default:
                break;
        }

        bgStrategies[bg->GetInstanceID()] = data;
    }

    void OnBattlegroundEnd(Battleground* bg, TeamId /*winnerTeam*/) override { bgStrategies.erase(bg->GetInstanceID()); }
};

// Workaround for missing InitEnabledHooksIfNeeded for new BattlefieldScript in ScriptMgr
class PlayerbotsBattlefieldScript : public BattlefieldScript
{
public:
    PlayerbotsBattlefieldScript() : BattlefieldScript("PlayerbotsBattlefieldScript") { }
};

void AddPlayerbotsSecureLoginScripts();
void AddPlayerbotsSelfBotAfkScripts();

void AddSC_MagtheridonBotScripts();
void AddSC_TempestKeepBotScripts();
void AddSC_HyjalBotScripts();
void AddSC_IcecrownBotScripts();
void AddSC_RubySanctumBotScripts();
void AddSC_randombot_level_mgr();

void AddPlayerbotsScripts()
{
    new PlayerbotsBattlefieldScript();
    new PlayerbotsDatabaseScript();
    new PlayerbotsPlayerScript();
    new PlayerbotsUnitScript();
    new PlayerbotsMiscScript();
    new PlayerbotsServerScript();
    new PlayerbotsWorldScript();
    new PlayerbotsScript();
    new PlayerBotsBGScript();
    AddPlayerbotsSecureLoginScripts();
    AddPlayerbotsSelfBotAfkScripts();
    AddPlayerbotsCommandscripts();
    PlayerBotsGuildValidationScript();
    AddSC_MagtheridonBotScripts();
    AddSC_TempestKeepBotScripts();
    AddSC_HyjalBotScripts();
    AddSC_IcecrownBotScripts();
    AddSC_RubySanctumBotScripts();
    AddSC_randombot_level_mgr();
}
