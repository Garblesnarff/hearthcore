/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#include "LfgActions.h"
#include "AiFactory.h"
#include "CompanionBotMgr.h"
#include "CompanionBrain.h"
#include "CompanionLfgBridgeLogic.h"
#include "ItemVisitors.h"
#include "LFGMgr.h"
#include "Opcodes.h"
#include "Playerbots.h"
#include "RandomPlayerbotMgr.h"
#include "World.h"
#include "WorldPacket.h"

using namespace lfg;

bool LfgJoinAction::Execute(Event /*event*/) { return JoinLFG(); }

uint32 LfgJoinAction::GetRoles()
{
    if (!RandomPlayerbotMgr::instance().IsRandomBot(bot))
    {
        if (botAI->IsTank(bot))
            return PLAYER_ROLE_TANK;
        if (botAI->IsHeal(bot))
            return PLAYER_ROLE_HEALER;
        else
            return PLAYER_ROLE_DAMAGE;
    }

    uint8 spec = AiFactory::GetPlayerSpecTab(bot);
    switch (bot->getClass())
    {
        case CLASS_DRUID:
            if (spec == 2)
                return PLAYER_ROLE_HEALER;
            else if (spec == 1 && bot->HasAura(16931) /* thick hide */)
                return PLAYER_ROLE_TANK;
            else
                return PLAYER_ROLE_DAMAGE;
            break;
        case CLASS_PALADIN:
            if (spec == 1)
                return PLAYER_ROLE_TANK;
            else if (!spec)
                return PLAYER_ROLE_HEALER;
            else
                return PLAYER_ROLE_DAMAGE;
            break;
        case CLASS_PRIEST:
            if (spec != 2)
                return PLAYER_ROLE_HEALER;
            else
                return PLAYER_ROLE_DAMAGE;
            break;
        case CLASS_SHAMAN:
            if (spec == 2)
                return PLAYER_ROLE_HEALER;
            else
                return PLAYER_ROLE_DAMAGE;
            break;
        case CLASS_WARRIOR:
            if (spec == 2)
                return PLAYER_ROLE_TANK;
            else
                return PLAYER_ROLE_DAMAGE;
            break;
        case CLASS_DEATH_KNIGHT:
            if (spec == 0)
                return PLAYER_ROLE_TANK;
            else
                return PLAYER_ROLE_DAMAGE;
            break;

        default:
            return PLAYER_ROLE_DAMAGE;
            break;
    }

    return PLAYER_ROLE_DAMAGE;
}

