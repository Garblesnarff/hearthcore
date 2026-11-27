-- LoganReads coach (version 2 — Arms Warrior Lv 1-80): teaches Logan to play an Arms Warrior and find keys.
-- Works only while LoganReads is ON (/lr on). Commands: /lr coach ...
-- The coach never auto-casts and never presses a key for him. It shows, speaks, binds optional letter keys, and counts.
-- Target: WoW 3.3.5a (interface 30300, Lua 5.1).

LoganReadsCoach = {}
local C = LoganReadsCoach

local FONT_PATH = "Fonts\\ARIALN.TTF"
local TEX_WHITE = "Interface\\Buttons\\WHITE8X8"
local TEX_STAR = "Interface\\Cooldown\\star4"
local TEX_TAB = "Interface\\Icons\\Ability_Tracking"
local TEX_FOOD = "Interface\\Icons\\INV_Misc_Food_15"
local TEX_CLOSER = "Interface\\Icons\\Ability_Rogue_Sprint"
local TEX_STANCE = "Interface\\Icons\\Ability_Warrior_OffensiveStance"
local SND_CHIME = "igMainMenuOptionCheckBoxOn"   -- PlaySound name, used by 3.3.5a FrameXML
local AUDIO_DIR = "Interface\\AddOns\\LoganReads\\audio\\coach\\"
local SKULL = 8                                   -- raid target index for the skull
local BIG_SIZE = 128
local TARGET_SCALE = 1.35
local LEVEL_UP_PRESSES = 20
local VOICE_GAP = 3                               -- at most one prompt voice line every 3 seconds
local PRAISE_GAP = 2.5                            -- separate cooldown so fast presses ALWAYS hear praise!
local ATTACKER_TTL = 5                            -- seconds to count recent melee attackers from combat log
local BOOKTYPE_SPELL = "spell"                    -- same value as SpellBookFrame.lua
-- binding sets: ACCOUNT_BINDINGS / CHARACTER_BINDINGS live in load-on-demand Blizzard_BindingUI
local ACCOUNT_SET, CHARACTER_SET = 1, 2

