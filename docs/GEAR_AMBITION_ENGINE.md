# Gear ambition engine — bracketed, attainable BiS projects

Status: proposed; issue #24. Source baseline bfeed54. No game changes or deployments. All examples synthetic. Use installed WotLK 3.3.5a data and actual progression policies. Preserve native playerbot autonomy, legitimate rewards, owner-controlled raids, and optional personal interests.

## 1. Objective and planning brackets

Give every persistent character a dynamic equipment wishlist, not a mandatory shopping list. Distinguish theoretical BiS from practical upgrades. Quest, craft, dungeon, vendor, auction, reputation, PvP, world-drop and other legitimate sources belong in the catalog. Unsupported execution methods remain advisory, never simulated.

Proposed bracket endpoints: 9, 14, 19, 24, 29, 34, 39, 44, 49, 54, 59, 60, 65, 70, 75, 80. A bracket is a planning horizon, NOT an eligibility rule. The actual server exports current level, effective XP cap, owner/group policy and expansion stage. Do not assume the same +3 cap for every guildmate. Example: level 21 with cap 23 gets NOW (21), CAP (23), NEXT (24+) previews. A level-24 requirement is not eligible at cap 23. If actual cap is below bracket endpoint, truncate the attainable list. At expansion cap 60/70/80, use longer potential gear lifespan but never assume exact owner progression timing. Future XP-locked twinks get explicit fixed horizon; no automatic XP lock or alt creation now.

For every equipment slot/role: equipped item; owned usable alternatives; top 3–5 achievable candidates now; cap targets; theoretical bracket BiS with prerequisites; optional next-bracket preview; 1–3 active item projects. Gear targets persist across sessions and refresh on actual cap, role, level, gear, inventory, quest, skill, stage or access changes. Avoid mandatory replacement of each item at every bracket. Preserve owner heirlooms and designated gear, useful alternate-role pieces and raid minimums. Allow zero-XP quests for gear, gold, reputation and unlocks. Personal ambitions may be deferred or abandoned.

## 2. Shared data catalog

Generate an indexed, versioned, read-only catalog from exact installed world DB, DBC and native game data, offline or in a bounded worker. Audit actual schema and conditions; do not rely on Classic/retail websites or LLM-invented IDs. Record data/build hash, source table/entry, item requirements, binding, slot, base stats, weapon speed/damage, armor, ratings, set/effect references, unique limits, class/race/proficiency, source alternatives and confidence. Resolve nested reference loot with cycle detection, loot groups/exclusivity, conditions and quest drops. Unknown chances stay unknown. Audit item-instance suffixes, enchants, sockets and legal ownership/transfers; don't treat every template as obtainable.

| Source | Verified prerequisites and native action |
|---|---|
| Owned | Legal equip from actual bags or accessible bank; no duplicate purchase. |
| Quest | Actual level/class/faction, prerequisites, chain, log capacity, one-time reward, choice; native accept/complete/turn in. |
| Dungeon | Native eligible LFG IDs, boss/drop, access, difficulty, level, lockouts, group role, encounter support. |
| Raid | Owner-authorized raid only, actual attunements/locks/eligibility; no autonomous raid. |
| Crafted | Recipe learned/learnable, trained skill cap, materials, gold, binding; actual native crafting or existing guild work order. BoP not promised by trade. |
| Vendor | Native stock, gold/currency, extended cost, rep and travel. |
| AH/trade | Actual listing, funds, legal binding, price, stock and counterparty; no injected gold. |
| Reputation/PvP/currency | Actual progression/access and earned currency; advisory until executor verified. |
| Outdoor drops/rare | Verified creature/object and loot condition; actual encounter, no fake spawn knowledge. |
| Enhancements | Legal sockets, enchant/gem/recipe, required skill and materials; compare remaining life of gear. |

Use shared immutable catalog, bounded indexed queries by slot/role/level/source, cached shortlist per bracket, refinement from character snapshot. No complete loot graph per bot, no repeated full DB scan at session or tick. Do not commit production DB/client data or secrets to public Git.

## 3. Gear valuation and acquisition choice

Define reviewed versioned class/spec/role stat policies for 3.3.5a: role-relevant stats, armor, healing, weapon damage/speed, ratings at actual level, hit/expertise and defensive caps, mana/sustain, known set bonuses, actual item effects where supported. Do not claim universal DPS or exact proc value without simulation/evidence. Flag unsupported effects and uncertain rankings. Item level/color alone is not gear value.

Compute marginal legal set utility, not simply independent slot winners. Rings/trinkets unique-equipped, duplicate instances, two-hand vs shield/offhand, dual-wield/weapon skill, set bonuses, caps and alternate-role sets. Bound top-K/beam or enumerate small coupled slots; verify against brute force synthetic fixtures. Actual native equip validation is authoritative. Keep current useful gear; no automatic disenchant/sell protected items.