bool LfgJoinAction::JoinLFG()
{
    // Issue #20 (P1-9 & P2-1): Cooperative companions NEVER fall through to the random-bot LfgDungeons list or
    // touch non-thread-safe LFGMgr state from map-thread AI actions; solo voluntary cooperative RDF queueing is
    // driven exclusively on the world thread by CompanionBrain::TickCoopLfgOnWorldThread() -> SendCompanionLfgJoin().
    if (sCompanionBotMgr.IsCompanion(bot) && !sCompanionBotMgr.IsGroupedWithOwner(bot))
        return false;

    // check if already in lfg
    LfgState state = sLFGMgr->GetState(bot->GetGUID());
    if (state != LFG_STATE_NONE)
        return false;

    /*ItemCountByQuality visitor;
    IterateItems(&visitor, ITERATE_ITEMS_IN_EQUIP);
    bool random = urand(0, 100) < 20;
    bool heroic = urand(0, 100) < 50 &&
                  (visitor.count[ITEM_QUALITY_EPIC] >= 3 || visitor.count[ITEM_QUALITY_RARE] >= 10) &&
                  bot->GetLevel() >= 70;
    bool rbotAId = !heroic && (urand(0, 100) < 50 && visitor.count[ITEM_QUALITY_EPIC] >= 5 &&
                               (bot->GetLevel() == 60 || bot->GetLevel() == 70 || bot->GetLevel() == 80));*/

    LfgDungeonSet list;
    std::vector<uint32> selected;

    std::vector<uint32> dungeons = RandomPlayerbotMgr::instance().LfgDungeons[bot->GetTeamId()];
    if (!dungeons.size())
        return false;

    for (std::vector<uint32>::iterator i = dungeons.begin(); i != dungeons.end(); ++i)
    {
        LFGDungeonEntry const* dungeon = sLFGDungeonStore.LookupEntry(*i);
        if (!dungeon || (dungeon->TypeID != LFG_TYPE_RANDOM && dungeon->TypeID != LFG_TYPE_DUNGEON &&
                         dungeon->TypeID != LFG_TYPE_HEROIC && dungeon->TypeID != LFG_TYPE_RAID))
            continue;

        auto const& botLevel = bot->GetLevel();

        /*LFG_TYPE_RANDOM on classic is 15-58 so bot over level 25 will never queue*/
        if ((dungeon->MinLevel && (botLevel < dungeon->MinLevel || botLevel > dungeon->MaxLevel)) ||
            (botLevel > dungeon->MinLevel + 10 && dungeon->TypeID == LFG_TYPE_DUNGEON))
            continue;

        selected.push_back(dungeon->ID);
        list.insert(dungeon->ID);
    }

    if (!selected.size())
        return false;

    if (list.empty())
        return false;

    bool many = list.size() > 1;
    LFGDungeonEntry const* dungeon = sLFGDungeonStore.LookupEntry(*list.begin());

    // Issue #12: Guard automated companion LFG queue join (never interrupts owner-grouped or human-grouped companions).
    if (sCompanionBotMgr.IsCompanion(bot) && !sCompanionBotMgr.IsGroupedWithOwner(bot))
    {
        bool hasHumanMember = false;
        if (Group const* grp = bot->GetGroup())
        {
            for (Group::MemberSlot const& slot : grp->GetMemberSlots())
            {
                if (!sCompanionBotMgr.IsCompanionSlot(slot.guid, slot.name))
                {
                    hasHumanMember = true;
                    break;
                }
            }
        }
        if (!hasHumanMember)
        {
            std::vector<uint32> targetMaps;
            targetMaps.reserve(list.size());
            for (uint32 rawEntry : list)
            {
                LFGDungeonEntry const* entry = sLFGDungeonStore.LookupEntry(rawEntry & 0x00FFFFFF);
                if (!entry || entry->TypeID == LFG_TYPE_RANDOM || entry->MapID <= 0)
                {
                    targetMaps.push_back(0);  // forces fail-closed rejection on unknown/random dungeon
                }
                else
                {
                    targetMaps.push_back(static_cast<uint32>(entry->MapID));
                }
            }
            std::string admitReason;
            if (!CompanionBrain::TryAdmitCompanionLfgJoin(bot, targetMaps, &admitReason))
            {
                LOG_INFO("playerbots", "[CompanionCoop] {} LFG queue join blocked ({})", bot->GetName(), admitReason);
                return false;
            }
        }
    }

    // check role for console msg
    std::string _roles = "multiple roles";
    uint32 roleMask = GetRoles();
    if (roleMask & PLAYER_ROLE_TANK)
        _roles = "TANK";

    if (roleMask & PLAYER_ROLE_HEALER)
        _roles = "HEAL";

    if (roleMask & PLAYER_ROLE_DAMAGE)
        _roles = "DPS";

    LOG_INFO("playerbots", "Bot {} {}:{} <{}>: queues LFG, Dungeon as {} ({})", bot->GetGUID().ToString().c_str(),
             bot->GetTeamId() == TEAM_ALLIANCE ? "A" : "H", bot->GetLevel(), bot->GetName().c_str(), _roles,
             many ? "several dungeons" : dungeon->Name[0]);

    // Set RbotAId Browser comment
    std::string const _gs = std::to_string(botAI->GetEquipGearScore(bot/*, false, false*/));

    // JoinLfg is not threadsafe, so make packet and queue into session
    // sLFGMgr->JoinLfg(bot, roleMask, list, _gs);

    WorldPacket* data = new WorldPacket(CMSG_LFG_JOIN);
    *data << (uint32)roleMask;
    *data << (bool)false;
    *data << (bool)false;
    // Slots
    *data << (uint8)(list.size());
    for (uint32 dungeon : list)
        *data << (uint32)dungeon;
    // Needs
    *data << (uint8)3 << (uint8)0 << (uint8)0 << (uint8)0;
    *data << _gs;
    bot->GetSession()->QueuePacket(data);

    return true;
}

