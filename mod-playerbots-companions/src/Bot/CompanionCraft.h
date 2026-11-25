/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#ifndef PLAYERBOTS_COMPANIONCRAFT_H
#define PLAYERBOTS_COMPANIONCRAFT_H

#include <set>
#include <string>
#include <utility>
#include <vector>

#include "Define.h"
#include "ObjectGuid.h"

class Player;
struct ItemTemplate;

// Companion professions (brain phase 2): the CRAFT and FISH duties, the material flow through the guild bank
// (reservations), the vendor spend cap, and the hooks other companion code asks (item class, train signature).
//
// CRAFT. From the recipes the bot knows (SkillLineAbility of a crafting skill line, a SPELL_EFFECT_CREATE_ITEM
// effect), the best skill-up colour wins (orange, yellow, green; never grey), then a result the party can use, then
// the largest skill gap. Materials come from the bags, the guild bank (withdraw, reserved in memory and in
// companion_ledger) and vendors (unlimited, gold-priced items, within the daily spend cap). Tools (Totem and
// TotemCategory) are bought too when a vendor sells them. A recipe that needs a spell focus (forge, anvil, cooking
// fire) walks to the nearest spawn of that focus, or casts a known spell that summons it (Basic Campfire).
// Smelting (Mining skill line) is preferred over every other recipe while ore is available.
// Need: the skill gap = min(5 x level, rank max) - value; need = min(100, gap x Craft.NeedPerPoint). Zero at the rank
// cap, and zero when no recipe has reachable materials.
//
// FISH. Need from the Fishing skill gap (same formula, Fish.NeedPerPoint), +Fish.CookingBonus while Cooking has a
// gap and the bags hold no raw food. Water: a fishing pool spawn (GAMEOBJECT_TYPE_FISHINGHOLE, catches are 100%) on
// the bot's map within Fish.SearchYards, or water next to the bot when the zone's catch chance
// (skill_fishing_base_level) is at least Fish.MinCatchPct. A bot without a fishing pole buys one first from a vendor with its own gold.
//
// Threads: Evaluate*/ChooseRecipe run on the companion's map thread (duty evaluation) or on the world thread (GM
// command, map threads idle). Run state, reservations, spend and saved weapons are under one mutex; nothing here
// calls CompanionDuty or CompanionLease while holding it.
class CompanionCraft
{
public:
    enum class Color : uint8
    {
        Grey,
        Green,
        Yellow,
        Orange
    };

    // A material-blocked craft need (Craft.GatherForNeed): skill = the skill line the recipe raises, blocked = the
    // reagent the bot lacks, source = the item to gather or smelt for it.
    struct GatherNeed
    {
        uint32 skill = 0;
        uint32 blocked = 0;
        uint32 source = 0;
    };

    enum class Force : uint8
    {
        None,
        Session,   // a craft session in a city (optional city name)
        Mill,      // mill a 5-stack of a herb from the bags or the guild bank
        Prospect   // prospect a 5-stack of an ore from the bags or the guild bank
    };

    enum Step : uint8
    {
        STEP_BANK = 0,      // withdraw planned materials from the guild bank
        STEP_VENDOR = 1,    // buy vendor materials, tools, the fishing pole
        STEP_STATION = 2,   // walk to the spell focus or the water
        STEP_WORK = 3,      // craft or fish
        STEP_DONE = 4
    };

    // A walk target: a spawn (creature or game object) or a plain point. valid = false when there is none.
    struct Place
    {
        bool valid = false;
        bool isGo = false;
        uint32 mapId = 0;
        float x = 0.0f, y = 0.0f, z = 0.0f;
        uint32 spawnId = 0;
        uint32 entry = 0;
    };

    struct Material
    {
        uint32 entry = 0;
        uint32 perCraft = 0;     // per craft (tools: 1)
        uint32 inBags = 0;       // in the bags now
        uint32 fromBank = 0;     // planned withdraw
        uint32 fromVendor = 0;   // planned purchase (items)
        uint32 vendorPrice = 0;  // copper per item
        bool tool = false;       // a Totem / TotemCategory tool (not consumed)
    };

