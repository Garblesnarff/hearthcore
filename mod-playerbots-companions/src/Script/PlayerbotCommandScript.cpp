/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#include "AccountMgr.h"
#include "BattleGroundTactics.h"
#include "Chat.h"
#include "Bag.h"
#include "CharacterCache.h"
#include "CompanionBotMgr.h"
#include "CompanionBrain.h"
#include "CompanionCraft.h"
#include "CompanionDuty.h"
#include "CompanionLease.h"
#include "CompanionLife.h"
#include "CompanionQuest.h"
#include "CompanionTransport.h"
#include "DatabaseEnv.h"
#include "GuildBankAction.h"
#include "GuildMgr.h"
#include "MapMgr.h"
#include "DBCStores.h"
#include "GameTime.h"
#include "GuildTaskMgr.h"
#include "Item.h"
#include "Log.h"
#include "ObjectAccessor.h"
#include "ObjectMgr.h"
#include "WorldSession.h"
#include "PerfMonitor.h"
#include "Playerbots.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "PlayerbotMgr.h"
#include "NewRpgInfo.h"
#include "RandomPlayerbotMgr.h"
#include "ScriptMgr.h"
#include <algorithm>
#include <cctype>
#include <sstream>

using namespace Acore::ChatCommands;

class playerbots_commandscript : public CommandScript
{
public:
    playerbots_commandscript() : CommandScript("playerbots_commandscript") {}

    ChatCommandTable GetCommands() const override
    {
        static ChatCommandTable playerbotsDebugCommandTable = {
            {"bg", HandleDebugBGCommand, SEC_GAMEMASTER, Console::Yes},
        };

        static ChatCommandTable playerbotsAccountCommandTable = {
            {"setKey", HandleSetSecurityKeyCommand, SEC_PLAYER, Console::No},
            {"link", HandleLinkAccountCommand, SEC_PLAYER, Console::No},
            {"linkedAccounts", HandleViewLinkedAccountsCommand, SEC_PLAYER, Console::No},
            {"unlink", HandleUnlinkAccountCommand, SEC_PLAYER, Console::No},
        };

        static ChatCommandTable playerbotsCommandTable = {
            {"bot", HandlePlayerbotCommand, SEC_PLAYER, Console::No},
            {"gtask", HandleGuildTaskCommand, SEC_GAMEMASTER, Console::Yes},
            {"pmon", HandlePerfMonCommand, SEC_GAMEMASTER, Console::Yes},
            {"rndbot", HandleRandomPlayerbotCommand, SEC_GAMEMASTER, Console::Yes},
            {"debug", playerbotsDebugCommandTable},
            {"account", playerbotsAccountCommandTable},
        };

        static ChatCommandTable companionCommandTable = {
            {"status", HandleCompanionStatusCommand, SEC_PLAYER, Console::Yes},
            {"repair", HandleCompanionRepairCommand, SEC_GAMEMASTER, Console::Yes},
            {"why", HandleCompanionWhyCommand, SEC_GAMEMASTER, Console::Yes},
            {"wear", HandleCompanionWearCommand, SEC_GAMEMASTER, Console::Yes},
            {"lead", HandleCompanionLeadCommand, SEC_GAMEMASTER, Console::Yes},
            {"craft", HandleCompanionCraftCommand, SEC_GAMEMASTER, Console::Yes},
            {"abandon", HandleCompanionAbandonCommand, SEC_GAMEMASTER, Console::Yes},
            {"skills", HandleCompanionSkillsCommand, SEC_GAMEMASTER, Console::Yes},
            {"travel", HandleCompanionTravelCommand, SEC_GAMEMASTER, Console::Yes},
            {"hearth", HandleCompanionHearthCommand, SEC_GAMEMASTER, Console::Yes},
            {"bank", HandleCompanionBankCommand, SEC_GAMEMASTER, Console::Yes},
            {"create", HandleCompanionCreateCommand, SEC_GAMEMASTER, Console::Yes},
            {"schedule", HandleCompanionScheduleCommand, SEC_GAMEMASTER, Console::Yes},
            {"life", HandleCompanionLifeCommand, SEC_GAMEMASTER, Console::Yes},
        };

        static ChatCommandTable commandTable = {
            {"playerbots", playerbotsCommandTable},
            {"companion", companionCommandTable},
        };

        return commandTable;
    }

    static char const* NewRpgStatusName(NewRpgStatus status)
    {
        switch (status)
        {
            case RPG_IDLE: return "idle";
            case RPG_REST: return "rest";
            case RPG_WANDER_RANDOM: return "wander random";
            case RPG_WANDER_NPC: return "wander npc";
            case RPG_GO_GRIND: return "go grind";
            case RPG_GO_CAMP: return "go camp";
            case RPG_DO_QUEST: return "do quest";
            case RPG_TRAVEL_FLIGHT: return "travel flight";
            case RPG_OUTDOOR_PVP: return "outdoor pvp";
            default: return "unknown";
        }
    }

    static std::string QuestEventText(CompanionBotMgr::QuestEvent const& ev, uint32 nowSec)
    {
        if (!ev.atSec)
            return "none since start";
        std::ostringstream out;
        out << "quest " << ev.questId << ", " << (nowSec - ev.atSec) << "s ago";
        return out.str();
    }