bool LfgRoleCheckAction::Execute(Event /*event*/)
{
    if (bot->GetGroup())
    {
        uint32 newRoles = GetRoles();
        // if (currentRoles == newRoles)
        //     return false;

        WorldPacket* packet = new WorldPacket(CMSG_LFG_SET_ROLES);
        *packet << (uint8)newRoles;
        bot->GetSession()->QueuePacket(packet);
        // sLFGMgr->SetRoles(bot->GetGUID(), newRoles);
        // sLFGMgr->UpdateRoleCheck(group->GetGUID(), bot->GetGUID(), newRoles);

        LOG_INFO("playerbots", "Bot {} {}:{} <{}>: LFG roles checked", bot->GetGUID().ToString().c_str(),
                 bot->GetTeamId() == TEAM_ALLIANCE ? "A" : "H", bot->GetLevel(), bot->GetName().c_str());

        return true;
    }

    return false;
}

bool LfgAcceptAction::Execute(Event event)
{
    uint32 id = AI_VALUE(uint32, "lfg proposal");

    auto isHumanLedGroup = [this]() -> bool
    {
        Group const* grp = bot->GetGroup();
        if (!grp)
            return false;
        for (Group::MemberSlot const& slot : grp->GetMemberSlots())
        {
            if (sCompanionBotMgr.IsAnyOwnerLow(slot.guid.GetCounter()))
                return true;
            if (Player* member = ObjectAccessor::FindConnectedPlayer(slot.guid))
            {
                if (!GET_PLAYERBOT_AI(member))
                    return true;
            }
            else if (!sCompanionBotMgr.IsCompanionSlot(slot.guid, slot.name))
            {
                return true;
            }
        }
        return false;
    };

    auto handleCompanionProposal = [this](uint32 propId, uint32 rawDungeon, uint8 propState) -> bool
    {
        bool const shiftEnded = !sCompanionBotMgr.IsScheduledOnline(bot->GetName(), bot);
        CompanionLfgBridge::ProposalDecision const dec =
            CompanionBrain::EvaluateCompanionLfgProposal(bot, propId, rawDungeon, propState, shiftEnded);

        if (dec.action == CompanionLfgBridge::ProposalAction::ClearStoredOnly)
        {
            botAI->GetAiObjectContext()->GetValue<uint32>("lfg proposal")->Set(0);
            CompanionBrain::OnCompanionLfgProposalFinished(bot->GetGUID().GetCounter(), propId, propState);
            return true;
        }
        if (dec.action == CompanionLfgBridge::ProposalAction::Ignore)
            return false;
        if (dec.action == CompanionLfgBridge::ProposalAction::DeferInCombat)
        {
            // Keep proposal stored while finishing combat / flight / vehicle / cast; world-thread
            // CompanionBrain::TickCoopLfgOnWorldThread re-evaluates every 2s until safe boundary or timeout.
            botAI->GetAiObjectContext()->GetValue<uint32>("lfg proposal")->Set(propId);
            return true;
        }
        if (dec.action == CompanionLfgBridge::ProposalAction::Decline)
        {
            LOG_INFO("playerbots", "[CompanionCoop] {} LFG proposal {} declined ({})", bot->GetName(), propId,
                     dec.reason);
            botAI->GetAiObjectContext()->GetValue<uint32>("lfg proposal")->Set(0);
            WorldPacket* packet = new WorldPacket(CMSG_LFG_PROPOSAL_RESULT);
            *packet << propId << false;
            bot->GetSession()->QueuePacket(packet);
            CompanionBrain::OnCompanionLfgProposalDeclined(bot->GetGUID().GetCounter(), propId, dec.reason);
            return true;
        }
        // Accept at safe boundary (P2-3: do NOT call ClearUnitState(UNIT_STATE_ALL_STATE) or botAI->Reset(),
        // which can corrupt taxi/vehicle or companion mode/lease state!).
        botAI->GetAiObjectContext()->GetValue<uint32>("lfg proposal")->Set(0);
        WorldPacket* packet = new WorldPacket(CMSG_LFG_PROPOSAL_RESULT);
        *packet << propId << true;
        bot->GetSession()->QueuePacket(packet);
        CompanionBrain::OnCompanionLfgProposalAccepted(bot->GetGUID().GetCounter(), dec.opportunityId, propId,
                                                       dec.lfgDungeonId);
        LOG_INFO("playerbots", "[CompanionCoop] {} accepted native LFG proposal {} for opp #{} (lfgId={})",
                 bot->GetName(), propId, dec.opportunityId, dec.lfgDungeonId);
        return true;
    };

    bool const isCoopCompanion =
        sCompanionBotMgr.IsCompanion(bot) && !sCompanionBotMgr.IsGroupedWithOwner(bot) && !isHumanLedGroup();

    // Try accept if already stored (world-thread TickCoopLfgOnWorldThread handles deferred coop proposals safely
    // without map-thread LFGMgr reads — P1-9!)
    if (id)
    {
        if (isCoopCompanion)
            return false;
        if (bot->IsInCombat() || bot->isDead())
        {
            botAI->GetAiObjectContext()->GetValue<uint32>("lfg proposal")->Set(0);
            WorldPacket* packet = new WorldPacket(CMSG_LFG_PROPOSAL_RESULT);
            *packet << id << false;
            bot->GetSession()->QueuePacket(packet);
            return true;
        }

        botAI->GetAiObjectContext()->GetValue<uint32>("lfg proposal")->Set(0);
        bot->ClearUnitState(UNIT_STATE_ALL_STATE);

        WorldPacket* packet = new WorldPacket(CMSG_LFG_PROPOSAL_RESULT);
        *packet << id << true;
        bot->GetSession()->QueuePacket(packet);

        if (RandomPlayerbotMgr::instance().IsRandomBot(bot) && !bot->GetGroup())
        {
            RandomPlayerbotMgr::instance().Refresh(bot);
            botAI->ResetStrategies();
        }

        botAI->Reset();
        return true;
    }

    // If we get the proposal packet, check state & safe boundary
    if (!event.getPacket().empty())
    {
        WorldPacket p(event.getPacket());
        uint32 dungeonId;
        uint8 state;
        p >> dungeonId >> state >> id;

        if (id)
        {
            if (isCoopCompanion)
                return handleCompanionProposal(id, dungeonId, state);
            if (state != lfg::LFG_PROPOSAL_INITIATING)
            {
                botAI->GetAiObjectContext()->GetValue<uint32>("lfg proposal")->Set(0);
                return true;
            }
            if (bot->IsInCombat() || bot->isDead())
            {
                botAI->GetAiObjectContext()->GetValue<uint32>("lfg proposal")->Set(0);
                WorldPacket* packet = new WorldPacket(CMSG_LFG_PROPOSAL_RESULT);
                *packet << id << false;
                bot->GetSession()->QueuePacket(packet);
                return true;
            }

            botAI->GetAiObjectContext()->GetValue<uint32>("lfg proposal")->Set(0);
            bot->ClearUnitState(UNIT_STATE_ALL_STATE);

            WorldPacket* packet = new WorldPacket(CMSG_LFG_PROPOSAL_RESULT);
            *packet << id << true;
            bot->GetSession()->QueuePacket(packet);

            if (RandomPlayerbotMgr::instance().IsRandomBot(bot) && !bot->GetGroup())
            {
                RandomPlayerbotMgr::instance().Refresh(bot);
                botAI->ResetStrategies();
            }

            botAI->Reset();
            return true;
        }
    }

    return false;
}

