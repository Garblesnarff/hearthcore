-- LoganReads: replaces displayed quest text with early-reader text.
-- Only text is changed. Quest IDs, counts, items, rewards and XP are untouched.
-- Anchored to the RUNNING 3.3.5a UI: all text flows through QuestInfo_Display.
-- OFF by default on every character (the account name is not proven, see README).
-- Type "/lr on" once on each of Logan's characters.

local ADDON_NAME = "LoganReads"
local FONT_PATH = "Fonts\\ARIALN.TTF"

local COL_TITLE = { 0.20, 0.10, 0.05 }
local COL_BODY  = { 0.12, 0.07, 0.03 }

local buttons = {}
local dbg = { offer = 0, progress = 0, reward = 0, log = 0, lastTitle = "" }
local savedStyle = {}

-- audio manifest (generated file): "id_field" -> true
local haveAudio = LoganReads_Audio_Files or {}

local function audioExists(id, field)
	return haveAudio[tostring(id) .. "_" .. field] == true
end

-- HELP LOG: one record per "Hear it" press, saved account-wide in LoganReadsLog
local function logHearIt(row, field)
	LoganReadsLog = LoganReadsLog or {}
	table.insert(LoganReadsLog, {
		date = date("%Y-%m-%d %H:%M:%S"),
		quest = row.id,
		field = field,
		text = row[field] or "",
	})
end

local function playAudio(row, field)
	pcall(logHearIt, row, field)
	pcall(PlaySoundFile, "Interface\\AddOns\\LoganReads\\audio\\" .. tostring(row.id) .. "_" .. field .. ".mp3")
end

local function active()
	return LoganReadsPC and LoganReadsPC.enabled
end

local function findRow(title)
	if not title then return nil end
	local row = LoganReads_Data[title]
	if row then return row end
	local cut = string.find(title, " - ", 1, true)
	if cut then
		return LoganReads_Data[string.sub(title, 1, cut - 1)]
	end
	return nil
end

-- save/restore original look so non-table quests render untouched
local function style(fs, size, col)
	if not fs then return end
	local name = fs:GetName()
	if name and not savedStyle[name] then
		local fpath, fsize, flags = fs:GetFont()
		local r, g, b, a = fs:GetTextColor()
		savedStyle[name] = { fpath = fpath, fsize = fsize, flags = flags, r = r, g = g, b = b, a = a }
	end
	fs:SetFont(FONT_PATH, size)
	fs:SetTextColor(col[1], col[2], col[3])
end

local function unstyle(fs)
	if not fs then return end
	local name = fs:GetName()
	local s = savedStyle[name]
	if s then
		fs:SetFont(s.fpath, s.fsize, s.flags)
		fs:SetTextColor(s.r, s.g, s.b, s.a)
	end
end

local function setButton(key, parent, anchorTo, row, field)
	local btn = buttons[key]
	if not btn then return end
	if row and anchorTo and audioExists(row.id, field) then
		if btn:GetParent() ~= parent then
			btn:SetParent(parent)
		end
		btn:ClearAllPoints()
		btn:SetPoint("TOPLEFT", anchorTo, "BOTTOMLEFT", 0, -12)
		btn.row = row
		btn.field = field
		btn:Show()
	else
		btn.row = nil
		btn.field = nil
		btn:Hide()
	end
end

local function makeButton(key)
	local btn = CreateFrame("Button", nil, UIParent, "UIPanelButtonTemplate")
	btn:SetWidth(96)
	btn:SetHeight(30)
	btn:SetText("Hear it")
	btn:SetScript("OnClick", function(self)
		if self.row and self.field then
			playAudio(self.row, self.field)
		end
	end)
	btn:Hide()
	buttons[key] = btn
end

-- ============ one hook drives offer + reward + quest log ============

local function refreshQuestInfo(template, parentFrame)
	local ok, err = pcall(function()
		local title = QuestInfoTitleHeader and QuestInfoTitleHeader:GetText()
		local row = active() and findRow(title)
		if not row then
			unstyle(QuestInfoTitleHeader)
			unstyle(QuestInfoDescriptionText)
			unstyle(QuestInfoObjectivesText)
			unstyle(QuestInfoRewardText)
			for _, key in pairs({ "offer", "reward", "log" }) do
				buttons[key]:Hide()
			end
			return
		end
		QuestInfoTitleHeader:SetText(row.title)
		style(QuestInfoTitleHeader, 28, COL_TITLE)
		if QuestInfoDescriptionText then
			QuestInfoDescriptionText:SetText(row.story)
			style(QuestInfoDescriptionText, 20, COL_BODY)
		end
		if QuestInfoObjectivesText then
			QuestInfoObjectivesText:SetText(row.todo)
			style(QuestInfoObjectivesText, 20, COL_BODY)
		end
		if parentFrame == _G["QuestRewardScrollChildFrame"] then
			dbg.reward = dbg.reward + 1
			QuestInfoRewardText:SetText(row.done)
			style(QuestInfoRewardText, 20, COL_BODY)
			setButton("reward", parentFrame, QuestInfoRewardText, row, "done")
			buttons.offer:Hide()
			buttons.log:Hide()
		elseif parentFrame == _G["QuestLogDetailScrollChildFrame"] then
			dbg.log = dbg.log + 1
			setButton("log", parentFrame, QuestInfoObjectivesText, row, "story")
			buttons.offer:Hide()
			buttons.reward:Hide()
		elseif parentFrame == _G["QuestDetailScrollChildFrame"] then
			dbg.offer = dbg.offer + 1
			dbg.lastTitle = row.origTitle
			setButton("offer", parentFrame, QuestInfoObjectivesText, row, "story")
			buttons.reward:Hide()
			buttons.log:Hide()
		end
	end)
	if not ok then
		DEFAULT_CHAT_FRAME:AddMessage("LR: ERROR: " .. tostring(err))
	end
