--[[--
piread_cache.lua — Device-side X-Ray cache for the piread plugin.

Stores X-Ray data in KOReader's settings dir:
  settings/piread/<book_hash>.json

The book hash comes from the bridge (md5 of the epub file on Mac),
ensuring the device cache keys match the Mac cache.
--]]--

local DataStorage = require("datastorage")
local rapidjson   = require("rapidjson")
local logger      = require("logger")

local ok_lfs, lfs = pcall(require, "libs/libkoreader-lfs")
if not ok_lfs or type(lfs) ~= "table" then
    ok_lfs, lfs = pcall(require, "lfs")
end

local Cache = {}

local function cacheDir()
    return DataStorage:getSettingsDir() .. "/piread"
end

local function ensureDir()
    if not (ok_lfs and lfs) then return end
    local d = cacheDir()
    if lfs.attributes(d, "mode") ~= "directory" then
        lfs.mkdir(d)
    end
end

local function cachePath(book_hash)
    return cacheDir() .. "/" .. book_hash .. ".json"
end

local function indexPath()
    return cacheDir() .. "/index.json"
end


-- ── Per-book X-Ray data ────────────────────────────────────────────────────────

--- Load X-Ray record for a book by hash. Returns table or nil.
function Cache.loadXray(book_hash)
    if not book_hash then return nil end
    local path = cachePath(book_hash)
    local f = io.open(path, "r")
    if not f then return nil end
    local raw = f:read("*a")
    f:close()
    local ok, data = pcall(rapidjson.decode, raw)
    if ok and data then return data end
    logger.warn("piread cache: decode error for", path)
    return nil
end

--- Save X-Ray record. `data` is the full record as returned by /xray/init.
function Cache.saveXray(book_hash, data)
    if not book_hash or not data then return false end
    ensureDir()
    local path = cachePath(book_hash)
    local ok, encoded = pcall(rapidjson.encode, data)
    if not ok then
        logger.warn("piread cache: encode error:", encoded)
        return false
    end
    local f = io.open(path, "w")
    if not f then
        logger.warn("piread cache: cannot write to", path)
        return false
    end
    f:write(encoded)
    f:close()
    -- Update index
    Cache.updateIndex(book_hash, data)
    logger.info("piread cache: saved", path)
    return true
end

--- Check whether X-Ray is cached for a given book hash.
function Cache.hasXray(book_hash)
    if not book_hash then return false end
    local f = io.open(cachePath(book_hash), "r")
    if f then f:close(); return true end
    return false
end

--- Delete cached X-Ray (e.g. to force re-generation).
function Cache.deleteXray(book_hash)
    if not book_hash then return end
    os.remove(cachePath(book_hash))
    local idx = Cache.loadIndex()
    if idx and idx.books then
        idx.books[book_hash] = nil
        Cache.saveIndex(idx)
    end
end


-- ── Index (quick lookup without reading full records) ─────────────────────────

function Cache.loadIndex()
    local f = io.open(indexPath(), "r")
    if not f then return { books = {} } end
    local raw = f:read("*a")
    f:close()
    local ok, idx = pcall(rapidjson.decode, raw)
    if ok and idx then return idx end
    return { books = {} }
end

function Cache.saveIndex(idx)
    ensureDir()
    local ok, encoded = pcall(rapidjson.encode, idx)
    if not ok then return end
    local f = io.open(indexPath(), "w")
    if not f then return end
    f:write(encoded)
    f:close()
end

function Cache.updateIndex(book_hash, data)
    local idx  = Cache.loadIndex()
    idx.books  = idx.books or {}
    local book = data.book or {}
    local xray = data.xray or {}
    idx.books[book_hash] = {
        hash            = book_hash,
        title           = book.title or "",
        author          = book.author or "",
        series          = book.series,
        series_index    = book.series_index,
        generated_at    = data.generated_at or "",
        character_count = #(xray.characters or {}),
        location_count  = #(xray.locations or {}),
        term_count      = #(xray.terms or {}),
        timeline_count  = #(xray.timeline or {}),
    }
    Cache.saveIndex(idx)
end

--- Find cached book by title (case-insensitive). Returns full record or nil.
function Cache.findByTitle(title)
    if not title then return nil end
    local tl = title:lower():gsub("^%s+", ""):gsub("%s+$", "")
    local idx = Cache.loadIndex()
    for hash, meta in pairs(idx.books or {}) do
        if (meta.title or ""):lower() == tl then
            return Cache.loadXray(hash), hash
        end
    end
    return nil, nil
end

--- Update just the reading position without reloading the full record.
function Cache.updateReadingPct(book_hash, pct)
    local idx = Cache.loadIndex()
    if idx.books and idx.books[book_hash] then
        idx.books[book_hash].last_reading_pct = pct
        Cache.saveIndex(idx)
    end
    -- Also patch the full record in place
    local rec = Cache.loadXray(book_hash)
    if rec then
        rec.last_reading_pct = pct
        Cache.saveXray(book_hash, rec)
    end
end

return Cache