    static bool HandleCompanionRepairCommand(ChatHandler* handler, char const* /*args*/)
    {
        for (std::string const& name : sCompanionBotMgr.CompanionNames())
        {
            Player* p = ObjectAccessor::FindPlayerByName(name);
            if (!p)
            {
                handler->PSendSysMessage("{}: offline, skipped", name);
                continue;
            }
            if (p->IsInCombat())
            {
                handler->PSendSysMessage("{}: in combat, skipped", name);
                continue;
            }
            std::vector<std::string> report;
            sCompanionBotMgr.RepairHeirlooms(p, report);
            for (std::string const& line : report)
                handler->PSendSysMessage("{}", line);
        }
        return true;
    }

    // .companion wear <name> <pct>: sets every equipped item's durability to pct% of its maximum (1..100, GM test
    // tool). Changes the durability field only; auras and item mods are not reapplied.
    // .companion lead <on|off> [name]: sets lead mode for one companion, or all when no name is given. The lead and
    // assist strategies follow at the companion's next sync tick (TickBot -> ApplyLeadStrategies).
    static bool HandleCompanionLeadCommand(ChatHandler* handler, char const* args)
    {
        std::istringstream in(args ? args : "");
        std::string mode;
        std::string name;
        in >> mode >> name;
        std::transform(mode.begin(), mode.end(), mode.begin(), [](unsigned char c) { return std::tolower(c); });
        if (mode != "on" && mode != "off")
        {
            handler->PSendSysMessage("Usage: .companion lead <on|off> [name]");
            return true;
        }
        bool const on = mode == "on";

        std::vector<std::string> names;
        if (name.empty())
            names = sCompanionBotMgr.CompanionNames();
        else if (sCompanionBotMgr.IsCompanionName(name))
            names.push_back(name);
        else
        {
            handler->PSendSysMessage("{} is not a configured companion", name);
            return true;
        }

        for (std::string const& companion : names)
        {
            ObjectGuid guid = sCharacterCache->GetCharacterGuidByName(companion);
            if (!guid)
            {
                handler->PSendSysMessage("{}: character not found", companion);
                continue;
            }
            sCompanionBotMgr.SetLeadMode(guid.GetCounter(), on);
            handler->PSendSysMessage("{}: lead mode {}", companion, on ? "on" : "off");
            LOG_INFO("playerbots", "[Companion] {} lead mode {} by GM command", companion, on ? "on" : "off");
        }
        return true;
    }

    static bool HandleCompanionWearCommand(ChatHandler* handler, char const* args)
    {
        std::istringstream in(args ? args : "");
        std::string name;
        uint32 pct = 0;
        if (!(in >> name >> pct))
        {
            handler->PSendSysMessage("Usage: .companion wear <name> <pct>   (pct 1-100 of max durability)");
            return true;
        }
        if (pct < 1 || pct > 100)
        {
            handler->PSendSysMessage("Usage: .companion wear <name> <pct>   (pct must be 1-100)");
            return true;
        }
        Player* p = ObjectAccessor::FindPlayerByName(name);
        if (!p || !sCompanionBotMgr.IsCompanion(p))
        {
            handler->PSendSysMessage("{} is not an online configured companion", name);
            return true;
        }

        uint32 changed = 0;
        for (uint8 slot = EQUIPMENT_SLOT_START; slot < EQUIPMENT_SLOT_END; ++slot)
        {
            Item* item = p->GetItemByPos(INVENTORY_SLOT_BAG_0, slot);
            if (!item)
                continue;
            uint32 maxDur = item->GetUInt32Value(ITEM_FIELD_MAXDURABILITY);
            if (!maxDur)
                continue;
            uint32 newDur = std::max<uint32>(1, maxDur * pct / 100);
            item->SetUInt32Value(ITEM_FIELD_DURABILITY, newDur);
            item->SetState(ITEM_CHANGED, p);
            ++changed;
        }
        handler->PSendSysMessage("{}: {} equipped item(s) set to {}% durability", p->GetName(), changed, pct);
        LOG_INFO("playerbots", "[Companion] {} durability set to {}% by GM command", p->GetName(), pct);
        return true;
    }

    // Phase 2. .companion craft <name>: one craft evaluation now (recipe, skill-up colour, materials and their source,
    // station, score); the companion's next duty evaluation runs at once. .companion skills <name>: profession values.
    // Both read an online companion on the world thread, while the map threads are idle (CLI commands).
    static Player* OnlineCompanion(ChatHandler* handler, char const* args, char const* usage)
    {
        std::istringstream in(args ? args : "");
        std::string name;
        in >> name;
        if (name.empty())
        {
            handler->PSendSysMessage("{}", usage);
            return nullptr;
        }
        Player* p = ObjectAccessor::FindPlayerByName(name);
        if (!p || !p->IsInWorld() || !sCompanionBotMgr.IsCompanion(p))
        {
            handler->PSendSysMessage("{} is not an online configured companion", name);
            return nullptr;
        }
        return p;
    }

