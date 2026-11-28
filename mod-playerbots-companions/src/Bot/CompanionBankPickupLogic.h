/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#ifndef PLAYERBOTS_COMPANION_BANK_PICKUP_LOGIC_H
#define PLAYERBOTS_COMPANION_BANK_PICKUP_LOGIC_H

#include <cstdint>

// Pure rules for what a companion takes from the guild bank, and what the organizer may never pull out to vendor.
// No AzerothCore headers, so a standalone C++17 test compiles it. Numbers are the 3.3.5a item_template values.
namespace CompanionBankPickup
{
inline constexpr std::uint32_t CLASS_CONTAINER = 1;
inline constexpr std::uint32_t CLASS_WEAPON = 2;
inline constexpr std::uint32_t CLASS_ARMOR = 4;
inline constexpr std::uint32_t CLASS_RECIPE = 9;
inline constexpr std::uint32_t SUBCLASS_GENERAL_BAG = 0;
inline constexpr std::uint32_t QUALITY_UNCOMMON = 2;

// Per bank visit, so one companion does not empty the bank before the others arrive.
inline constexpr std::uint32_t MAX_RECIPES_PER_VISIT = 5;
inline constexpr std::uint32_t MAX_BAGS_PER_VISIT = 2;
inline constexpr std::uint32_t MAX_GEAR_PER_VISIT = 2;
// A pickup raises the bank duty at most this often, so a refused withdrawal (rank daily limit) cannot loop.
inline constexpr std::uint32_t RETRY_SECONDS = 1800;

enum class Kind : std::uint8_t
{
    None = 0,
    Recipe = 1,
    Bag = 2,
    Gear = 3,
};

// Owner deposits of these are never vendored by the organizer, whatever the stock limit says.
inline bool NeverPurge(std::uint32_t itemClass, std::uint32_t quality)
{
    if (itemClass == CLASS_CONTAINER || itemClass == CLASS_RECIPE)
        return true;
    return (itemClass == CLASS_WEAPON || itemClass == CLASS_ARMOR) && quality >= QUALITY_UNCOMMON;
}

struct Candidate
{
    std::uint32_t itemClass = 0;
    std::uint32_t subClass = 0;
    bool canUse = false;          // class, race, level, skill rank and required spell all pass
    bool alreadyHolds = false;    // a copy is equipped or in the bags
    bool teachesSpell = false;    // recipe has a learn spell
    bool knowsSpell = false;
    bool emptyBagSlot = false;
    bool biggerBag = false;       // larger than the smallest equipped general bag
    bool isUpgrade = false;       // item usage says equip / replace
    bool equipAllowed = false;    // companion equip rules (heirlooms, armor type)
};

inline Kind Decide(Candidate const& c)
{
    if (!c.canUse || c.alreadyHolds)
        return Kind::None;
    if (c.itemClass == CLASS_RECIPE)
        return c.teachesSpell && !c.knowsSpell ? Kind::Recipe : Kind::None;
    if (c.itemClass == CLASS_CONTAINER)
        return c.subClass == SUBCLASS_GENERAL_BAG && (c.emptyBagSlot || c.biggerBag) ? Kind::Bag : Kind::None;
    if (c.itemClass == CLASS_WEAPON || c.itemClass == CLASS_ARMOR)
        return c.isUpgrade && c.equipAllowed ? Kind::Gear : Kind::None;
    return Kind::None;
}
}  // namespace CompanionBankPickup

#endif