Separate gear value from effort:
- gain = max(0, utility(new legal set) - utility(current legal set));
- useful duration = conservative horizon using actual cap, known gates and replacement risk (unknown if uncertain);
- cost = actual prerequisites, materials/gold/currency, travel, queue, attempts and opportunity cost;
- choose bounded normalized benefit/effort score, with verified readiness priority and persona interests applied once. Do not stack memory and AI adjustments for same evidence.
For a repeatable independent eligible loot opportunity with known chance p, expected attempts roughly 1/p, not a guarantee; competition, lockouts, conditional loot and unknown drop odds complicate. No fake estimates or pity timer. Prefer attainable adequate upgrade over marginal BiS if expensive/likely obsolete; long cap may make farming worthwhile. No global punishment for unlucky no-drop. Compare crafting/quest/AH/vendor alternatives. Different personas can legitimately choose differently.

## 4. Project and native contracts

Proposed modules: gear_catalog, gear_policy, gear_projects, native export/acquisition adapters (final names after audit). Use existing bridge candidate and proposal envelopes; no second planner. Export current character key (realm/GUID/incarnation), persona/session, snapshot version/freshness, actual level/cap/stage, class/spec/talents/proficiencies, equipped item-instance IDs/attributes, accessible owned gear, quest/reward state, skills/recipes, money/currencies, rep/access, locks and native viable roles. Unknown facts block irreversible execution.

Project: stable id, character key, target item and slot/set, source variant and native IDs, revision, policy/data hash, eligibility and blockers, alternatives, estimated effort/confidence, active state and evidence IDs. States aspirational -> eligible -> proposed -> accepted -> executing -> acquired -> equipped/retained; blocked, deferred, obsolete, abandoned, interrupted and rejected. Acquired != equipped. Inventory event/current state is authority. Successful dungeon clear != successful gear project. Re-evaluate after cap/gear changes or source disappearance.

Candidate example: {"id":"gear:project_842","type":"gear","score":78,"description":"Main-hand upgrade via eligible dungeon; alternative quest reward available"}. Synthetic ID; trusted exporter maps to real target. Claude may choose only provided eligible IDs, not invent item/source or game commands. Native revalidates snapshot revision, active character, actual eligibility, ownership, resources and lease/owner priorities. Quest reward choice must account for other valid gear goals; work order native claim/delivery; RDF specific queue when gear source relevant, random only when legitimate pool and persona accepts uncertainty. While queued, continue other valid tasks. Use normal vendor, bank, AH, equip paths; no direct DB mutation of game rewards/inventory. Deterministic fallback without LLM required.

## 5. Actual outcomes, dashboard and guild

Correlate project/plan/lease/queue/party/run/boss/loot attempt IDs with actual event journal. Record attempt only on real eligible loot opportunity, not every instance visit. Store acquired item, source, materials/gold spent, quest progress, choice, equip outcome, and actual remaining objective. Missing evidence -> unknown, no invented learning. Repeated technical failure -> scoped operational alert/alternate method; no-drop -> neutral opportunity count. Defer/abandon/obsolete goals retain history. Guild requests expose real materials and crafting need; bank allocation and resource reservation must be authoritative, not double-counted. Readiness uses actual verified equipment and role, not aspirational list.

Azeroth Watch: per-character equipment view showing current, attainable and aspirational targets; source, gain rationale, eligibility and uncertainty, current cap, outstanding prerequisites, goal state, real acquisition history, dungeon queue demand, profession commission and alternatives. Distinguish slot suggestions from combined legal set. No fake completeness percent; show catalog coverage.

## 6. Deliver in vertical slices

G0 audit installed item/loot/quest/recipe/DBC, equip/reward/commerce paths, true level caps and role policy.
G1 shared static catalog, schema coverage, deterministic tests, index and size measurement.
G2 role valuation/legal set shortlist, horizon and practical alternatives.
G3 actual character export and read-only dashboard, validate against in-game equipment.
G4 typed project lifecycle and shadow candidates, no native writes.
G5 one real acquisition vertical slice (owned/quest/craft/vendor), then RDF drop once #20/#21/#17 validated; other sources enabled individually after actual execution proof.
G6 actual outcome journal, Quartermaster/readiness integration and alternate sources.
G7 supervised pilot, bounded resource budget and genuine acquisitions; optional choices preserved.

Mandatory tests: cap 23 excludes item req 24; stage gates; different owner/core/guild caps; missing telemetry; unique/equip/two-hand conflicts; already owned superior item; wrong role/weapon proficiency; unsupported proc; reference loot cycle/conditional drop; quest already rewarded; BoP craft; insufficient skill/funds; AH listing disappears; valid vs ineligible loot attempt; no-drop, stale/replayed proposal, identity restore, source changes; no shadow mutation, API offline, no auto-raid, no fake rewards. Test legal native acquisition and actual equip on exact installed build. Close #24 only with evidence-backed lists and a verified acquisition vertical slice; track unsupported sources separately. No new API call needed to generate lists.