bool LfgLeaveAction::Execute(Event /*event*/)
{
    // Don't leave if lfg strategy enabled
    // if (botAI->HasStrategy("lfg", BOT_STATE_NON_COMBAT))
    //    return false;

    // Don't leave if already invited / in dungeon
    if (sLFGMgr->GetState(bot->GetGUID()) > LFG_STATE_QUEUED)
        return false;

    // Don't drop a queue we deliberately joined. The "seldom" tick (RandomTrigger, ~300s)
    // otherwise pulls random bots straight back out, so LFGQueue::CheckCompatibility never
    // sees a role-complete pool that holds still long enough to form a group. Turning
    // RandomBotJoinLfg off still lets whoever is mid-queue fall through and leave.
    // Issue #20 & #22 (P1-9 & P2-2): Cooperative companion queue leaves are driven exclusively on the world thread
    // by CompanionBrain::TickCoopLfgOnWorldThread() so map-thread random triggers never race LFGMgr or spam leaves.
    if (sCompanionBotMgr.IsCompanion(bot) && !sCompanionBotMgr.IsGroupedWithOwner(bot))
        return false;
    if (sPlayerbotAIConfig.randomBotJoinLfg && RandomPlayerbotMgr::instance().IsRandomBot(bot))
        return false;

    WorldPacket* packet = new WorldPacket(CMSG_LFG_LEAVE);
    bot->GetSession()->QueuePacket(packet);
    // sLFGMgr->LeaveLfg(bot->GetGUID());
    return true;
}