    struct Recipe
    {
        bool valid = false;
        uint32 spellId = 0;
        std::string spellName;
        uint32 skillId = 0;
        std::string skillName;
        Color color = Color::Grey;
        uint32 skillValue = 0;
        uint32 skillMax = 0;
        uint32 yellowAt = 0;     // SkillLineAbility TrivialSkillLineRankLow
        uint32 greyAt = 0;       // SkillLineAbility TrivialSkillLineRankHigh
        uint32 product = 0;      // created item entry
        uint32 crafts = 0;       // planned crafts in this batch
        std::vector<Material> materials;
        uint32 vendorCost = 0;   // copper for the planned purchases
        uint32 focusId = 0;      // RequiresSpellFocus
        uint32 campfireSpell = 0;// known spell that summons the focus (0 = walk to a spawn)
        bool partyUse = false;   // bandage, food, potion, elixir, armor kit, sharpening stone, gem, ink, bar, ...
        bool smelting = false;
        // B8: an item-targeted spell (Milling, Prospecting): one cast per 5-stack of the material, the results come
        // from the loot window (milling_loot_template / prospecting_loot_template). product = 0.
        bool targetItem = false;
        std::vector<uint32> lootItems;   // possible results (targetItem)
        uint32 gap = 0;
        float need = 0.0f;
        float rank = 0.0f;       // recipe choice score
        std::string source;      // bags, bags+bank, bags+vendor, bags+bank+vendor
        Place bank;              // valid when something is withdrawn
        Place vendor;            // valid when something is bought
        Place station;           // valid when a focus spawn must be walked to
    };

    // Result of a duty evaluation: need, the first walk target (or here), the lease destination key.
    struct Offer
    {
        float need = 0.0f;
        bool valid = false;      // need > 0 and something to do
        bool here = false;       // no walk: the first step is at the bot
        Place first;
        std::string key;         // lease destination key, e.g. "recipe:2660", "go:1234" (fishing pool) or "fish:here"
        std::string reason;      // why not, when need is 0 or nothing is reachable
        std::string summary;     // one line for "why" and the log
        // B6: a session pays the trip to the city once for every craft there; the duty scores its travel with
        // this factor (CraftSession.TravelFactor). 1 for a single recipe.
        float travelFactor = 1.0f;
    };

    // B6: a crafting session is planned around one city (a guild bank spawn) and everything within
    // CraftSession.CityYards of it: vendors, stations, the bank. ChooseRecipe then reads every place from there.
    struct Origin
    {
        bool valid = false;
        uint32 mapId = 0;
        float x = 0.0f, y = 0.0f, z = 0.0f;
        float radius = 0.0f;
        Place bank;                   // the session's guild bank spawn
        std::set<uint32> skip;        // recipes that failed in this session
    };

    // Runner state of one duty (craft or fish) for one companion.
    struct Run
    {
        bool valid = false;
        bool started = false;
        std::string duty;        // "craft" or "fish"
        Recipe recipe;           // craft
        uint8 step = STEP_BANK;
        uint32 crafted = 0;
        uint32 failures = 0;
        bool castPending = false;
        uint32 skillBefore = 0;
        uint32 productBefore = 0;
        uint32 startSec = 0;
        uint32 stepStartSec = 0;
        bool campfireCast = false;
        // fish
        Place water;             // pool spawn or the bot's position
        bool pool = false;
        uint32 zoneSkill = 0;
        uint32 chancePct = 0;
        Place poleVendor;        // valid when a pole must be bought
        uint32 poleItem = 0;
        uint32 endSec = 0;
        uint32 casts = 0;
        uint32 fishSkillStart = 0;
        uint32 lastWaterCheckMs = 0;
        uint32 noWaterChecks = 0;
        // craft session (B6)
        bool session = false;
        uint8 phase = 0;              // SessionPhase
        Origin city;
        std::string cityName;
        float sessionNeed = 0.0f;
        bool recipeActive = false;    // run.recipe is the current recipe of the session
        uint32 sessionCrafts = 0;
        uint32 sessionRecipes = 0;
        uint32 sessionFailures = 0;   // recipes in a row that made nothing
        uint32 sessionEndSec = 0;
        uint32 castSec = 0;           // item-targeted cast: when it was sent
        std::vector<std::pair<uint32, uint32>> skillStart;   // skill -> value at session start
        // gather-farm (active farming for blocked craft reagents: Copper Ore, Rough Stone, Herbs, ...)
        uint32 gatherSkill = 0;
        uint32 gatherItem = 0;
        uint32 gatherTargetCount = 20;
        uint32 gatherStartCount = 0;
        uint32 gatheredNodes = 0;
        Place gatherSpot;
        std::vector<uint32> gatherNodeEntries;
        std::set<uint32> gatherSkipSpawns;
    };

