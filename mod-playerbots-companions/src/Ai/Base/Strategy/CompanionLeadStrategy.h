/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#ifndef PLAYERBOTS_COMPANIONLEADSTRATEGY_H
#define PLAYERBOTS_COMPANIONLEADSTRATEGY_H

#include "NonCombatStrategy.h"
#include "ObjectGuid.h"

#include <string>
#include <vector>

class Group;
class Multiplier;
class Player;
class PlayerbotAI;
class Unit;

// Companion lead mode (dungeons/raids grouped with owner, or authorized Phase II coop dungeons).
namespace CompanionLead
{
// Raid target icon indices used by lead mode (see RtiTargetValue::skullIndex).
constexpr uint8 CIRCLE_ICON = 1;
constexpr uint8 CROSS_ICON = 6;
constexpr uint8 SKULL_ICON = 7;

// Deterministic single lead tank per group (issue #83): chooses the lowest-GUID alive, lead-active companion tank
// on the same map (when reference is given). Cached per group; never switches mid-combat while still valid, and
// only re-elects out of combat when another eligible alive tank is available or the current lead is no longer valid.
Player* GroupLead(Group* group, Player const* reference = nullptr);
// True only for the single elected companion lead of bot's group.
bool IsGroupLead(Player* bot);

// Tank (combat, lead mode): maintains the group kill order list (Skull=1st, Cross=2nd, Circle=3rd) when
// AiPlayerbot.Companion.Lead.KillOrder = 1. Respects owner-placed marks and excludes Diamond & CC'd mobs.
void UpdateKillOrder(PlayerbotAI* botAI);
// Clears pack kill-order state once the group leaves combat.
void ClearKillOrderIfIdle(PlayerbotAI* botAI);

// Tank: all ready-check conditions hold and a pull target exists. Null otherwise. far is set when the target is a
// walk-to target beyond PullRange (found by path distance); the caller waits for the group and walks to it.
Unit* PickPullTarget(PlayerbotAI* botAI, bool* far = nullptr);
// Damage dealer: target selection with lead v2 persistence (or v1 fallback when TargetPersistence=0).
Unit* AssistTarget(PlayerbotAI* botAI, std::string* reason = nullptr);
// Damage dealer (DpsTargetValue hook): returns the persisted/lead target when target persistence is active,
// setting handled=true. When handled=true and return is null with an active fight, caller falls through to upstream.
Unit* LeadDpsTarget(PlayerbotAI* botAI, bool* handled = nullptr);
// Records an upstream fallback target into the bot's persistence lock so it sticks until death/CC/OOR/new skull.
void LockFallbackTarget(PlayerbotAI* botAI, Unit* target);
// True when any member of the group is currently being attacked by a live hostile creature.
bool GroupHasActiveFight(Group* group);
// Lead mode v2: returns the live group lead tank for melee DPS (and non-lead melee off-tanks) to follow
// (null for ranged, the group lead itself, healers, or when lead mode is off).
Player* MeleeFollowTarget(PlayerbotAI* botAI);
// Lead mode v2: owner "pull" command releases the soft healer tether wait. Returns true if tank was waiting on tether.
bool NotifyOwnerPullCommand(PlayerbotAI* botAI, Player* requester);
// Non-lead companion in lead mode: true when the unit (the raid icon's target) is not engaged with any group member,
// so it must not be a damage target yet. False for the elected group lead and outside lead mode.
bool IconHeldUnengaged(PlayerbotAI* botAI, ObjectGuid guid);
// Non-tank in lead mode: the skull is set on a unit that is not engaged with the group (and no active fight when v2).
bool SkullHeldUnengaged(PlayerbotAI* botAI);
// Tank in combat: the skull target that the chain pull will open on, when the chain check passes. Null otherwise.
// reason receives why the chain pull is refused (empty when there is no skull target at all).
Unit* ChainTarget(PlayerbotAI* botAI, std::string* reason = nullptr);
// Damage dealer: logs "[Companion] <bot> lead: <old> -> <new> reason=<r> tankDist=<d>" when the attack target changes.
void NoteAttackTarget(PlayerbotAI* botAI, Unit* target, std::string const& reason = "");
// Owner's "lead on" / "lead off" in party chat. Only the owner can switch the mode.
bool SetLeadFromOwner(PlayerbotAI* botAI, Player* from, bool on);
}  // namespace CompanionLead

// Combat strategy for a companion tank in lead mode: chain pulls the next skull target while fighting.
class CompanionChainStrategy : public Strategy
{
public:
    CompanionChainStrategy(PlayerbotAI* botAI) : Strategy(botAI) {}

    std::string const getName() override { return "companion chain"; }
    void InitTriggers(std::vector<TriggerNode*>& triggers) override;
};

// Non-combat strategy for a companion tank in lead mode. Its trigger starts a pull when the ready check passes.
class CompanionLeadStrategy : public NonCombatStrategy
{
public:
    CompanionLeadStrategy(PlayerbotAI* botAI) : NonCombatStrategy(botAI) {}

    std::string const getName() override { return "companion lead"; }
    void InitTriggers(std::vector<TriggerNode*>& triggers) override;
};

// Non-combat and combat strategy for a companion damage dealer (or non-lead off-tank) in lead mode. Attacks the
// skull target or the lead tank's current victim, never an unengaged unit, and suppresses taunt wars for a second tank.
class CompanionAssistStrategy : public Strategy
{
public:
    CompanionAssistStrategy(PlayerbotAI* botAI) : Strategy(botAI) {}

    std::string const getName() override { return "companion assist"; }
    void InitTriggers(std::vector<TriggerNode*>& triggers) override;
    void InitMultipliers(std::vector<Multiplier*>& multipliers) override;
};

// Factories used by the context registries.
class Trigger;
class Action;
Trigger* CreateCompanionLeadReadyTrigger(PlayerbotAI* botAI);
Trigger* CreateCompanionAssistReadyTrigger(PlayerbotAI* botAI);
Action* CreateCompanionLeadPullAction(PlayerbotAI* botAI);
Action* CreateCompanionAssistAction(PlayerbotAI* botAI);
Action* CreateCompanionLeadOnAction(PlayerbotAI* botAI);
Action* CreateCompanionLeadOffAction(PlayerbotAI* botAI);
Trigger* CreateCompanionChainReadyTrigger(PlayerbotAI* botAI);
Action* CreateCompanionChainPullAction(PlayerbotAI* botAI);

#endif
