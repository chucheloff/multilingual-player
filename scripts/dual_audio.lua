-- dual_audio.lua - play two audio tracks of one file at once.
--
-- mode=ears:  each track is downmixed to mono; the "left" track goes to the
--             left channel and the "right" track to the right.
-- mode=pairs: each track stays stereo; "left" goes to channels 1-2 and
--             "right" to 3-4 of a 4-channel output - one headphone pair each
--             when those are two devices combined (see tools/audio-pairs.swift).
--
-- Built on mpv's --lavfi-complex, which can decode several audio tracks of
-- the same file simultaneously.
--
-- Keys (rebind in input.conf with `KEY script-binding dual_audio/<name>`):
--   a  toggle          y  swap
--   {  cycle-left      }  cycle-right   (Shift+[ / Shift+]; replace mpv's speed halve/double)
--   h  balance-left    k  balance-right
--
-- Both sides may play the same track. Each side also has its own loudness
-- and delay, set from the bubbaTV menu (tools/bubbatv-menu.swift) or with
-- `script-message-to dual_audio set-track|set-gain|set-delay SIDE VALUE`
-- (SIDE 1 = left ear / first pair, 2 = the other; gain in dB, delay in ms).
-- The state is published in user-data/dual_audio for the menu.
--
-- Options below are set in script-opts/dual_audio.conf or with
-- --script-opts=dual_audio-KEY=VALUE.

local msg = require "mp.msg"
local options = require "mp.options"

local opts = {
    auto = false,       -- turn on automatically for files with 2+ audio tracks
    mode = "ears",      -- "ears" or "pairs" (see above)
    left_lang = "",     -- preferred languages for the left ear / first pair, e.g. "eng,en"
    right_lang = "",    -- preferred languages for the right ear / second pair, e.g. "rus,ru"
    pair_names = "",    -- "first|second": device names shown on the OSD in pairs mode
    balance_step = 2,   -- dB per balance keypress
}
options.read_options(opts, "dual_audio")
local pairs_mode = opts.mode == "pairs"

local side_names = pairs_mode and { "Pair 1", "Pair 2" } or { "Left ear", "Right ear" }
local devices = {}      -- pairs mode: each pair's device, from pair_names
for name in opts.pair_names:gmatch("[^|]+") do devices[#devices + 1] = name end

local enabled = opts.auto
local left, right       -- track ids for the current file (may be the same)
local gain = { 0, 0 }   -- dB per side: left ear / first pair, then the other
local delay = { 0, 0 }  -- ms per side; negative plays that side earlier
local delay_shift = 0   -- s added to audio-delay for negative delays (see apply)
local ours = false      -- lavfi-complex currently holds our graph
-- Languages to look for in the next file; follows manual track changes so a
-- playlist of episodes keeps the same assignment.
local pref = { left = opts.left_lang, right = opts.right_lang }

local function audio_tracks()
    local list = {}
    for _, t in ipairs(mp.get_property_native("track-list", {})) do
        if t.type == "audio" then list[#list + 1] = t end
    end
    return list
end

local function by_id(tracks, id)
    for _, t in ipairs(tracks) do
        if t.id == id then return t end
    end
end

-- Files tag languages as "en" (newer MKVs, BCP 47) or "eng" (ISO 639-2),
-- sometimes with a region ("pt-BR"), so compare the base language.
local iso639 = {  -- 3-letter codes (both B and T forms) -> 2-letter
    eng = "en", rus = "ru", ukr = "uk", bel = "be", ger = "de", deu = "de",
    fre = "fr", fra = "fr", spa = "es", ita = "it", por = "pt", dut = "nl",
    nld = "nl", pol = "pl", cze = "cs", ces = "cs", slo = "sk", slk = "sk",
    hun = "hu", rum = "ro", ron = "ro", bul = "bg", srp = "sr", hrv = "hr",
    slv = "sl", bos = "bs", mac = "mk", mkd = "mk", gre = "el", ell = "el",
    swe = "sv", nor = "no", dan = "da", fin = "fi", ice = "is", isl = "is",
    lit = "lt", lav = "lv", est = "et", cat = "ca", baq = "eu", eus = "eu",
    glg = "gl", wel = "cy", cym = "cy", gle = "ga", tur = "tr", ara = "ar",
    heb = "he", per = "fa", fas = "fa", hin = "hi", urd = "ur", ben = "bn",
    tam = "ta", tel = "te", tha = "th", vie = "vi", ind = "id", may = "ms",
    msa = "ms", tgl = "tl", fil = "tl", jpn = "ja", kor = "ko", chi = "zh",
    zho = "zh", kaz = "kk", uzb = "uz", aze = "az", geo = "ka", kat = "ka",
    arm = "hy", hye = "hy",
}

local function base_lang(code)
    local c = code:lower():match("^%a+") or ""
    return iso639[c] or c
end

local function by_lang(tracks, langs, except)
    for lang in langs:gmatch("[^,%s]+") do
        for _, t in ipairs(tracks) do
            if t.id ~= except and t.lang and base_lang(t.lang) == base_lang(lang) then
                return t.id
            end
        end
    end
end

local function first_other(tracks, except)
    for _, t in ipairs(tracks) do
        if t.id ~= except then return t.id end
    end
end

-- Preferred languages first; otherwise the track already playing stays in
-- the left ear and the next one goes right. Both sides get the same track
-- if they shared one in the last file, or if the right side's languages
-- match nothing else.
local function pick(tracks)
    left = by_lang(tracks, pref.left)
    right = by_lang(tracks, pref.right, left) or by_lang(tracks, pref.right)
    if not left then
        local cur = mp.get_property_number("current-tracks/audio/id")
        left = (cur ~= right and cur) or first_other(tracks, right)
    end
    right = pref.same and left or right or first_other(tracks, left)
end

local function remember(tracks)
    pref.left = by_id(tracks, left).lang or ""
    pref.right = by_id(tracks, right).lang or ""
    pref.same = left == right
end

-- The graph can only hold a side back, so negative delays shift both sides
-- later by the most negative one, and mpv's audio-delay takes it back.
local function base_delay() return math.min(0, delay[1], delay[2]) end

local function input(src, side, pad)
    local d = delay[side] - base_delay()
    return string.format("%saresample=48000,aformat=channel_layouts=%s,%svolume=%gdB[%s];",
        src, pairs_mode and "stereo" or "mono",
        d > 0 and string.format("adelay=%d:all=1,", d) or "", gain[side], pad)
end

local function graph()
    local l, r = "[aid" .. left .. "]", "[aid" .. right .. "]"
    local split = ""
    -- A track feeds only one filter, so split it when both sides play it.
    if left == right then split, l, r = l .. "asplit=2[sl][sr];", "[sl]", "[sr]" end
    local g = split .. input(l, 1, "l") .. input(r, 2, "r")
    if pairs_mode then
        return g .. "[l][r]join=inputs=2:channel_layout=quad:map=0.0-FL|0.1-FR|1.0-BL|1.1-BR[ao]"
    end
    return g .. "[l][r]join=inputs=2:channel_layout=stereo:map=0.0-FL|1.0-FR[ao]"
end

local function label(tracks, id)
    local t = by_id(tracks, id)
    local s = string.format("[%d] %s", id, t.title or t.lang or "untitled")
    if t.title and t.lang then s = s .. " (" .. t.lang .. ")" end
    return s
end

-- The state as the bubbaTV menu shows it. `device` is left out when mpv
-- plays to the system default output; the menu looks that one up.
local function publish()
    local tracks, list = audio_tracks(), {}
    for _, t in ipairs(tracks) do list[#list + 1] = { id = t.id, title = label(tracks, t.id) } end
    local out = mp.get_property("audio-device", "auto")
    for _, d in ipairs(out ~= "auto" and mp.get_property_native("audio-device-list", {}) or {}) do
        if d.name == out then out = d.description end
    end
    local sides = {}
    for i, id in ipairs({ left or false, right or false }) do
        sides[i] = { name = side_names[i], device = devices[i] or (out ~= "auto" and out or nil),
                     track = id or nil, gain = gain[i], delay = delay[i] }
    end
    mp.set_property_native("user-data/dual_audio", {
        enabled = enabled, active = ours, mode = opts.mode, tracks = list, sides = sides,
    })
end

-- Changing lavfi-complex during playback makes mpv stop reading the
-- subtitle streams while sid still names them, so subtitles vanish once the
-- packets already read run out. Selecting them again brings them back.
local function reselect_subs()
    for _, prop in ipairs({ "sid", "secondary-sid" }) do
        local id = mp.get_property_number(prop)
        if id then
            mp.set_property(prop, "no")
            mp.set_property_number(prop, id)
        end
    end
end

-- Installs or removes the graph. `live` means playback is under way: mpv
-- then re-reads the file for the newly selected tracks, and its refresh seek
-- can land on the next keyframe (up to ~10s ahead). A seek of our own issued
-- while that re-read is going on loses its keyframe the same way, so drop
-- the re-read first, then seek to the exact position.
local function apply(live)
    local tracks = audio_tracks()
    local want = enabled and #tracks >= 2
    if not want and not ours then return publish() end
    if want and not (by_id(tracks, left) and by_id(tracks, right)) then pick(tracks) end
    local pos = live and mp.get_property_number("time-pos")
    mp.set_property("lavfi-complex", want and graph() or "")
    ours = want
    local shift = want and base_delay() / 1000 or 0
    if shift ~= delay_shift then
        mp.set_property_number("audio-delay",
            mp.get_property_number("audio-delay", 0) + shift - delay_shift)
        delay_shift = shift
    end
    -- Back to normal playback: keep the left-ear / first-pair language.
    if not want and left then mp.set_property_number("aid", left) end
    if live then reselect_subs() end
    if pos then
        mp.commandv("drop-buffers")
        mp.commandv("seek", pos, "absolute+exact")
    end
    publish()
end

-- ", -2 dB, +120 ms" for a side that isn't at the defaults.
local function tweaks(i)
    local s = ""
    if gain[i] ~= 0 then s = s .. string.format(", %+g dB", gain[i]) end
    if delay[i] ~= 0 then s = s .. string.format(", %+d ms", delay[i]) end
    return s
end

local function show()
    local tracks = audio_tracks()
    local text
    if not enabled then
        text = "Dual audio: off"
    elseif not ours then
        text = "Dual audio: needs 2+ audio tracks, this file has " .. #tracks
    else
        text = string.format("Dual audio\n%s: %s%s\n%s: %s%s",
            devices[1] or side_names[1], label(tracks, left), tweaks(1),
            devices[2] or side_names[2], label(tracks, right), tweaks(2))
    end
    msg.info((text:gsub("\n", " | ")))
    mp.osd_message(text, 4)
end

local function toggle()
    enabled = not enabled
    apply(true)
    show()
end

-- Next track for one side, the other side's track included.
local function cycle(side)
    if not ours then return show() end
    local tracks = audio_tracks()
    local cur = side == "left" and left or right
    local nxt = tracks[1].id
    for i, t in ipairs(tracks) do
        if t.id == cur then nxt = tracks[i % #tracks + 1].id end
    end
    if side == "left" then left = nxt else right = nxt end
    remember(tracks)
    apply(true)
    show()
end

local function swap()
    if not ours or left == right then return show() end
    left, right = right, left
    remember(audio_tracks())
    apply(true)
    show()
end

-- h / k: make one side louder than the other - by bringing it back up if
-- it is below 0 dB, otherwise by turning the other side down.
local function nudge(side)
    if not ours then return show() end
    if gain[side] < 0 then
        gain[side] = math.min(0, gain[side] + opts.balance_step)
    else
        gain[3 - side] = gain[3 - side] - opts.balance_step
    end
    apply(true)
    show()
end

-- set-track / set-gain / set-delay SIDE VALUE, from the bubbaTV menu.
local setters = {
    ["set-track"] = function(i, id)
        local tracks = audio_tracks()
        if not (ours and by_id(tracks, id)) then return false end
        if i == 1 then left = id else right = id end
        remember(tracks)
    end,
    ["set-gain"] = function(i, db) gain[i] = db end,
    ["set-delay"] = function(i, ms) delay[i] = math.floor(ms + 0.5) end,
}
for name, set in pairs(setters) do
    mp.register_script_message(name, function(side, value)
        local i, v = tonumber(side), tonumber(value)
        if (i == 1 or i == 2) and v and set(i, v) ~= false then
            apply(true)
            if ours then show() end
        end
    end)
end

mp.add_hook("on_preloaded", 50, function()
    left, right = nil, nil
    apply(false)
end)

mp.register_event("file-loaded", function()
    if ours then show() end
end)

-- Tracks come and go (next file, --audio-file), and so does the output.
mp.observe_property("track-list", nil, publish)
mp.observe_property("audio-device", nil, publish)

-- Pairs mode on an ordinary stereo device folds channels 3-4 into 1-2, so
-- everyone hears both languages. Say so instead of leaving it a mystery.
if pairs_mode then
    mp.observe_property("audio-out-params/channel-count", "number", function(_, n)
        if ours and n and n < 4 then
            local text = string.format("Dual audio: the output device has %d channels, "
                .. "so both languages are mixed.\nPlay with dualplay -2 to use two headphone pairs.", n)
            msg.warn((text:gsub("\n", " ")))
            mp.osd_message(text, 8)
        end
    end)
end

mp.add_key_binding("a", "toggle", toggle)
mp.add_key_binding("{", "cycle-left", function() cycle("left") end)
mp.add_key_binding("}", "cycle-right", function() cycle("right") end)
mp.add_key_binding("y", "swap", swap)
mp.add_key_binding("h", "balance-left", function() nudge(1) end)
mp.add_key_binding("k", "balance-right", function() nudge(2) end)
