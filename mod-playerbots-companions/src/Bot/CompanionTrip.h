/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#ifndef PLAYERBOTS_COMPANIONTRIP_H
#define PLAYERBOTS_COMPANIONTRIP_H

#include "Define.h"

class Aura;
enum AuraRemoveMode : uint8;
class Player;
class PlayerbotAI;
class Unit;
class WorldObject;

// Companion trip rules (Part B: B1 trip peace, B7 duty approach). Companions only; random bots never reach this code.
//
// Trip peace (AiPlayerbot.Companion.TripPeace.Enabled, default 1): while a companion holds an active lease (any
// leased trip: quest turn-in / hub / mount trip, duty walk, craft or fish trip), it does not START fights. The
// grind target ("attack anything") is restricted to
//   - units that attack the companion, its group, its pet or its owner (reason=attacked), and
//   - creatures of an incomplete kill objective of a quest in its log (reason=quest) within TripPeace.QuestYards
//     (default 30) of the companion, i.e. of its path; with TripPeace.QuestLoot = 1 (default 0) also creatures that
//     drop an item a quest in the log still needs (reason=quest-loot).
// The gate holds while the lease is paused too: the lease notices the end of a fight only on the 10-s sync tick, and
// in that window the stock grind target took the next mob (fight chains of 100-270 s in run 2).
//
// Log (Info, playerbots), one line per new attack target while a lease is held:
//   Companion <Name>: trip combat start target=<entry> name="<name>" reason=attacked|quest|quest-loot|other
//                     via=<action> lease=<owner> dist=<d> mounted=<0|1>
//   Companion <Name>: trip mount spell=<id> leg=<d> lease=<owner>
//   Companion <Name>: trip mount blocked reason=hostile-near range=<r> lease=<owner>        (rate limited, 60 s)
//
// Combat dismount (AiPlayerbot.Companion.CombatDismount.Enabled, default 1): a mounted player cannot attack
// (Unit::Attack returns false while IsMounted, so AttackAction "succeeds" but the bot never swings or casts), and the
// stock dismount (CheckMountStateAction) only fires when a current target is within dismount distance at the moment
// it runs. A companion attacked while mounted on a trip sat in combat for minutes until a mob's Daze knocked it off
// the mount (run 3). Now a companion dismounts when it picks an attack target while mounted, and the 10-s companion
// sync dismounts a companion that is in combat, mounted and attacked (safety net).
//   Companion <Name>: trip combat dismount via=<action|tick> target=<entry> dist=<d> lease=<owner|none>
//   Companion <Name>: trip combat end dur=<s> mounted=<0|1> dismounts=<n> lease=<owner|none>
//     (mounted = mounted when the fight started; logged on the first sync tick out of combat, +-10 s)
//
// Trip combat avoidance (AiPlayerbot.Companion.TripAvoid.Enabled, default 1; Part C "avoid"; rules as the owner
// specified them): a companion on a quest trip that is attacked while MOUNTED keeps riding toward the trip target and
// does not stop, dismount or turn to fight. While the ride-through runs:
//   - AttackAction::Attack refuses every target (no dismount, no stop, the engine stays non-combat),
//   - the "companion quest trip" trigger stays active in combat, so the travel action keeps moving,
//   - the lease is not paused for combat (the trip is progressing), and the 10-s dismount safety net is skipped.
// It fights (dismount + fight, CombatDismount) for the rest of the fight when one of these holds, and only then:
//   dismounted   the companion is not mounted any more (Daze, spell, other),
//   health       below TripAvoid.MinHealthPct (50),
//   quest        a mob that attacks it is a quest mob: an incomplete kill objective, or a creature that drops an item
//                a quest in its log still needs (QuestReason rules, no TripPeace.QuestLoot switch needed),
//   caught       the SAME mob has been hitting it (in melee range now, or dealt damage within 3 s) for
//                TripAvoid.CaughtSeconds (10) counted from that mob's first hit on this fight,
//   stuck        it moved less than 5 yd in 4 s while riding (the guard against standing still in combat),
//   trip-ended   the trip or the lease ended, or it is in a battleground / arena,
//   flight       on a taxi or flying (no attack is possible there anyway).
// Only quest trips (CompanionQuest) qualify: duty, craft and fish trips have no mover that runs in combat, so they
// keep the old behaviour. On foot the existing fight behaviour stays. All checks are O(attackers) per AI tick.
//   Companion <Name>: trip combat avoid start attackers=<n> hp=<pct> target=<entry> lease=<o>
//   Companion <Name>: trip combat avoid skip reason=<why> attackers=<n> hp=<pct> mounted=<0|1> lease=<o>
//     (attacked on a quest trip but the ride-through was not even started; the fight is then fought)
//   Companion <Name>: trip combat avoid end result=escaped|fought|died reason=<why> dur=<s> lease=<o|none>
//     (escaped = out of combat without a fight; fought = fallback, a "trip combat start/end" pair follows for the
//      fight itself, whose dur= does not include the avoid seconds)
// Counting: fights fought = "trip combat end" lines; fights avoided = "avoid end result=escaped"; combat minutes
// fought = sum of "trip combat end" dur=; lease paused= no longer includes ride-through seconds.
//
// Assist refusal (AiPlayerbot.Companion.TripAvoid.NoAssist, default 1): a companion does not attack a unit whose
// victim is another companion that is riding through (TripAvoid). Companions are not grouped, so no assist either.
//   Companion <Name>: trip combat assist refused target=<entry> victim=<name>     (rate limited, 10 s)
//
// Dismount cause (AiPlayerbot.Companion.DismountLog, default 1): every dismount of a companion that holds a trip
// lease or is in combat. Mount aura removal is caught by the UnitScript hooks in Playerbots.cpp (OnAuraRemove); the
// cause comes from the removal mode and from the companion's own last hit and last hostile aura (1 s windows).
// The companion's own dismounts (CompanionTrip::SelfDismount) log cause=self with via=<action>.
//   Companion <Name>: dismount cause=<daze|spell|damage|self|other> spell=<id> attacker=<entry> lease=<owner|none>
//                     inCombat=<0|1>[ via=<action>]
//
// Stuck teleport (NewRpgBaseAction::MoveFarTo, companions): one line per teleport.
//   Companion <Name>: stuck teleport map=<m> from=x,y,z to=x,y,z zone=<z> cause=<stuck-walk|cross-map> stuckSec=<s>
//                     lease=<owner|none>
//
// Duty approach (AiPlayerbot.Companion.DutyApproach.Enabled, default 1): see ApproachPoint / CheckApproach.
//   Companion <Name>: duty approach duty=<d> target=<entry> spawn=<id> bot=<x,y,z> point=<x,y,z> tz=<z>
//                     path=<type hex> gap=<yd> result=ok|unreachable
// The quest trips use the same approach for quest enders, hub quest givers, mount vendors and flight masters:
//   Companion <Name>: trip approach kind=<turnin|hub|vendor|flightmaster> target=<entry> spawn=<id> ... result=...
namespace CompanionTrip
{
// Any thread. Switches, re-read from the config at most every 10 s (".reload config" works).
bool PeaceEnabled();
float PeaceQuestYards();
bool ApproachEnabled();
bool CombatDismountEnabled();
bool AvoidEnabled();
// A quest mob of the companion (kill objective or quest loot, as the trip fight rules see it). The trip route does not
// treat these as aggro obstacles: the companion fights them on the way.
bool IsQuestObjectiveMob(Player* bot, Unit* unit);
bool NoAssistEnabled();
bool DismountLogEnabled();

// Map thread. Trip combat avoidance: starts, checks and ends the ride-through for this companion (see above) and
// returns true while it runs, i.e. while attacks must be refused and the trip may move in combat. Once it returns
// false during a fight it stays false until the companion is out of combat (the fight is fought). Also call it out
// of combat: that ends a running ride-through with result=escaped. Companions only; false for everyone else.
bool AvoidActive(PlayerbotAI* botAI);

// Any thread, no evaluation: true while a ride-through runs for this companion (CompanionLease::Tick uses it to
// not pause the lease). Must not be called while holding the lease mutex.
bool Avoiding(uint32 lowGuid);

// Map thread, from AttackAction::Attack before bot->Attack. True when the attack must be refused: the target's victim
// is another companion that is riding through (NoAssist). Logs the refusal (rate limited). Companions only.
bool AssistRefused(PlayerbotAI* botAI, Unit* target);

// Map thread, from AttackAction::Attack before bot->Attack. A mounted companion (not on a taxi, not flying)
// dismounts so the attack can start. Returns true when it dismounted. Companions only.
bool DismountForCombat(PlayerbotAI* botAI, Unit* target, char const* via);

// Map thread, RAII: wraps a dismount the companion's own code makes (mount aura removal or Dismount()). Logs
// "dismount cause=self via=<via>" when the companion was mounted on entry and is not on exit. While one is alive,
// the UnitScript hook does not log the same removal again. Companions only (checked on exit).
class SelfDismount
{
public:
    SelfDismount(Player* bot, char const* via, uint32 attackerEntry = 0);
    ~SelfDismount();
    SelfDismount(SelfDismount const&) = delete;
    SelfDismount& operator=(SelfDismount const&) = delete;

private:
    Player* _bot;
    char const* _via;
    uint32 _attacker;
    uint32 _spell = 0;
    bool _wasMounted;
};

// Map thread, from the 10-s companion sync (CompanionBotMgr::TickBot). Safety net: in combat, mounted and attacked
// -> dismount (via=tick). Also logs "trip combat end" once the companion is out of combat.
void CombatTick(PlayerbotAI* botAI);

// Map thread, UnitScript hooks (Playerbots.cpp). Record the companion's last hits (per attacker: first and last hit)
// and its last hostile aura; NoteMountRemoved logs a dismount that was not the companion's own (cause from the
// removal mode, the last hit and the last hostile aura).
void NoteDamage(Unit* attacker, Unit* victim);
void NoteAuraApplied(Unit* unit, Aura const* aura);
void NoteMountRemoved(Unit* unit, AuraRemoveMode removeMode);

// Map thread. True while trip peace applies to this bot: a companion, switch on, an active lease (paused or not).
bool PeaceActive(PlayerbotAI* botAI);

// Map thread. Why the companion may fight unit during a trip: "attacked", "quest", "quest-loot", or nullptr (not
// allowed). The quest reasons need unit within PeaceQuestYards of the bot.
char const* PeaceReason(PlayerbotAI* botAI, Unit* unit);

// Map thread, from AttackAction::Attack on a target change. Logs "trip combat start" while a lease is held.
void NoteAttack(PlayerbotAI* botAI, Unit* target, char const* via);

// Map thread. Tries to mount for a trip leg (used by CompanionQuest::TryMount). While trip peace is on, only a
// hostile within 10 yd (or an attacker) blocks the mount, instead of any hostile within 25 yd.
float MountHostileRange(PlayerbotAI* botAI);
void NoteMount(PlayerbotAI* botAI, uint32 spell, float leg);
void NoteMountBlocked(PlayerbotAI* botAI, float range);

// Map thread. Where a companion should stand to use target (trainer, vendor, banker, bank chest): a point 1.5 yd in
// front of the target when that point is on the target's floor (height within 1.5 yd) and in its line of sight,
// otherwise the target's own position. Always the target's floor, never a point on the straight line from the bot.
void ApproachPoint(Player* bot, WorldObject* target, float& x, float& y, float& z);

// Map thread. Path check from the bot to (x, y, z) for target. Returns false on no path, or when the path ends
// more than INTERACTION_DISTANCE from the target without getting 5 yd closer to it than the bot is (dead end).
// gap = distance from the path end to the target; pathType = PathType flags.
bool CheckApproach(Player* bot, WorldObject* target, float x, float y, float z, float& gap, uint32& pathType);
}  // namespace CompanionTrip

#endif