    // B9 / B13 proofs. .companion travel docks: the dock, tram, portal and Dalaran portal table.
    // .companion travel <name> [status]: the companion's legs. .companion travel <name> quest <id> | work <id> |
    // vendor <entry> | <map> <x> <y> <z>: a forced leased trip (turn-in at the quest's ender, the nearest open
    // objective area of an incomplete quest on any companion map, mount purchase at that vendor, or a point), taken
    // at the companion's next quest evaluation on its map thread, routed by boat / tram / portal / hearth when needed.
    // Autonomy only (not grouped with the owner, not in an instance).
    static bool HandleCompanionTravelCommand(ChatHandler* handler, char const* args)
    {
        char const* usage = "Usage: .companion travel docks | <name> [status] | <name> quest <id> | <name> work <id> | "
                            "<name> vendor <creatureEntry> | <name> <map> <x> <y> <z>";
        std::istringstream in(args ? args : "");
        std::string name;
        std::string what;
        in >> name >> what;
        if (name.empty())
        {
            handler->PSendSysMessage("{}", usage);
            return true;
        }
        if (name == "docks")
        {
            for (std::string const& line : CompanionTransport::TableLines())
                handler->PSendSysMessage("{}", line);
            return true;
        }
        Player* p = ObjectAccessor::FindPlayerByName(name);
        if (!p || !p->IsInWorld() || !sCompanionBotMgr.IsCompanion(p))
        {
            handler->PSendSysMessage("{} is not an online configured companion", name);
            return true;
        }
        uint32 const low = p->GetGUID().GetCounter();
        if (what.empty() || what == "status")
        {
            for (std::string const& line : CompanionTransport::WhyLines(low))
                handler->PSendSysMessage("{}", line);
            return true;
        }
        if (!CompanionTransport::Enabled())
            handler->PSendSysMessage("Note: AiPlayerbot.Companion.Transport.Enabled = 0 (same-landmass trips only)");
        if (sCompanionBotMgr.IsGroupedWithOwner(p) || !p->GetMap() || p->GetMap()->Instanceable())
        {
            handler->PSendSysMessage("{} is grouped with the owner or in an instance: no autonomous trip", name);
            return true;
        }
        CompanionTransport::ForceRequest req;
        if (what == "quest" || what == "work" || what == "vendor")
        {
            req.kind = what == "quest"  ? CompanionTransport::ForceRequest::Kind::Quest
                       : what == "work" ? CompanionTransport::ForceRequest::Kind::Work
                                        : CompanionTransport::ForceRequest::Kind::Vendor;
            in >> req.id;
            if (!req.id)
            {
                handler->PSendSysMessage("{}", usage);
                return true;
            }
        }
        else
        {
            req.kind = CompanionTransport::ForceRequest::Kind::Point;
            std::istringstream mapIn(what);
            if (!(mapIn >> req.mapId) || !(in >> req.x >> req.y >> req.z))
            {
                handler->PSendSysMessage("{}", usage);
                return true;
            }
            // World thread (maps idle during commands): the target's zone for its landmass.
            if (Map* map = sMapMgr->FindBaseMap(req.mapId))
                req.zone = map->GetZoneId(PHASEMASK_NORMAL, req.x, req.y, req.z);
        }
        CompanionTransport::RequestForce(low, req);
        LOG_INFO("playerbots", "Companion {}: travel queued by GM kind={} id={} map={} x={} y={} zone={}", name, what,
                 req.id, req.mapId, req.x, req.y, req.zone);
        handler->PSendSysMessage("{}: forced trip queued ({}), taken at the next quest evaluation", name, what);
        return true;
    }

    // D route hearth test forms. .companion hearth <name> [status]: the cooldown and the forced need.
    // .companion hearth <name> need <craft|inn|return|none> [minutes]: forces that need for the companion's hearth
    // decisions (default 30 minutes): craft = a craft session planned in the home city, inn = a bind duty, return =
    // a return need (the destination far from the home bind). The rules: CompanionTransport::HearthNeed.
    static bool HandleCompanionHearthCommand(ChatHandler* handler, char const* args)
    {
        char const* usage = "Usage: .companion hearth <name> [status] | <name> need <craft|inn|return|none> [minutes]";
        std::istringstream in(args ? args : "");
        std::string name;
        std::string what;
        std::string need;
        in >> name >> what;
        if (name.empty())
        {
            handler->PSendSysMessage("{}", usage);
            return true;
        }
        Player* p = ObjectAccessor::FindPlayerByName(name);
        if (!p || !p->IsInWorld() || !sCompanionBotMgr.IsCompanion(p))
        {
            handler->PSendSysMessage("{} is not an online configured companion", name);
            return true;
        }
        uint32 const low = p->GetGUID().GetCounter();
        if (what.empty() || what == "status")
        {
            for (std::string const& line : CompanionTransport::WhyLines(low))
                handler->PSendSysMessage("{}", line);
            return true;
        }
        in >> need;
        uint32 minutes = 30;
        if (!(in >> minutes) || !minutes)
            minutes = 30;
        if (what != "need" || (need != "craft" && need != "inn" && need != "return" && need != "none"))
        {
            handler->PSendSysMessage("{}", usage);
            return true;
        }
        CompanionTransport::SetTestNeed(low, need, minutes);
        LOG_INFO("playerbots", "Companion {}: hearth test need={} minutes={} set by GM", name, need, minutes);
        handler->PSendSysMessage("{}: hearth test need {} for {} min (cleared with need none)", name, need, minutes);
        return true;
    }

