--[[--
main.lua — Pi reading assistant plugin for KOReader.

On book open: silently requests X-Ray from piread-bridge (or serves from
local cache instantly). Adds "Ask Pi" to the highlight dialog for
conversational queries, and "Pi X-Ray" to the menu for the entity browser.

Requires piread-bridge running on the same local network as the device.
All features degrade gracefully when offline or bridge is unreachable.
--]]--

local ButtonDialog     = require("ui/widget/buttondialog")
local Device           = require("device")
local Dispatcher       = require("dispatcher")
local InfoMessage      = require("ui/widget/infomessage")
local NetworkMgr       = require("ui/network/manager")
local Screen = require("device").screen
local TextViewer       = require("ui/widget/textviewer")
local UIManager        = require("ui/uimanager")
local WidgetContainer  = require("ui/widget/container/widgetcontainer")
local logger           = require("logger")
local util             = require("util")
local T                = require("ffi/util").template
local _                = require("gettext")

local Bridge    = require("bridge")
local Cache     = require("piread_cache")
local Context   = require("piread_context")
local XRayUI    = require("piread_xray")

local PiRead = WidgetContainer:extend{
    name = "piread",
    -- Populated on init:
    _xray        = nil,    -- current book's X-Ray data (table)
    _book_hash   = nil,    -- epub hash (from bridge)
    _book_meta   = nil,    -- {title, author, series, ...}
    _xray_job_id = nil,    -- pending generation job id
    _poll_handle = nil,    -- UIManager scheduled handle for polling
}

local SETTINGS_KEY   = "piread"
local POLL_INTERVAL  = 30    -- seconds between status polls
local PROGRESS_EVERY = 5     -- report reading position every N% change

local DEFAULT_SETTINGS = {
    host    = "macbook.local",
    port    = 7731,
    token   = "",
    enabled = true,
    spoiler_free = true,    -- hide characters/events past reading position
}

-- ── Settings ──────────────────────────────────────────────────────────────────

function PiRead:loadSettings()
    local s = G_reader_settings:readSetting(SETTINGS_KEY) or {}
    for k, v in pairs(DEFAULT_SETTINGS) do
        if s[k] == nil then s[k] = v end
    end
    return s
end

function PiRead:saveSettings(s)
    G_reader_settings:saveSetting(SETTINGS_KEY, s)
end

function PiRead:applySettings()
    local s = self:loadSettings()
    Bridge.host  = s.host
    Bridge.port  = s.port
    Bridge.token = s.token
end

-- ── Lifecycle ─────────────────────────────────────────────────────────────────

function PiRead:init()
    self:applySettings()
    self:onDispatcherRegisterActions()
    if self.document then
        self:onDocLoad()
        self:hookHighlightDialog()
    end
    self.ui.menu:registerToMainMenu(self)
end

function PiRead:onDispatcherRegisterActions()
    Dispatcher:registerAction("piread_now_reading", {
        category = "none",
        event    = "PiReadNowReading",
        title    = _("Pi: Now Reading dashboard"),
        reader   = true,
    })
end

function PiRead:onPiReadNowReading()
    local s = self:loadSettings()
    if not s.enabled then return end
    local pct = s.spoiler_free and self:currentReadingPct() or nil
    Context.show(self.ui, self._xray, Bridge, pct)
end

function PiRead:onDocLoad()
    local s = self:loadSettings()
    if not s.enabled then return end

    -- Get book info
    local props = self.ui.document:getProps()
    local title  = (props and props.title)   or ""
    local author = (props and props.authors) or ""
    if title == "" then return end

    -- Check local cache first (instant)
    local record, hash = Cache.findByTitle(title)
    if record and record.xray then
        logger.info("piread: X-Ray loaded from local cache:", title)
        self._xray      = record.xray
        self._book_hash = hash or record.book and record.book.epub_hash
        self._book_meta = record.book
        return
    end

    -- No local cache — try bridge (only if network available)
    if not NetworkMgr:isConnected() then return end

    local reading_pct = self:currentReadingPct()
    UIManager:scheduleIn(2, function()
        self:requestXRay(title, author, reading_pct)
    end)