-- ============ PART 1: Arms Warrior Lv 1-80 key & action-bar table ============
-- Uses Logan's 1-6 number row + C/R/T/V/H letters.
-- NEVER touches W A S D Q E (movement), 3 (Logan's Shoot key), B (Bags), or M (World Map).
-- Battle Shout has key = nil (clicked on BottomLeft slot 12).
-- barButton = ActionButtonN (main bar, Battle Stance only); fixedSlot = MultiBarBottomLeft slot (61-72).
-- Keys and bar slots are only applied once Logan actually learns the ability in his spellbook.
local ABILITIES = {
	{ name = "Charge",        key = "C", color = { 1.00, 0.55, 0.10 }, slug = "charge",       melee = false, startAttackAfter = true, fixedSlot = 61 },
	{ name = "Rend",          key = "R", color = { 0.90, 0.20, 0.20 }, slug = "rend",         melee = true,  fixedSlot = 62 },
	{ name = "Thunder Clap",  key = "T", color = { 0.70, 0.35, 1.00 }, slug = "thunderclap",  melee = true,  fixedSlot = 63 },
	{ name = "Victory Rush",  key = "V", color = { 0.30, 0.90, 0.35 }, slug = "victoryrush",  melee = true,  fixedSlot = 64 },
	{ name = "Hamstring",     key = "H", color = { 0.95, 0.55, 0.85 }, slug = "hamstring",    melee = true,  fixedSlot = 65 },
	{ name = "Heroic Strike", key = "1", color = { 1.00, 0.90, 0.25 }, slug = "heroicstrike", melee = true,  queued = true, barButton = 1 },
	{ name = "Overpower",     key = "2", color = { 1.00, 0.72, 0.15 }, slug = "overpower",    melee = true,  barButton = 2 },
	-- Key 3 / ActionButton3 is reserved for Logan's Shoot!
	{ name = "Execute",       key = "4", color = { 1.00, 0.18, 0.18 }, slug = "execute",      melee = true,  barButton = 4 },
	{ name = "Mortal Strike", key = "5", color = { 0.95, 0.32, 0.12 }, slug = "mortalstrike", melee = true,  barButton = 5 },
	{ name = "Bladestorm",    key = "6", color = { 0.25, 0.88, 1.00 }, slug = "bladestorm",   melee = true,  barButton = 6 },
	{ name = "Battle Shout",  key = nil, color = { 0.30, 0.60, 1.00 }, slug = "battleshout",  melee = false, fixedSlot = 72 }, -- clicked
}
local FORBIDDEN_KEYS = { W = true, A = true, S = true, D = true, Q = true, E = true, ["3"] = true, B = true, M = true }
local BY_NAME, BY_KEY = {}, {}
for _, ab in ipairs(ABILITIES) do
	BY_NAME[ab.name] = ab
	if ab.key then
		BY_KEY[ab.key] = ab
		if FORBIDDEN_KEYS[ab.key] then
			DEFAULT_CHAT_FRAME:AddMessage("LR coach: forbidden key in table for " .. ab.name)
		end
	end
end

local TAB_ENTRY    = { name = "Find a foe!",  key = "TAB", color = { 1.00, 1.00, 1.00 }, icon = TEX_TAB,    voice = "tab" }
local EAT_ENTRY    = { name = "Eat!",         key = nil,   color = { 0.40, 1.00, 0.40 }, icon = TEX_FOOD,   voice = "eat" }
local CLOSER_ENTRY = { name = "Move closer!", key = nil,   color = { 1.00, 0.78, 0.25 }, icon = TEX_CLOSER, voice = "closer" }
local STANCE_ENTRY = { name = "Battle Stance!", key = nil, color = { 0.95, 0.65, 0.25 }, icon = TEX_STANCE, voice = "stance" }

local STAGE_RANK = { A = 1, B = 2, C = 3, D = 4 }

-- ============ PART 2: Arms Warrior suggestion rules, in priority order ============
-- stage: lowest stage where rule runs (A=1 core keys, B=2 adds Tab/Eat/Move Closer/Battle Stance).
-- needTarget: rule only runs with a live target.
local RULES = {
	-- 1. Combat target acquisition & out-of-combat recovery (Stage B)
	{ id = "tab", stage = 2, needTarget = false, entry = TAB_ENTRY,
	  test = function(x) return x.inCombat and not x.hasTarget end },
	{ id = "eat", stage = 2, needTarget = false, entry = EAT_ENTRY,
	  test = function(x) return (not x.inCombat) and x.pHealth < 0.55 and (not x.eating) end },

	-- 2. Arms Warrior stance check (Battle Stance = form 1; prevents silent coach if he accidentally swaps stance!)
	{ id = "stance", stage = 2, needTarget = true, entry = STANCE_ENTRY,
	  test = function(x)
		return x.hostile and x.knowsBattleStance and x.stance ~= 1 and x.stance ~= 0
	  end },

	-- 3. Charge opener (8-25 yd) — IsUsableSpell automatically supports Juggernaut in-combat Charge at Lv 40+!
	{ id = "charge", stage = 1, needTarget = true, ab = "Charge",
	  test = function(x)
		return x.hostile and IsSpellInRange("Charge", "target") == 1
			and x.ready("Charge") and x.res("Charge")
	  end },

	-- 4. Battle Shout (+AP buff, keep active when fighting)
	{ id = "battleshout", stage = 1, needTarget = true, ab = "Battle Shout",
	  test = function(x)
		return x.hostile and (not x.hasBuffPlayer("Battle Shout")) and x.ready("Battle Shout") and x.res("Battle Shout")
	  end },

	-- 5. Out of melee range & cannot Charge -> tell him to move closer instead of falsely suggesting melee spells!
	{ id = "closer", stage = 2, needTarget = true, entry = CLOSER_ENTRY,
	  test = function(x) return x.hostile and (not x.inMelee) end },

	-- 6. Victory Rush (free massive hit after a kill — always use before it expires!)
	{ id = "victoryrush", stage = 1, needTarget = true, ab = "Victory Rush",
	  test = function(x) return x.hostile and x.inMelee and x.ready("Victory Rush") and x.res("Victory Rush") end },

	-- 7. Bladestorm (Lv 60 iconic Arms cooldown — AoE 2+ mobs or healthy enemy!)
	{ id = "bladestorm", stage = 1, needTarget = true, ab = "Bladestorm",
	  test = function(x)
		return x.hostile and x.inMelee and x.inCombat and x.ready("Bladestorm") and x.res("Bladestorm")
			and (x.attackers >= 2 or x.tHealth > 0.65)
	  end },

	-- 8. Execute (Lv 24+ finisher at <=20% HP OR Sudden Death proc!)
	{ id = "execute", stage = 1, needTarget = true, ab = "Execute",
	  test = function(x) return x.hostile and x.inMelee and x.ready("Execute") and x.res("Execute") end },

	-- 9. Overpower (Lv 12+ huge Arms proc on dodge or Taste for Blood — 5 rage, +50% crit!)
	{ id = "overpower", stage = 1, needTarget = true, ab = "Overpower",
	  test = function(x) return x.hostile and x.inMelee and x.ready("Overpower") and x.res("Overpower") end },

	-- 10. Hamstring (slow Humanoid runners before they flee at ~15-20% HP and pull extra mobs!)
	{ id = "hamstring", stage = 1, needTarget = true, ab = "Hamstring",
	  test = function(x)
		return x.hostile and x.inMelee and x.inCombat and x.creatureType == "Humanoid"
			and x.tHealth <= 0.40 and x.tHealth > 0.08
			and (not x.targetHasDebuff("Hamstring"))
			and x.ready("Hamstring") and x.res("Hamstring")
	  end },

	-- 11. Rend (core Arms bleed & Taste for Blood engine — uses unitCaster == "player"!)
	{ id = "rend", stage = 1, needTarget = true, ab = "Rend",
	  test = function(x)
		return x.hostile and x.inMelee and (not x.myDebuffTarget("Rend"))
			and x.ready("Rend") and x.res("Rend") and x.tHealth > 0.25
	  end },

	-- 11. Mortal Strike (Lv 40+ signature Arms strike on 6s CD)
	{ id = "mortalstrike", stage = 1, needTarget = true, ab = "Mortal Strike",
	  test = function(x) return x.hostile and x.inMelee and x.ready("Mortal Strike") and x.res("Mortal Strike") end },

	-- 12. Thunder Clap (AoE 2+ attackers tracked via combat log, OR defensive slow when Logan HP < 75%)
	{ id = "thunderclap", stage = 1, needTarget = true, ab = "Thunder Clap",
	  test = function(x)
		return x.hostile and x.inMelee and x.inCombat and x.ready("Thunder Clap") and x.res("Thunder Clap")
			and (not x.targetHasDebuff("Thunder Clap"))
			and (x.attackers >= 2 or x.pHealth < 0.75)
	  end },

	-- 13. Heroic Strike (on-next-melee rage dump — checks IsCurrentSpell so it never sticks when already queued!)
	{ id = "heroicstrike", stage = 1, needTarget = true, ab = "Heroic Strike",
	  test = function(x)
		if not (x.hostile and x.inMelee and x.res("Heroic Strike")) then return false end
		if IsCurrentSpell("Heroic Strike") then return false end
		-- Conserve rage for Mortal Strike (30 rage) once learned at Lv 40; before Lv 40 dump freely!
		if x.knowsMortalStrike and x.rage < 45 then return false end
		return true
	  end },
}

-- ============ runtime state (not saved) ============
local st = {
	known = {},            -- spell name -> true, from spellbook
	knownIndex = {},       -- spell name -> highest-rank spellbook slot index (for PickupSpell)
	suggestion = nil,      -- current entry
	pending = nil,         -- { name = , t0 = } key suggestion waiting for press
	lastVoiceAt = -100,
	lastPraiseAt = -100,
	stars = 0,             -- stars this fight
	starFlash = 0,
	summaryLeft = 0,
	tickT = 0,
	markedByUs = false,
	moving = false,
	pendingKeys = false,
	pendingBars = false,
	pendingScale = false,
	scaled = false,
	newLeft = 0,
	attackers = {},        -- srcGUID -> GetTime() of last hostile swing/hit on Logan
}
local big, kb, tg, sumText, newPanel, arrows = {}, {}, {}, nil, nil, {}
local driver

-- ============ saved data helpers ============
local function cfg()
	if not LoganReadsPC then return nil end
	LoganReadsPC.coach = LoganReadsPC.coach or {}
	local c = LoganReadsPC.coach
	if c.on == nil then c.on = false end
	if c.keys == nil then c.keys = false end
	if c.frame == nil then c.frame = false end
	if not c.v2CleanFrame then
		c.frame = false
		c.v2CleanFrame = true
	end
	if c.scaleFrame == nil then c.scaleFrame = false end -- taint-free red glow ON by default; TargetFrame scaling opt-in
	if c.arrows == nil then c.arrows = false end
	if c.marks == nil then c.marks = false end
	c.stage = c.stage or "A"
	c.levels = c.levels or {}
	c.seen = c.seen or {}
	c.starsTotal = c.starsTotal or 0
	return c
end

local function msg(text)
	DEFAULT_CHAT_FRAME:AddMessage("LR coach: " .. text)
end

local function lrOn()
	return LoganReadsPC and LoganReadsPC.enabled == true
end

local function coachOn()
	local c = cfg()
	return lrOn() and c ~= nil and c.on == true
end

-- ============ voice (separate praise channel so fast presses ALWAYS get praised!) ============
local function say(name, isPraise)
	if not name then return end
	local have = LoganReads_Coach_Audio or {}
	if not have[name] then return end
	local t = GetTime()
	if isPraise then
		if t - st.lastPraiseAt < PRAISE_GAP then return end
		st.lastPraiseAt = t
	else
		if t - st.lastVoiceAt < VOICE_GAP then return end
		st.lastVoiceAt = t
	end
	pcall(PlaySoundFile, AUDIO_DIR .. name .. ".mp3")
end

local function effectiveLevel(name)
	local c = cfg()
	if c.override then return c.override end
	local L = c.levels[name]
	return (L and L.level) or 1
end

local function voiceFor(entry)
	if entry.voice then return entry.voice end
	local lvl = effectiveLevel(entry.name)
	if lvl == 1 then return "L1_" .. entry.slug end   -- "Press C! Charge!"
	if lvl == 2 then return "L2_" .. entry.slug end   -- "Charge!"
	return nil                                        -- level 3: icon + keyboard only (no voice)
end

-- ============ spellbook: which warrior abilities he knows (and highest-rank slot index) ============
local function scanKnown()
	local known, idx = {}, {}
	local tabs = GetNumSpellTabs() or 0
	for t = 1, tabs do
		local _, _, offset, num = GetSpellTabInfo(t)
		for i = (offset or 0) + 1, (offset or 0) + (num or 0) do
			local name = GetSpellName(i, BOOKTYPE_SPELL)
			if name then
				known[name] = true
				idx[name] = i -- later slot = highest rank in 3.3.5a spellbook
			end
		end
	end
	return known, idx
end

-- ============ the stats log (account-wide) ============
local function logKeyResult(name, secs)
	LoganReadsCoachLog = LoganReadsCoachLog or {}
	local rec = { date = date("%Y-%m-%d %H:%M:%S"), ability = name }
	if secs then
		rec.secs = math.floor(secs * 10 + 0.5) / 10
	else
		rec.secs = "missed"
	end
	table.insert(LoganReadsCoachLog, rec)
end

-- ============ PART 5: stage, level progress ============
local function progress(name)
	local c = cfg()
	c.levels[name] = c.levels[name] or { level = 1, presses = 0 }
	local L = c.levels[name]
	L.presses = L.presses + 1
	if L.presses >= LEVEL_UP_PRESSES and L.level < 3 then
		L.level = L.level + 1
		L.presses = 0
	end
end

-- ============ PART 2: the rules, evaluated from live state ============
local function pct(unit)
	local m = UnitHealthMax(unit)
	if not m or m == 0 then return 1 end
	return UnitHealth(unit) / m
end

local function readyNow(name)
	local start, dur = GetSpellCooldown(name)
	if not start or start == 0 then return true end
	if dur and dur <= 1.5 then return true end     -- only the global cooldown is running
	return (start + dur - GetTime()) <= 0
end

-- In 3.3.5a, IsUsableSpell returns (1, nil) when usable (including procs like Overpower/Victory Rush/Execute).
local function haveRage(name)
	local usable, noMana = IsUsableSpell(name)
	return (usable and not noMana) and true or false
end

local function playerHasBuff(name)
	for i = 1, 40 do
		local n = UnitBuff("player", i)
		if not n then return false end
		if n == name then return true end
	end
	return false
end

-- 3.3.5a UnitDebuff 8th return is unitCaster ("player", "party1", etc.), NOT a boolean!
local function myDebuffOnTarget(name)
	for i = 1, 40 do
		local n, _, _, _, _, _, _, unitCaster = UnitDebuff("target", i)
		if not n then return false end
		if n == name and unitCaster == "player" then return true end
	end
	return false
end

local function targetHasDebuff(name)
	for i = 1, 40 do
		local n = UnitDebuff("target", i)
		if not n then return false end
		if n == name then return true end
	end
	return false
end

local MELEE_PROBES = { "Rend", "Heroic Strike", "Overpower", "Mortal Strike", "Victory Rush", "Hamstring" }
local function inMeleeRange()
	for _, sp in ipairs(MELEE_PROBES) do
		if st.known[sp] then
			local r = IsSpellInRange(sp, "target")
			if r == 1 then return true end
			if r == 0 then return false end
		end
	end
	return CheckInteractDistance("target", 3) == 1
end

local function countAttackers()
	local now = GetTime()
	local cnt = 0
	for guid, ts in pairs(st.attackers) do
		if now - ts > ATTACKER_TTL then
			st.attackers[guid] = nil
		else
			cnt = cnt + 1
		end
	end
	return cnt
end

local function buildCtx()
	local x = {}
	x.inCombat = UnitAffectingCombat("player") and true or false
	x.hasTarget = (UnitExists("target") and not UnitIsDead("target")) and true or false
	x.hostile = x.hasTarget and UnitCanAttack("player", "target") and true or false
	x.inMelee = x.hostile and inMeleeRange() or false
	x.tHealth = x.hasTarget and pct("target") or 1
	x.pHealth = pct("player")
	x.creatureType = x.hasTarget and UnitCreatureType("target") or ""
	x.rage = UnitPower("player") or 0
	x.stance = GetShapeshiftForm() or 0
	x.knowsBattleStance = st.known["Battle Stance"] == true
	x.knowsMortalStrike = st.known["Mortal Strike"] == true
	x.eating = playerHasBuff("Food") or playerHasBuff("Drink") or playerHasBuff("Refreshment")
	x.attackers = x.inCombat and countAttackers() or 0
	x.ready = readyNow
	x.res = haveRage
	x.hasBuffPlayer = playerHasBuff
	x.myDebuffTarget = myDebuffOnTarget
	x.targetHasDebuff = targetHasDebuff
	return x
end

local function chooseSuggestion()
	local x = buildCtx()
	local c = cfg()
	local stage = STAGE_RANK[c.stage] or 1
	for _, r in ipairs(RULES) do
		local ok = r.enabled ~= false and stage >= r.stage and (not r.needTarget or x.hasTarget)
		if ok and r.ab and not st.known[r.ab] then ok = false end
		if ok and r.test(x) then
			return r.entry or BY_NAME[r.ab]
		end
	end
	return nil
end

-- ============ keys & action bars (PART 1): changes run out of combat only ============
-- Leaves 3 (Shoot), B (Bags), M (World Map), and W/A/S/D/Q/E completely untouched!
local BIND_ALL = { "C", "R", "T", "V", "H", "1", "2", "4", "5", "6" }

local function resolveActionSlot(ab)
	if ab.barButton then
		-- Only touch main Bar 1 slots when in Battle Stance (form 1) or unstanced (0)
		local form = GetShapeshiftForm() or 0
		if form ~= 0 and form ~= 1 then return nil end
		local btn = _G["ActionButton" .. ab.barButton]
		if btn and btn.action and btn.action > 0 then
			return btn.action
		end
		return (form == 1 and 72 or 0) + ab.barButton
	end
	return ab.fixedSlot
end

local function slotAlreadyHasSpell(slot, wantName)
	local aType, aId = GetActionInfo(slot)
	if aType ~= "spell" or not aId then return false end
	-- In 3.3.5a, GetActionInfo returns either a spellbook index or global spellID depending on version;
	-- check GetSpellName(aId, "spell") first and fall back to GetSpellInfo(aId).
	local nm = GetSpellName(aId, BOOKTYPE_SPELL)
	if nm == wantName then return true end
	local nm2 = GetSpellInfo and GetSpellInfo(aId) or nil
	return nm2 == wantName
end

local function syncActionBars()
	if InCombatLockdown() then
		st.pendingBars = true
		return 0
	end
	st.pendingBars = false
	if not (PickupSpell and PlaceAction and ClearCursor) then return 0 end
	local moved = 0
	for _, ab in ipairs(ABILITIES) do
		local sIdx = st.knownIndex[ab.name]
		local slot = sIdx and resolveActionSlot(ab) or nil
		if sIdx and slot and not slotAlreadyHasSpell(slot, ab.name) then
			ClearCursor()
			PickupSpell(sIdx, BOOKTYPE_SPELL)
			PlaceAction(slot)
			ClearCursor()
			moved = moved + 1
		end
	end
	return moved
end

local function applyKeys()
	if InCombatLockdown() then
		st.pendingKeys = true
		return
	end
	st.pendingKeys = false
	local c = cfg()
	if not c then return end
	local want = coachOn() and c.keys == true

	if want then
		-- 1. remember old bindings once (and top up any newly added keys if upgraded)
		if not c.savedBindings then
			c.savedBindings = {}
			c.origSet = GetCurrentBindingSet()
		end
		for _, k in ipairs(BIND_ALL) do
			if c.savedBindings[k] == nil then
				c.savedBindings[k] = GetBindingAction(k) or ""
			end
		end
		-- 2. switch to character-specific bindings so only Logan's character changes
		if GetCurrentBindingSet() ~= CHARACTER_SET then
			LoadBindings(CHARACTER_SET)
		end
		-- 3. bind ONLY known abilities that have a key (unlearned 4/5/6 stay default action bar!)
		for _, ab in ipairs(ABILITIES) do
			if ab.key and st.known[ab.name] then
				local b = _G["LoganReadsCoachKey" .. ab.key]
				if b then
					SetBinding(ab.key, "CLICK " .. b:GetName() .. ":LeftButton")
				end
			end
		end
		SaveBindings(CHARACTER_SET)
		-- 4. place known abilities visually onto their matching Battle Stance & BottomLeft bar slots!
		syncActionBars()
		return
	end

	-- keys OFF: put back every saved binding cleanly
	if c.savedBindings then
		for _, k in ipairs(BIND_ALL) do
			local old = c.savedBindings[k]
			if old ~= nil then
				if old ~= "" then SetBinding(k, old) else SetBinding(k) end
			end
		end
		local cur = GetCurrentBindingSet()
		SaveBindings(cur)
		if c.origSet and c.origSet ~= cur then
			LoadBindings(c.origSet)
		end
		c.savedBindings = nil
		c.origSet = nil
	end
end

-- macro text for each secure key button (set out of combat only)
-- Melee abilities include /startattack so pressing the key after Tab immediately swings!
local function makeKeyButtons()
	for _, ab in ipairs(ABILITIES) do
		if ab.key then
			local name = "LoganReadsCoachKey" .. ab.key
			if not _G[name] then
				local b = CreateFrame("Button", name, UIParent, "SecureActionButtonTemplate")
				b:SetAttribute("type", "macro")
				local mtext
				if ab.melee then
					mtext = "/startattack\n/cast " .. ab.name
				elseif ab.startAttackAfter then
					mtext = "/cast " .. ab.name .. "\n/startattack"
				else
					mtext = "/cast " .. ab.name
				end
				b:SetAttribute("macrotext", mtext)
				b:Hide()
			end
		end
	end
end

-- ============ the big icon (PART 2) ============
local function savePos(frame)
	local c = cfg()
	local p, _, rp, x, y = frame:GetPoint()
	c.pos = { p, rp, x, y }
end

local function edgeTextures(frame, thick)
	local e = {}
	e.top = frame:CreateTexture(nil, "OVERLAY")
	e.top:SetTexture(TEX_WHITE)
	e.top:SetPoint("TOPLEFT", frame, "TOPLEFT", 0, 0)
	e.top:SetPoint("TOPRIGHT", frame, "TOPRIGHT", 0, 0)
	e.top:SetHeight(thick)
	e.bottom = frame:CreateTexture(nil, "OVERLAY")
	e.bottom:SetTexture(TEX_WHITE)
	e.bottom:SetPoint("BOTTOMLEFT", frame, "BOTTOMLEFT", 0, 0)
	e.bottom:SetPoint("BOTTOMRIGHT", frame, "BOTTOMRIGHT", 0, 0)
	e.bottom:SetHeight(thick)
	e.left = frame:CreateTexture(nil, "OVERLAY")
	e.left:SetTexture(TEX_WHITE)
	e.left:SetPoint("TOPLEFT", frame, "TOPLEFT", 0, 0)
	e.left:SetPoint("BOTTOMLEFT", frame, "BOTTOMLEFT", 0, 0)
	e.left:SetWidth(thick)
	e.right = frame:CreateTexture(nil, "OVERLAY")
	e.right:SetTexture(TEX_WHITE)
	e.right:SetPoint("TOPRIGHT", frame, "TOPRIGHT", 0, 0)
	e.right:SetPoint("BOTTOMRIGHT", frame, "BOTTOMRIGHT", 0, 0)
	e.right:SetWidth(thick)
	return e
end

local function setEdgeColor(e, r, g, b, a)
	for _, side in ipairs({ "top", "bottom", "left", "right" }) do
		e[side]:SetVertexColor(r, g, b, a or 1)
	end
end

-- keyboard picture: 1-6 number row + 3 letter rows + Tab; the suggested key is lit!
local KB_ROWS = {
	{ 30, { "1", "2", "3", "4", "5", "6" } },
	{ 44, { "Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P" } },
	{ 58, { "A", "S", "D", "F", "G", "H", "J", "K", "L" } },
	{ 74, { "Z", "X", "C", "V", "B", "N", "M" } },
}
local KEY_STEP, KEY_SIZE = 29, 26

local function buildKeyboard(parent)
	kb.frame = CreateFrame("Frame", "LoganReadsCoachKeyboard", parent)
	kb.frame:SetWidth(336)
	kb.frame:SetHeight(124)
	kb.frame:SetPoint("LEFT", parent, "RIGHT", 16, 0)
	kb.keys = {}
	local function key(letter, x, y, w)
		local t = kb.frame:CreateTexture(nil, "BACKGROUND")
		t:SetTexture(TEX_WHITE)
		t:SetWidth(w)
		t:SetHeight(KEY_SIZE)
		t:SetPoint("TOPLEFT", kb.frame, "TOPLEFT", x, -y)
		local fs = kb.frame:CreateFontString(nil, "OVERLAY")
		fs:SetFont(FONT_PATH, 14, "OUTLINE")
		fs:SetPoint("CENTER", t, "CENTER", 0, 0)
		fs:SetText(letter)
		kb.keys[letter] = { tex = t, fs = fs }
	end
	key("TAB", 0, 4 + (KEY_SIZE + 4), 40) -- aligns TAB with the Q-W-E row (row 2)
	local y = 4
	for r, row in ipairs(KB_ROWS) do
		local x0 = row[1]
		y = 4 + (r - 1) * (KEY_SIZE + 4)
		for i, letter in ipairs(row[2]) do
			key(letter, x0 + (i - 1) * KEY_STEP, y, KEY_SIZE)
		end
	end
end

local function updateKeyboard(entry)
	if not kb.keys then return end
	local lit = entry and entry.key
	for letter, k in pairs(kb.keys) do
		if letter == lit then
			local col = entry.color
			k.tex:SetVertexColor(col[1], col[2], col[3], 1)
			k.fs:SetTextColor(0, 0, 0, 1)
		else
			k.tex:SetVertexColor(0.25, 0.25, 0.30, 0.9)
			k.fs:SetTextColor(0.8, 0.8, 0.8, 1)
		end
	end
end

local function buildBig()
	local f = CreateFrame("Frame", "LoganReadsCoachFrame", UIParent)
	f:SetWidth(BIG_SIZE)
	f:SetHeight(BIG_SIZE)
	f:SetFrameStrata("MEDIUM")
	f:SetClampedToScreen(true)
	f:SetMovable(true)
	f:EnableMouse(false)
	f:RegisterForDrag("LeftButton")
	f:SetScript("OnDragStart", function(self)
		if st.moving then self:StartMoving() end
	end)
	f:SetScript("OnDragStop", function(self)
		self:StopMovingOrSizing()
		savePos(self)
	end)
	local c = cfg()
	f:ClearAllPoints()
	if c and c.pos then
		f:SetPoint(c.pos[1], UIParent, c.pos[2], c.pos[3], c.pos[4])
	else
		f:SetPoint("BOTTOM", UIParent, "BOTTOM", 0, 170)
	end
	big.frame = f

	big.icon = f:CreateTexture(nil, "ARTWORK")
	big.icon:SetPoint("TOPLEFT", f, "TOPLEFT", 8, -8)
	big.icon:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -8, 8)
	big.icon:SetTexCoord(0.07, 0.93, 0.07, 0.93)

	big.edge = edgeTextures(f, 8)

	big.star = f:CreateTexture(nil, "OVERLAY")
	big.star:SetTexture(TEX_STAR)
	big.star:SetBlendMode("ADD")
	big.star:SetWidth(96)
	big.star:SetHeight(96)
	big.star:SetPoint("CENTER", f, "CENTER", 0, 0)
	big.star:SetAlpha(0)

	big.letter = f:CreateFontString(nil, "OVERLAY")
	big.letter:SetFont(FONT_PATH, 72, "OUTLINE")
	big.letter:SetPoint("CENTER", f, "CENTER", 0, 0)
	big.letter:SetTextColor(1, 1, 1, 1)

	big.name = f:CreateFontString(nil, "OVERLAY")
	big.name:SetFont(FONT_PATH, 20, "OUTLINE")
	big.name:SetPoint("TOP", f, "BOTTOM", 0, -6)
	big.name:SetTextColor(1, 1, 1, 1)

	buildKeyboard(f)
	f:Hide()