    // .companion craft <name> mill | prospect | session [city]: a GM test hook. The next craft evaluation plans that
    // duty (a session in the named city, or anywhere), and the duty runs its normal path (lease, travel, withdraw,
    // craft, deposit). Pending for 10 minutes.
    static bool HandleCompanionCraftCommand(ChatHandler* handler, char const* args)
    {
        std::istringstream in(args ? args : "");
        std::string name;
        std::string action;
        std::string city;
        in >> name >> action;
        std::getline(in, city);
        size_t const first = city.find_first_not_of(' ');
        city = first == std::string::npos ? std::string() : city.substr(first);
        char const* usage = "Usage: .companion craft <name> [mill | prospect | session [city]]";
        Player* p = OnlineCompanion(handler, name.c_str(), usage);
        if (!p)
            return true;
        if (!action.empty())
        {
            CompanionCraft::Force kind;
            if (action == "mill")
                kind = CompanionCraft::Force::Mill;
            else if (action == "prospect")
                kind = CompanionCraft::Force::Prospect;
            else if (action == "session")
                kind = CompanionCraft::Force::Session;
            else
            {
                handler->PSendSysMessage("{}", usage);
                return true;
            }
            uint32 const low = p->GetGUID().GetCounter();
            CompanionCraft::RequestForce(low, kind, city);
            handler->PSendSysMessage("{}: craft forced to {}{} (taken at the next craft evaluation, pending 10 min)",
                                     p->GetName(), action, city.empty() ? std::string() : " in " + city);
            CompanionDuty::EvaluateSoon(low);
            return true;
        }
        for (std::string const& line : CompanionCraft::CraftLines(p))
            handler->PSendSysMessage("{}", line);
        CompanionDuty::EvaluateSoon(p->GetGUID().GetCounter());
        LOG_INFO("playerbots", "[Companion] {} craft evaluation forced by GM command", p->GetName());
        return true;
    }

    // .companion abandon <name> <questId>: the companion's abandon path for one quest (CompanionQuest, behind the B2
    // owner-shared guard). Run at its next quest evaluation: refused and logged when the owner has the quest too.
    static bool HandleCompanionAbandonCommand(ChatHandler* handler, char const* args)
    {
        std::istringstream in(args ? args : "");
        std::string name;
        uint32 questId = 0;
        in >> name >> questId;
        char const* usage = "Usage: .companion abandon <name> <questId>";
        if (name.empty() || !questId)
        {
            handler->PSendSysMessage("{}", usage);
            return true;
        }
        Player* p = OnlineCompanion(handler, name.c_str(), usage);
        if (!p)
            return true;
        CompanionQuest::RequestAbandon(p->GetGUID().GetCounter(), questId);
        handler->PSendSysMessage("{}: abandon of quest {} queued (taken at the next quest evaluation)", p->GetName(),
                                 questId);
        return true;
    }

    static bool HandleCompanionSkillsCommand(ChatHandler* handler, char const* args)
    {
        Player* p = OnlineCompanion(handler, args, "Usage: .companion skills <name>");
        if (!p)
            return true;
        for (std::string const& line : CompanionCraft::SkillLines(p))
            handler->PSendSysMessage("{}", line);
        return true;
    }

    // .companion why <name> [activity]: without an activity, the duty scores, last choices and the lease. With one
    // ("bank", "not bank", ...): its blocking condition, last attempt, failures and retry time.
    static bool HandleCompanionWhyCommand(ChatHandler* handler, char const* args)
    {
        std::istringstream in(args ? args : "");
        std::string name;
        in >> name;
        std::string activity;
        std::getline(in, activity);
        if (name.empty())
        {
            handler->PSendSysMessage("Usage: .companion why <name> [activity]");
            return true;
        }
        if (!sCompanionBotMgr.IsCompanionName(name))
        {
            handler->PSendSysMessage("{} is not a configured companion", name);
            return true;
        }
        ObjectGuid guid = sCharacterCache->GetCharacterGuidByName(name);
        if (!guid)
        {
            handler->PSendSysMessage("{}: character not found", name);
            return true;
        }
        bool const hasActivity = activity.find_first_not_of(' ') != std::string::npos;
        std::vector<std::string> lines = hasActivity ? CompanionLease::WhyNot(guid.GetCounter(), activity)
                                                     : CompanionDuty::WhyLines(guid.GetCounter());
        // B11: the quest project lines (current pick, top options with scores, complete quests, blocked reasons)
        // follow in both forms.
        std::vector<std::string> const quest = CompanionQuest::WhyLines(guid.GetCounter());
        lines.insert(lines.end(), quest.begin(), quest.end());
        for (std::string const& line : lines)
            handler->PSendSysMessage("{}", line);
        return true;
    }