bool LfgLeaveAction::isUseful() { return true; }

bool LfgTeleportAction::Execute(Event event)
{
    bool out = false;

    WorldPacket p(event.getPacket());
    if (!p.empty())
    {
        p.rpos(0);
        p >> out;
    }

    bot->ClearUnitState(UNIT_STATE_ALL_STATE);

    WorldPacket* packet = new WorldPacket(CMSG_LFG_TELEPORT);
    *packet << out;
    bot->GetSession()->QueuePacket(packet);
    // sLFGMgr->TeleportPlayer(bot, out);

    return true;
}

bool LfgJoinAction::isUseful()
{
    // Issue #20 (P0-2, P1-9, P2-1): Cooperative companions queue exclusively on the world thread via
    // CompanionBrain::TickCoopLfgOnWorldThread() — never via map-thread LfgJoinAction or random-bot LfgDungeons!
    if (sCompanionBotMgr.IsCompanion(bot) && !sCompanionBotMgr.IsGroupedWithOwner(bot))
        return false;

    if (!sPlayerbotAIConfig.randomBotJoinLfg)
    {
        // botAI->ChangeStrategy("-lfg", BOT_STATE_NON_COMBAT);
        return false;
    }

    if (bot->GetLevel() < 15)
        return false;

    // Don't use for selfbots (a real player is at the keyboard).
    if (IsSelfBot(bot))
        return false;

    if (bot->GetGroup() && bot->GetGroup()->GetLeaderGUID() != bot->GetGUID())
    {
        // botAI->ChangeStrategy("-lfg", BOT_STATE_NON_COMBAT);
        return false;
    }

    if (bot->IsBeingTeleported())
        return false;

    if (bot->InBattleground())
        return false;

    if (bot->InBattlegroundQueue())
        return false;

    if (bot->isDead())
        return false;

    Map* map = bot->GetMap();
    if (map && map->Instanceable())
        return false;

    if (!RandomPlayerbotMgr::instance().IsRandomBot(bot))
        return false;

    LfgState state = sLFGMgr->GetState(bot->GetGUID());
    if (state != LFG_STATE_NONE)
        return false;

    return true;
}
