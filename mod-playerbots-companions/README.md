# mod-playerbots Companion Subsystem (Hearthguard Mirror)

This directory mirrors our custom C++ companion subsystem and targeted patches from our local `companions` branch of [AzerothCore `mod-playerbots`](https://github.com/mod-playerbots/mod-playerbots) (GNU GPLv2).

- **Live Branch Commit:** `862e9bb0` (`autonomy-fix-862e9bb0`)
- **Upstream Base:** `mod-playerbots` (AzerothCore WotLK 3.3.5a)

## Mirrored Files

### `src/Bot/Companion*.{h,cpp}`
- **`CompanionBotMgr.{h,cpp}`** — 38-companion roster schedules, `COMPANION_AUTONOMY` lifecycle, strategy drift self-heal (`-follow`, `-stay`, `+companion duty/quest/new rpg/grind`), 15m motionless unstuck watchdog, 3s `live_positions.json` writer.
- **`CompanionBrain.{h,cpp}`** — Loads bounded `0.5–1.5` activity weights from `acore_characters.companion_weights` and guild gathering targets from `companion_bank_requests` every ~5 minutes (zero restart needed).
- **`CompanionCraft.{h,cpp}`** — Autonomous crafting, work-order `+180` priority boost, `EvaluateGatherFarm` (maps vault bar requests → ores), `NextGatherSpot` (2600 yd + zone node density + prime Alliance farm bonuses), and `EvaluateFish` (Sagefish school targeting).
- **`CompanionDuty.{h,cpp}`** — Core autonomy dispatcher integrating `CompanionBrain` weights, questing, crafting, gathering, fishing, training, banking, vendoring, and Hunter ammo restock.
- **`CompanionLife.{h,cpp}`** — Guild Life v1 & v2: authoritative `companion_events`, `companion_relationships` proximity bonds, 15m autonomous work-order pulse, weekly guild event calendar (Wed Craft / Fri Fishing / Sun Dungeon), and neighbourhood presence.
- **`CompanionQuest.{h,cpp}`**, **`CompanionLease.{h,cpp}`**, **`CompanionRoute.{h,cpp}`**, **`CompanionTransport.{h,cpp}`**, **`CompanionTrip.{h,cpp}`** — Questing, tank-lead/group leasing, road routing, transports, and multi-step travel trips.

### Targeted Upstream Patches
- **`src/Ai/Base/Actions/ReviveFromCorpseAction.cpp`** — Danger-death graveyard escape using `CachedZoneAt()` fallback on corpse, bot, and closest graveyard (`gyZone`) to prevent corpse-camping loops in high-level zones.
- **`src/Script/PlayerbotCommandScript.cpp`** — `.companion` GM commands (`.companion life weekly|pulse|order`, `.companion schedule reload`, etc.).

## Key `playerbots.conf` Companion Switches

```ini
AiPlayerbot.Companion.GatherFarm.GuildRequests = 1
AiPlayerbot.Companion.Lead.Default = 1
AiPlayerbot.CompanionLevelLead = 3
```