end

function PiRead:currentReadingPct()
    if not (self.ui and self.ui.document) then return 0 end
    local cur   = self.ui:getCurrentPage()
    local total = self.ui.document:getPageCount()
    if not cur or not total or total == 0 then return 0 end
    return math.floor(cur / total * 100)
end

-- ── X-Ray request & polling ───────────────────────────────────────────────────

function PiRead:requestXRay(title, author, reading_pct)
    logger.info("piread: requesting X-Ray for", title)
    local resp, err = Bridge:xrayInit({
        book_title  = title,
        book_author = author,
        reading_pct = reading_pct or 0,
    })
    if err then
        logger.warn("piread: /xray/init error:", err)
        return
    end

    if resp.status == "ready" then
        -- Cache hit — save locally and load
        logger.info("piread: X-Ray ready (cached=%s)", tostring(resp.cached))
        self:_storeXRay(resp)

    elseif resp.status == "generating" then
        -- Background job started — poll
        logger.info("piread: X-Ray generating, job_id=%s", tostring(resp.job_id))
        self._xray_job_id = resp.job_id
        self:schedulePoll()
        UIManager:show(InfoMessage:new{
            text    = _("Pi is building your X-Ray…"),
            timeout = 4,
        })
    end
end

function PiRead:schedulePoll()
    if self._poll_handle then
        UIManager:unschedule(self._poll_handle)
    end
    self._poll_handle = function() self:pollXRayStatus() end
    UIManager:scheduleIn(POLL_INTERVAL, self._poll_handle)
end

function PiRead:pollXRayStatus()
    self._poll_handle = nil
    if not self._xray_job_id then return end
    if not NetworkMgr:isConnected() then
        -- No network, retry later
        self:schedulePoll()
        return
    end

    local resp, err = Bridge:xrayStatus(self._xray_job_id)
    if err then
        logger.warn("piread: status poll error:", err)
        self:schedulePoll()  -- retry
        return
    end

    if resp.status == "ready" then
        self._xray_job_id = nil
        self:_storeXRay(resp)
        UIManager:show(InfoMessage:new{
            text    = _("Pi X-Ray ready!"),
            timeout = 3,
        })

    elseif resp.status == "failed" then
        self._xray_job_id = nil
        logger.warn("piread: X-Ray generation failed:", resp.error)

    else
        -- Still generating
        logger.info("piread: still generating (%s)", resp.progress or "…")
        self:schedulePoll()
    end
end

function PiRead:_storeXRay(resp)
    if not resp or not resp.xray then return end
    self._xray      = resp.xray
    self._book_meta = resp.book
    local hash      = resp.book and resp.book.epub_hash
    self._book_hash = hash
    -- Save to local device cache
    if hash then
        Cache.saveXray(hash, {
            xray         = resp.xray,
            book         = resp.book,
            generated_at = resp.generated_at,
        })
    end
end

-- ── Highlight dialog hook ─────────────────────────────────────────────────────

function PiRead:hookHighlightDialog()
    self.ui.highlight:addToHighlightDialog("11_ask_pi", function(this)
        local s = self:loadSettings()
        if not s.enabled then return nil end
        if not NetworkMgr:isConnected() then return nil end

        return {
            text = _("Ask Pi"),
            callback = function()
                local sel = this.selected_text
                if not (sel and sel.text and sel.text ~= "") then return end

                local text = util.cleanupSelectedText(sel.text)
                local prev_ctx, next_ctx = this:getSelectedWordContext(40)
                local props = this.ui.document:getProps()
                local book_title  = (props and props.title)   or ""
                local book_author = (props and props.authors) or ""

                -- First: check if this word is in the local X-Ray cache
                if self._xray then
                    local kind, entity = XRayUI.lookup(self._xray, text)
                    if kind and entity then
                        this:onClose(true)
                        -- Check spoiler guard
                        local s2 = self:loadSettings()
                        if s2.spoiler_free then
                            local pct = self:currentReadingPct()
                            if (entity.first_appearance_pct or 0) > pct + 5 then
                                UIManager:show(InfoMessage:new{
                                    text    = _("This character/place appears later in the book."),
                                    timeout = 4,
                                })
                                return
                            end
                        end
                        XRayUI.showLookupResult(kind, entity)
                        return
                    end
                end

                -- Not in cache — ask bridge
                this:onClose(true)
                self:showModeDialog(text, prev_ctx, next_ctx, book_title, book_author)
            end,
        }
    end)
