--[[--
piread_xray.lua — X-Ray browser UI for the piread plugin.

Displays characters, locations, timeline, and terms from the local
X-Ray cache. Entirely offline — no network calls.
--]]--

local BD            = require("ui/bidi")
local ButtonDialog  = require("ui/widget/buttondialog")
local Device        = require("device")
local Font          = require("ui/font")
local InfoMessage   = require("ui/widget/infomessage")
local Menu          = require("ui/widget/menu")
local Screen        = require("device/screen")
local TextViewer    = require("ui/widget/textviewer")
local UIManager     = require("ui/uimanager")
local logger        = require("logger")
local util          = require("util")
local T             = require("ffi/util").template
local _             = require("gettext")

local XRayUI = {}

-- ── Helpers ───────────────────────────────────────────────────────────────────

local function pct_badge(pct)
    if not pct then return "" end
    return string.format(" [%d%%]", math.floor(pct))
end

local function show_detail(title, lines)
    UIManager:show(TextViewer:new{
        title  = title,
        text   = table.concat(lines, "\n"),
        width  = math.floor(Screen:getWidth()  * 0.92),
        height = math.floor(Screen:getHeight() * 0.82),
    })
end

-- Spoiler guard: items whose first_appearance_pct > reading_pct get hidden.
-- Pass reading_pct = 100 (or nil) to show everything.
local function is_spoiler(item, reading_pct)
    if not reading_pct or reading_pct >= 100 then return false end
    local pct = tonumber(item.first_appearance_pct or item.position_pct or 0)
    return pct > reading_pct + 5  -- 5% grace margin
end


-- ── Characters ────────────────────────────────────────────────────────────────