    static bool HandleCompanionStatusCommand(ChatHandler* handler, char const* /*args*/)
    {
        uint32 nowSec = static_cast<uint32>(GameTime::GetGameTime().count());
        uint32 ownerLevel = sCompanionBotMgr.GetOwnerLevel();
        uint32 lead = sPlayerbotAIConfig.companionLevelLead;
        handler->PSendSysMessage("Companions: owner level {}, cap {} (lead {}), {} configured",
                                 ownerLevel, ownerLevel + lead, lead, sCompanionBotMgr.CompanionNames().size());

        for (std::string const& name : sCompanionBotMgr.CompanionNames())
        {
            Player* p = ObjectAccessor::FindPlayerByName(name);
            if (!p)
            {
                handler->PSendSysMessage("{}: offline", name);
                continue;
            }

            uint32 low = p->GetGUID().GetCounter();
            AreaTableEntry const* area = sAreaTableStore.LookupEntry(p->GetZoneId());
            std::string zone = area ? PlayerbotAI::GetLocalizedAreaName(area) : "unknown";

            uint32 quests = 0;
            for (uint16 slot = 0; slot < MAX_QUEST_LOG_SIZE; ++slot)
                if (p->GetQuestSlotQuestId(slot))
                    ++quests;

            std::string strategies = "no AI";
            std::string rpg = "no AI";
            if (PlayerbotAI* botAI = GET_PLAYERBOT_AI(p))
            {
                std::ostringstream list;
                for (std::string const& s : botAI->GetStrategies(BOT_STATE_NON_COMBAT))
                    list << (list.tellp() > 0 ? "," : "") << s;
                strategies = list.str();
                rpg = NewRpgStatusName(botAI->rpgInfo.GetStatus());
            }

            uint32 freeSlots = 0;
            for (uint8 slot = INVENTORY_SLOT_ITEM_START; slot < INVENTORY_SLOT_ITEM_END; ++slot)
                if (!p->GetItemByPos(INVENTORY_SLOT_BAG_0, slot))
                    ++freeSlots;
            for (uint8 bagSlot = INVENTORY_SLOT_BAG_START; bagSlot < INVENTORY_SLOT_BAG_END; ++bagSlot)
            {
                Bag* bag = (Bag*)p->GetItemByPos(INVENTORY_SLOT_BAG_0, bagSlot);
                if (bag)
                    for (uint32 i = 0; i < bag->GetBagSize(); ++i)
                        if (!bag->GetItemByPos(i))
                            ++freeSlots;
            }

            handler->PSendSysMessage("{}: level {}, cap {} ({}), zone {}, group {}, quests {}, free bag slots {}",
                                     name, p->GetLevel(), ownerLevel + lead,
                                     sCompanionBotMgr.OwnerLevelUnknown() ? "closed: owner level unknown"
                                     : (sCompanionBotMgr.IsXpCapped(p) ? "reached" : "open"),
                                     zone,
                                     p->GetGroup() ? "yes" : "no", quests, freeSlots);
            handler->PSendSysMessage("  lead mode: {}, grouped with owner: {}",
                                     sCompanionBotMgr.LeadModeOn(low) ? "on" : "off",
                                     sCompanionBotMgr.IsGroupedWithOwner(p) ? "yes" : "no");
            handler->PSendSysMessage("  non-combat strategies: {}", strategies);
            handler->PSendSysMessage("  new rpg state: {}", rpg);
            handler->PSendSysMessage("  last accept: {}",
                                     QuestEventText(sCompanionBotMgr.LastQuestAccept(low), nowSec));
            handler->PSendSysMessage("  last turn-in: {}",
                                     QuestEventText(sCompanionBotMgr.LastQuestTurnIn(low), nowSec));
        }
        return true;
    }

    static bool HandlePlayerbotCommand(ChatHandler* handler, char const* args)
    {
        return PlayerbotMgr::HandlePlayerbotMgrCommand(handler, args);
    }

    static bool HandleRandomPlayerbotCommand(ChatHandler* handler, char const* args)
    {
        return RandomPlayerbotMgr::HandlePlayerbotConsoleCommand(handler, args);
    }

    static bool HandleGuildTaskCommand(ChatHandler* handler, char const* args)
    {
        return GuildTaskMgr::HandleConsoleCommand(handler, args);
    }

    static bool HandlePerfMonCommand(ChatHandler* /*handler*/, char const* args)
    {
        if (!strcmp(args, "reset"))
        {
            sPerfMonitor.Reset();
            return true;
        }

        if (!strcmp(args, "tick"))
        {
            sPerfMonitor.PrintStats(true, false);
            sPerfMonitor.DumpJson(true);
            return true;
        }

        if (!strcmp(args, "stack"))
        {
            sPerfMonitor.PrintStats(false, true);
            sPerfMonitor.DumpJson(false);
            return true;
        }

        if (!strcmp(args, "toggle"))
        {
            sPlayerbotAIConfig.perfMonEnabled = !sPlayerbotAIConfig.perfMonEnabled;
            if (sPlayerbotAIConfig.perfMonEnabled)
                LOG_INFO("playerbots", "Performance monitor enabled");
            else
                LOG_INFO("playerbots", "Performance monitor disabled");
            return true;
        }

        sPerfMonitor.PrintStats();
        sPerfMonitor.DumpJson(false);
        return true;
    }

    static bool HandleDebugBGCommand(ChatHandler* handler, char const* args)
    {
        return BGTactics::HandleConsoleCommand(handler, args);
    }

    static bool HandleSetSecurityKeyCommand(ChatHandler* handler, char const* args)
    {
        if (!args || !*args)
        {
            handler->PSendSysMessage("Usage: .playerbots account setKey <securityKey>");
            return false;
        }

        Player* player = handler->GetSession()->GetPlayer();
        std::string key = args;

        PlayerbotMgr* mgr = PlayerbotsMgr::instance().GetPlayerbotMgr(player);
        if (mgr)
        {
            mgr->HandleSetSecurityKeyCommand(player, key);
            return true;
        }
        else
        {
            handler->PSendSysMessage("PlayerbotMgr instance not found.");
            return false;
        }
    }

