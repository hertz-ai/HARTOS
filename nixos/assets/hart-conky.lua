-- ============================================================
-- HART OS Conky Lua Helper
-- Fetches live data from the HART backend API (localhost:6777)
-- Theme-aware colors from active_theme.json
-- ============================================================

local http = require("socket.http")
local ltn12 = require("ltn12")

-- Configuration
local BACKEND_PORT = os.getenv("HARTOS_BACKEND_PORT") or "6777"
local DATA_DIR = os.getenv("HART_DATA_DIR") or os.getenv("HEVOLVE_DATA_DIR") or "/var/lib/hart"
local BASE_URL = "http://localhost:" .. BACKEND_PORT

-- Cache: avoid hammering API every Conky cycle
-- TTL auto-scales: 5s standard, 10s potato (from theme performance config)
local cache = {}
local cache_ttl = 5  -- default, updated by load_perf_config()
local last_fetch = 0
local perf_loaded = false

-- Theme colors (loaded from active_theme.json)
local theme_colors = {}
local theme_mtime = 0

-- ── HTTP helper ──────────────────────────────────────────

local function http_get(path)
    local body = {}
    local ok, code = http.request{
        url = BASE_URL .. path,
        sink = ltn12.sink.table(body),
        headers = { ["Accept"] = "application/json" },
        -- 2 second timeout to avoid blocking Conky
        create = function()
            local sock = require("socket").tcp()
            sock:settimeout(2)
            return sock
        end,
    }
    if ok and code == 200 then
        return table.concat(body)
    end
    return nil
end

-- ── Simple JSON value extractor ──────────────────────────
-- Lightweight: no external JSON lib needed, just pattern matching

local function json_value(json_str, key)
    if not json_str then return nil end
    -- Match "key": "value" or "key": number
    local pattern = '"' .. key .. '"%s*:%s*"?([^",}]+)"?'
    return json_str:match(pattern)
end

local function json_number(json_str, key)
    if not json_str then return 0 end
    local val = json_value(json_str, key)
    return tonumber(val) or 0
end

-- ── Fetch and cache backend data ─────────────────────────

local function refresh_cache()
    load_perf_config()
    local now = os.time()
    if now - last_fetch < cache_ttl then return end
    last_fetch = now

    -- /status — node health, basic info
    cache.status = http_get("/status")

    -- /api/social/dashboard/agents — agent overview
    cache.dashboard = http_get("/api/social/dashboard/agents")

    -- /api/social/dashboard/health — peer count
    cache.health = http_get("/api/social/dashboard/health")
end

-- ── Read node identity from disk ─────────────────────────

local function read_file(path)
    local f = io.open(path, "r")
    if not f then return nil end
    local content = f:read("*a")
    f:close()
    return content and content:gsub("%s+$", "") or nil
end

