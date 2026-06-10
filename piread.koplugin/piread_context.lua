--[[--
piread_context.lua — "Now Reading" contextual dashboard.

Opens without requiring text selection. Shows which characters, locations,
references, and terms from the X-Ray appear in the current chapter.
Tap any entry to see its full X-Ray detail. "Ask Pi" button sends the
current passage to the bridge for a narrative summary.

Entry points:
  - Menu → Pi reading assistant → Now Reading
  - Can be gesture-bound via KOReader's Gestures settings

Does NOT require network once X-Ray is cached.
--]]--

local ButtonDialog  = require("ui/widget/buttondialog")
local InfoMessage   = require("ui/widget/infomessage")
local Menu          = require("ui/widget/menu")
local Screen        = require("device/screen")
local TextViewer    = require("ui/widget/textviewer")
local UIManager     = require("ui/uimanager")
local logger        = require("logger")
local T             = require("ffi/util").template
local _             = require("gettext")

local XRayUI = require("piread_xray")

local Context = {}

-- ── Text extraction ───────────────────────────────────────────────────────────

--- Get text for the current chapter (or nearby pages as fallback).
-- Returns (text, chapter_title, chapter_pct) or (nil).
function Context.getCurrentChapterText(ui, max_chars)
    max_chars = max_chars or 25000
    if not (ui and ui.document) then return nil, nil, nil end

    local current_page = ui:getCurrentPage()
    local total_pages  = ui.document:getPageCount() or 1
    local chapter_pct  = math.floor(current_page / total_pages * 100)

    -- ── Find chapter boundaries from TOC ──────────────────────────────────────
    local toc = ui.document:getToc()
    local chapter_title    = nil
    local chapter_start    = math.max(1, current_page - 5)   -- fallback
    local chapter_end      = math.min(total_pages, current_page + 30)

    if toc and #toc > 0 then
        for i = #toc, 1, -1 do
            if (toc[i].page or 1) <= current_page then
                chapter_title = toc[i].title
                chapter_start = toc[i].page
                chapter_end   = (i < #toc) and (toc[i+1].page - 1) or total_pages
                break
            end
        end
    end

    -- ── Reflowable (EPUB): XPointer-based extraction ──────────────────────────
    if ui.rolling then
        local saved_xp = ui.document:getXPointer()
        local text

        local ok, result = pcall(function()
            local start_xp, end_xp

            if ui.document.getPageXPointer then
                start_xp = ui.document:getPageXPointer(chapter_start)
                end_xp   = ui.document:getPageXPointer(math.min(chapter_end, current_page + 60))
            end
            if not start_xp then
                ui.document:gotoPage(chapter_start)
                start_xp = ui.document:getXPointer()
                ui.document:gotoPage(math.min(chapter_end, current_page + 60))
                end_xp = ui.document:getXPointer()
            end
            if not (start_xp and end_xp) then return nil end
            return ui.document:getTextFromXPointers(start_xp, end_xp) or ""
        end)

        -- Always restore position
        if saved_xp then
            pcall(function() ui.document:gotoXPointer(saved_xp) end)
        end

        if ok and result and #result > 0 then
            text = result:sub(1, max_chars)
        end

        return text, chapter_title, chapter_pct
    end

    -- ── Paged (PDF): page-by-page ─────────────────────────────────────────────
    local parts = {}
    local total_len = 0
    for page = chapter_start, math.min(chapter_end, current_page + 15) do
        local raw = ui.document:getPageText(page)
        local chunk
        if type(raw) == "table" then
            local words = {}
            for _, block in ipairs(raw) do
                if type(block) == "table" then
                    for _, span in ipairs(block) do
                        if type(span) == "table" and span.word then
                            words[#words+1] = span.word
                        end
                    end
                end
            end
            chunk = table.concat(words, " ")
        elseif type(raw) == "string" then
            chunk = raw
        end
        if chunk and #chunk > 0 then
            parts[#parts+1] = chunk
            total_len = total_len + #chunk
            if total_len >= max_chars then break end
        end
    end
    local text = #parts > 0 and table.concat(parts, "\n") or nil
    return text, chapter_title, chapter_pct
end


-- ── Entity matching ───────────────────────────────────────────────────────────

local function name_in_text(name, text_lower)
    if not name or name == "" then return false end
    -- Word-boundary-aware search: the name appears as a distinct word/phrase
    local nl = name:lower()
    -- Simple: check if the name (or just the last name for long names) appears
    if text_lower:find(nl, 1, true) then return true end
    -- Also check last word of the name (e.g. "au Andromedus" → "Andromedus")
    local last = nl:match("(%S+)%s*$")
    if last and #last >= 4 and text_lower:find(last, 1, true) then return true end
    return false
end

local function aliases_in_text(aliases, text_lower)
    if not aliases then return false end
    for _, alias in ipairs(aliases) do
        if name_in_text(alias, text_lower) then return true end
    end
    return false
end

--- Scan chapter text for X-Ray entities, grouped by type.
-- Returns {characters, locations, terms, references, historical_figures}
-- Each list is ordered by frequency of appearance in the text.
function Context.findEntitiesInText(xray, text)
    if not (xray and text) then
        return {}, {}, {}, {}, {}
    end
    local tl = text:lower()

    local function scan(items, name_key, alias_key)
        local found = {}
        for _, item in ipairs(items or {}) do
            local name = item[name_key] or ""
            if name_in_text(name, tl) or (alias_key and aliases_in_text(item[alias_key], tl)) then
                -- Count approximate occurrences for sorting
                local count = 0
                local nl    = name:lower()
                local pos   = 1
                while true do
                    local s = tl:find(nl, pos, true)
                    if not s then break end
                    count = count + 1
                    pos   = s + 1
                end
                found[#found+1] = { item = item, count = count }
            end
        end
        -- Sort by frequency descending
        table.sort(found, function(a, b) return a.count > b.count end)
        local result = {}
        for _, f in ipairs(found) do result[#result+1] = f.item end
        return result
    end

    local chars   = scan(xray.characters,         "name", "aliases")
    local locs    = scan(xray.locations,           "name", nil)
    local terms   = scan(xray.terms,               "name", "aliases")
    local refs    = scan(xray.references,          "name", nil)
    local hist    = scan(xray.historical_figures,  "name", nil)

    return chars, locs, terms, refs, hist
end


-- ── Dashboard UI ──────────────────────────────────────────────────────────────

local function section_items(title, entities, kind, reading_pct)
    -- Returns a list of menu items for one section, with a section header
    if not entities or #entities == 0 then return {} end

    local items = {}
    -- Section header (non-tappable separator)
    items[#items+1] = {
        text      = "── " .. title .. " ──",
        mandatory = "",
        dim       = true,
        callback  = function() end,
    }
    for _, entity in ipairs(entities) do
        -- Spoiler guard
        local pct = entity.first_appearance_pct or entity.position_pct or 0
        if reading_pct and reading_pct < 100 and pct > reading_pct + 5 then
            -- skip spoiler items silently
        else
            local sub = ""
            if kind == "character"  then sub = entity.role or ""
            elseif kind == "location" then sub = entity.importance or ""
            elseif kind == "term"     then sub = (entity.definition or ""):sub(1, 40)
            elseif kind == "reference" then sub = entity.type or ""
            elseif kind == "hist"     then sub = entity.context_in_book or ""
            end
            items[#items+1] = {
                text      = entity.name,
                mandatory = sub:sub(1, 38),
                callback  = function()
                    XRayUI.showLookupResult(kind, entity)
                end,
            }
        end
    end
    -- If all items were spoilers, only the header remains — remove it
    if #items == 1 then return {} end
    return items
end


function Context.show(ui, xray, bridge, reading_pct)
    -- Extract current chapter text
    local text, chapter_title, chapter_pct = Context.getCurrentChapterText(ui)

    if not text or #text < 50 then
        UIManager:show(InfoMessage:new{
            text    = _("Could not extract page text for scanning."),
            timeout = 4,
        })
        return
    end

    if not xray then
        UIManager:show(InfoMessage:new{
            text    = _("X-Ray not ready. Pi is still building it — try again in a minute."),
            timeout = 5,
        })
        return
    end

    -- Find entities in this chapter
    local chars, locs, terms, refs, hist =
        Context.findEntitiesInText(xray, text)

    local total_found = #chars + #locs + #terms + #refs + #hist

    -- Build menu items (grouped by category)
    local items = {}

    local function extend(t)
        for _, v in ipairs(t) do items[#items+1] = v end
    end

    if #chars > 0 then
        extend(section_items(_("Characters"), chars, "character", reading_pct))
    end
    if #locs > 0 then
        extend(section_items(_("Locations"), locs, "location", reading_pct))
    end
    if #refs > 0 then
        extend(section_items(_("References"), refs, "reference", reading_pct))
    end
    if #hist > 0 then
        extend(section_items(_("Historical figures"), hist, "hist", reading_pct))
    end
    if #terms > 0 then
        -- Only show terms actually named in text (not all 30 of them)
        local term_subset = {}
        for _, t in ipairs(terms) do
            if #term_subset >= 6 then break end
            term_subset[#term_subset+1] = t
        end
        extend(section_items(_("Terms"), term_subset, "term", reading_pct))
    end

    -- If nothing found, say so
    if #items == 0 then
        items[#items+1] = {
            text      = _("No X-Ray entities found on this page."),
            mandatory = _("Try a few pages in"),
            callback  = function() end,
        }
    end

    -- "Ask Pi about this chapter" action item at the bottom
    if bridge then
        items[#items+1] = {
            text      = "──────────────────────────────",
            mandatory = "",
            dim       = true,
            callback  = function() end,
        }
        items[#items+1] = {
            text      = _("Ask Pi: what's happening here?"),
            mandatory = _("Sends passage to Pi"),
            callback  = function()
                UIManager:close(Context._menu)
                Context._askAboutChapter(text, chapter_title, ui, bridge)
            end,
        }
    end

    local chap_label = chapter_title
        and string.format("%s (%d%%)", chapter_title, chapter_pct)
        or  string.format("%d%%", chapter_pct)

    Context._menu = Menu:new{
        title          = string.format(_("Now Reading — %s"), chap_label),
        item_table     = items,
        is_borderless  = true,
        width          = Screen:getWidth(),
        height         = Screen:getHeight(),
        single_line    = true,
        onMenuSelect   = function(_, item) item.callback() end,
        close_callback = function(menu) UIManager:close(menu) end,
    }
    UIManager:show(Context._menu)
end


-- ── Ask Pi about this chapter ─────────────────────────────────────────────────

function Context._askAboutChapter(chapter_text, chapter_title, ui, bridge)
    -- Use a reasonable excerpt — first ~1500 chars is enough context
    local excerpt = chapter_text:sub(1, 1500):gsub("\n+", " "):gsub("%s+", " ")

    local props = ui and ui.document and ui.document:getProps()
    local book_title  = (props and props.title)   or ""
    local book_author = (props and props.authors) or ""

    local loading = InfoMessage:new{ text = _("Asking Pi…"), timeout = 30 }
    UIManager:show(loading)
    UIManager:scheduleIn(0.1, function()
        local response, err = bridge:ask({
            text        = excerpt,
            book_title  = book_title ~= "" and book_title  or nil,
            book_author = book_author ~= "" and book_author or nil,
            mode        = "summarize",
            context     = chapter_title and ("Current chapter: " .. chapter_title) or nil,
        })
        UIManager:close(loading)
        if err then
            UIManager:show(InfoMessage:new{ text = T(_("Pi: %1"), err), timeout = 5 })
            return
        end
        UIManager:show(TextViewer:new{
            title  = chapter_title and T(_("Pi on %1"), chapter_title) or _("Pi summary"),
            text   = response,
            width  = math.floor(Screen:getWidth()  * 0.92),
            height = math.floor(Screen:getHeight() * 0.78),
        })
    end)
end

return Context