end

local function showBig(entry)
	if not big.frame then return end
	local moving = st.moving
	if not entry and not moving then
		big.frame:Hide()
		return
	end
	big.frame:Show()
	if not entry then
		big.icon:SetTexture(TEX_WHITE)
		big.icon:SetVertexColor(0.2, 0.2, 0.2, 0.6)
		big.letter:SetText("")
		big.name:SetText("Drag me (/lr coach move)")
		setEdgeColor(big.edge, 0.6, 0.6, 0.6, 0.8)
		updateKeyboard(nil)
		return
	end
	big.icon:SetVertexColor(1, 1, 1, 1)
	big.icon:SetTexture(entry.icon or GetSpellTexture(entry.name) or TEX_WHITE)
	if entry.key == "TAB" then
		big.letter:SetFont(FONT_PATH, 40, "OUTLINE")
	else
		big.letter:SetFont(FONT_PATH, 72, "OUTLINE")
	end
	big.letter:SetText(entry.key or "")
	big.name:SetText(entry.name)
	setEdgeColor(big.edge, entry.color[1], entry.color[2], entry.color[3], 1)
	updateKeyboard(entry)
end

-- ============ "N stars!" after combat, and the star flash ============
local function buildSummary()
	local f = CreateFrame("Frame", "LoganReadsCoachSummary", UIParent)
	f:SetWidth(400)
	f:SetHeight(60)
	f:SetPoint("CENTER", UIParent, "CENTER", 0, 150)
	sumText = f:CreateFontString(nil, "OVERLAY")
	sumText:SetFont(FONT_PATH, 40, "OUTLINE")
	sumText:SetPoint("CENTER", f, "CENTER", 0, 0)
	sumText:SetTextColor(1, 0.9, 0.2, 1)
	f:Hide()
	sumText.frame = f