    static bool HandleCompanionBankCommand(ChatHandler* handler, char const* args)
    {
        std::istringstream iss(args ? args : "");
        std::string sub;
        iss >> sub;
        std::transform(sub.begin(), sub.end(), sub.begin(), [](unsigned char c) { return std::tolower(c); });

        if (sub == "organize")
        {
            std::string botName;
            iss >> botName;
            Player* target = nullptr;
            if (!botName.empty())
                target = ObjectAccessor::FindPlayerByName(botName);
            else
            {
                for (std::string const& name : sCompanionBotMgr.CompanionNames())
                {
                    if (Player* p = ObjectAccessor::FindPlayerByName(name))
                    {
                        target = p;
                        break;
                    }
                }
            }
            PlayerbotAI* botAI = target ? GET_PLAYERBOT_AI(target) : nullptr;
            if (!target || !botAI)
            {
                handler->PSendSysMessage("Usage: .companion bank organize [companionName] (companion must be online)");
                return true;
            }
            uint32 purged = 0, merged = 0, sorted = 0;
            GuildBankAction(botAI).OrganizeCompanionBank(true, &purged, &merged, &sorted);
            handler->PSendSysMessage("{}: bank organize done (purged={}, merged={}, sorted={})", target->GetName(),
                                     purged, merged, sorted);
            return true;
        }

        if (sub == "request")
        {
            std::string op;
            iss >> op;
            std::transform(op.begin(), op.end(), op.begin(), [](unsigned char c) { return std::tolower(c); });
            if (op.empty() || op == "list")
            {
                auto const list = CompanionBrain::ListBankRequests();
                if (list.empty())
                {
                    handler->PSendSysMessage("No custom bank mat requests.");
                    return true;
                }
                for (auto const& r : list)
                {
                    ItemTemplate const* proto = sObjectMgr->GetItemTemplate(r.itemEntry);
                    handler->PSendSysMessage("  item={} ({}) min={} by={} note={}", r.itemEntry,
                                             proto ? proto->Name1 : "unknown", r.minCount, r.requestedBy, r.note);
                }
                return true;
            }
            if (op == "add")
            {
                uint32 itemId = 0;
                uint32 minCount = 20;
                iss >> itemId;
                if (!itemId)
                {
                    handler->PSendSysMessage("Usage: .companion bank request add <itemId> [minCount] [note]");
                    return true;
                }
                if (!(iss >> minCount))
                    minCount = 20;
                std::string note;
                std::getline(iss, note);
                size_t first = note.find_first_not_of(" \t");
                note = (first == std::string::npos) ? "" : note.substr(first);
                if (note.size() > 120)
                    note.resize(120);
                std::string by = handler->GetSession() && handler->GetSession()->GetPlayer()
                                     ? handler->GetSession()->GetPlayer()->GetName()
                                     : "console";
                CharacterDatabase.EscapeString(by);
                CharacterDatabase.EscapeString(note);
                CharacterDatabase.Execute(
                    "INSERT INTO companion_bank_requests (item_entry, requested_by, min_count, note) "
                    "VALUES ({}, '{}', {}, '{}') "
                    "ON DUPLICATE KEY UPDATE requested_by=VALUES(requested_by), min_count=VALUES(min_count), note=VALUES(note)",
                    itemId, by, minCount, note);
                CompanionBrain::ReloadBankRequests();
                ItemTemplate const* proto = sObjectMgr->GetItemTemplate(itemId);
                handler->PSendSysMessage("Added bank request: item={} ({}) min={}", itemId,
                                         proto ? proto->Name1 : "unknown", minCount);
                return true;
            }
            if (op == "del" || op == "remove")
            {
                uint32 itemId = 0;
                iss >> itemId;
                if (!itemId)
                {
                    handler->PSendSysMessage("Usage: .companion bank request del <itemId>");
                    return true;
                }
                CharacterDatabase.Execute("DELETE FROM companion_bank_requests WHERE item_entry = {}", itemId);
                CompanionBrain::ReloadBankRequests();
                handler->PSendSysMessage("Removed bank request for item={}", itemId);
                return true;
            }
        }

        handler->PSendSysMessage(
            "Usage: .companion bank organize [name] | .companion bank request <list|add <id> [min] [note]|del <id>>");
        return true;
    }

