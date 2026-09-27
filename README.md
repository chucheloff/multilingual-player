# multilingual-player

Watch a movie with a friend in two languages at once, on one Mac:

- **Two pairs of AirPods:** each person gets their language in stereo on their own pair.
- **One pair, shared:** one bud each, a language per ear.

It's a plugin for [mpv](https://mpv.io) (`scripts/dual_audio.lua`) plus a
small launcher (`dualplay`).

## Quick start

```sh
brew install mpv
./dualplay -2 -l eng -r rus movie.mkv    # two pairs: eng on one, rus on the other
./dualplay -l eng -r rus movie.mkv       # one pair: eng left ear, rus right ear
```

`-l` / `-r` take language codes: `en` and `eng` both work, whichever the file
uses (mpv lists the tracks at startup). Give a list for fallbacks: `-r fre,eng`.
If a language has several tracks (dub, voice-overs…), the first one is used;
press `[` / `]` to step through the others. Without `-l` / `-r`, the track that
would normally play comes first and the next one second. Any other mpv option works after
the file name, e.g. `--sub-lang=eng`.

## Two pairs of AirPods

1. Connect both pairs to the Mac (Control Center → Bluetooth, click each).
   Neither has to be the selected sound output, but a freshly connected pair
   may not show up as an audio device until you pick it once in
   Control Center → Sound.
2. `./dualplay -2 movie.mkv` — it prints which pair gets which track, and so
   does the on-screen message. Press `y` if it's the wrong way round.

Other headphones or speakers work too: `-2` picks the two connected Bluetooth
outputs, or name them with `-p`, e.g.
`./dualplay -2 -p "Nikita" -p "MacBook Pro Speakers" movie.mkv`.

Volume: `9` / `0` changes both, `h` / `k` makes one pair quieter than the other.
If speech lags the picture, `Ctrl+-` plays the audio 100 ms earlier (`Ctrl++` later).
`samples/sync.mkv` flashes and beeps once a second to check this.

## Keys

| Key | Action |
| --- | --- |
| `a` | dual audio on/off (off plays only the first track) |
| `[` | next track for the **left** ear / first pair |
| `]` | next track for the **right** ear / second pair |
| `y` | swap |
| `h` / `k` | balance: make the left ear / first pair louder, or the other side (2 dB steps) |

`[` / `]` take over mpv's speed −/+10% keys; `{` / `}` still halve / double the
speed and Backspace resets it.

Rebind in `~/.config/mpv/input.conf`, e.g. `Ctrl+d script-binding dual_audio/toggle`
(names: `toggle`, `cycle-left`, `cycle-right`, `swap`, `balance-left`, `balance-right`).
Lines there win over the plugin's keys, so `[ multiply speed 1/1.1` and
`] multiply speed 1.1` bring the speed keys back.

## Install as a plugin

Makes it available in every mpv window (press `a`); `dualplay` then picks up
the installed copy too.

```sh
mkdir -p ~/.config/mpv/scripts && ln -s "$PWD/scripts/dual_audio.lua" ~/.config/mpv/scripts/
```

Optional defaults in `~/.config/mpv/script-opts/dual_audio.conf`:

```ini
# turn on for every file with 2+ audio tracks (comments need their own line)
auto=yes
left_lang=eng,en
right_lang=rus,ru
balance_step=2
```

## Good to know

- **Separate dub file:** `./dualplay -r rus movie.mkv --audio-file=movie.rus.mka`
- **Series:** in a playlist, the next episode keeps the languages you chose.
- **With one shared pair, each ear gets a mono downmix** of its track. For 5.1 tracks the centre
  channel (dialogue) keeps its full level, so a stereo and a 5.1 track sound
  about equally loud. If one is quieter, use `h` / `k`.
- Video, subtitles and seeking are untouched: the graph only handles audio.

## How it works

mpv's `--lavfi-complex` feeds several tracks of one file into one ffmpeg filter
graph. The plugin builds this graph:

```
[aid1] → resample → mono → volume ─┐
                                   ├─ join → [left, right] → output
[aid2] → resample → mono → volume ─┘
```

With `-2` each track stays stereo and the join gives four channels
(FL FR BL BR). `tools/audio-pairs.swift` combines the two headphone outputs
into one CoreAudio *aggregate device*: channels 1–2 go to the first pair, 3–4
to the second. The second pair follows the first one's clock (drift
correction). The device exists only while `dualplay` runs.

The helper also labels the four channels L, R, Ls, Rs. By default macOS labels
them all "unknown". mpv then treats the device as stereo and folds channels 3–4
into 1–2, so both pairs would hear both languages. With the labels, mpv sees a
standard `quad` layout that matches the graph exactly. If playback ever ends
up on a device with fewer than 4 channels, the plugin says so on screen.

Why not a VLC plugin: VLC decodes only one audio track at a time, so a VLC
audio filter never sees the second language. Doing this in VLC means patching
its C core.

One mpv quirk is worked around: changing the graph during playback makes mpv
jump to the next keyframe (up to ~10 s ahead), so the plugin seeks back to the
exact position.

## Tests

```sh
python3 tests/test_dual_audio.py
```

Generates `samples/` (via `tools/make-samples.sh`): `tones.mkv` has a
distinct sine tone per track, and the tests play it through mpv and check which
tone lands in which channel, including key presses sent over mpv's IPC socket.
For listening: `samples/speech.mkv` (English and Russian speech) and
`samples/sync.mkv` (flash + beep).

If `dualplay -2` was killed hard and a "Dual headphones (dualplay)" device is
left in the Sound settings, the next `-2` run replaces it, or remove it with
`tools/.build/audio-pairs destroy`.