end

local function showSummary(n)
	if not sumText or n <= 0 then return end
	sumText:SetText(n .. (n == 1 and " star!" or " stars!"))
	sumText.frame:Show()
	st.summaryLeft = 3
end

-- ============ the one-time "New power!" panel ============
local function buildNewPanel()
	local f = CreateFrame("Button", "LoganReadsCoachNewPower", UIParent)
	f:SetWidth(340)
	f:SetHeight(110)
	f:SetPoint("CENTER", UIParent, "CENTER", 0, 60)
	f:SetFrameStrata("DIALOG")
	f:SetScript("OnClick", function(self) self:Hide() end)
	newPanel = f
	f.bg = f:CreateTexture(nil, "BACKGROUND")
	f.bg:SetAllPoints(f)
	f.bg:SetTexture(TEX_WHITE)
	f.bg:SetVertexColor(0.06, 0.08, 0.12, 0.92)
	f.icon = f:CreateTexture(nil, "ARTWORK")
	f.icon:SetWidth(64)
	f.icon:SetHeight(64)
	f.icon:SetPoint("LEFT", f, "LEFT", 12, 0)
	f.edge = edgeTextures(f, 4)
	f.title = f:CreateFontString(nil, "OVERLAY")
	f.title:SetFont(FONT_PATH, 26, "OUTLINE")
	f.title:SetPoint("TOPLEFT", f.icon, "TOPRIGHT", 14, -8)
	f.title:SetText("New power!")
	f.letter = f:CreateFontString(nil, "OVERLAY")
	f.letter:SetFont(FONT_PATH, 22, "OUTLINE")
	f.letter:SetPoint("TOPLEFT", f.title, "BOTTOMLEFT", 0, -6)
	f:Hide()
