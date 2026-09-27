# multilingual-player

Watch a movie with a friend in two languages at once, on one Mac:

- **Two pairs of AirPods:** each person gets their language in stereo on their own pair.
- **One pair, shared:** one bud each, a language per ear.

It's a plugin for [mpv](https://mpv.io) (`scripts/dual_audio.lua`) plus a
small launcher (`dualplay`), with a **bubbaTV** menu in mpv's menu bar for
each pair's headphones, track, loudness and delay, and subtitles in both
languages. Loudness and delay are remembered per headphones, and a sync
pattern helps set the delays. The **bubbaTV** app starts a movie without the
terminal.

## Quick start

```sh
brew install mpv
./dualplay -2 -l eng -r rus movie.mkv    # two pairs: eng on one, rus on the other
./dualplay -l eng -r rus movie.mkv       # one pair: eng left ear, rus right ear
```

`-l` / `-r` take language codes: `en` and `eng` both work, whichever the file
uses (mpv lists the tracks at startup). Give a list for fallbacks: `-r fre,eng`.
If a language has several tracks (dub, voice-overs…), the first one is used;
press `{` / `}` (Shift+`[` / Shift+`]`) to step through the others, or pick one in the
bubbaTV menu. Both sides can play the same track, e.g. `-l eng -r eng`. Without `-l` / `-r`, the track that
would normally play comes first and the next one second. Any other mpv option works after
the file name, e.g. `--sub-lang=eng`.

## bubbaTV.app: without the terminal

```sh
tools/make-app.sh ~/Applications/bubbaTV.app
```