end

-- ── Mode picker ───────────────────────────────────────────────────────────────

local MODES = {
    { id = "whois",     label = _("Who / What is this?") },
    { id = "explain",   label = _("Explain this passage") },
    { id = "summarize", label = _("Story context so far") },
    { id = "translate", label = _("Translate to English") },
}

function PiRead:showModeDialog(text, prev_ctx, next_ctx, book_title, book_author)
    local buttons = {}
    for _, mode in ipairs(MODES) do
        local mid, mlabel = mode.id, mode.label
        table.insert(buttons, {{ text = mlabel, callback = function()
            UIManager:close(self._mode_dialog)
            self._mode_dialog = nil
            self:askBridge(text, prev_ctx, next_ctx, book_title, book_author, mid, mlabel)
        end }})
    end
    table.insert(buttons, {{ text = _("Cancel"), callback = function()
        UIManager:close(self._mode_dialog)
        self._mode_dialog = nil
    end }})

    self._mode_dialog = ButtonDialog:new{
        title       = _("Ask Pi"),
        title_align = "center",
        buttons     = buttons,
    }
    UIManager:show(self._mode_dialog)
end

-- ── Ask bridge (conversational) ───────────────────────────────────────────────

function PiRead:askBridge(text, prev_ctx, next_ctx, book_title, book_author, mode_id, mode_label)
    local loading = InfoMessage:new{ text = _("Asking Pi…"), timeout = 30 }
    UIManager:show(loading)
    UIManager:scheduleIn(0.1, function()
        local ctx = ""
        if prev_ctx and prev_ctx ~= "" then ctx = prev_ctx .. " " end
        if next_ctx and next_ctx ~= "" then ctx = ctx .. next_ctx end

        local response, err = Bridge:ask({
            text        = text,
            context     = ctx ~= "" and ctx or nil,
            book_title  = book_title ~= "" and book_title  or nil,
            book_author = book_author ~= "" and book_author or nil,
            mode        = mode_id,
        })
        UIManager:close(loading)
        if err then
            logger.warn("piread:", err)
            UIManager:show(InfoMessage:new{ text = T(_("Pi: %1"), err), timeout = 6 })
            return
        end
        UIManager:show(TextViewer:new{
            title  = mode_label,
            text   = response,
            width  = math.floor(Screen:getWidth()  * 0.92),
            height = math.floor(Screen:getHeight() * 0.78),
        })
    end)
end

-- ── Menu ──────────────────────────────────────────────────────────────────────

function PiRead:addToMainMenu(menu_items)
    menu_items.piread = {
        text         = _("Pi reading assistant"),
        sorting_hint = "more_tools",
        sub_item_table = self:buildMenu(),
    }
end