end

local function showNewPanel(ab)
	if not newPanel then return end
	newPanel.icon:SetTexture(GetSpellTexture(ab.name) or TEX_WHITE)
	setEdgeColor(newPanel.edge, ab.color[1], ab.color[2], ab.color[3], 1)
	newPanel.letter:SetText(ab.key and ("Press " .. ab.key .. " for " .. ab.name) or ("Click " .. ab.name))
	newPanel.letter:SetTextColor(ab.color[1], ab.color[2], ab.color[3], 1)
	newPanel:Show()
	st.newLeft = 8
	say("newpower", true)
end

-- ============ learn detection: SPELLS_CHANGED / LEARNED_SPELL_IN_TAB ============
local function refreshKnown(silent)
	local fresh, freshIdx = scanKnown()
	local c = cfg()
	local learned = {}
	for _, ab in ipairs(ABILITIES) do
		if fresh[ab.name] and not st.known[ab.name] then table.insert(learned, ab) end
	end
	st.known = fresh
	st.knownIndex = freshIdx
	if not c then return end
	if silent then
		for _, ab in ipairs(ABILITIES) do
			if fresh[ab.name] then c.seen[ab.name] = true end
		end
		return
	end
	for _, ab in ipairs(learned) do
		if not c.seen[ab.name] then
			c.seen[ab.name] = true
			if coachOn() then showNewPanel(ab) end
		end
	end
	if #learned > 0 then applyKeys() end