local function show_character(char)
    local lines = {}
    lines[#lines+1] = char.role or ""
    if char.first_appearance_pct then
        lines[#lines+1] = string.format(_("First appears at %d%% of book"), char.first_appearance_pct)
    end
    if char.aliases and #char.aliases > 0 then
        lines[#lines+1] = _("Also known as: ") .. table.concat(char.aliases, ", ")
    end
    lines[#lines+1] = ""
    lines[#lines+1] = char.description or _("No description available.")
    show_detail(char.name, lines)
end

function XRayUI.showCharacters(xray, reading_pct)
    local chars = xray.characters or {}
    if #chars == 0 then
        UIManager:show(InfoMessage:new{ text = _("No characters found."), timeout = 3 })
        return
    end

    local items = {}
    for _, c in ipairs(chars) do
        if not is_spoiler(c, reading_pct) then
            items[#items+1] = {
                text     = c.name,
                mandatory = c.role and c.role:sub(1, 35) or "",
                callback  = function() show_character(c) end,
            }
        end
    end

    local hidden = #chars - #items
    local title  = string.format(_("Characters (%d)"), #items)
    if hidden > 0 then
        title = title .. string.format(_(" — %d ahead"), hidden)
    end

    UIManager:show(Menu:new{
        title           = title,
        item_table      = items,
        is_borderless   = true,
        width           = Screen:getWidth(),
        height          = Screen:getHeight(),
        single_line     = true,
        onMenuSelect    = function(_, item) item.callback() end,
        close_callback  = function(menu) UIManager:close(menu) end,
    })
end


-- ── Locations ─────────────────────────────────────────────────────────────────

function XRayUI.showLocations(xray)
    local locs = xray.locations or {}
    if #locs == 0 then
        UIManager:show(InfoMessage:new{ text = _("No locations found."), timeout = 3 })
        return
    end

    local items = {}
    for _, loc in ipairs(locs) do
        items[#items+1] = {
            text      = loc.name,
            mandatory = (loc.importance or ""):sub(1, 35),
            callback  = function()
                local lines = { loc.description or "", "", _("Importance: ") .. (loc.importance or "") }
                show_detail(loc.name, lines)
            end,
        }
    end

    UIManager:show(Menu:new{
        title          = string.format(_("Locations (%d)"), #items),
        item_table     = items,
        is_borderless  = true,
        width          = Screen:getWidth(),
        height         = Screen:getHeight(),
        single_line    = true,
        onMenuSelect   = function(_, item) item.callback() end,
        close_callback = function(menu) UIManager:close(menu) end,
    })
end


-- ── Terms / Lexicon ───────────────────────────────────────────────────────────

function XRayUI.showTerms(xray)
    local terms = xray.terms or {}
    if #terms == 0 then
        UIManager:show(InfoMessage:new{ text = _("No terms found."), timeout = 3 })
        return
    end

    local items = {}
    for _, t in ipairs(terms) do
        items[#items+1] = {
            text      = t.name,
            mandatory = (t.definition or ""):sub(1, 40),
            callback  = function()
                local lines = { t.definition or "" }
                if t.aliases and #t.aliases > 0 then
                    lines[#lines+1] = ""
                    lines[#lines+1] = _("Also: ") .. table.concat(t.aliases, ", ")
                end
                show_detail(t.name, lines)
            end,
        }
    end

    UIManager:show(Menu:new{
        title          = string.format(_("Terms & Lexicon (%d)"), #items),
        item_table     = items,
        is_borderless  = true,
        width          = Screen:getWidth(),
        height         = Screen:getHeight(),
        single_line    = true,
        onMenuSelect   = function(_, item) item.callback() end,
        close_callback = function(menu) UIManager:close(menu) end,
    })
end


-- ── Timeline ──────────────────────────────────────────────────────────────────

function XRayUI.showTimeline(xray, reading_pct)
    local events = xray.timeline or {}
    if #events == 0 then
        UIManager:show(InfoMessage:new{ text = _("No timeline events found."), timeout = 3 })
        return
    end

    -- Filter to events up to reading position (with a grace margin)
    local visible = {}
    for _, e in ipairs(events) do
        if not is_spoiler(e, reading_pct) then
            visible[#visible+1] = e
        end
    end

    if #visible == 0 then
        UIManager:show(InfoMessage:new{
            text = _("No events yet at your reading position."), timeout = 3
        })
        return
    end

    -- Render as a scrollable text view for timeline (better than a menu)
    local lines = {}
    local last_pct = -1
    for _, e in ipairs(visible) do
        local pct = tonumber(e.position_pct or 0)
        if pct ~= last_pct then
            lines[#lines+1] = string.format("── %s (%d%%) ──", e.chapter or "?", pct)
            last_pct = pct
        end
        lines[#lines+1] = "  • " .. (e.event or "")
        lines[#lines+1] = ""
    end

    local hidden = #events - #visible
    local title  = string.format(_("Timeline (%d events)"), #visible)
    if hidden > 0 then
        title = title .. string.format(_(" — %d ahead"), hidden)
    end

    show_detail(title, lines)
end


-- ── Author info ───────────────────────────────────────────────────────────────

function XRayUI.showAuthorInfo(xray)
    local ai = xray.author_info
    if not ai or not ai.name then
        UIManager:show(InfoMessage:new{ text = _("No author info available."), timeout = 3 })
        return
    end
    local lines = {}
    if ai.born or ai.died then
        local dates = (ai.born or "?") .. (ai.died and ("–" .. ai.died) or "")
        lines[#lines+1] = dates
    end
    lines[#lines+1] = ""
    lines[#lines+1] = ai.bio or ""
    show_detail(ai.name, lines)
end


-- ── Main X-Ray menu ───────────────────────────────────────────────────────────

function XRayUI.showMenu(xray, reading_pct)
    if not xray then
        UIManager:show(InfoMessage:new{
            text = _("X-Ray not available. Open the book first to generate it."),
            timeout = 4,
        })
        return
    end

    local chars    = xray.characters       or {}
    local locs     = xray.locations        or {}
    local terms    = xray.terms            or {}
    local refs     = xray.references       or {}
    local timeline = xray.timeline         or {}
    local author   = xray.author_info

    local function visible_count(items, pct_key)
        local n = 0
        for _, item in ipairs(items) do
            if not is_spoiler(item, reading_pct) then n = n + 1 end
        end
        return n
    end

    local n_chars  = visible_count(chars,    "first_appearance_pct")
    local n_events = visible_count(timeline, "position_pct")

    local buttons = {
        {{
            text = string.format(_("Characters (%d)"), n_chars),
            callback = function()
                UIManager:close(XRayUI._menu)
                XRayUI.showCharacters(xray, reading_pct)
            end,
        }},
        {{
            text = string.format(_("Timeline (%d events)"), n_events),
            callback = function()
                UIManager:close(XRayUI._menu)
                XRayUI.showTimeline(xray, reading_pct)
            end,
        }},
        {{
            text = string.format(_("Locations (%d)"), #locs),
            callback = function()
                UIManager:close(XRayUI._menu)
                XRayUI.showLocations(xray)
            end,
        }},
    }

    if #refs > 0 then
        table.insert(buttons, {{
            text = string.format(_("References (%d)"), #refs),
            callback = function()
                UIManager:close(XRayUI._menu)
                XRayUI.showReferences(xray)
            end,
        }})
    end

    table.insert(buttons, {{
        text = string.format(_("Terms & Lexicon (%d)"), #terms),
        callback = function()
            UIManager:close(XRayUI._menu)
            XRayUI.showTerms(xray)
        end,
    }})

    if author and author.name then
        table.insert(buttons, {{
            text = string.format(_("About %s"), author.name),
            callback = function()
                UIManager:close(XRayUI._menu)
                XRayUI.showAuthorInfo(xray)
            end,
        }})
    end

    table.insert(buttons, {{
        text = _("Close"),
        callback = function() UIManager:close(XRayUI._menu) end,
    }})

    local book_type = xray.book_type or "fiction"
    XRayUI._menu = ButtonDialog:new{
        title       = _("Pi X-Ray") .. (reading_pct and string.format(" [%d%%]", reading_pct) or ""),
        title_align = "center",
        buttons     = buttons,
    }
    UIManager:show(XRayUI._menu)
end



-- ── References ────────────────────────────────────────────────────────────────

function XRayUI.showReferences(xray)
    local refs = xray.references or {}
    if #refs == 0 then
        UIManager:show(InfoMessage:new{ text = _("No references found."), timeout = 3 })
        return
    end

    -- Group by type
    local by_type, type_order = {}, {}
    for _, ref in ipairs(refs) do
        local t = ref.type or "other"
        if not by_type[t] then
            by_type[t] = {}
            table.insert(type_order, t)
        end
        table.insert(by_type[t], ref)
    end

    local items = {}
    for _, t in ipairs(type_order) do
        items[#items+1] = {
            text = t:sub(1,1):upper() .. t:sub(2),
            mandatory = string.format("(%d)", #by_type[t]),
            dim = true, callback = function() end,
        }
        for _, ref in ipairs(by_type[t]) do
            items[#items+1] = {
                text      = ref.name,
                mandatory = (ref.context_in_book or ""):sub(1, 38),
                callback  = function() XRayUI.showLookupResult("reference", ref) end,
            }
        end
    end

    UIManager:show(Menu:new{
        title          = string.format(_("References (%d)"), #refs),
        item_table     = items,
        is_borderless  = true,
        width          = Screen:getWidth(),
        height         = Screen:getHeight(),
        single_line    = true,
        onMenuSelect   = function(_, item) item.callback() end,
        close_callback = function(menu) UIManager:close(menu) end,
    })
end

-- ── Inline lookup (check local cache before going to network) ─────────────────

--- Look up a word/name in the local X-Ray cache.
-- Returns the first matching entity (any category), or nil.
function XRayUI.lookup(xray, word)
    if not xray or not word then return nil end
    local wl = word:lower()

    -- Check characters
    for _, c in ipairs(xray.characters or {}) do
        if c.name:lower() == wl then return "character", c end
        for _, alias in ipairs(c.aliases or {}) do
            if alias:lower() == wl then return "character", c end
        end
        -- Partial name match (last name etc)
        if c.name:lower():find(wl, 1, true) then return "character", c end
    end

    -- Check locations
    for _, loc in ipairs(xray.locations or {}) do
        if loc.name:lower() == wl or loc.name:lower():find(wl, 1, true) then
            return "location", loc
        end
    end

    -- Check terms
    for _, t in ipairs(xray.terms or {}) do
        if t.name:lower() == wl then return "term", t end
        for _, alias in ipairs(t.aliases or {}) do
            if alias:lower() == wl then return "term", t end
        end
    end

    -- Check references (by name)
    for _, ref in ipairs(xray.references or {}) do
        if ref.name:lower() == wl or ref.name:lower():find(wl, 1, true) then
            return "reference", ref
        end
    end

    -- Check historical figures
    for _, h in ipairs(xray.historical_figures or {}) do
        if h.name:lower() == wl or h.name:lower():find(wl, 1, true) then
            return "historical_figure", h
        end
    end

    return nil, nil
end


--- Show the lookup result for a word in a TextViewer.
function XRayUI.showLookupResult(kind, entity)
    if not kind or not entity then return end

    local title  = entity.name
    local lines  = {}

    if kind == "character" then
        lines[#lines+1] = entity.role or ""
        if entity.first_appearance_pct then
            lines[#lines+1] = string.format(_("First appears at %d%%"), entity.first_appearance_pct)
        end
        if entity.aliases and #entity.aliases > 0 then
            lines[#lines+1] = _("Also known as: ") .. table.concat(entity.aliases, ", ")
        end
        lines[#lines+1] = ""
        lines[#lines+1] = entity.description or ""

    elseif kind == "location" then
        lines[#lines+1] = entity.description or ""
        if entity.importance and entity.importance ~= "" then
            lines[#lines+1] = ""
            lines[#lines+1] = _("Importance: ") .. entity.importance
        end

    elseif kind == "term" then
        lines[#lines+1] = entity.definition or ""
        if entity.aliases and #entity.aliases > 0 then
            lines[#lines+1] = ""
            lines[#lines+1] = _("Also: ") .. table.concat(entity.aliases, ", ")
        end

    elseif kind == "reference" then
        -- Real-world or literary reference
        local type_label = entity.type or "reference"
        lines[#lines+1] = "[" .. type_label .. "]"
        lines[#lines+1] = ""
        lines[#lines+1] = entity.description or ""
        if entity.context_in_book and entity.context_in_book ~= "" then
            lines[#lines+1] = ""
            lines[#lines+1] = _("In this book: ") .. entity.context_in_book
        end

    elseif kind == "historical_figure" or kind == "hist" then
        lines[#lines+1] = entity.biography or ""
        if entity.context_in_book and entity.context_in_book ~= "" then
            lines[#lines+1] = ""
            lines[#lines+1] = _("In this book: ") .. entity.context_in_book
        end
    end

    show_detail(title, lines)
end

return XRayUI