Then drop a movie on **bubbaTV** (keep it in the Dock, or in Finder), or
right-click a movie → Open With → bubbaTV. Opened on its own, it offers to pick
one. It plays with `dualplay`: on two pairs (`-2`) when exactly two Bluetooth
headphones are connected, otherwise on one shared pair, an ear each. The
languages are `left_lang` / `right_lang` from `dual_audio.conf` (see
[Install as a plugin](#install-as-a-plugin)); without them, the track that
would normally play comes first. Several files play as a playlist, in name
order. bubbaTV quits when mpv does, and quitting bubbaTV stops mpv.

- It is only offered under Open With: it never becomes the default app for movies.
- Problems (mpv not installed, nothing to play, playback failing) show up in an alert.
  mpv's output is in `~/Library/Logs/bubbaTV.log`.
- The app runs `dualplay` from this folder (the path is built in), so a
  `git pull` here updates it too, and what `dualplay` compiles stays in
  `tools/.build`, not in the app. If you move the folder, run `make-app.sh`
  again. The app also keeps the `PATH` of the shell that built it, so it finds
  `mpv` and `swiftc` as that shell does.
- `make-app.sh` replaces an older bubbaTV, but never another app of that name.
  Without an argument it builds `tools/.build/bubbaTV.app`. To see
  what the app would run: `~/Applications/bubbaTV.app/Contents/MacOS/bubbaTV --dry-run movie.mkv`.
- If macOS asks whether bubbaTV may access a folder (Downloads, Documents…),
  allow it: mpv reads the movie on its behalf.

## Two pairs of AirPods

1. Connect both pairs to the Mac (Control Center → Bluetooth, click each).
   Neither has to be the selected sound output, but a freshly connected pair
   may not show up as an audio device until you pick it once in
   Control Center → Sound.
2. `./dualplay -2 movie.mkv` — it prints which pair gets which track, and so
   does the on-screen message. Press `y` if it's the wrong way round.

Other headphones or speakers work too: `-2` picks the two connected Bluetooth
outputs, or name them with `-p` (`./dualplay -L` lists the outputs), e.g.
`./dualplay -2 -p "Nikita" -p "MacBook Pro Speakers" movie.mkv`.

**Other headphones mid-movie:** each pair's **Device** in the bubbaTV menu
lists the connected outputs; pick one and that pair plays there (pick the
other pair's and the two swap). Switching takes a few seconds of silence.
The headphones bring their own loudness and delay
([remembered](#remembered-per-headphones)).

**Headphones that drop out** (flat battery, out of range) pause the movie,
and the screen says which pair it was. Reconnect them (Control Center →
Bluetooth); once they're back the sound is set up again and the screen says
to press Space. If that fails, the screen says why: pick the headphones in
**Device** again to retry.

Volume: `9` / `0` changes both, `h` / `k` makes one pair quieter than the other,
and the bubbaTV menu sets each pair's loudness on its own.
If speech lags the picture, `Ctrl+-` plays the audio 100 ms earlier (`Ctrl++` later);
if only one pair lags (Bluetooth headphones differ), give that pair a negative delay
in the bubbaTV menu: **Calibrate delay…** there flashes and beeps once a second to set
it by ([below](#calibrating-the-delays)).
Both are remembered for those headphones, so you set them once
([below](#remembered-per-headphones)).

## The bubbaTV menu

`dualplay` adds a **bubbaTV** menu to mpv's menu bar (next to Playback). With
dual audio on it has a group per pair (or ear):

- **Device** — the headphones (or output) that pair plays on; with `-2`, pick
  other connected headphones here (marked "disconnected" while they're gone)
- **Audio track** — the track it plays; pick any, the other pair's too
- **Loudness** — −30 … +6 dB, on top of mpv's volume
- **Delay** — −1000 … +1000 ms, on top of mpv's audio delay (`Ctrl+-` / `Ctrl++`)

plus switches for dual audio on/off, **Calibrate delay…**
([below](#calibrating-the-delays)), swapping the pairs and
**Subtitles follow pairs** (below). Loudness and delay change as you drag,
without restarting the sound, so you can tune them by ear while the movie
plays: a delay change makes that pair skip or pause for the difference, and
when the earlier pair's delay changes, the picture runs a little slower or
faster for a couple of seconds to catch up. Click a value to reset it to 0.
Picking a track still restarts the sound for a moment.

The menu is a native mpv plugin (`tools/bubbatv-menu.swift`). `dualplay`
compiles it on first run with `swiftc` (`xcode-select --install`) against the
installed mpv; without `swiftc`, mpv plays as before, just without the menu.
The script takes the same settings from anywhere:
`script-message-to dual_audio set-track|set-gain|set-delay SIDE VALUE`
(SIDE 1 = left ear / first pair, 2 = the other; dB, ms), and with `-2`
`set-device SIDE NAME` (the output's full name, as the menu lists it). The menu
reports the connected outputs to the script with `outputs NAME...`.

## Calibrating the delays

Bluetooth headphones play late, some more than others. mpv makes up for the
latency macOS reports, but that isn't always right, and with two pairs on one
combined device it is one figure for both. To line each pair up with the
picture:

1. Choose **Calibrate delay…** in the bubbaTV menu. The movie makes way for a
   sync pattern: the screen flashes white and both pairs beep at the same
   moment, once a second.
2. Each of you moves your own pair's **Delay** in the menu until your beep
   lands on the flash: if it comes after the flash, lower the delay (below 0
   plays that pair earlier); if before, raise it.
3. Choose **Done calibrating**. The movie is back where it was, paused or
   playing as before, with the same tracks and subtitles and the new delays,
   [remembered](#remembered-per-headphones) for those headphones.

- The beep goes through each pair's delay and loudness just as the movie's
  sound does, so what lines up here lines up in the movie. mpv's own audio
  delay (`Ctrl+-` / `Ctrl++`) moves both beeps.
- With one shared pair, each ear beeps, and each ear's delay is set the same way.
- mpv makes the pattern itself, so it needs no extra file. It has the
  movie's size, but mpv closes its window and opens a new one as the pattern
  starts ([below](#how-it-works)); Done keeps that one. Subtitles are hidden
  meanwhile.
- Picking a track, swapping, dual audio off and the next file end it too,
  the same way. Seeking moves the pattern along; Done still goes back to
  where you were.
- Headphones that drop out meanwhile keep the movie paused after Done.
- It has no key of its own: `Alt+c script-binding dual_audio/calibrate` in
  `input.conf` gives it one (press it again for Done).
- Quit after Done, not while calibrating: `Shift+Q` would save the pattern's
  position, and the hidden subtitles, for the next start.

## Subtitles in both languages

Tick **Subtitles follow pairs** in the bubbaTV menu (with one shared pair:
**Subtitles follow ears**) and each pair gets subtitles in the language it
hears: the first pair's at the bottom (mpv's main subtitles), the second
pair's at the top (mpv's secondary subtitles). They change along with the
pairs' tracks — `{` / `}`, `y`, the menu — and carry on into the next file.
To have it on from the start, put `follow_subs=yes` in `dual_audio.conf`
(below) or add `--script-opts-append=dual_audio-follow_subs=yes` after the
file name.

- A language without subtitles leaves its line empty; the other line stays in its place.
- Both pairs hear the same language: one line, at the bottom.
- Full subtitles come before forced ones (those only translate the bits said in another language).
- Pick subtitles yourself (`j` / `J`, mpv's menus) and they stop following:
  yours stay, and the menu item unticks. Tick it again to go back.
  Hiding them (`v` bottom, `Alt+v` top) is not a pick.
- Unticking it, or dual audio off, removes the top line and keeps the bottom
  one: the first pair's language is what plays with dual audio off.
- The top line is plain text (mpv drops its styling), and picture subtitles
  (PGS, VobSub) can't move up there, so as a second line they overlap the first.

## Remembered per headphones

A pair's loudness and delay — from the menu, `h` / `k` or a script message —
are saved for its headphones as soon as they change, and come back whenever
those headphones play again: the next file, the next launch, and after `y`,
with the pairs picked the other way round (`-p B -p A`) or when a pair moves
to other headphones in the menu's **Device**. They stay with the headphones,
not with the track or language. Pairs are told apart by name, so if both are
called just "AirPods", rename one in its Bluetooth settings.

With one shared pair, each ear keeps its own. The script can't see which
device the Mac's sound output is (mpv only calls it "auto"), so the ears are
saved as the left and right ear of "system output": whichever headphones the
Mac plays to. With `--audio-device=…` that device's ears get their own.

They are kept in `~/.config/mpv/dual_audio-devices.json`, one entry each:

```json
{"Nikita’s AirPods Pro": {"gain": -4, "delay": 0}, "Friend’s Beats": {"gain": 0, "delay": -120},
 "system output (left ear)": {"gain": -2, "delay": 0}}
```

Setting both back to 0 forgets a pair. `devices_file` in the options below
moves the file, or turns this off (`devices_file=`); with `mpv --no-config`
they last only until mpv quits.

## Keys

| Key | Action |
| --- | --- |
| `a` | dual audio on/off (off plays only the first track) |
| `{` (Shift+`[`) | next track for the **left** ear / first pair (the other side's track included) |
| `}` (Shift+`]`) | next track for the **right** ear / second pair |
| `y` | swap |
| `h` / `k` | balance: make the left ear / first pair louder, or the other side (2 dB steps) |

Shift is required so a stray keypress can't switch the language mid-movie.
`{` / `}` take over mpv's halve / double speed keys; `[` / `]` still change the
speed by −/+10% and Backspace resets it.

Rebind in `~/.config/mpv/input.conf`, e.g. `Ctrl+d script-binding dual_audio/toggle`
(names: `toggle`, `cycle-left`, `cycle-right`, `swap`, `balance-left`, `balance-right`).
Subtitles follow pairs has no key of its own: `Alt+j script-binding dual_audio/follow-subs`
gives it one, and `Alt+c script-binding dual_audio/calibrate` does for
Calibrate delay… (again for Done).
Lines there win over the plugin's keys, so `{ multiply speed 0.5` and
`} multiply speed 2.0` bring the speed keys back.

## Install as a plugin

Makes it available in every mpv window (press `a`); `dualplay` then picks up
the installed copy too.

```sh
mkdir -p ~/.config/mpv/scripts && ln -s "$PWD/scripts/dual_audio.lua" ~/.config/mpv/scripts/
# the bubbaTV menu too, once dualplay has built it:
ln -s "$PWD/tools/.build/bubbatv.so" ~/.config/mpv/scripts/
```

Optional defaults in `~/.config/mpv/script-opts/dual_audio.conf`:

```ini
# turn on for every file with 2+ audio tracks (comments need their own line)
auto=yes
left_lang=eng,en
right_lang=rus,ru
balance_step=2
# subtitles in both pairs' languages
follow_subs=yes
# where each headphones' loudness and delay are remembered (~~ = mpv's config dir)
devices_file=~~/dual_audio-devices.json
```

## Good to know

- **Separate dub file:** `./dualplay -r rus movie.mkv --audio-file=movie.rus.mka`
- **Series:** in a playlist, the next episode keeps the languages you chose.
- **Quit and resume** (`Shift+Q`) saves your own audio delay (`Ctrl+-` / `Ctrl++`)
  as mpv does; the pairs' delays come back from their headphones.
- **With one shared pair, each ear gets a mono downmix** of its track. For 5.1 tracks the centre
  channel (dialogue) keeps its full level, so a stereo and a 5.1 track sound
  about equally loud. If one is quieter, use `h` / `k`.
- Video and seeking are untouched, and so are the subtitles unless they
  follow the pairs: the graph only handles audio (while calibrating, the
  pattern's picture too).

## How it works

mpv's `--lavfi-complex` feeds several tracks of one file into one ffmpeg filter
graph. The plugin builds this graph:

```
[aid1] → resample → mono ─┐
                          ├─ join → [left, right] → volume, delay per side → output
[aid2] → resample → mono ─┘
         └────────── --lavfi-complex ───────────┘   └─ --af @dual_audio ─┘
```

With `-2` each track stays stereo and the join gives four channels
(FL FR BL BR). When both sides play one track, `asplit` feeds it to both.
Each side's loudness (`volume`) and delay come after the join, in an `--af`
filter labelled `@dual_audio`, in front of any audio filters of your own.
mpv's `af-command` changes them there while the movie plays: no rebuild, no
seek, no gap. (It can't reach filters inside `--lavfi-complex`.) For the
delays an `adelay` holds the later side back by the difference, and
`asetpts` moves both by the earlier side's delay (earlier, for a negative
one); the earlier side is never held back itself, as `adelay`'s leading
silence would have no timestamps. That leaves mpv's own audio delay alone,
which mpv saves for the next start on `Shift+Q`. mpv builds `--af` filters
anew from their settings on a seek and when it reopens the audio output, so
the plugin writes the current values into its filter's settings then. `tools/audio-pairs.swift` combines the two headphone outputs
into one CoreAudio *aggregate device*: channels 1–2 go to the first pair, 3–4
to the second. The second pair follows the first one's clock (drift
correction). The device exists only while `dualplay` runs.

A device like that can't swap one of its headphones, so moving a pair (or
getting back one that dropped out) has the helper build it again with the
same ID, and mpv reopens its audio output on it (`ao-reload`). The menu
watches CoreAudio's device list to notice headphones coming and going.

The helper also labels the four channels L, R, Ls, Rs. By default macOS labels
them all "unknown". mpv then treats the device as stereo and folds channels 3–4
into 1–2, so both pairs would hear both languages. With the labels, mpv sees a
standard `quad` layout that matches the graph exactly. If playback ever ends
up on a device with fewer than 4 channels, the plugin says so on screen.

Why not a VLC plugin: VLC decodes only one audio track at a time, so a VLC
audio filter never sees the second language. Doing this in VLC means patching
its C core.

One mpv quirk is worked around: changing the graph during playback (a track
switch, dual audio on/off) makes mpv jump to the next keyframe (up to ~10 s
ahead), so the plugin drops mpv's buffers and seeks back to the exact position.

Calibrating puts a graph of generated sources in `--lavfi-complex` instead
of the tracks: `color` with a `drawbox` for the flash as `[vo]`, `aevalsrc`
for the beep (on every channel) as `[ao]`, both on for 0.1 s at the same
whole seconds, and `setpts` / `asetpts` moving them to where the movie was.
The beep then goes through the same `@dual_audio` filter as the movie's
sound, so its delays apply exactly as in the movie. Done installs the
movie's graph again, selects the video track, and seeks back. A `[vo]` in
the graph makes mpv drop the video track playing, and with it (unless
`--force-window` is on) its video output, so the window is opened anew for
the pattern; going back to the video track keeps it.

## Tests

```sh
python3 tests/test_dual_audio.py
```

Generates `samples/` (via `tools/make-samples.sh`): `tones.mkv` has a
distinct sine tone per track, and the tests play it through mpv and check which
tone lands in which channel, including key presses sent over mpv's IPC socket,
and which subtitle lines show for which pair. `sync.mkv` beeps once a second
on both tracks: the tests check when each ear's beeps arrive, change a delay
or loudness during playback and measure when and how loud each channel's
beeps come out, and that nothing seeks. They also calibrate, measuring
when each channel beeps against when the picture flashes (brightness logged
by a `--vf` filter), and check what Done puts back. Remembered settings and
watch-later files go to temp dirs.
With `swiftc` they also build the bubbaTV menu, load it into mpv and click
through it (`tests/menu_clicks/`). They build bubbaTV.app too and check the
command its `--dry-run` prints.
They never create or remove audio devices: headphones coming and going are a
fake list of outputs (`BUBBATV_DEVICES=FILE`, read by the menu instead of
CoreAudio), and a fake `audio-pairs` logs what it's asked to do.
For listening: `samples/speech.mkv` (English and Russian speech) and
`samples/sync.mkv` (flash + beep).

If `dualplay -2` was killed hard and a "Dual headphones (dualplay)" device is
left in the Sound settings, the next `-2` run replaces it, or remove it with
`tools/.build/audio-pairs destroy`.