    enum SessionPhase : uint8
    {
        PHASE_TRAVEL = 0,    // leased trip to the city
        PHASE_WORK = 1,      // withdraw, buy, craft (KeepAlive)
        PHASE_DEPOSIT = 2,   // bank duty at the city's guild bank, then the session ends
    };

    // --- config (cached for 60 s; AiPlayerbot.Companion.*) -------------------------------------------------------
    // AiPlayerbot.Companion.<area>.Enabled, default on (area: Gather, Craft, Fish, Train).
    static bool AreaEnabled(char const* area);
    // A float option by full key.
    static float Option(char const* key, float fallback);

    // --- duties ----------------------------------------------------------------------------------------------------
    // Best recipe now (see the class comment). reason is set when none is valid. at (optional): plan from a city
    // instead of the bot's position (bank, vendors and stations within at->radius). perSkill (optional): the best
    // recipe of every skill line.
    // forced (GM test hook): Mill / Prospect plan only that feeder (the 5-stack of the herb or ore whose results the
    // bot's own recipes need is not required: the GM asks for it).
    // gatherWant (optional): every reagent a known recipe with a skill gap lacks (no bags, bank or vendor stock), with
    // the item a gatherer would bring (the ore a smelting spell turns into it, else the item itself).
    static Recipe ChooseRecipe(Player* bot, std::string& reason, Origin const* at = nullptr,
                               std::vector<Recipe>* perSkill = nullptr, Force forced = Force::None,
                               std::vector<GatherNeed>* gatherWant = nullptr);
    // B6: the best session city on the bot's map (guild bank spawns of its team with vendors and the stations its
    // recipes need within CraftSession.CityYards). only (optional): the city whose name matches (case blind, spaces
    // or underscores). Invalid when none.
    static Origin ChooseCity(Player* bot, std::string& name, char const* only = nullptr);
    // GM test hook (.companion craft <name> mill | prospect | session [city]): the next craft evaluation plans that
    // duty (and the duty wins over the others when its lease can be taken). Pending for 10 minutes or until the run
    // starts. The duty then runs the normal path: lease, travel, withdraw, craft, deposit.
    static void RequestForce(uint32 lowGuid, Force kind, std::string const& city);
    // Gather needs of the last craft evaluation (fresh for 2 minutes). Empty when none or the switch is off.
    static std::vector<GatherNeed> GatherNeeds(uint32 lowGuid);
    // Craft.KeepReagents: an item a known, not capped craft recipe uses as a reagent (companions only). Such an item is
    // KEEP for the sell and bank duties, so ore, bars and stone for a recipe are not sold or banked away.
    static bool IsKnownReagent(Player* bot, uint32 entry);
    // Duty evaluation. active = the duty is running: the running plan is kept (its need), nothing is recomputed.
    // Not active: a new plan is computed and stored as the pending run (started by the runner after the duty starts).
    static Offer EvaluateCraft(Player* bot, bool active);
    static Offer EvaluateFish(Player* bot, bool active);
    static Offer EvaluateGatherFarm(Player* bot, bool active);
    // Finds the next safe spawn on bot's map yielding gatherItem via gatherSkill, skipping skipSpawns.
    static Place NextGatherSpot(Player* bot, uint32 gatherItem, uint32 gatherSkill,
                                std::set<uint32> const& skipSpawns);
    // Lease owner of a duty: "craft:<skill>" for craft ("craft:session" for a session), "fish" for fish.
    static std::string LeaseOwner(uint32 lowGuid, std::string const& duty);
    // Skill gap: min(5 x level, max) - value (0 when the bot does not have the skill or is at the cap).
    static uint32 SkillGap(Player* bot, uint32 skillId);

    static bool GetRun(uint32 lowGuid, std::string const& duty, Run& out);
    static void SaveRun(uint32 lowGuid, Run const& run);
    static void ClearRun(uint32 lowGuid, std::string const& duty);