    static bool HandleCompanionCreateCommand(ChatHandler* handler, char const* args)
    {
        // Usage: .companion create <name> <raceId> <classId> <gender:0|1> <accountName> [profSpell1] [profSpell2]
        std::istringstream iss(args ? args : "");
        std::string name, accountName;
        uint32 race = 0, cls = 0, gender = 0, prof1 = 0, prof2 = 0;
        if (!(iss >> name >> race >> cls >> gender >> accountName))
        {
            handler->PSendSysMessage(
                "Usage: .companion create <name> <raceId> <classId> <gender:0=M,1=F> <accountName> [profSpell1] [profSpell2]");
            return true;
        }
        iss >> prof1 >> prof2;

        if (!normalizePlayerName(name))
        {
            handler->PSendSysMessage("Invalid character name: {}", name);
            return true;
        }
        if (sCharacterCache->GetCharacterGuidByName(name))
        {
            handler->PSendSysMessage("Character '{}' already exists.", name);
            return true;
        }
        uint32 const accountId = AccountMgr::GetId(accountName);
        if (!accountId)
        {
            handler->PSendSysMessage("Account '{}' does not exist.", accountName);
            return true;
        }
        if (!sObjectMgr->GetPlayerInfo(static_cast<uint8>(race), static_cast<uint8>(cls)))
        {
            handler->PSendSysMessage("Invalid race/class combination: race={} class={}", race, cls);
            return true;
        }

        // Pick a valid appearance for race & gender from CharSectionsStore (same logic as RandomPlayerbotFactory).
        std::vector<std::pair<uint8, uint8>> faces, hairs;
        std::vector<uint8> facialHairs;
        for (CharSectionsEntry const* sec : sCharSectionsStore)
        {
            if (sec->Race != race || sec->Gender != gender)
                continue;
            if (sec->GenType == SECTION_TYPE_FACE)
                faces.emplace_back(sec->Type, sec->Color);
            else if (sec->GenType == SECTION_TYPE_HAIR)
                hairs.emplace_back(sec->Type, sec->Color);
            else if (sec->GenType == SECTION_TYPE_FACIAL_HAIR)
                facialHairs.push_back(sec->Type);
        }
        if (faces.empty() || hairs.empty())
        {
            handler->PSendSysMessage("No CharSections appearance found for race={} gender={}", race, gender);
            return true;
        }
        auto const face = faces[urand(0, faces.size() - 1)];
        auto const hair = hairs[urand(0, hairs.size() - 1)];
        bool const noFacial = (race == RACE_TAUREN) || (race == RACE_DRAENEI) ||
                              (gender == GENDER_FEMALE && race != RACE_NIGHTELF && race != RACE_UNDEAD_PLAYER);
        uint8 const facialHair = (noFacial || facialHairs.empty()) ? 0 : facialHairs[urand(0, facialHairs.size() - 1)];

        auto* session = new WorldSession(accountId, "", 0x0, nullptr, SEC_PLAYER, EXPANSION_WRATH_OF_THE_LICH_KING,
                                         time_t(0), LOCALE_enUS, 0, false, false, 0, true);
        auto charInfo = std::make_unique<CharacterCreateInfo>(
            name, static_cast<uint8>(race), static_cast<uint8>(cls), static_cast<uint8>(gender),
            face.second, face.first, hair.first, hair.second, facialHair);

        Player* player = new Player(session);
        player->GetMotionMaster()->Initialize();
        ObjectGuid::LowType const lowGuid = sObjectMgr->GetGenerator<HighGuid::Player>().Generate();
        if (!player->Create(lowGuid, charInfo.get()))
        {
            player->CleanupsBeforeDelete();
            delete player;
            delete session;
            handler->PSendSysMessage("Player::Create failed for '{}' (race={} class={}).", name, race, cls);
            return true;
        }

        player->setCinematic(2);
        player->SetAtLoginFlag(AT_LOGIN_NONE);

        auto teachProf = [player](uint32 spellId)
        {
            if (!spellId)
                return;
            player->learnSpell(spellId, false);
            if (spellId == 2575) // Mining Apprentice -> Smelting + Find Minerals + Mining Pick
            {
                player->learnSpell(2656, false);
                player->learnSpell(2580, false);
                player->StoreNewItemInBestSlots(2901, 1);
            }
            else if (spellId == 2366) // Herbalism Apprentice -> Find Herbs
                player->learnSpell(2383, false);
            else if (spellId == 8613) // Skinning Apprentice -> Skinning Knife
                player->StoreNewItemInBestSlots(7005, 1);
            else if (spellId == 2018) // Blacksmithing Apprentice -> Blacksmith Hammer
                player->StoreNewItemInBestSlots(5956, 1);
            else if (spellId == 7411) // Enchanting Apprentice -> Disenchant + Runed Copper Rod
            {
                player->learnSpell(13262, false);
                player->StoreNewItemInBestSlots(6218, 1);
            }
        };
        teachProf(prof1);
        teachProf(prof2);

        ObjectGuid const guid = player->GetGUID();
        player->SaveToDB(true, false);
        sCharacterCache->AddCharacterCacheEntry(guid, accountId, player->GetName(), player->getGender(),
                                                player->getRace(), player->getClass(), player->GetLevel());
        player->CleanupsBeforeDelete();
        delete player;
        delete session;

        // Clear any stale orphan guild_member rows from deleted random bots before joining owner's guild.
        CharacterDatabase.DirectExecute("DELETE FROM guild_member WHERE guid = {}", guid.GetCounter());
        sCharacterCache->UpdateCharacterGuildId(guid, 0);

        // Add to the owner's guild (rank 4 = Initiate) if configured.
        uint32 const guildId = sCompanionBotMgr.OwnerGuildId();
        bool inGuild = false;
        if (guildId)
        {
            if (Guild* guild = sGuildMgr->GetGuildById(guildId))
                inGuild = guild->AddMember(guid, 4);
        }

        handler->PSendSysMessage(
            "Created level-1 companion '{}' (guid={}, account={}, race={}, class={}, gender={}, profs={}/{}, guild={})",
            name, guid.GetCounter(), accountName, race, cls, gender, prof1, prof2, inGuild ? "yes" : "no");
        return true;
    }

    static bool HandleCompanionScheduleCommand(ChatHandler* handler, char const* args)
    {
        std::istringstream iss(args ? args : "");
        std::string sub;
        iss >> sub;
        std::transform(sub.begin(), sub.end(), sub.begin(), [](unsigned char c) { return std::tolower(c); });
        if (sub == "reload")
        {
            sCompanionBotMgr.ReloadSchedules();
            handler->PSendSysMessage("Reloaded companion_schedule. {}", sCompanionBotMgr.ScheduleSummary());
            return true;
        }
        handler->PSendSysMessage("{}", sCompanionBotMgr.ScheduleSummary());
        return true;
    }

