--[[--
bridge.lua — HTTP client for piread-bridge server on Mac.

Public API:
  Bridge:ask(params)              → response_text | nil, err
  Bridge:xrayInit(params)         → response_table | nil, err
  Bridge:xrayStatus(job_id)       → response_table | nil, err
  Bridge:xrayProgress(hash, pct)  → ok | nil, err
  Bridge:ping()                   → bool
--]]--

local http       = require("socket.http")
local ltn12      = require("ltn12")
local rapidjson  = require("rapidjson")
local socketutil = require("socketutil")
local logger     = require("logger")

local Bridge = {
    host          = "macbook.local",
    port          = 7731,
    token         = "",
    TIMEOUT_BLOCK = 20,
    TIMEOUT_TOTAL = 25,
    PING_BLOCK    = 3,
    PING_TOTAL    = 5,
}

function Bridge:url(path)
    return string.format("http://%s:%d%s", self.host, self.port, path)
end

-- ── Low-level HTTP ─────────────────────────────────────────────────────────────

function Bridge:_get(path, block_t, total_t)
    local sink = {}
    socketutil:set_timeout(block_t or self.PING_BLOCK, total_t or self.PING_TOTAL)
    local ok, code = http.request({
        url    = self:url(path),
        method = "GET",
        sink   = ltn12.sink.table(sink),
    })
    socketutil:reset_timeout()
    if not ok then
        return nil, "network: " .. (code or "unreachable")
    end
    if code ~= 200 then
        return nil, "HTTP " .. tostring(code)
    end
    local resp, err = rapidjson.decode(table.concat(sink))
    if not resp then return nil, "decode: " .. (err or "?") end
    return resp
end

function Bridge:_post(path, params)
    if self.token and self.token ~= "" then
        params.token = self.token
    end
    local body_json, enc_err = rapidjson.encode(params)
    if not body_json then
        return nil, "encode: " .. (enc_err or "?")
    end
    local sink = {}
    socketutil:set_timeout(self.TIMEOUT_BLOCK, self.TIMEOUT_TOTAL)
    local ok, code = http.request({
        url     = self:url(path),
        method  = "POST",
        source  = ltn12.source.string(body_json),
        sink    = ltn12.sink.table(sink),
        headers = {
            ["Content-Type"]   = "application/json",
            ["Content-Length"] = tostring(#body_json),
        },
    })
    socketutil:reset_timeout()
    if not ok then
        logger.warn("piread bridge:", code)
        return nil, "Bridge unreachable (" .. (code or "no route") .. ")"
    end
    -- Accept both 200 (ready) and 202 (generating)
    if code ~= 200 and code ~= 202 then
        return nil, "Server error (HTTP " .. tostring(code) .. ")"
    end
    local resp, err = rapidjson.decode(table.concat(sink))
    if not resp then return nil, "Bad response: " .. (err or "?") end
    return resp
end

-- ── Public API ─────────────────────────────────────────────────────────────────

--- Quick reachability check.
function Bridge:ping()
    socketutil:set_timeout(self.PING_BLOCK, self.PING_TOTAL)
    local ok, code = http.request(self:url("/ping"))
    socketutil:reset_timeout()
    return ok ~= nil and code == 200
end

--- Conversational query (explain / translate / summarize).
-- params: {text, context, book_title, book_author, mode}
-- Returns (response_text, nil) or (nil, err)
function Bridge:ask(params)
    local resp, err = self:_post("/ask", params)
    if err then return nil, err end
    if resp.error then return nil, resp.error end
    return resp.response
end

--- Initialise X-Ray for a book.
-- params: {book_title, book_author, reading_pct}
-- Returns:
--   {status="ready",      xray={...}, book={...}}  ← cache hit
--   {status="generating", job_id="...", poll_url}  ← background job
-- or (nil, err)
function Bridge:xrayInit(params)
    return self:_post("/xray/init", params)
end

--- Poll an in-progress X-Ray generation job.
-- Returns response table or (nil, err).
function Bridge:xrayStatus(job_id)
    return self:_get("/xray/status/" .. tostring(job_id))
end

--- Report reading progress to keep the bridge cache current.
function Bridge:xrayProgress(book_hash, reading_pct)
    return self:_post("/xray/progress", {
        book_hash   = book_hash,
        reading_pct = reading_pct,
    })
end

return Bridge