end

-- ============ progress window (its own layout) ============

local function refreshProgress()
	local ok, err = pcall(function()
		local title = QuestProgressTitleText and QuestProgressTitleText:GetText()
		local row = active() and findRow(title)
		if not row then
			unstyle(QuestProgressTitleText)
			unstyle(QuestProgressText)
			buttons.progress:Hide()
			return
		end
		dbg.progress = dbg.progress + 1
		QuestProgressTitleText:SetText(row.title)
		style(QuestProgressTitleText, 28, COL_TITLE)
		if row.notDone and row.notDone ~= "" then
			QuestProgressText:SetText(row.notDone)
			style(QuestProgressText, 20, COL_BODY)
		else
			unstyle(QuestProgressText)
		end
		setButton("progress", QuestProgressScrollChildFrame, QuestProgressText,
			(row.notDone and row.notDone ~= "") and row or nil, "notDone")
	end)
	if not ok then
		DEFAULT_CHAT_FRAME:AddMessage("LR: ERROR: " .. tostring(err))
	end
end

-- ============ quiet mode (default on, but only while LoganReads is on) ============

local QUIET_CHANNELS = { "General", "Trade", "LocalDefense", "LookingForGroup" }

local function quietOn()
	return active() and LoganReadsPC.quiet ~= false
end

local function setBlockTrades(on)
	SetCVar("blockTrades", on and "1" or "0")
end

local function leaveQuietChannels()
	if not quietOn() then return end
	-- GetChannelList returns id, name, disabled for each joined channel
	local list = { GetChannelList() }
	for i = 1, #list, 3 do
		local name = list[i + 1]
		if type(name) == "string" then
			for _, prefix in ipairs(QUIET_CHANNELS) do
				if string.find(name, "^" .. prefix) then
					LeaveChannelByName(name)
				end
			end
		end
	end
end

-- wait a few seconds so the server has finished joining the default channels
local function after(seconds, fn)
	local waiter = CreateFrame("Frame")
	local t = 0
	waiter:SetScript("OnUpdate", function(self, elapsed)
		t = t + elapsed
		if t >= seconds then
			self:SetScript("OnUpdate", nil)
			fn()
		end
	end)
end

local function onQuietEvent(event)
	if not quietOn() then return end
	if event == "DUEL_REQUESTED" then
		CancelDuel()                 -- declines the duel
	elseif event == "GUILD_INVITE_REQUEST" then
		DeclineGuild()               -- declines the guild invite
	elseif event == "PETITION_SHOW" then
		ClosePetition()              -- closes the charter signing window without signing
	end
end

-- ============ big action buttons (out of combat only) ============

local BIG_SCALE = 1.6
local HOTKEY_BIG_SIZE = 20
local bigPending = nil          -- true/false while a change waits for combat to end
local hotkeyFont = {}

local function applyBig(on)
	if InCombatLockdown() then
		bigPending = on and true or false
		return
	end
	bigPending = nil
	local big = on and active()
	for i = 1, 3 do
		local b = _G["ActionButton" .. i]
		local hk = _G["ActionButton" .. i .. "HotKey"]
		if b then
			b:SetScale(big and BIG_SCALE or 1)
		end
		if hk then
			if not hotkeyFont[i] then
				local path, size, flags = hk:GetFont()
				hotkeyFont[i] = { path, size, flags }
			end
			if big then
				hk:SetFont(FONT_PATH, HOTKEY_BIG_SIZE, "OUTLINE")
			elseif hotkeyFont[i][1] then
				hk:SetFont(hotkeyFont[i][1], hotkeyFont[i][2], hotkeyFont[i][3])
			end
		end
	end
end

-- ============ init ============

local frame = CreateFrame("Frame", "LoganReadsFrame", UIParent)
frame:RegisterEvent("ADDON_LOADED")