function PiRead:buildMenu()
    local items = {}

    -- Now Reading dashboard (top of menu — primary entry point)
    table.insert(items, {
        text     = _("Now Reading"),
        callback = function()
            local s = self:loadSettings()
            if not s.enabled then
                UIManager:show(InfoMessage:new{ text = _("Pi is disabled."), timeout = 3 })
                return
            end
            local pct = s.spoiler_free and self:currentReadingPct() or nil
            Context.show(self.ui, self._xray, Bridge, pct)
        end,
    })

    -- X-Ray browser
    table.insert(items, {
        text_func = function()
            if self._xray then
                local n = #(self._xray.characters or {})
                return string.format(_("X-Ray (%d characters)"), n)
            elseif self._xray_job_id then
                return _("X-Ray (building…)")
            else
                return _("X-Ray (not available)")
            end
        end,
        callback = function()
            if not self._xray then
                UIManager:show(InfoMessage:new{
                    text    = self._xray_job_id
                                and _("X-Ray is being generated. Try again in a minute.")
                                or  _("No X-Ray data. Open a book that's in your Calibre library."),
                    timeout = 5,
                })
                return
            end
            local pct = self:loadSettings().spoiler_free and self:currentReadingPct() or nil
            XRayUI.showMenu(self._xray, pct)
        end,
    })

    -- Toggle enabled
    table.insert(items, {
        text_func = function()
            return T(_("Enabled: %1"), self:loadSettings().enabled and _("yes") or _("no"))
        end,
        checked_func = function() return self:loadSettings().enabled end,
        callback = function()
            local s = self:loadSettings(); s.enabled = not s.enabled; self:saveSettings(s)
        end,
    })

    -- Spoiler-free toggle
    table.insert(items, {
        text_func = function()
            return T(_("Spoiler-free: %1"), self:loadSettings().spoiler_free and _("on") or _("off"))
        end,
        checked_func = function() return self:loadSettings().spoiler_free end,
        callback = function()
            local s = self:loadSettings(); s.spoiler_free = not s.spoiler_free; self:saveSettings(s)
        end,
    })

    -- Bridge host
    table.insert(items, {
        text_func = function() return T(_("Host: %1"), self:loadSettings().host) end,
        callback  = function() self:editSetting("host", _("Bridge host"), false) end,
    })

    -- Bridge port
    table.insert(items, {
        text_func = function() return T(_("Port: %1"), tostring(self:loadSettings().port)) end,
        callback  = function() self:editSetting("port", _("Bridge port"), true) end,
    })

    -- Rebuild X-Ray
    table.insert(items, {
        text = _("Rebuild X-Ray for this book"),
        enabled_func = function()
            return self.ui and self.ui.document ~= nil
        end,
        callback = function()
            if not (self.ui and self.ui.document) then return end
            local props = self.ui.document:getProps()
            local title  = (props and props.title)   or ""
            local author = (props and props.authors) or ""
            if title == "" then return end
            -- Clear local cache
            if self._book_hash then Cache.deleteXray(self._book_hash) end
            self._xray      = nil
            self._book_hash = nil
            if not NetworkMgr:isConnected() then
                UIManager:show(InfoMessage:new{ text = _("Not connected to network."), timeout = 3 })
                return
            end
            self:requestXRay(title, author, self:currentReadingPct())
        end,
    })

    -- Test connection
    table.insert(items, {
        text = _("Test connection"),
        callback = function()
            self:applySettings()
            local loading = InfoMessage:new{ text = _("Pinging bridge…"), timeout = 6 }
            UIManager:show(loading)
            UIManager:scheduleIn(0.1, function()
                UIManager:close(loading)
                if Bridge:ping() then
                    UIManager:show(InfoMessage:new{
                        text    = T(_("✓ Connected to %1:%2"), Bridge.host, tostring(Bridge.port)),
                        timeout = 4,
                    })
                else
                    UIManager:show(InfoMessage:new{
                        text    = T(_("✗ Cannot reach %1:%2"), Bridge.host, tostring(Bridge.port)),
                        timeout = 5,
                    })
                end
            end)
        end,
    })

    return items
end

-- ── Settings editor ───────────────────────────────────────────────────────────

function PiRead:editSetting(key, title, numeric)
    local InputDialog = require("ui/widget/inputdialog")
    local s = self:loadSettings()
    local dialog
    dialog = InputDialog:new{
        title      = title,
        input      = tostring(s[key] or ""),
        input_type = numeric and "number" or "string",
        buttons = {{
            { text = _("Cancel"), id = "close", callback = function() UIManager:close(dialog) end },
            { text = _("Save"), is_enter_default = true, callback = function()
                local val = dialog:getInputText()
                if numeric then
                    val = tonumber(val)
                    if not val then
                        UIManager:show(InfoMessage:new{ text = _("Enter a valid number"), timeout = 3 })
                        return
                    end
                end
                s[key] = val; self:saveSettings(s); self:applySettings()
                UIManager:close(dialog)
            end },
        }},
    }
    UIManager:show(dialog); dialog:onShowKeyboard()
end

return PiRead
