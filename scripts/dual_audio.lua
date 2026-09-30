-- dual_audio.lua - play two audio tracks of one file at once.
--
-- mode=ears:  each track is downmixed to mono; the "left" track goes to the
--             left channel and the "right" track to the right.
-- mode=pairs: each track stays stereo; "left" goes to channels 1-2 and
--             "right" to 3-4 of a 4-channel output - one headphone pair each
--             when those are two devices combined (see tools/audio-pairs.swift).
--
-- Built on mpv's --lavfi-complex, which can decode several audio tracks of
-- the same file simultaneously. Each side's loudness and delay are in a
-- labelled --af filter after it, where they change live (see adjust).
--
-- Keys (rebind in input.conf with `KEY script-binding dual_audio/<name>`):
--   a  toggle          y  swap
--   {  cycle-left      }  cycle-right   (Shift+[ / Shift+]; replace mpv's speed halve/double)
--   h  balance-left    k  balance-right
--   Left / Right  seek-back / seek-forward: exactly seek_step seconds (see seek)
--
-- Both sides may play the same track. Each side also has its own loudness
-- and delay, set from the bubbaTV menu (tools/bubbatv-menu.swift) or with
-- `script-message-to dual_audio set-track|set-gain|set-delay SIDE VALUE`
-- (SIDE 1 = left ear / first pair, 2 = the other; gain in dB, delay in ms).
-- The state is published in user-data/dual_audio for the menu.
-- Loudness and delay are remembered per headphones (see device_key).
--
-- Subtitles can follow the sides (follow_subs, or the follow-subs binding,
-- which has no key of its own): the first side's language as the main
-- subtitles, the other side's as mpv's secondary ones (at the top).
--
-- The calibrate binding (no key of its own either; "Calibrate delay..." in
-- the menu) plays a sync pattern in place of the movie - a flash and a beep
-- once a second - while each side's delay is set by ear. The binding again
-- (Done) brings the movie back where it was.
--
-- Under dualplay -2 (the helper option set), `set-device SIDE NAME` moves a
-- pair to other headphones, and the menu reports the connected outputs with
-- `outputs NAME...`: headphones that drop out pause the movie, and once they
-- are back the combined device is built again.
--
-- Options below are set in script-opts/dual_audio.conf or with
-- --script-opts=dual_audio-KEY=VALUE.

local msg = require "mp.msg"
local options = require "mp.options"
local utils = require "mp.utils"

local opts = {
    auto = false,       -- turn on automatically for files with 2+ audio tracks (1+ in pairs mode)
    mode = "ears",      -- "ears" or "pairs" (see above)
    left_lang = "",     -- preferred languages for the left ear / first pair, e.g. "eng,en"
    right_lang = "",    -- preferred languages for the right ear / second pair, e.g. "rus,ru"
    pair_names = "",    -- "first|second": device names shown on the OSD in pairs mode
    helper = "",        -- pairs mode: tools/audio-pairs, to rebuild the combined device (dualplay sets it)
    balance_step = 2,   -- dB per balance keypress
    follow_subs = false, -- subtitles in both sides' languages (see update_subs)
    devices_file = "~~/dual_audio-devices.json",  -- remembered loudness and delay; "" for none
    seek_step = 5,      -- seconds that Left / Right and the menu's Back / Forward move
    seek_keys = true,   -- Left / Right seek exactly; no: mpv's own (keyframe) seeks stay on them
}
options.read_options(opts, "dual_audio")
local pairs_mode = opts.mode == "pairs"
-- A file with one track still needs the graph in pairs mode: played as it
-- is, it would reach only the first pair (channels 1-2 of the 4). With one
-- pair, one track plays as it is, in both ears.
local min_tracks = pairs_mode and 1 or 2

local side_names = pairs_mode and { "Pair 1", "Pair 2" } or { "Left ear", "Right ear" }
local sides_word = pairs_mode and "pairs" or "ears"
local devices = {}      -- pairs mode: each pair's device, from pair_names
for name in opts.pair_names:gmatch("[^|]+") do devices[#devices + 1] = name end
local lost = {}         -- pairs mode: side -> true while its headphones are gone
local queue = {}        -- pairs mode: device changes for the helper, the running one first

-- The pairs play on one device combining both headphones (dualplay -2), so
-- the helper that built it can build it again from other headphones.
local function can_combine() return pairs_mode and opts.helper ~= "" and #devices == 2 end

local enabled = opts.auto
local left, right       -- track ids for the current file (may be the same)
local gain = { 0, 0 }   -- dB per side: left ear / first pair, then the other
local delay = { 0, 0 }  -- ms per side; negative plays that side earlier
local ours = false      -- lavfi-complex currently holds our graph
-- Languages to look for in the next file; follows manual track changes so a
-- playlist of episodes keeps the same assignment.
local pref = { left = opts.left_lang, right = opts.right_lang }
local follow = opts.follow_subs
local sub_props = { "sid", "secondary-sid" }  -- the first side's subtitles, then the other's
local sub_shown = { "sub-visibility", "secondary-sub-visibility" }  -- both slots; hidden while calibrating
local subs_set          -- the subtitle ids we selected while following, by slot; nil otherwise
local loading = true    -- a file is loading: mpv picks its own subtitles until on_preloaded
local calib             -- while calibrating: where the pattern starts and what to put back (see calibrate)

-- The tracks of one kind, and how many there are of any kind.
local function tracks_of(kind)
    local all, list = mp.get_property_native("track-list", {}), {}
    for _, t in ipairs(all) do
        if t.type == kind then list[#list + 1] = t end
    end
    return list, #all
end

local function audio_tracks() return tracks_of("audio") end

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
    -- With a single track (pairs mode) both pairs play it.
    right = pref.same and left or right or first_other(tracks, left) or left
end

local function remember(tracks)
    pref.left = by_id(tracks, left).lang or ""
    pref.right = by_id(tracks, right).lang or ""
    pref.same = left == right
end

local function input(src, pad)
    return string.format("%saresample=48000,aformat=channel_layouts=%s[%s];",
        src, pairs_mode and "stereo" or "mono", pad)
end

-- The tracks, joined: ears FL | FR, pairs FL FR | BL BR.
local function graph()
    local l, r = "[aid" .. left .. "]", "[aid" .. right .. "]"
    local split = ""
    -- A track feeds only one filter, so split it when both sides play it.
    if left == right then split, l, r = l .. "asplit=2[sl][sr];", "[sl]", "[sr]" end
    local g = split .. input(l, "l") .. input(r, "r")
    if pairs_mode then
        return g .. "[l][r]join=inputs=2:channel_layout=quad:map=0.0-FL|0.1-FR|1.0-BL|1.1-BR[ao]"
    end
    return g .. "[l][r]join=inputs=2:channel_layout=stereo:map=0.0-FL|1.0-FR[ao]"
end

-- Output channels in order; the first half is side 1's.
local channels = pairs_mode and { "FL", "FR", "BL", "BR" } or { "FL", "FR" }
local function side_of(c) return c <= #channels / 2 and 1 or 2 end

-- adelay only holds a side back, so it holds back the later side by the
-- difference, and asetpts moves the whole output by the earlier side's
-- delay, either way. Not mpv's audio-delay: mpv writes that into
-- watch-later files (inside the quit command, with no event before it),
-- and the next start would be off by the shift. The earlier side stays
-- undelayed: were every channel held back, adelay would start with silence
-- that has no timestamps.
local function base_delay() return math.min(delay[1], delay[2]) end

-- adelay's "delays": ms per output channel.
local function delays()
    local list = {}
    for c = 1, #channels do list[c] = delay[side_of(c)] - base_delay() end
    return table.concat(list, "|")
end

-- asetpts's "expr".
local function shift() return string.format("PTS%+g/TB", base_delay() / 1000) end

-- Loudness and delay on the joined channels, as a labelled --af filter:
-- af-command reaches filters there, not those inside lavfi-complex. The
-- channels of one side share the name volume@dual_audioN, so one command
-- sets them all.
local function af_graph()
    local layout = pairs_mode and "quad" or "stereo"
    local outs, volumes, ins, map = "", "", "", {}
    for c, name in ipairs(channels) do
        outs = outs .. "[c" .. c .. "]"
        volumes = volumes .. string.format("[c%d]volume@dual_audio%d=%gdB[v%d];",
            c, side_of(c), gain[side_of(c)], c)
        ins = ins .. "[v" .. c .. "]"
        map[c] = (c - 1) .. ".0-" .. name
    end
    return string.format("channelsplit=channel_layout=%s%s;%s%sjoin=inputs=%d:channel_layout=%s:map=%s,"
        .. "adelay@dual_audio=delays=%s,asetpts@dual_audio=%s", layout, outs, volumes, ins, #channels, layout,
        table.concat(map, "|"), delays(), shift())
end

-- Our --af graph as mpv has it, nil if none. Looked up rather than kept:
-- mpv saves the --af list in watch-later files and sets it again when it
-- resumes, and the user can change the list too.
local function installed()
    for _, f in ipairs(mp.get_property_native("af", {})) do
        if f.label == "dual_audio" then return f.params and f.params.graph end
    end
end

-- Installs our --af filter in front of the user's own (they are left as
-- they are), updates it, or takes it out. mpv replaces a filter whose
-- string changes, losing the audio it holds, so this happens only when the
-- sound breaks anyway: graph changes, seeks and a reopened audio output.
local function set_af(want)
    local g = want and af_graph() or nil
    if g == installed() then return end
    if g then
        mp.commandv("af", "pre", "@dual_audio:lavfi=[" .. g .. "]")
    else
        mp.commandv("af", "remove", "@dual_audio")
    end
end

-- Loudness and delay changes: sent to the running --af filter, so they are
-- heard at once, with no graph rebuild and no seek. A new shift moves the
-- sound against the picture, which then slows down or speeds up for a
-- moment to catch up. The filter's string still has the old values; it
-- catches up when mpv empties the filter anyway (see catch_up).
local function adjust()
    local ok = true
    local function send(cmd, arg, target)
        ok = mp.commandv("af-command", "dual_audio", cmd, arg, target) and ok
    end
    for i = 1, 2 do send("volume", string.format("%gdB", gain[i]), "volume@dual_audio" .. i) end
    send("delays", delays(), "adelay@dual_audio")
    send("expr", shift(), "asetpts@dual_audio")
    -- mpv builds the filter when audio first reaches it; until then there
    -- is nothing in it to lose, so the new string can go in straight away.
    if not ok then set_af(true) end
end

local function label(tracks, id)
    local t = by_id(tracks, id)
    local s = string.format("[%d] %s", id, t.title or t.lang or "untitled")
    if t.title and t.lang then s = s .. " (" .. t.lang .. ")" end
    return s
end

-- The output mpv plays to by name, or nil for the system default output:
-- mpv only calls that "auto" (the menu looks it up through CoreAudio).
local function output()
    local out = mp.get_property("audio-device", "auto")
    if out == "auto" then return nil end
    for _, d in ipairs(mp.get_property_native("audio-device-list", {})) do
        if d.name == out then return d.description end
    end
    return out
end

-- Loudness and delay are saved per headphones, by name: in pairs mode each
-- pair's device. In ears mode both ears share one output, so each ear of it
-- has an entry, e.g. "system output (left ear)" - mpv's default output,
-- which is whichever headphones the Mac plays to.
local function device_key(i)
    return devices[i] or string.format("%s (%s)", output() or "system output", side_names[i]:lower())
end

-- The saved settings, { [device_key] = { gain = dB, delay = ms } }, as in
-- devices_file. With --no-config "~~/" has no directory to point to (mpv
-- skips its watch-later files then too), so they last only while mpv runs.
local saved = {}
local store = opts.devices_file ~= "" and mp.command_native({ "expand-path", opts.devices_file }) or nil
if store and opts.devices_file:find("^~~") and not store:find("^/") then store = nil end

local function load()
    local f = store and io.open(store)
    if not f then return end
    local t = utils.parse_json(f:read("*a"))
    f:close()
    if type(t) == "table" then saved = t end
end

-- Takes the settings of the sides' devices; true if that changed anything.
local function restore()
    load()
    local changed = false
    for i = 1, 2 do
        local s = saved[device_key(i)]
        s = type(s) == "table" and s or {}
        local g, d = tonumber(s.gain) or 0, math.floor((tonumber(s.delay) or 0) + 0.5)
        changed = changed or g ~= gain[i] or d ~= delay[i]
        gain[i], delay[i] = g, d
    end
    return changed
end

-- After each loudness or delay change. Re-reads the file first to keep
-- what another mpv saved meanwhile; a device back at 0 / 0 is dropped.
local function save()
    load()
    for i = 1, 2 do
        saved[device_key(i)] = (gain[i] ~= 0 or delay[i] ~= 0) and { gain = gain[i], delay = delay[i] } or nil
    end
    if not store then return end
    local tmp = store .. ".tmp"
    local f = io.open(tmp, "w")
    if not f then  -- the first save, before mpv's config dir exists
        mp.command_native({ name = "subprocess", playback_only = false,
                            args = { "mkdir", "-p", (utils.split_path(store)) } })
        f = io.open(tmp, "w")
    end
    local ok = f and f:write(next(saved) and utils.format_json(saved) or "{}")
    if f then f:close() end
    if not (ok and os.rename(tmp, store)) then msg.warn("could not save " .. store) end
end

-- The state as the bubbaTV menu shows it. `device` is left out when mpv
-- plays to the system default output; the menu looks that one up.
-- `file_loaded`: a file is open, paused or not (it has tracks), for the
-- menu's Back / Forward.
local function publish()
    local tracks, any = audio_tracks()
    local list = {}
    for _, t in ipairs(tracks) do list[#list + 1] = { id = t.id, title = label(tracks, t.id) } end
    local sides = {}
    for i, id in ipairs({ left or false, right or false }) do
        sides[i] = { name = side_names[i], device = devices[i] or output(),
                     lost = lost[i], track = id or nil, gain = gain[i], delay = delay[i] }
    end
    mp.set_property_native("user-data/dual_audio", {
        enabled = enabled, active = ours, mode = opts.mode, tracks = list, sides = sides,
        follow_subs = follow, switch_devices = can_combine(), calibrating = calib ~= nil,
        file_loaded = any > 0, seek_step = opts.seek_step,
    })
end

-- Changing lavfi-complex during playback makes mpv stop reading the
-- subtitle streams while sid still names them, so subtitles vanish once the
-- packets already read run out. Selecting them again brings them back.
local function reselect_subs()
    for _, prop in ipairs(sub_props) do
        local id = mp.get_property_number(prop)
        if id then
            mp.set_property(prop, "no")
            mp.set_property_number(prop, id)
        end
    end
end

-- The subtitle track in the language of audio track `id`: a full one before
-- a forced one (that only covers what is said in another language).
local function sub_for(id)
    local lang = by_id(audio_tracks(), id).lang
    local full, forced = {}, {}
    for _, t in ipairs(tracks_of("sub")) do table.insert(t.forced and forced or full, t) end
    return lang and (by_lang(full, lang) or by_lang(forced, lang))
end

-- While following, each side's language gets its subtitle slot: empty if
-- there are none in it, and one line is enough when both sides hear the same
-- language. Selecting them anew also brings them back after a graph change
-- (see reselect_subs). Otherwise the second line goes if it was ours, and the
-- main one stays: the first side's language is what plays with dual audio off.
local function update_subs(live)
    if not (follow and ours) then
        if subs_set then mp.set_property("secondary-sid", "no") end
        subs_set = nil
        if live then reselect_subs() end
        return
    end
    subs_set = { sub_for(left), sub_for(right) }
    if subs_set[2] == subs_set[1] then subs_set[2] = nil end
    -- Clear both first: a track selected in one slot can't be selected in the
    -- other, and after a swap they trade places.
    for _, prop in ipairs(sub_props) do mp.set_property(prop, "no") end
    for i, prop in ipairs(sub_props) do
        if subs_set[i] then mp.set_property_number(prop, subs_set[i]) end
    end
end

-- A subtitle change we didn't make is the user's own pick: stop following
-- instead of fighting it. While a file loads, the changes are mpv's.
local function subs_changed()
    if loading or not subs_set then return end
    for i, prop in ipairs(sub_props) do
        if mp.get_property_number(prop) ~= subs_set[i] then
            follow, subs_set = false, nil
            msg.info("Subtitles picked by hand: they no longer follow the " .. sides_word)
            return publish()
        end
    end
end

local hint = mp.create_osd_overlay("ass-events")  -- on screen while calibrating

-- Installs or removes the graph. `live` means playback is under way: mpv
-- then re-reads the file for the newly selected tracks, and its refresh seek
-- can land on the next keyframe (up to ~10s ahead). A seek of our own issued
-- while that re-read is going on loses its keyframe the same way, so drop
-- the re-read first, then seek to the exact position.
local function apply(live)
    local tracks = audio_tracks()
    local want = enabled and #tracks >= min_tracks
    -- A new graph ends calibrating: the movie comes back as it was then.
    local back = calib
    calib = nil
    if not want and not ours then
        set_af(false)  -- there may be one from a watch-later file
        return publish()
    end
    if want and not (by_id(tracks, left) and by_id(tracks, right)) then pick(tracks) end
    local pos = live and (back and back.pos or mp.get_property_number("time-pos"))
    -- Our --af filter goes in after the graph and out before it, so it only
    -- ever gets the joined channels.
    if not want then set_af(false) end
    mp.set_property("lavfi-complex", want and graph() or "")
    if want then set_af(true) end
    ours = want
    -- Back to normal playback: keep the left-ear / first-pair language.
    if not want and left then mp.set_property_number("aid", left) end
    if back then
        -- The pattern had the picture's place (a new file picks its own).
        if live and back.vid then mp.set_property_number("vid", back.vid) end
        for i, prop in ipairs(sub_shown) do mp.set_property_native(prop, back.shown[i]) end
        hint:remove()
    end
    update_subs(live)
    if pos then
        mp.commandv("drop-buffers")
        mp.commandv("seek", pos, "absolute+exact")
    end
    -- Headphones that dropped out meanwhile keep the movie paused.
    if back then mp.set_property_bool("pause", back.pause or next(lost) ~= nil) end
    publish()
end

-- ", -2 dB, +120 ms" for a side that isn't at the defaults.
local function tweaks(i)
    local s = ""
    if gain[i] ~= 0 then s = s .. string.format(", %+g dB", gain[i]) end
    if delay[i] ~= 0 then s = s .. string.format(", %+d ms", delay[i]) end
    return s
end

-- On the OSD, and on one line in the log.
local function say(text, secs)
    msg.info((text:gsub("\n", " | ")))
    mp.osd_message(text, secs)
end

local function show()
    local tracks = audio_tracks()
    local text
    if not enabled then
        text = "Dual audio: off"
    elseif not ours then
        text = string.format("Dual audio: needs %d+ audio tracks, this file has %d", min_tracks, #tracks)
    else
        text = string.format("Dual audio\n%s: %s%s\n%s: %s%s",
            devices[1] or side_names[1], label(tracks, left), tweaks(1),
            devices[2] or side_names[2], label(tracks, right), tweaks(2))
    end
    say(text, 4)
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

-- New loudness or delay: live while dual audio is on, otherwise kept for
-- when it is.
local function tune()
    if ours then adjust() end
    publish()
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
    save()
    tune()
    show()
end

-- Only the subtitles change, so the graph stays as it is.
local function toggle_follow()
    follow = not follow
    update_subs(false)
    publish()
    mp.osd_message(string.format("Subtitles follow the %s: %s", sides_word, follow and "on" or "off"), 3)
end

-- The sync pattern: a white flash and a beep on every channel (1 kHz at
-- -20 dBFS, like a film's sync pop), together for 0.1 s each second,
-- generated in the graph instead of read from the file. The beep goes
-- through our --af filter like the movie's sound, so each side hears it as
-- late as it hears the movie. The sources start at 0; setpts moves them to
-- calib.from, the whole second (exact in both time bases) where calibrating
-- started or a seek landed, so the position goes on from there. The picture
-- has the movie's size: mpv drops the video track for a [vo] in the graph,
-- and without --force-window its window too, and opens the new one at the
-- picture's size. (Back to the track, the window stays.)
local function pattern()
    local on = "lt(mod(t,1),0.1)"
    return string.format("color=black:s=%dx%d:r=25,drawbox=c=white:t=fill:enable='%s',setpts=PTS+%d/TB[vo];"
        .. "aevalsrc=exprs='0.1*sin(2*PI*1000*t)*%s':s=48000:c=%s,asetpts=PTS+%d/TB[ao]",
        calib.w, calib.h, on, calib.from, on, pairs_mode and "quad" or "stereo", calib.from)
end

-- Calibrate delay... in the menu: the pattern plays, unpaused, while each
-- side's delay is set by ear, live as during the movie. The movie's
-- subtitles would go on meanwhile, so they are hidden. Done (the binding
-- again) puts back the graph, picture, subtitles, position and pause (see
-- apply), and the delays stay, remembered for the headphones as always.
local function calibrate()
    if calib then return apply(true) end
    if not ours then return show() end
    local pos = mp.get_property_number("time-pos")
    local w, h = mp.get_property_number("video-params/dw"), mp.get_property_number("video-params/dh")
    if not (pos and w and h) then return say("Delay calibration needs a video track", 4) end
    calib = { pos = pos, from = math.floor(pos), w = w, h = h, pause = mp.get_property_bool("pause"),
              vid = mp.get_property_number("current-tracks/video/id"), shown = {} }
    for i, prop in ipairs(sub_shown) do
        calib.shown[i] = mp.get_property_native(prop)
        mp.set_property_bool(prop, false)
    end
    mp.set_property("lavfi-complex", pattern())
    set_af(true)  -- with the live values: the sound breaks here anyway
    mp.set_property_bool("pause", false)
    hint.data = string.format("{\\an2\\fs28}Set each %s's Delay in the bubbaTV menu until its beep lands on "
        .. "the flash,\\Nthen choose Done calibrating.", sides_word:sub(1, -2))
    hint:update()
    msg.info("Calibrating the delays")
    publish()
end

-- mpv builds the graph anew on a seek, so the pattern would start over from
-- calib.from: generated and dropped all the way to where a seek forward
-- lands (half a minute for 25 minutes of a 1080p movie), and a seek back
-- would land on calib.from. Start it where the seek lands instead. (Should
-- mpv be playing again by the time this reads the position, a seek back
-- just goes on from calib.from.)
local function follow_seek()
    local pos = calib and mp.get_property_number("time-pos")
    if pos and math.floor(pos) ~= calib.from then
        calib.from = math.floor(pos)
        mp.set_property("lavfi-complex", pattern())
    end
end

-- Left / Right and the menu's Back / Forward, to hear a line again: mpv's
-- own arrow seeks land on a keyframe instead, which can be seconds off
-- (relative seeks aren't exact by default). The OSD shows where it landed.
local function seek(dir)
    mp.commandv("osd-msg-bar", "seek", dir * opts.seek_step, "relative+exact")
end

-- set-track / set-gain / set-delay SIDE VALUE, from the bubbaTV menu.
local setters = {
    ["set-track"] = function(i, id)
        local tracks = audio_tracks()
        if not (ours and by_id(tracks, id)) then return false end
        if i == 1 then left = id else right = id end
        remember(tracks)
        apply(true)
    end,
    ["set-gain"] = function(i, db) gain[i] = db; save(); tune() end,
    ["set-delay"] = function(i, ms) delay[i] = math.floor(ms + 0.5); save(); tune() end,
}
for name, set in pairs(setters) do
    mp.register_script_message(name, function(side, value)
        local i, v = tonumber(side), tonumber(value)
        if (i == 1 or i == 2) and v and set(i, v) ~= false and ours then show() end
    end)
end

-- Builds the combined device again (first pair's output, second pair's) and
-- reopens mpv's audio output on it: it keeps its UID, so --audio-device
-- still names it and dualplay's exit trap still removes it. The helper takes
-- a few seconds, so changes asked for meanwhile wait their turn; `want`
-- gives the two outputs once it is theirs.
local function run_queue()
    local job = queue[1]
    if not job then return end
    local names = job.want()
    mp.command_native_async({ name = "subprocess", args = { opts.helper, "create", names[1], names[2] },
                              capture_stdout = true, capture_stderr = true, playback_only = false },
        function(_, r)
            table.remove(queue, 1)
            if r and r.status == 0 then
                -- It prints UID<TAB>first<TAB>second, with the devices' full names.
                local first, second = r.stdout:match("^[^\t]*\t([^\t]*)\t([^\t\n]*)")
                devices = { first or names[1], second or names[2] }
                lost = {}
                -- Other headphones bring their own loudness and delay. The
                -- reopened output gets our filter built anew from its
                -- string, so that has them, and any live changes, first.
                restore()
                if ours then set_af(true) end
                mp.commandv("ao-reload")
                publish()
                job.done()
            else
                local why = (r and r.stderr or ""):gsub("^audio%-pairs: ", ""):gsub("%s+$", "")
                say("Dual audio: could not switch the headphones\n"
                    .. (why ~= "" and why or "the helper did not run"), 8)
            end
            run_queue()
        end)
end

local function combine(want, done)
    queue[#queue + 1] = { want = want, done = done }
    if #queue == 1 then run_queue() end
end

local function pair(i) return string.format("%s (%s)", side_names[i], devices[i]) end

-- set-device SIDE NAME, from the bubbaTV menu: that pair moves to other
-- headphones (to the other pair's, and the two swap).
mp.register_script_message("set-device", function(side, name)
    local i = tonumber(side)
    if not (can_combine() and (i == 1 or i == 2) and name) then return end
    combine(function()
        local names = { devices[1], devices[2] }
        if names[3 - i] == name then names[3 - i] = names[i] end
        names[i] = name
        return names
    end, show)
end)

-- outputs NAME..., from the bubbaTV menu whenever the connected outputs
-- change. Headphones that drop out pause the movie; once every pair's are
-- back, the combined device (which lost them) is built again - the movie
-- stays paused for the people to get ready.
mp.register_script_message("outputs", function(...)
    if not can_combine() then return end
    local connected, gone, back, missing = {}, {}, {}, false
    for _, name in ipairs({ ... }) do connected[name] = true end
    for i = 1, 2 do
        if not connected[devices[i]] then
            missing = true
            if not lost[i] then gone[#gone + 1] = pair(i) end
            lost[i] = true
        elseif lost[i] then
            back[#back + 1] = pair(i)
        end
    end
    if #gone > 0 then
        publish()
        mp.set_property_bool("pause", true)
        say(table.concat(gone, " and ") .. " dropped out, so the movie is paused.\n"
            .. "Reconnect the headphones to go on.", 10)
    elseif #back > 0 and not missing then
        combine(function() return devices end, function()
            say(table.concat(back, " and ") .. " reconnected."
                .. (mp.get_property_bool("pause") and "\nPress Space to resume." or ""), 10)
        end)
    end
end)

mp.register_event("start-file", function() loading = true end)

-- Before mpv selects the tracks: it takes the subtitles set here too.
mp.add_hook("on_preloaded", 50, function()
    left, right = nil, nil
    restore()
    apply(false)
    loading = false
end)

mp.register_event("file-loaded", function()
    if ours then show() end
end)

-- Tracks come and go (next file, --audio-file, no file), and so does the
-- output - which in ears mode brings its own saved settings.
mp.observe_property("track-list", nil, publish)
mp.observe_property("audio-device", nil, function()
    if restore() then tune() else publish() end
end)
for _, prop in ipairs(sub_props) do mp.observe_property(prop, "number", subs_changed) end

-- mpv builds the --af filter anew from its string after a seek and when it
-- reopens the audio output (another output device, ao-reload), with nothing
-- in the old one to lose: the moments to write live changes into the
-- string. Should mpv get sound through before this runs, that moment still
-- has the old values.
local function catch_up()
    if ours then set_af(true) end
end
mp.register_event("seek", catch_up)
mp.register_event("seek", follow_seek)
mp.register_event("audio-reconfig", catch_up)

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
mp.add_key_binding(nil, "follow-subs", toggle_follow)
mp.add_key_binding(nil, "calibrate", calibrate)
-- Held down, they go on seeking. input.conf lines for Left / Right win over
-- these, as for the other keys.
mp.add_key_binding(opts.seek_keys and "LEFT" or nil, "seek-back", function() seek(-1) end, { repeatable = true })
mp.add_key_binding(opts.seek_keys and "RIGHT" or nil, "seek-forward", function() seek(1) end, { repeatable = true })