end

-- ============ PART 4a: target frame glow (taint-free) & optional scale ============
local function buildTargetGlow()
	if not TargetFrame then return end
	tg.frame = CreateFrame("Frame", "LoganReadsTargetGlow", TargetFrame)
	tg.frame:SetPoint("TOPLEFT", TargetFrame, "TOPLEFT", -6, 6)
	tg.frame:SetPoint("BOTTOMRIGHT", TargetFrame, "BOTTOMRIGHT", 6, -6)
	tg.frame:Hide()
	tg.edge = edgeTextures(tg.frame, 3)
end

local function setTargetScale(on)
	if not TargetFrame then return end
	local c = cfg()
	if not (c and c.scaleFrame) then
		on = false
	end
	if InCombatLockdown() then
		st.pendingScale = true
		return
	end
	st.pendingScale = false
	if st.scaled ~= on then
		TargetFrame:SetScale(on and TARGET_SCALE or 1)
		st.scaled = on
	end
end

local function updateTargetHighlight()
	if not tg.frame then return end
	local c = cfg()
	local active = coachOn() and c.frame and UnitExists("target") and not UnitIsDead("target")
	if not active then
		tg.frame:Hide()
		setTargetScale(false)
		return
	end
	local hostile = UnitCanAttack("player", "target") and true or false
	local outOfRange = hostile and (not inMeleeRange())
	if hostile and not outOfRange then
		setEdgeColor(tg.edge, 1.0, 0.25, 0.25, 0.9)
	else
		setEdgeColor(tg.edge, 0.55, 0.55, 0.55, 0.9)   -- friendly or out of range: grey
	end
	tg.frame:Show()
	setTargetScale(hostile)
end

-- ============ PART 4b: skull mark on hostile mob ============
local function markTest()
	if not UnitExists("target") then
		msg("mark test: select a mob first, then type /lr coach mark test.")
		return
	end
	local before = GetRaidTargetIndex("target")
	local ok, err = pcall(SetRaidTarget, "target", SKULL)
	local after = GetRaidTargetIndex("target")
	msg("mark test: before=" .. tostring(before) .. " call=" .. (ok and "ok" or ("error " .. tostring(err)))
		.. " after=" .. tostring(after) .. " (skull is 8; check the skull over the mob)")
	local t0 = GetTime()
	st.delayed = { at = t0 + 0.5, fn = function()
		msg("mark test (later): raid target now=" .. tostring(GetRaidTargetIndex("target")))
	end }
end

local function updateMark()
	local c = cfg()
	if not (coachOn() and c.marks) then return end
	if not (UnitExists("target") and UnitCanAttack("player", "target")) or UnitIsPlayer("target") then
		st.markedByUs = false
		return
	end
	if UnitIsDead("target") then
		if st.markedByUs and GetRaidTargetIndex("target") == SKULL then
			pcall(SetRaidTarget, "target", 0)
		end
		st.markedByUs = false
		return
	end
	if GetRaidTargetIndex("target") ~= SKULL then
		local ok = pcall(SetRaidTarget, "target", SKULL)
		st.markedByUs = ok
	end
end

-- ============ PART 4c: nameplate arrows (works in 1v1 and multi-mob fights!) ============
local function buildArrows()
	local f = CreateFrame("Frame", "LoganReadsArrows", UIParent)
	f:SetFrameStrata("HIGH")
	f:Hide()
	arrows.frame = f
	arrows.left = f:CreateFontString(nil, "OVERLAY")
	arrows.left:SetFont(FONT_PATH, 28, "OUTLINE")
	arrows.left:SetTextColor(1, 0.9, 0.2, 1)
	arrows.left:SetText("<")
	arrows.right = f:CreateFontString(nil, "OVERLAY")
	arrows.right:SetFont(FONT_PATH, 28, "OUTLINE")
	arrows.right:SetTextColor(1, 0.9, 0.2, 1)
	arrows.right:SetText(">")
	arrows.edge = edgeTextures(f, 2)
	f:SetScript("OnUpdate", function(self)
		if not arrows.bar then return end
		local off = math.sin(GetTime() * 6) * 4
		arrows.left:ClearAllPoints()
		arrows.left:SetPoint("RIGHT", arrows.bar, "LEFT", -6 - off, 0)
		arrows.right:ClearAllPoints()
		arrows.right:SetPoint("LEFT", arrows.bar, "RIGHT", 6 + off, 0)
	end)
end

-- The target's plate is the one at full alpha (works in both 1v1 and multi-mob fights).
local function findTargetBar()
	if not UnitExists("target") or not WorldFrame then return nil end
	local fullBar, fullCount = nil, 0
	for _, f in ipairs({ WorldFrame:GetChildren() }) do
		if f:IsShown() and not f:GetName() then
			local bar
			for _, ch in ipairs({ f:GetChildren() }) do
				if ch:GetObjectType() == "StatusBar" then bar = ch break end
			end
			if bar and f:GetAlpha() >= 0.99 then
				fullBar, fullCount = bar, fullCount + 1
			end
		end
	end
	if fullCount == 1 then return fullBar end
	return nil
end