frame:SetScript("OnEvent", function(self, event, arg1)
	if event == "ADDON_LOADED" and arg1 == ADDON_NAME then
		LoganReadsPC = LoganReadsPC or {}
		if LoganReadsPC.enabled == nil then LoganReadsPC.enabled = false end
		if LoganReadsPC.quiet == nil then LoganReadsPC.quiet = true end
		if LoganReadsPC.big == nil then LoganReadsPC.big = false end
		LoganReadsLog = LoganReadsLog or {}
		makeButton("offer")
		makeButton("reward")
		makeButton("log")
		makeButton("progress")
		hooksecurefunc("QuestInfo_Display", refreshQuestInfo)
		hooksecurefunc("QuestFrameProgressItems_Update", refreshProgress)
		DEFAULT_CHAT_FRAME:AddMessage("LoganReads 1.2 loaded - " .. (active() and "ON." or "OFF. Type /lr on."))
		-- backup: re-apply after the server events too, so a second quest
		-- from the same NPC also gets the kid text even if a hook path missed
		frame:RegisterEvent("QUEST_DETAIL")
		frame:RegisterEvent("QUEST_PROGRESS")
		frame:RegisterEvent("QUEST_COMPLETE")
		-- quiet mode
		frame:RegisterEvent("PLAYER_ENTERING_WORLD")
		frame:RegisterEvent("DUEL_REQUESTED")
		frame:RegisterEvent("GUILD_INVITE_REQUEST")
		frame:RegisterEvent("PETITION_SHOW")
		frame:RegisterEvent("PLAYER_REGEN_ENABLED")
	elseif event == "QUEST_DETAIL" then
		refreshQuestInfo(nil, _G["QuestDetailScrollChildFrame"])
	elseif event == "QUEST_PROGRESS" then
		refreshProgress()
	elseif event == "QUEST_COMPLETE" then
		refreshQuestInfo(nil, _G["QuestRewardScrollChildFrame"])
	elseif event == "PLAYER_ENTERING_WORLD" then
		if quietOn() then
			setBlockTrades(true)
			after(5, leaveQuietChannels)
		end
		if LoganReadsPC.big then
			applyBig(true)
		end
	elseif event == "DUEL_REQUESTED" or event == "GUILD_INVITE_REQUEST" or event == "PETITION_SHOW" then
		onQuietEvent(event)
	elseif event == "PLAYER_REGEN_ENABLED" then
		if bigPending ~= nil then
			applyBig(bigPending)
		end
	end
end)

-- slash commands
SLASH_LOGANREADS1 = "/lr"
SlashCmdList["LOGANREADS"] = function(msg)
	local cmd, arg = string.match(string.lower(msg or ""), "^(%S*)%s*(%S*)")
	cmd = cmd or ""
	arg = arg or ""
	if cmd == "coach" then
		-- coach commands live in LoganReads_Coach.lua
		local rest = string.match(string.lower(msg or ""), "^%S*%s*(.*)$") or ""
		if LoganReadsCoach then LoganReadsCoach.command(rest) end
		return
	end
	if cmd == "on" then
		LoganReadsPC.enabled = true
		setBlockTrades(quietOn())
		if quietOn() then after(1, leaveQuietChannels) end
		applyBig(LoganReadsPC.big)
		if LoganReadsCoach then LoganReadsCoach.refresh() end
		DEFAULT_CHAT_FRAME:AddMessage("LoganReads is ON.")
	elseif cmd == "off" then
		LoganReadsPC.enabled = false
		setBlockTrades(false)
		applyBig(false)
		if LoganReadsCoach then LoganReadsCoach.refresh() end
		DEFAULT_CHAT_FRAME:AddMessage("LoganReads is OFF.")
	elseif cmd == "quiet" and arg == "on" then
		LoganReadsPC.quiet = true
		if active() then
			setBlockTrades(true)
			leaveQuietChannels()
		end
		DEFAULT_CHAT_FRAME:AddMessage("LoganReads quiet mode is ON." .. (active() and "" or " (applies after /lr on)"))
	elseif cmd == "quiet" and arg == "off" then
		LoganReadsPC.quiet = false
		setBlockTrades(false)
		DEFAULT_CHAT_FRAME:AddMessage("LoganReads quiet mode is OFF. Trades are allowed again.")
	elseif cmd == "big" and arg == "on" then
		LoganReadsPC.big = true
		applyBig(true)
		DEFAULT_CHAT_FRAME:AddMessage("LoganReads big buttons are ON." .. (active() and "" or " (applies after /lr on)"))
	elseif cmd == "big" and arg == "off" then
		LoganReadsPC.big = false
		applyBig(false)
		DEFAULT_CHAT_FRAME:AddMessage("LoganReads big buttons are OFF.")
	elseif cmd == "test" then
		local n = 0
		for k in pairs(LoganReads_Data) do
			if type(k) == "number" then n = n + 1 end
		end
		DEFAULT_CHAT_FRAME:AddMessage("LoganReads: " .. n .. " quest(s). enabled=" .. tostring(LoganReadsPC.enabled)
			.. " quiet=" .. tostring(LoganReadsPC.quiet) .. " big=" .. tostring(LoganReadsPC.big)
			.. " | windows since load: offer=" .. dbg.offer .. " progress=" .. dbg.progress
			.. " reward=" .. dbg.reward .. " log=" .. dbg.log)
		DEFAULT_CHAT_FRAME:AddMessage("LoganReads: last matched title: '" .. dbg.lastTitle .. "'")
	else
		DEFAULT_CHAT_FRAME:AddMessage("LoganReads: /lr on | /lr off | /lr test | /lr quiet on|off | /lr big on|off | /lr coach ...")
	end
end