local function bytes_to_hex(data)
    if not data then return nil end
    local hex = {}
    for i = 1, math.min(#data, 8) do
        hex[#hex + 1] = string.format("%02x", data:byte(i))
    end
    return table.concat(hex)
end

-- ═════════════════════════════════════════════════════════════
-- Conky display functions (called from .conkyrc via ${lua ...})
-- ═════════════════════════════════════════════════════════════

function conky_hart_node_id()
    local key_data = read_file(DATA_DIR .. "/node_public.key")
    if key_data then
        local hex = bytes_to_hex(key_data)
        return hex and (hex .. "...") or "generating..."
    end
    return "not yet generated"
end

function conky_hart_tier()
    local tier = read_file(DATA_DIR .. "/capability_tier")
    return tier or "detecting..."
end

function conky_hart_peer_count()
    refresh_cache()
    local count = json_number(cache.health, "peer_count")
    if count > 0 then
        return tostring(count)
    end
    -- Fallback: try status endpoint
    count = json_number(cache.status, "peer_count")
    return count > 0 and tostring(count) or "0"
end

function conky_hart_agent_count()
    refresh_cache()
    local count = json_number(cache.dashboard, "active_count")
    if count > 0 then
        return tostring(count)
    end
    count = json_number(cache.dashboard, "total_agents")
    return count > 0 and tostring(count) or "0"
end

function conky_hart_llm_status()
    refresh_cache()
    local model = json_value(cache.status, "llm_model")
    if model and model ~= "" and model ~= "null" then
        return "${color1}" .. model .. "${color}"
    end
    -- Check if llm service is configured
    local llm_port = json_value(cache.status, "llm_port")
    if llm_port then
        return "${color3}loading...${color}"
    end
    return "${color4}peer offload${color}"
end

function conky_hart_current_goal()
    refresh_cache()
    local goal = json_value(cache.dashboard, "current_goal")
    if goal and goal ~= "" and goal ~= "null" then
        -- Truncate long goal names
        if #goal > 18 then
            goal = goal:sub(1, 18) .. "..."
        end
        return goal
    end
    return "${color4}idle${color}"
end

function conky_hart_goal_progress()
    refresh_cache()
    local pct = json_number(cache.dashboard, "goal_progress")
    if pct > 0 then
        return tostring(pct) .. "%"
    end
    return "--"
end

function conky_hart_goal_bar()
    -- Draw a simple text progress bar
    refresh_cache()
    local pct = json_number(cache.dashboard, "goal_progress")
    local filled = math.floor(pct / 5)  -- 20 chars = 100%
    local empty = 20 - filled
    local bar = string.rep("█", filled) .. string.rep("░", empty)
    if pct > 0 then
        return "  ${color1}" .. bar .. "${color}"
    end
    return "  ${color4}" .. string.rep("░", 20) .. "${color}"
end


-- ═════════════════════════════════════════════════════════════
-- Performance config (reads from active_theme.json performance section)
-- Auto-scales cache TTL and graph complexity for potato mode
-- ═════════════════════════════════════════════════════════════

local function load_perf_config()
    if perf_loaded then return end
    local path = DATA_DIR .. '/active_theme.json'
    local f = io.open(path, 'r')
    if not f then return end
    local content = f:read('*a')
    f:close()
    if content then
        local interval = content:match('"conky_update_interval"%s*:%s*(%d+)')
        if interval then
            cache_ttl = tonumber(interval) or 5
        end
    end
    perf_loaded = true
end

-- ═════════════════════════════════════════════════════════════
-- Theme-aware color system
-- Reads active_theme.json (written by ThemeService) every cycle.
-- Falls back to default HART colors if file doesn't exist.
-- ═════════════════════════════════════════════════════════════

local theme_fallback = {
    heading     = '6C63FF',
    active      = '00e676',
    error       = 'FF6B6B',
    caution     = 'ffab40',
    muted       = '78909c',
    default_text = 'b0b0b0',
    background  = '0F0E17',
}

local function load_theme()
    local path = DATA_DIR .. '/active_theme.json'
    local attr = lfs and lfs.attributes or nil

    -- Check mtime to avoid re-parsing unchanged file
    if attr then
        local info = attr(path)
        if info and info.modification == theme_mtime then return end
        if info then theme_mtime = info.modification end
    end

    local f = io.open(path, 'r')
    if not f then return end
    local content = f:read('*a')
    f:close()

    -- Lightweight JSON parse for conky section
    if content then
        for key, _ in pairs(theme_fallback) do
            local val = content:match('"' .. key .. '"%s*:%s*"([^"]+)"')
            if val then
                theme_colors[key] = val
            end
        end
    end
end

function conky_hart_color(name)
    load_theme()
    local hex = theme_colors[name] or theme_fallback[name] or 'b0b0b0'
    return '${color #' .. hex .. '}'
end

function conky_hart_bg_color()
    load_theme()
    local hex = theme_colors['background'] or theme_fallback['background'] or '0F0E17'
    return hex
end

function conky_hart_bg_opacity()
    load_theme()
    local val = theme_colors['background_opacity']
    return tonumber(val) or 180
end