    static bool HandleCompanionLifeCommand(ChatHandler* handler, char const* args)
    {
        std::istringstream iss(args ? args : "");
        std::string sub;
        iss >> sub;
        std::transform(sub.begin(), sub.end(), sub.begin(), [](unsigned char c) { return std::tolower(c); });

        if (sub == "reload")
        {
            CompanionLife::ReloadAll();
            for (std::string const& line : CompanionLife::StatusLines(""))
                handler->PSendSysMessage("{}", line);
            return true;
        }
        if (sub == "events")
        {
            std::string name;
            iss >> name;
            if (name.empty())
            {
                handler->PSendSysMessage("Usage: .companion life events <botName>");
                return false;
            }
            for (std::string const& line : CompanionLife::EventLines(name))
                handler->PSendSysMessage("{}", line);
            return true;
        }
        if (sub == "relations")
        {
            std::string name;
            iss >> name;
            for (std::string const& line : CompanionLife::RelationLines(name))
                handler->PSendSysMessage("{}", line);
            return true;
        }
        if (sub == "orders")
        {
            for (std::string const& line : CompanionLife::WorkOrderLines())
                handler->PSendSysMessage("{}", line);
            return true;
        }
        if (sub == "weekly")
        {
            for (std::string const& line : CompanionLife::WeeklyEventLines())
                handler->PSendSysMessage("{}", line);
            return true;
        }
        if (sub == "pulse")
        {
            uint32 const created = CompanionLife::TriggerWorkOrderPulse();
            handler->PSendSysMessage("Guild Life work-order pulse created {} new guild order(s).", created);
            for (std::string const& line : CompanionLife::WorkOrderLines())
                handler->PSendSysMessage("{}", line);
            return true;
        }
        if (sub == "order")
        {
            std::string reqName;
            uint32 itemId = 0, qty = 1;
            iss >> reqName >> itemId;
            if (!(iss >> qty) || qty == 0)
                qty = 1;
            std::string story;
            std::getline(iss, story);
            while (!story.empty() && std::isspace(static_cast<unsigned char>(story.front())))
                story.erase(story.begin());
            if (reqName.empty() || !itemId)
            {
                handler->PSendSysMessage("Usage: .companion life order <requesterName> <itemId> [qty] [story]");
                return false;
            }
            ObjectGuid const og = sCharacterCache->GetCharacterGuidByName(reqName);
            if (!og)
            {
                handler->PSendSysMessage("Character '{}' not found.", reqName);
                return false;
            }
            bool const ok = CompanionLife::CreateWorkOrder(og.GetCounter(), reqName, itemId, qty, story);
            handler->PSendSysMessage(ok ? "Created guild work order for {} (item {} x{})."
                                        : "Work order already open or invalid for {} (item {}).",
                                     reqName, itemId, qty);
            return ok;
        }

        // Default or "status [botName]"
        std::string filter;
        if (sub == "status")
            iss >> filter;
        else
            filter = sub;
        for (std::string const& line : CompanionLife::StatusLines(filter))
            handler->PSendSysMessage("{}", line);
        return true;
    }

    static bool HandleLinkAccountCommand(ChatHandler* handler, char const* args)
    {
        if (!args || !*args)
            return false;

        char* accountName = strtok((char*)args, " ");
        char* key = strtok(nullptr, " ");

        if (!accountName || !key)
        {
            handler->PSendSysMessage("Usage: .playerbots account link <accountName> <securityKey>");
            return false;
        }

        Player* player = handler->GetSession()->GetPlayer();

        PlayerbotMgr* mgr = PlayerbotsMgr::instance().GetPlayerbotMgr(player);
        if (mgr)
        {
            mgr->HandleLinkAccountCommand(player, accountName, key);
            return true;
        }
        else
        {
            handler->PSendSysMessage("PlayerbotMgr instance not found.");
            return false;
        }
    }

    static bool HandleViewLinkedAccountsCommand(ChatHandler* handler, char const* /*args*/)
    {
        Player* player = handler->GetSession()->GetPlayer();

        PlayerbotMgr* mgr = PlayerbotsMgr::instance().GetPlayerbotMgr(player);
        if (mgr)
        {
            mgr->HandleViewLinkedAccountsCommand(player);
            return true;
        }
        else
        {
            handler->PSendSysMessage("PlayerbotMgr instance not found.");
            return false;
        }
    }

    static bool HandleUnlinkAccountCommand(ChatHandler* handler, char const* args)
    {
        if (!args || !*args)
            return false;

        char* accountName = strtok((char*)args, " ");
        if (!accountName)
        {
            handler->PSendSysMessage("Usage: .playerbots account unlink <accountName>");
            return false;
        }

        Player* player = handler->GetSession()->GetPlayer();

        PlayerbotMgr* mgr = PlayerbotsMgr::instance().GetPlayerbotMgr(player);
        if (mgr)
        {
            mgr->HandleUnlinkAccountCommand(player, accountName);
            return true;
        }
        else
        {
            handler->PSendSysMessage("PlayerbotMgr instance not found.");
            return false;
        }
    }
};

void AddPlayerbotsCommandscripts() { new playerbots_commandscript(); }