local function updateArrows()
	local c = cfg()
	if not c or not arrows.frame then return end
	if not (coachOn() and c.arrows) then
		if arrows.frame then arrows.frame:Hide() end
		arrows.bar = nil
		return
	end
	local bar = findTargetBar()
	if not bar then
		if arrows.frame then arrows.frame:Hide() end
		arrows.bar = nil
		return
	end
	arrows.bar = bar
	arrows.frame:ClearAllPoints()
	arrows.frame:SetPoint("TOPLEFT", bar, "TOPLEFT", -2, 2)
	arrows.frame:SetPoint("BOTTOMRIGHT", bar, "BOTTOMRIGHT", 2, -2)
	setEdgeColor(arrows.edge, 1, 0.9, 0.2, 1)
	arrows.frame:Show()
end

local function setNameplateCVar(on)
	local c = cfg()
	if on then
		if c.prevNameplate == nil then
			c.prevNameplate = GetCVarBool("nameplateShowEnemies") and "1" or "0"
		end
		SetCVar("nameplateShowEnemies", "1")
	else
		if c.prevNameplate ~= nil then
			SetCVar("nameplateShowEnemies", c.prevNameplate)
			c.prevNameplate = nil
		end
	end
end

-- ============ suggestion changes: voice, log, big icon ============
local function refreshSuggestion()
	local new = nil
	if coachOn() then new = chooseSuggestion() end
	local newName = new and new.name or nil
	local oldName = st.suggestion and st.suggestion.name or nil
	if newName == oldName then return end

	-- the old key suggestion did not get pressed first: "missed"
	if st.pending and st.pending.name ~= newName then
		logKeyResult(st.pending.name, nil)
	end
	st.pending = nil
	st.suggestion = new
	if new then
		if new.slug then
			st.pending = { name = new.name, t0 = GetTime() }
		end
		local line = voiceFor(new)
		say(line, false)
	end
	showBig(new)
end

-- ============ correct press: he cast (or queued Heroic Strike!) the suggested spell ============
local function creditSuggestion(spellName)
	local s = st.suggestion
	if not s or not s.slug or spellName ~= s.name then return end
	local secs = 0
	if st.pending and st.pending.name == s.name then
		secs = GetTime() - st.pending.t0
	else
		return -- already credited for this suggestion
	end
	st.pending = nil
	logKeyResult(s.name, secs)
	progress(s.name)
	st.stars = st.stars + 1
	local c = cfg()
	c.starsTotal = c.starsTotal + 1
	st.starFlash = 0.6
	pcall(PlaySound, SND_CHIME)
	say("praise" .. tostring(math.random(1, 3)), true)
end

local function onSpellSucceeded(unit, spellName)
	if unit ~= "player" or not spellName then return end
	if not coachOn() then return end
	creditSuggestion(spellName)
end

local function onCurrentSpellChanged()
	if not coachOn() then return end
	if st.pending and st.pending.name == "Heroic Strike" and IsCurrentSpell("Heroic Strike") then
		creditSuggestion("Heroic Strike")
		refreshSuggestion()
	end
end

-- Track hostile mobs actively swinging at / damaging Logan for AoE Thunder Clap & Bladestorm rules
local function onCombatLog(timestamp, subEvent, srcGUID, srcName, srcFlags, dstGUID)
	if not subEvent or not srcGUID or not dstGUID then return end
	local myGuid = UnitGUID("player")
	if not myGuid or dstGUID ~= myGuid or srcGUID == myGuid then return end
	if subEvent == "SWING_DAMAGE" or subEvent == "SWING_MISSED"
		or subEvent == "SPELL_DAMAGE" or subEvent == "SPELL_MISSED"
		or subEvent == "RANGE_DAMAGE" or subEvent == "RANGE_MISSED" then
		st.attackers[srcGUID] = GetTime()
	end
end

-- ============ time-based visuals ============
local function timers(elapsed)
	if st.summaryLeft > 0 then
		st.summaryLeft = st.summaryLeft - elapsed
		if st.summaryLeft <= 0 and sumText then sumText.frame:Hide() end
	end
	if st.newLeft > 0 then
		st.newLeft = st.newLeft - elapsed
		if st.newLeft <= 0 and newPanel then newPanel:Hide() end
	end
	if st.starFlash > 0 then
		st.starFlash = st.starFlash - elapsed
		if big.star then big.star:SetAlpha(math.max(st.starFlash, 0) / 0.6) end
	end
	if big.edge and st.suggestion and big.frame and big.frame:IsShown() then
		local a = 0.6 + 0.4 * math.abs(math.sin(GetTime() * 3))
		setEdgeColor(big.edge, st.suggestion.color[1], st.suggestion.color[2], st.suggestion.color[3], a)
	end
	if st.delayed and GetTime() >= st.delayed.at then
		local f = st.delayed.fn
		st.delayed = nil
		f()
	end
end

local function tick()
	local c = cfg()
	if not c then return end
	if not coachOn() then
		if big.frame and not st.moving then big.frame:Hide() end
		if st.suggestion then
			if st.pending then logKeyResult(st.pending.name, nil) end
			st.pending = nil
			st.suggestion = nil
		end
		updateTargetHighlight()
		updateArrows()
		return
	end
	refreshSuggestion()
	if st.moving and not st.suggestion then showBig(nil) end
	updateTargetHighlight()
	updateMark()
	updateArrows()
end

-- ============ events ============
local ev = CreateFrame("Frame", "LoganReadsCoachEvents", UIParent)
local EVENTS = {
	"ADDON_LOADED", "PLAYER_ENTERING_WORLD", "SPELLS_CHANGED", "LEARNED_SPELL_IN_TAB",
	"PLAYER_TARGET_CHANGED", "PLAYER_REGEN_DISABLED", "PLAYER_REGEN_ENABLED",
	"UNIT_SPELLCAST_SUCCEEDED", "CURRENT_SPELL_CAST_CHANGED", "COMBAT_LOG_EVENT_UNFILTERED",
}

ev:SetScript("OnEvent", function(self, event, a1, a2, a3, a4, a5, a6)
	local ok, err = pcall(function()
		if event == "ADDON_LOADED" then
			if a1 ~= "LoganReads" then return end
			cfg()
			makeKeyButtons()
			buildBig()
			buildSummary()
			buildNewPanel()
			buildTargetGlow()
			buildArrows()
		elseif event == "PLAYER_ENTERING_WORLD" then
			refreshKnown(true)
			applyKeys()
			updateTargetHighlight()
		elseif event == "SPELLS_CHANGED" or event == "LEARNED_SPELL_IN_TAB" then
			refreshKnown(false)
		elseif event == "PLAYER_TARGET_CHANGED" then
			st.markedByUs = false
		elseif event == "PLAYER_REGEN_DISABLED" then
			st.stars = 0
			st.attackers = {}
		elseif event == "PLAYER_REGEN_ENABLED" then
			st.attackers = {}
			if coachOn() then showSummary(st.stars) end
			if st.pendingKeys then applyKeys() end
			if st.pendingBars then syncActionBars() end
			if st.pendingScale then updateTargetHighlight() end
		elseif event == "UNIT_SPELLCAST_SUCCEEDED" then
			onSpellSucceeded(a1, a2)
		elseif event == "CURRENT_SPELL_CAST_CHANGED" then
			onCurrentSpellChanged()
		elseif event == "COMBAT_LOG_EVENT_UNFILTERED" then
			onCombatLog(a1, a2, a3, a4, a5, a6)
		end
	end)
	if not ok then
		DEFAULT_CHAT_FRAME:AddMessage("LR coach: ERROR: " .. tostring(err))
	end
end)
for _, e in ipairs(EVENTS) do ev:RegisterEvent(e) end