    // --- material flow ----------------------------------------------------------------------------------------------
    // Guild bank count of an entry on the tabs the bot may view, minus what other companions reserved.
    static uint32 BankAvailable(Player* bot, uint32 entry);
    // Reserves count of entry for this bot (memory + a companion_ledger row "reserve"). Released by Release (at the
    // withdraw or when the run ends) or after 30 minutes ("release", note "expired").
    static void Reserve(Player* bot, uint32 entry, uint32 count);
    static void Release(uint32 lowGuid, uint32 entry, char const* note);
    static void ReleaseAll(uint32 lowGuid, char const* note);
    // Daily vendor spend (game day, memory): copper left under Craft.MaxVendorSpend (gold, x level/20 below 20).
    static uint32 SpendLeft(Player* bot);
    static void NoteSpend(uint32 lowGuid, uint32 copper);
    // Nearest guild bank / vendor selling the item / spawn of a spell focus, on the bot's map.
    // at (optional): within at->radius of the session city instead of near the bot.
    static Place NearestBank(Player* bot, Origin const* at = nullptr);
    static Place NearestVendorFor(Player* bot, std::vector<uint32> const& items, Origin const* at = nullptr);
    static Place NearestFocus(Player* bot, uint32 focusId, Origin const* at = nullptr);
    // Slot of item in the vendor entry's list, -1 when it does not sell it (unlimited, gold price).
    static int32 VendorSlot(uint32 vendorEntry, uint32 item);
    // A fishing pole a vendor sells (cheapest the bot can use), 0 when none on this map.
    static uint32 FishingPoleItem(Player* bot);
    static bool HasFishingPole(Player* bot);

    // --- hooks -----------------------------------------------------------------------------------------------------
    // CompanionBrain::ClassifyItem: a material of the bot's pending or running plan stays in the bags. During a craft
    // session (until its deposit) every trade good and gem stays too (pigments, inks, gems, bars feed later recipes).
    static bool IsPlannedMaterial(uint32 lowGuid, uint32 entry);
    // CompanionBrain::ClassifyItem: food or bandages above Craft.KeepStacks stacks are banked.
    static bool OverKeepStacks(Player* bot, ItemTemplate const* proto);
    // CompanionDuty TRAIN: changes when the level, a profession's rank cap state or a missing secondary skill
    // changes, so the far trainer search runs again although the level is capped.
    // B8/B12: also changes when a profession reaches a trainer spell's ReqSkillRank and when the number of class
    // trainer spells the bot may learn now changes.
    static uint32 TrainSignature(Player* bot);
    // TRAIN: trainer spells of the bot's map that wait only for a profession skill rank (ReqSkillRank above the
    // skill), as text lines; the CRAFT need of that skill gets Craft.RankTargetBonus.
    static std::vector<std::string> RankBlockedLines(Player* bot);
    static bool RankBlocked(Player* bot, uint32 skillId);
    // TRAIN: class trainer spells (trainer_spell of the bot's class trainers) the bot may learn now and does not know.
    static uint32 ClassSpellsPending(Player* bot);
    // World thread (CompanionDuty::UpdateWorld): loads the milling and prospecting loot tables once (with one level
    // of reference_loot_template).
    static void LoadWorldData();
    // Possible results of milling or prospecting an item (empty when the item has no such loot).
    static std::vector<uint32> TargetLoot(uint32 itemEntry, bool milling);
    // Weapons taken off for the fishing pole (restored on combat and when the fish duty ends).
    static void SaveWeapons(uint32 lowGuid, ObjectGuid mainHand, ObjectGuid offHand);
    static bool SavedWeapons(uint32 lowGuid, ObjectGuid& mainHand, ObjectGuid& offHand);
    static void ClearSavedWeapons(uint32 lowGuid);

    // --- reports ---------------------------------------------------------------------------------------------------
    static std::vector<std::string> SkillLines(Player* bot);
    static std::vector<std::string> CraftLines(Player* bot);
    static char const* ColorName(Color color);
    // Skill-up colour of a recipe at a skill value (Player::UpdateCraftSkill thresholds).
    static Color ColorFor(uint32 value, uint32 yellowAt, uint32 greyAt);
    // Registers craft, fish and gather with the lease's "why not" (once).
    static void RegisterActivities();
};

#endif
