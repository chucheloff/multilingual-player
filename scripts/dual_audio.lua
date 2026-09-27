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

local side_names = { "Left ear", "Right ear" }
if pairs_mode then
    side_names = { "Pair 1", "Pair 2" }
    local i = 1
    for name in opts.pair_names:gmatch("[^|]+") do
        side_names[i], i = name, i + 1
    end
end

local enabled = opts.auto
local left, right       -- track ids for the current file
local balance = 0       -- dB; > 0 quiets the left ear, < 0 quiets the right ear
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
-- the left ear and the next one goes right.
local function pick(tracks)
    left = by_lang(tracks, pref.left)
    right = by_lang(tracks, pref.right, left)
    if not left then
        local cur = mp.get_property_number("current-tracks/audio/id")
        left = (cur ~= right and cur) or first_other(tracks, right)
    end
    right = right or first_other(tracks, left)
end

local function remember(tracks)
    pref.left = by_id(tracks, left).lang or ""
    pref.right = by_id(tracks, right).lang or ""
end

local function input(id, layout, gain, pad)
    return string.format(
        "[aid%d]aresample=48000,aformat=channel_layouts=%s,volume=%gdB[%s];",
        id, layout, gain, pad)
end

local function graph()
    local l, r = math.min(0, -balance), math.min(0, balance)
    if pairs_mode then
        return input(left, "stereo", l, "l") .. input(right, "stereo", r, "r")
            .. "[l][r]join=inputs=2:channel_layout=quad:map=0.0-FL|0.1-FR|1.0-BL|1.1-BR[ao]"
    end
    return input(left, "mono", l, "l") .. input(right, "mono", r, "r")
        .. "[l][r]join=inputs=2:channel_layout=stereo:map=0.0-FL|1.0-FR[ao]"
end

-- Installs or removes the graph. `live` means playback is under way: mpv's
-- refresh seek for the newly decoded tracks lands on the next keyframe
-- (up to ~10s ahead), so seek back to the exact position. The seek has to
-- wait a tick - issued right away it is lost when paused.
local function apply(live)
    local tracks = audio_tracks()
    local want = enabled and #tracks >= 2
    if not want and not ours then return end
    if want and not (by_id(tracks, left) and by_id(tracks, right) and left ~= right) then
        pick(tracks)
    end
    local pos = live and mp.get_property_number("time-pos")
    mp.set_property("lavfi-complex", want and graph() or "")
    ours = want
    -- Back to normal playback: keep the left-ear / first-pair language.
    if not want and left then mp.set_property_number("aid", left) end
    if pos then
        mp.add_timeout(0, function() mp.commandv("seek", pos, "absolute+exact") end)
    end
end

local function label(tracks, id)
    local t = by_id(tracks, id)
    local s = string.format("[%d] %s", id, t.title or t.lang or "untitled")
    if t.title and t.lang then s = s .. " (" .. t.lang .. ")" end
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
        text = string.format("Dual audio\n%s: %s\n%s: %s",
            side_names[1], label(tracks, left), side_names[2], label(tracks, right))
        if balance ~= 0 then
            text = text .. string.format("\n%s %g dB quieter",
                side_names[balance > 0 and 1 or 2], math.abs(balance))
        end
    end
    msg.info((text:gsub("\n", " | ")))
    mp.osd_message(text, 4)
end

local function toggle()
    enabled = not enabled
    apply(true)
    show()
end

-- Next track for one ear, skipping the one the other ear is using.
local function cycle(side)
    if not ours then return show() end
    local tracks = audio_tracks()
    local cur, other = left, right
    if side == "right" then cur, other = right, left end
    local ids = {}
    for _, t in ipairs(tracks) do
        if t.id ~= other then ids[#ids + 1] = t.id end
    end
    local nxt = ids[1]
    for i, id in ipairs(ids) do
        if id == cur then nxt = ids[i % #ids + 1] end
    end
    if nxt ~= cur then
        if side == "left" then left = nxt else right = nxt end
        remember(tracks)
        apply(true)
    end
    show()
end

local function swap()
    if not ours then return show() end
    left, right = right, left
    remember(audio_tracks())
    apply(true)
    show()
end

local function nudge(db)
    if not ours then return show() end
    balance = balance + db
    apply(true)
    show()
end

mp.add_hook("on_preloaded", 50, function()
    left, right = nil, nil
    apply(false)
end)

mp.register_event("file-loaded", function()
    if ours then show() end
end)

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
mp.add_key_binding("h", "balance-left", function() nudge(-opts.balance_step) end)
mp.add_key_binding("k", "balance-right", function() nudge(opts.balance_step) end)