driver = CreateFrame("Frame", "LoganReadsCoachDriver", UIParent)
driver:SetScript("OnUpdate", function(self, elapsed)
	timers(elapsed)
	st.tickT = st.tickT + elapsed
	if st.tickT >= 0.2 then
		st.tickT = 0
		local ok, err = pcall(tick)
		if not ok and not st.errShown then
			st.errShown = true
			DEFAULT_CHAT_FRAME:AddMessage("LR coach: ERROR: " .. tostring(err))
		end
	end
end)

-- ============ called by LoganReads.lua after /lr on and /lr off ============
function C.refresh()
	cfg()
	applyKeys()
	if not coachOn() and st.suggestion then
		st.suggestion = nil
		st.pending = nil
	end
	if big.frame and not coachOn() and not st.moving then big.frame:Hide() end
	updateTargetHighlight()
	updateArrows()
end

-- ============ commands: /lr coach ... ============
local function levelLine()
	local parts = {}
	for _, ab in ipairs(ABILITIES) do
		if st.known[ab.name] then
			local L = cfg().levels[ab.name]
			local presses = L and L.presses or 0
			local kLabel = ab.key or "click"
			table.insert(parts, ab.name .. "(" .. kLabel .. ") L" .. effectiveLevel(ab.name) .. " " .. presses .. "/" .. LEVEL_UP_PRESSES)
		end
	end
	return table.concat(parts, "; ")
end

local HELP = "/lr coach on|off, keys on|off, bars, move, level <1|2|3|auto>, stage <A|B>, mark test|on|off, arrows on|off, frame on|off, status, reset"

function C.command(rest)
	local c = cfg()
	if not c then
		msg("LoganReads is not loaded yet.")
		return
	end
	local cmd, arg = string.match(rest or "", "^(%S*)%s*(%S*)")
	cmd, arg = cmd or "", arg or ""

	if cmd == "on" then
		c.on = true
		if not lrOn() then msg("the coach is ON, but LoganReads is OFF. Type /lr on.") end
		C.refresh()
		msg("coach ON" .. (lrOn() and "" or " (waiting for /lr on)"))
	elseif cmd == "off" then
		c.on = false
		C.refresh()
		msg("coach OFF.")
	elseif cmd == "keys" then
		if arg == "on" then
			c.keys = true
		elseif arg == "off" then
			c.keys = false
		else
			msg("keys is " .. (c.keys and "ON" or "OFF") .. ". Use /lr coach keys on|off.")
			return
		end
		applyKeys()
		msg("keys " .. (c.keys and "ON" or "OFF") .. (InCombatLockdown() and " (will change after combat)" or ""))
	elseif cmd == "bars" then
		refreshKnown(true)
		local n = syncActionBars()
		if InCombatLockdown() then
			msg("bars will arrange as soon as combat ends.")
		else
			msg("arranged action bars (" .. tostring(n) .. " slot" .. (n == 1 and "" or "s") .. " updated; slot 3 Shoot untouched).")
		end
	elseif cmd == "move" then
		st.moving = not st.moving
		if big.frame then
			big.frame:EnableMouse(st.moving)
			big.frame:Show()
		end
		if not st.moving and big.frame then
			big.frame:EnableMouse(false)
			savePos(big.frame)
			C.refresh()
		end
		msg(st.moving and "drag the icon, then type /lr coach move again to lock it." or "icon locked and saved.")
	elseif cmd == "level" then
		if arg == "auto" then
			c.override = nil
		elseif arg == "1" or arg == "2" or arg == "3" then
			c.override = tonumber(arg)
		else
			msg("level is " .. (c.override and tostring(c.override) or "auto") .. ". Use /lr coach level 1|2|3|auto.")
			return
		end
		msg("levels: " .. levelLine())
	elseif cmd == "stage" then
		local s = string.upper(arg)
		if s == "A" or s == "B" then
			c.stage = s
			msg("stage " .. s .. (s == "A" and " (keys only)" or " (keys + Tab + Eat + Move Closer + Battle Stance)"))
		elseif s == "C" or s == "D" then
			c.stage = s
			msg("stage " .. s .. " behaves like stage B for now.")
		else
			msg("stage is " .. c.stage .. ". Use /lr coach stage A|B.")
			return
		end
		C.refresh()
	elseif cmd == "mark" then
		if arg == "test" then
			markTest()
		elseif arg == "on" then
			c.marks = true
			msg("skull on hostile mobs is ON (only after 'mark test' works in game).")
		elseif arg == "off" then
			c.marks = false
			msg("skull marks OFF.")
		else
			msg("mark test | mark on | mark off")
		end
	elseif cmd == "arrows" then
		if arg == "on" then
			c.arrows = true
			setNameplateCVar(true)
		elseif arg == "off" then
			c.arrows = false
			setNameplateCVar(false)
		else
			msg("arrows is " .. (c.arrows and "ON" or "OFF") .. ". Use /lr coach arrows on|off.")
			return
		end
		msg("arrows " .. (c.arrows and "ON" or "OFF") .. " (nameplates are switched on/off with it).")
	elseif cmd == "frame" then
		if arg == "on" then
			c.frame = true
		elseif arg == "off" then
			c.frame = false
		else
			msg("frame is " .. (c.frame and "ON" or "OFF") .. ". Use /lr coach frame on|off.")
			return
		end
		C.refresh()
		msg("target frame highlight " .. (c.frame and "ON" or "OFF") .. ".")
	elseif cmd == "status" or cmd == "" then
		msg("coach " .. (c.on and "ON" or "OFF") .. ", LoganReads " .. (lrOn() and "ON" or "OFF")
			.. ", keys " .. (c.keys and "ON" or "OFF") .. ", stage " .. c.stage
			.. ", frame " .. (c.frame and "ON" or "OFF") .. ", arrows " .. (c.arrows and "ON" or "OFF")
			.. ", marks " .. (c.marks and "ON" or "OFF") .. ", stars total " .. c.starsTotal)
		msg("known: " .. levelLine())
		if cmd == "" then msg(HELP) end
	elseif cmd == "reset" then
		c.levels = {}
		c.override = nil
		c.starsTotal = 0
		st.stars = 0
		msg("star and level data cleared.")
	else
		msg(HELP)
	end
end
