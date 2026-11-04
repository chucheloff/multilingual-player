#!/usr/bin/env python3
"""End-to-end tests: run mpv with the plugin and check what reaches each channel.

Run: python3 tests/test_dual_audio.py   (needs mpv + ffmpeg; builds samples if missing)
"""
import json
import os
import plistlib
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import analyze  # noqa: E402

SCRIPT = os.path.join(ROOT, "scripts", "dual_audio.lua")
MENU = os.path.join(ROOT, "tools", "bubbatv-menu.swift")
DUALPLAY = os.path.join(ROOT, "dualplay")
TONES = os.path.join(ROOT, "samples", "tones.mkv")  # 1=eng 440Hz, 2=rus 1000Hz 5.1, 3=ger 2500Hz
SYNC = os.path.join(ROOT, "samples", "sync.mkv")    # both tracks beep for 0.1 s once a second
VIDEOS = (".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm")  # what dualplay plays from a folder
BASE = ["mpv", "--no-config", "--script=" + SCRIPT, "--vo=null", "--really-quiet"]


def srt(path, word):
    """A subtitle file with one line per second, no gaps: "<word> N" from N s."""
    with open(path, "w") as f:
        f.write("\n".join("%d\n00:00:%02d,000 --> 00:00:%02d,000\n%s %d\n" % (i + 1, i, i + 1, word, i)
                          for i in range(20)))


def fake_helper(tmpdir):
    """Stands in for tools/audio-pairs, which must not touch real devices here:
    logs each call (arguments joined by "|") and, like the real one after a
    moment, prints what `create` prints. Names with "Broken" in them fail.
    Returns its path and a function giving the calls so far."""
    path, log = os.path.join(tmpdir, "audio-pairs"), os.path.join(tmpdir, "audio-pairs.log")
    with open(path, "w") as f:
        f.write(f"""#!/bin/sh
(IFS='|'; printf '%s\\n' "$*") >> '{log}'
case "$*" in *Broken*) echo 'audio-pairs: "Broken" matches 0 outputs. Outputs:' >&2; exit 1 ;; esac
[ "$1" != create ] || {{ sleep 0.2
  printf 'com.multilingual-player.audio-pairs\\t%s\\t%s\\n' "${{2:-Pair A}}" "${{3:-Pair B}}"; }}
""")
    os.chmod(path, 0o755)
    return path, lambda: read(log).splitlines()


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def ao_inits(log):
    """How often mpv opened its audio output, from its --log-file."""
    return read(log).count("] AO: [")


def af_graph(mpv):
    """The graph string of the script's --af filter: what mpv builds it from."""
    return next(f["params"]["graph"] for f in mpv.get("af") if f.get("label") == "dual_audio")


def episode(path):
    """A short copy of the tones (5 s) at path, standing in for an episode.
    Matroska whatever the extension: mpv goes by the content."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-i", TONES, "-map", "0", "-t", "5", "-c", "copy", "-f", "matroska", path],
                   check=True)
    return path


def devices_file(tmpdir):
    """Keeps the remembered loudness and delay in the test's temp dir. Goes
    after the other script options: a plain --script-opts replaces them all."""
    return "--script-opts-append=dual_audio-devices_file=" + os.path.join(tmpdir, "devices.json")


def tracks_file(tmpdir):
    return "--script-opts-append=dual_audio-tracks_file=" + os.path.join(tmpdir, "tracks.json")


class Render(unittest.TestCase):
    """Render 3s of playback to a WAV and inspect the channels."""

    def render(self, *opts, src=TONES, video=False, analyse=analyze.tones):
        out = os.path.join(self.tmp.name, "out.wav")
        subprocess.run(BASE + ([] if video else ["--no-video"]) + ["--ao=pcm", "--ao-pcm-file=" + out,
                               "--untimed", "--end=3", *opts, devices_file(self.tmp.name), src],
                       check=True, timeout=60)
        return analyse(out)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_off_by_default(self):
        self.assertEqual(self.render(), [440, 440])

    def test_auto_splits_first_two_tracks(self):
        self.assertEqual(self.render("--script-opts=dual_audio-auto=yes"), [440, 1000])

    def test_language_preferences(self):
        self.assertEqual(self.render("--script-opts=dual_audio-auto=yes,"
                                     "dual_audio-left_lang=rus,dual_audio-right_lang=ger"),
                         [1000, 2500])

    def test_language_code_styles_match(self):
        # The sample is tagged "rus"/"ger"; newer MKVs say "ru"/"de" instead.
        self.assertEqual(self.render("--script-opts=dual_audio-auto=yes,"
                                     "dual_audio-left_lang=ru,dual_audio-right_lang=de-DE"),
                         [1000, 2500])

    def test_language_list_falls_through(self):
        self.assertEqual(self.render("--script-opts-append=dual_audio-auto=yes",
                                     "--script-opts-append=dual_audio-right_lang=fre,ger"),
                         [440, 2500])

    def test_same_language_on_both_sides(self):
        # Nothing else matches the right side's language: both get the one track.
        self.assertEqual(self.render("--script-opts=dual_audio-auto=yes,"
                                     "dual_audio-left_lang=ger,dual_audio-right_lang=ger"),
                         [2500, 2500])

    def test_only_left_preference(self):
        # ger goes left; the right ear takes the first remaining track.
        self.assertEqual(self.render("--script-opts=dual_audio-auto=yes,dual_audio-left_lang=ger"),
                         [2500, 440])

    def test_single_track_file_plays_normally(self):
        single = os.path.join(self.tmp.name, "single.mkv")
        subprocess.run(["ffmpeg", "-v", "error", "-i", TONES, "-map", "0:v", "-map", "0:a:1",
                        "-c", "copy", single], check=True)
        # The 5.1 track downmixed by the ao to stereo - both channels carry 1000 Hz.
        self.assertEqual(self.render("--script-opts=dual_audio-auto=yes",
                                     "--audio-channels=stereo", src=single), [1000, 1000])

    def test_pairs_mode_single_track_on_both_pairs(self):
        # Played as it is, a single track reached only channels 1-2: the
        # second pair heard nothing.
        single = os.path.join(self.tmp.name, "single.mkv")
        subprocess.run(["ffmpeg", "-v", "error", "-i", TONES, "-map", "0:v", "-map", "0:a:0",
                        "-c", "copy", single], check=True)
        self.assertEqual(self.render("--script-opts=dual_audio-auto=yes,dual_audio-mode=pairs",
                                     "--audio-channels=quad", src=single), [440, 440, 440, 440])

    def test_pairs_mode_stereo_per_pair(self):
        # Channels 1-2 -> first headphone pair, 3-4 -> second.
        self.assertEqual(self.render("--script-opts=dual_audio-auto=yes,dual_audio-mode=pairs",
                                     "--audio-channels=quad"), [440, 440, 1000, 1000])

    def test_pairs_mode_names_and_stereo_warning(self):
        # Played in real time: an untimed render is over within milliseconds,
        # sometimes before the script has seen the output's channel count.
        out = subprocess.run(
            BASE[:-1] + ["--no-video", "--ao=null", "--end=1", "--audio-channels=stereo",
                         "--msg-level=all=no,dual_audio=info",
                         "--script-opts-append=dual_audio-auto=yes",
                         "--script-opts-append=dual_audio-mode=pairs",
                         "--script-opts-append=dual_audio-pair_names=Nikita’s AirPods|Friend, Pro",
                         devices_file(self.tmp.name), TONES],
            capture_output=True, text=True, check=True, timeout=60)
        log = out.stdout + out.stderr
        self.assertIn("Nikita’s AirPods: [1] English 440Hz (eng)", log)
        self.assertIn("Friend, Pro: [2] Russian 1000Hz 5.1 (rus)", log)
        self.assertIn("the output device has 2 channels", log)

    def test_external_audio_file(self):
        # Dubs often ship as a separate .mka next to the video.
        video = os.path.join(self.tmp.name, "movie.mkv")
        dub = os.path.join(self.tmp.name, "dub.mka")
        subprocess.run(["ffmpeg", "-v", "error", "-i", TONES, "-map", "0:v", "-map", "0:a:0",
                        "-c", "copy", video], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-i", TONES, "-map", "0:a:2",
                        "-c", "copy", dub], check=True)
        self.assertEqual(self.render("--script-opts=dual_audio-auto=yes,dual_audio-right_lang=ger",
                                     "--audio-file=" + dub, src=video), [440, 2500])

    def save(self, settings):
        with open(os.path.join(self.tmp.name, "devices.json"), "w") as f:
            json.dump(settings, f)

    def test_saved_delays_move_each_ear(self):
        # Delays saved per ear come back at launch and move that ear against
        # the picture: the first beep after 0.5 s (with the flash) comes
        # that much later or, negative, earlier. Both ways round, as the
        # side held back changes.
        def beeps():
            return self.render("--script-opts=dual_audio-auto=yes", "--start=0.5", src=SYNC,
                               video=True, analyse=analyze.first_onsets)
        plain = beeps()
        for delays in ((-200, 0), (100, -300)):
            self.save({"system output (left ear)": {"gain": 0, "delay": delays[0]},
                       "system output (right ear)": {"gain": 0, "delay": delays[1]}})
            moved = [round(t - p, 3) for t, p in zip(beeps(), plain)]
            self.assertEqual(moved, [d / 1000 for d in delays])

    def test_pairs_keep_their_channels_when_held_back(self):
        # A delayed first pair still plays on 1-2.
        self.save({"system output (pair 1)": {"gain": 0, "delay": 100}})
        self.assertEqual(self.render("--script-opts=dual_audio-auto=yes,dual_audio-mode=pairs",
                                     "--audio-channels=quad"), [440, 440, 1000, 1000])


class Mpv:
    """A real-time mpv instance driven over its JSON IPC socket. `cmd` can put
    dualplay in front of it (with the file in `cmd` and src=None). The
    remembered device settings go to devices.json in `store` (default: tmpdir;
    False: wherever the plugin puts them by default). Events that arrive
    meanwhile are kept in `events` (names only)."""

    def __init__(self, tmpdir, *opts, src=TONES, cmd=BASE, env=None, store=None):
        self.path = os.path.join(tmpdir, "mpv.sock")
        if os.path.exists(self.path):  # left by an mpv before
            os.unlink(self.path)
        own = [] if store is False else [devices_file(store or tmpdir), tracks_file(store or tmpdir)]
        self.proc = subprocess.Popen(cmd + ["--ao=null", "--input-ipc-server=" + self.path,
                                            *opts, *own, *([src] if src else [])],
                                     env=env, stdout=subprocess.DEVNULL)
        deadline = time.time() + 10
        while not os.path.exists(self.path):
            if self.proc.poll() is not None or time.time() > deadline:
                self.proc.terminate()
                self.proc.wait(timeout=10)
                raise RuntimeError("mpv did not open its IPC socket")
            time.sleep(0.05)
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(self.path)
        self.buf = b""
        self.next_id = 0
        self.events = []

    def command(self, *args):
        self.next_id += 1
        self.sock.sendall(json.dumps({"command": list(args), "request_id": self.next_id}).encode() + b"\n")
        while True:
            while b"\n" not in self.buf:
                self.buf += self.sock.recv(65536)
            line, self.buf = self.buf.split(b"\n", 1)
            reply = json.loads(line)
            if "event" in reply:
                self.events.append(reply["event"])
            elif reply.get("request_id") == self.next_id:
                return reply.get("data")

    def get(self, prop):
        return self.command("get_property", prop)

    def wait_for(self, pred, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return
            time.sleep(0.05)
        raise AssertionError("condition not met in time")

    def quit(self):
        """Quits over the socket: a signal would not stop dualplay while its mpv runs."""
        try:
            self.sock.sendall(b'{"command": ["quit"]}\n')
        except OSError:
            pass
        self.proc.wait(timeout=10)

    def close(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)
        self.sock.close()


class Playing(unittest.TestCase):
    """Helpers for driving self.mpv, paused at 3.0s (--start=3 --pause) with
    a playlist of two files."""

    def press(self, key, expect):
        self.mpv.command("keypress", key)
        self.expect(expect)

    def expect(self, expect):
        """Wait until the graph routes aid L / aid R (None = off) at 3.0s, and
        the script has published it: the last thing it does for a change, so
        the subtitles are set by then too."""
        def routed():
            g = self.mpv.get("lavfi-complex")
            if expect is None:
                return g == ""
            ids = [int(i) for i in re.findall(r"\[aid(\d+)\]", g)]
            return (ids * 2)[:2] == list(expect)  # a track both sides play appears once

        def published():
            s = self.state()
            if expect is None:
                return not s["active"]
            return s["active"] and [side.get("track") for side in s["sides"]] == list(expect)
        self.mpv.wait_for(routed)
        self.mpv.wait_for(published)
        self.mpv.wait_for(lambda: abs((self.mpv.get("time-pos") or 0) - 3.0) < 0.05)

    def next_file(self):
        """Go on to the next playlist entry, once it plays (from its --start)."""
        self.mpv.command("seek", 5, "absolute")  # tells the files apart
        self.mpv.wait_for(lambda: abs((self.mpv.get("time-pos") or 0) - 5.0) < 0.05)
        self.mpv.command("playlist-next")
        self.mpv.wait_for(lambda: self.mpv.get("playlist-pos") == 1)
        self.mpv.wait_for(lambda: abs((self.mpv.get("time-pos") or 0) - 3.0) < 0.05)

    def message(self, *args):
        self.mpv.command("script-message-to", "dual_audio", *map(str, args))

    def state(self):
        return self.mpv.get("user-data/dual_audio")

    def wait_sides(self, want):
        """Wait until the state gives each side's (gain, delay) as `want`."""
        self.mpv.wait_for(lambda: [(s["gain"], s["delay"]) for s in self.state()["sides"]] == want)

    def af_graph(self):
        return af_graph(self.mpv)

    def af_has(self, *parts):
        self.mpv.wait_for(lambda: all(p in af_graph(self.mpv) for p in parts))

    def lands(self, pos):
        """Wait until playback (paused) is at `pos` with the seek over: while a
        seek goes on, time-pos already shows where it was asked to go, and a
        keyframe seek then ends up elsewhere."""
        def at():
            return abs((self.mpv.get("time-pos") or 0) - pos) < 0.05
        self.mpv.wait_for(lambda: at() and not self.mpv.get("seeking") and at())

    def seek_key(self, key, pos):
        self.mpv.command("keypress", key)
        self.lands(pos)


class Keys(Playing):
    """Key bindings while playing. Paused at 3.0s so any position jump shows up.
    The playlist holds the sample twice, standing in for two episodes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mpv = Mpv(self.tmp.name, "--start=3", "--pause", TONES)
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") is not None)

    def tearDown(self):
        self.mpv.close()
        self.tmp.cleanup()

    def restart(self, *opts):
        """Quit mpv and start it again as setUp does - a fresh socket, the same
        remembered device settings."""
        self.mpv.close()
        self.runs = getattr(self, "runs", 0) + 1
        run = os.path.join(self.tmp.name, "run%d" % self.runs)
        os.mkdir(run)
        self.mpv = Mpv(run, "--start=3", "--pause", *opts, TONES, store=self.tmp.name)
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") is not None)

    def saved(self):
        with open(os.path.join(self.tmp.name, "devices.json")) as f:
            return json.load(f)

    def test_toggle_cycle_swap(self):
        self.press("a", (1, 2))     # on: current track left, next right
        self.press("}", (1, 3))     # right ear -> next track
        self.press("}", (1, 1))     # wraps around to the left ear's track: both hear it
        self.assertIn("[shared]asplit=2[l][r]", self.mpv.get("lavfi-complex"))
        self.press("{", (2, 1))     # left ear -> next track
        self.assertEqual(self.mpv.get("speed"), 1)  # { } no longer change the speed
        self.mpv.command("keypress", "]")  # plain ] is mpv's speed key again
        self.mpv.wait_for(lambda: self.mpv.get("speed") > 1)
        self.expect((2, 1))         # ... and leaves the tracks alone
        self.press("y", (1, 2))     # swap
        self.press("a", None)       # off
        self.assertEqual(self.mpv.get("aid"), 1)  # left-ear language keeps playing

    def test_choice_carries_to_next_file(self):
        self.press("a", (1, 2))
        self.press("}", (1, 3))
        self.next_file()
        self.expect((1, 3))

    def test_same_track_carries_to_next_file(self):
        # Two English tracks: the one both sides share stays shared, rather
        # than the right side taking the other English one.
        twice = os.path.join(self.tmp.name, "twice.mkv")
        subprocess.run(["ffmpeg", "-v", "error", "-i", TONES, "-map", "0:v", "-map", "0:a:0",
                        "-map", "0:a:0", "-map", "0:a:1", "-c", "copy", twice], check=True)
        self.mpv.close()
        os.mkdir(os.path.join(self.tmp.name, "2"))  # a fresh socket path
        self.mpv = Mpv(os.path.join(self.tmp.name, "2"), "--start=3", "--pause", twice, src=twice)
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") is not None)
        self.press("a", (1, 2))
        self.message("set-track", 2, 1)
        self.expect((1, 1))
        self.next_file()
        self.expect((1, 1))

    def test_balance(self):
        self.press("a", (1, 2))
        self.mpv.command("keypress", "k")
        self.wait_sides([(-2, 0), (0, 0)])
        self.mpv.command("keypress", "h")
        self.mpv.command("keypress", "h")
        self.wait_sides([(0, 0), (-2, 0)])

    def test_menu_messages(self):
        # What the bubbaTV menu sends, and the state it reads back.
        self.press("a", (1, 2))
        self.assertEqual([t["id"] for t in self.state()["tracks"]], [1, 2, 3])
        self.message("set-track", 2, 1)
        self.expect((1, 1))
        self.message("set-track", 1, 3)
        self.expect((3, 1))
        self.message("set-gain", 2, -6)
        self.message("set-delay", 2, 150)
        self.wait_sides([(0, 0), (-6, 150)])
        self.expect((3, 1))
        sides = self.state()["sides"]
        self.assertEqual([s["track"] for s in sides], [3, 1])
        self.assertEqual([s["name"] for s in sides], ["Left ear", "Right ear"])

    def test_negative_delay_leaves_audio_delay(self):
        # adelay can only hold a side back: the other side is delayed
        # instead, and asetpts moves the output earlier. mpv's own
        # audio-delay (the user's 0.1 s here) is left alone, live too.
        self.mpv.command("set_property", "audio-delay", 0.1)
        self.press("a", (1, 2))
        self.message("set-delay", 1, -200)
        self.wait_sides([(0, -200), (0, 0)])
        self.message("set-delay", 1, -50)
        self.wait_sides([(0, -50), (0, 0)])
        self.assertAlmostEqual(self.mpv.get("audio-delay"), 0.1)
        self.press("}", (1, 3))  # a new graph: the --af string has the values
        self.af_has("delays=0|50,asetpts@dual_audio=PTS-0.05/TB")
        self.assertAlmostEqual(self.mpv.get("audio-delay"), 0.1)
        self.press("a", None)
        self.assertAlmostEqual(self.mpv.get("audio-delay"), 0.1)

    def test_watch_later_keeps_own_audio_delay(self):
        # mpv writes the watch-later file inside the quit command, from the
        # properties as they are right then. A negative delay must not be in
        # audio-delay by then, or the next start is off by it.
        later = os.path.join(self.tmp.name, "later")
        self.restart("--watch-later-dir=" + later)
        self.mpv.command("set_property", "audio-delay", 0.1)
        self.press("a", (1, 2))
        self.message("set-delay", 1, -200)
        self.wait_sides([(0, -200), (0, 0)])
        self.mpv.command("keypress", "Q")  # quit-watch-later
        self.mpv.proc.wait(timeout=10)
        confs = []
        for name in os.listdir(later):  # the file's, and redirects for its parent dirs
            with open(os.path.join(later, name)) as f:
                confs.append(f.read())
        [conf] = [c for c in confs if "start=" in c]
        self.assertIn("audio-delay=0.100000\n", conf)
        self.restart("--watch-later-dir=" + later)
        self.assertAlmostEqual(self.mpv.get("audio-delay"), 0.1)
        self.press("a", (1, 2))
        self.af_has("delays=0|200,asetpts@dual_audio=PTS-0.2/TB")
        self.assertAlmostEqual(self.mpv.get("audio-delay"), 0.1)

    def test_watch_later_and_our_filter(self):
        # mpv saves the --af list in watch-later files too, our filter with
        # it, and sets it again on resuming: with dual audio off, the plain
        # track must play without our filter, and with it on, the filter
        # must have the remembered values, not the saved ones.
        later = os.path.join(self.tmp.name, "later")
        self.restart("--watch-later-dir=" + later)
        self.press("a", (1, 2))
        self.mpv.command("keypress", "Q")  # quit-watch-later
        self.mpv.proc.wait(timeout=10)
        self.restart("--watch-later-dir=" + later)
        self.assertEqual(self.mpv.get("af"), [])
        self.press("a", (1, 2))
        self.message("set-gain", 2, -6)  # live: the string keeps 0 dB
        self.wait_sides([(0, 0), (-6, 0)])
        self.mpv.command("keypress", "Q")
        self.mpv.proc.wait(timeout=10)
        self.restart("--watch-later-dir=" + later, "--script-opts-append=dual_audio-auto=yes")
        self.expect((1, 2))
        self.af_has("volume@dual_audio2=-6dB")

    def test_settings_remembered(self):
        # Loudness and delay are saved as they change, and are back for the
        # next file and the next launch. In ears mode each ear of the system
        # output keeps its own.
        self.press("a", (1, 2))
        self.mpv.command("keypress", "h")  # the right ear 2 dB down
        self.message("set-delay", 1, 120)
        self.wait_sides([(0, 120), (-2, 0)])
        self.assertEqual(self.saved(), {"system output (left ear)": {"gain": 0, "delay": 120},
                                        "system output (right ear)": {"gain": -2, "delay": 0}})
        remembered = ("volume@dual_audio1=0dB", "volume@dual_audio2=-2dB", "delays=120|0")
        self.next_file()
        self.af_has(*remembered)
        self.restart()
        self.press("a", (1, 2))
        self.af_has(*remembered)
        self.assertEqual([(s["gain"], s["delay"]) for s in self.state()["sides"]], [(0, 120), (-2, 0)])
        self.message("set-delay", 1, 0)
        self.message("set-gain", 2, 0)
        self.mpv.wait_for(lambda: self.saved() == {})  # back at 0 / 0: nothing to remember

    def test_settings_follow_the_headphones(self):
        # Pairs mode keys them by device name: they stay with the pair when
        # the tracks swap, and find it again when it comes second next time.
        pairs = "--script-opts-append=dual_audio-mode=pairs"
        self.restart(pairs, "--script-opts-append=dual_audio-pair_names=Nikita’s AirPods|Friend, Pro")
        self.press("a", (1, 2))
        self.message("set-gain", 1, -4)
        self.message("set-delay", 2, -150)
        self.wait_sides([(-4, 0), (0, -150)])
        self.press("y", (2, 1))
        self.af_has("volume@dual_audio1=-4dB", "volume@dual_audio2=0dB",
                    "delays=150|150|0|0,asetpts@dual_audio=PTS-0.15/TB")
        self.assertEqual(self.saved(), {"Nikita’s AirPods": {"gain": -4, "delay": 0},
                                        "Friend, Pro": {"gain": 0, "delay": -150}})
        self.restart(pairs, "--script-opts-append=dual_audio-pair_names=Friend, Pro|Nikita’s AirPods")
        self.press("a", (1, 2))  # saved tracks follow the device as well as gain and delay
        self.af_has("volume@dual_audio1=0dB", "volume@dual_audio2=-4dB",
                    "delays=0|0|150|150,asetpts@dual_audio=PTS-0.15/TB")
        self.assertEqual([(s["device"], s["gain"], s["delay"]) for s in self.state()["sides"]],
                         [("Friend, Pro", 0, -150), ("Nikita’s AirPods", -4, 0)])

    def test_ears_settings_per_output(self):
        # Another output named in audio-device has ears of its own; going
        # back to the system output brings its settings back.
        self.press("a", (1, 2))
        self.message("set-gain", 1, -6)
        self.wait_sides([(-6, 0), (0, 0)])
        self.mpv.command("set_property", "audio-device", "coreaudio/test-uid")
        self.wait_sides([(0, 0), (0, 0)])
        self.expect((1, 2))
        self.message("set-gain", 2, -3)
        self.wait_sides([(0, 0), (-3, 0)])
        self.assertEqual(self.saved(), {"system output (left ear)": {"gain": -6, "delay": 0},
                                        "coreaudio/test-uid (right ear)": {"gain": -3, "delay": 0}})
        self.mpv.command("set_property", "audio-device", "auto")
        self.wait_sides([(-6, 0), (0, 0)])

    def test_default_file_in_config_dir(self):
        # ~~/dual_audio-devices.json: mpv's config dir, made on the first save.
        self.mpv.close()
        cfg = os.path.join(self.tmp.name, "cfg")
        os.mkdir(os.path.join(self.tmp.name, "run"))  # a fresh socket path
        self.mpv = Mpv(os.path.join(self.tmp.name, "run"), "--start=3", "--pause", TONES, store=False,
                       cmd=["mpv", "--config-dir=" + cfg] + BASE[2:])
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") is not None)
        self.press("a", (1, 2))
        self.message("set-gain", 2, -6)
        self.wait_sides([(0, 0), (-6, 0)])
        with open(os.path.join(cfg, "dual_audio-devices.json")) as f:
            self.assertEqual(json.load(f), {"system output (right ear)": {"gain": -6, "delay": 0}})

    def test_av_sync_while_playing(self):
        self.press("a", (1, 2))
        self.message("set-delay", 2, -200)  # the left ear held back, the output moved earlier
        self.wait_sides([(0, 0), (0, -200)])
        self.mpv.command("set_property", "pause", False)
        time.sleep(3)
        self.assertLess(abs(self.mpv.get("avsync")), 0.1)

    def test_live_while_playing(self):
        # Loudness and delay changes are live: no graph rebuild, no seek,
        # and time-pos goes on. A negative delay moves the sound 200 ms
        # earlier against the picture, which then catches up, and mpv's
        # audio-delay stays as it was.
        self.press("a", (1, 2))
        self.mpv.command("set_property", "pause", False)
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") > 3.3)
        graph = self.mpv.get("lavfi-complex")
        self.mpv.events.clear()
        self.message("set-gain", 2, -6)
        self.message("set-delay", 1, -200)
        self.message("set-delay", 2, 100)
        self.mpv.command("keypress", "k")
        self.wait_sides([(0, -200), (-4, 100)])
        positions, sync = [], []
        end = time.time() + 3.5  # time enough for the picture to catch up
        while time.time() < end:
            positions.append(self.mpv.get("time-pos"))
            sync.append(self.mpv.get("avsync"))
            time.sleep(0.05)
        self.assertEqual(self.mpv.get("lavfi-complex"), graph)
        self.assertEqual([e for e in self.mpv.events if e in ("seek", "playback-restart")], [])
        self.assertEqual(positions, sorted(positions))
        self.assertGreater(positions[-1] - positions[0], 2.5)
        self.assertAlmostEqual(min(sync), -0.2, delta=0.05)
        self.assertLess(abs(sync[-1]), 0.05)
        self.assertEqual(self.mpv.get("audio-delay"), 0)

    def test_live_values_after_seek(self):
        # mpv rebuilds the --af filter from its string after a seek, so the
        # values changed live must be in it by then; the other side takes
        # the negative delay.
        self.press("a", (1, 2))
        self.mpv.command("set_property", "pause", False)
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") > 3.3)  # long after the toggle's seek back
        self.message("set-gain", 2, -6)
        self.message("set-delay", 1, -200)
        self.wait_sides([(0, -200), (-6, 0)])
        self.assertIn("delays=0|0,asetpts@dual_audio=PTS+0/TB", self.af_graph())  # live: the string is as before
        self.mpv.command("seek", 5, "absolute")
        self.af_has("delays=0|200,asetpts@dual_audio=PTS-0.2/TB")
        self.assertIn("volume@dual_audio1=0dB", self.af_graph())
        self.assertIn("volume@dual_audio2=-6dB", self.af_graph())

    def test_live_values_after_reload(self):
        # mpv builds the --af filter anew from its string when it reopens
        # the audio output (another output device, ao-reload), too.
        self.press("a", (1, 2))
        # Let the toggle's seek and audio-reconfig finish before testing a
        # live command; an uninitialized filter legitimately uses set_af.
        self.mpv.command("set_property", "pause", False)
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") > 3.3)
        self.mpv.command("set_property", "pause", True)
        position = self.mpv.get("time-pos")
        self.message("set-gain", 2, -6)
        self.wait_sides([(0, 0), (-6, 0)])
        self.assertIn("volume@dual_audio2=0dB", self.af_graph())  # live
        self.mpv.command("ao-reload")
        self.af_has("volume@dual_audio2=-6dB")
        self.assertEqual(re.findall(r"\[aid(\d+)\]", self.mpv.get("lavfi-complex")), ["1", "2"])
        self.assertTrue(self.state()["active"])
        self.assertEqual([s["track"] for s in self.state()["sides"]], [1, 2])
        self.assertAlmostEqual(self.mpv.get("time-pos"), position, delta=0.05)

    def test_own_filters_left_alone(self):
        # Ours goes first, on the joined channels; the user's come after,
        # and are all that is left when dual audio goes off.
        self.restart("--af=@mine:lavfi=[volume=0.5],format=srate=48000")
        own = self.mpv.get("af")
        self.press("a", (1, 2))
        self.assertEqual(self.mpv.get("af")[1:], own)
        self.message("set-gain", 1, -3)
        self.wait_sides([(-3, 0), (0, 0)])
        self.assertEqual(self.mpv.get("af")[1:], own)
        self.press("a", None)
        self.assertEqual(self.mpv.get("af"), own)

    def test_seek_keys(self):
        # Left / Right seek exactly 5 s: from 3.0 to 8.0 and back, where mpv's
        # own arrow seeks go to the sample's keyframes, 10 and 0. Shift+Right /
        # Left stay mpv's exact 1 s.
        self.seek_key("RIGHT", 8)
        self.seek_key("LEFT", 3)
        self.seek_key("Shift+RIGHT", 4)
        self.seek_key("Shift+LEFT", 3)
        # The same with the graph installed, which stays as it is, and a
        # negative delay, whose shift moves the sound's timestamps (in the
        # --af filter once a seek rebuilds it); and the bindings the menu's
        # Back / Forward run.
        self.press("a", (1, 2))
        self.message("set-delay", 2, -200)
        self.wait_sides([(0, 0), (0, -200)])
        graph = self.mpv.get("lavfi-complex")
        self.seek_key("RIGHT", 8)
        self.af_has("asetpts@dual_audio=PTS-0.2/TB")
        self.seek_key("LEFT", 3)
        self.mpv.command("script-binding", "dual_audio/seek-forward")
        self.lands(8)
        self.mpv.command("script-binding", "dual_audio/seek-back")
        self.lands(3)
        self.assertEqual(self.mpv.get("lavfi-complex"), graph)
        self.assertEqual(self.state()["seek_step"], 5)
        # ... and the sound goes on from there in step with the picture.
        self.seek_key("RIGHT", 8)
        self.mpv.command("set_property", "pause", False)
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") > 10.5)
        self.assertLess(abs(self.mpv.get("avsync")), 0.1)

    def test_seek_key_held(self):
        # Held down, Left goes on seeking back, as mpv's arrow keys do.
        self.mpv.command("seek", 18, "absolute")
        self.lands(18)
        self.mpv.command("keydown", "LEFT")
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") < 12)
        self.mpv.command("keyup", "LEFT")

    def test_seek_step(self):
        self.restart("--script-opts-append=dual_audio-seek_step=10")
        self.seek_key("RIGHT", 13)
        self.seek_key("LEFT", 3)
        self.mpv.command("script-binding", "dual_audio/seek-forward")
        self.lands(13)
        self.assertEqual(self.state()["seek_step"], 10)  # for the menu

    def test_mouse_double_click_seeks(self):
        for dual in (False, True):
            if dual:
                self.press("a", (1, 2))
            graph = self.mpv.get("lavfi-complex")
            self.seek_key("MBTN_RIGHT_DBL", 8)
            self.seek_key("MBTN_LEFT_DBL", 3)
            self.assertEqual(self.mpv.get("lavfi-complex"), graph)
            self.assertFalse(self.mpv.get("fullscreen"))
            self.assertTrue(self.mpv.get("pause"))

    def test_mouse_seeks_can_be_disabled(self):
        self.restart("--script-opts-append=dual_audio-seek_mouse=no")
        self.mpv.command("keypress", "MBTN_LEFT_DBL")
        self.mpv.wait_for(lambda: self.mpv.get("fullscreen"))
        self.lands(3)

    def test_right_mouse_click_sequence_keeps_pause_state(self):
        # mpv generates a double-click event from two real down/up pairs.
        self.mpv.command("keydown", "MBTN_RIGHT")
        self.mpv.command("keyup", "MBTN_RIGHT")
        time.sleep(0.08)
        self.mpv.command("keydown", "MBTN_RIGHT")
        self.mpv.command("keyup", "MBTN_RIGHT")
        self.lands(8)
        time.sleep(0.4)
        self.assertTrue(self.mpv.get("pause"))
        self.lands(8)
        # A later single right click still toggles playback.
        self.mpv.command("keydown", "MBTN_RIGHT")
        self.mpv.command("keyup", "MBTN_RIGHT")
        self.mpv.wait_for(lambda: not self.mpv.get("pause"))

    def test_mpv_arrow_keys(self):
        # seek_keys=no leaves mpv's own arrow seeks on Left / Right (to the
        # next keyframe); the menu's Back / Forward still seek exactly.
        self.restart("--script-opts-append=dual_audio-seek_keys=no")
        self.seek_key("RIGHT", 10)
        self.mpv.command("script-binding", "dual_audio/seek-back")
        self.lands(5)
        # An input.conf line for one wins over the script's, as for the other keys.
        conf = os.path.join(self.tmp.name, "input.conf")
        with open(conf, "w") as f:
            f.write("RIGHT seek 5\n")
        self.restart("--input-conf=" + conf)
        self.seek_key("RIGHT", 10)
        self.seek_key("LEFT", 5)  # still the script's


class TrackMemory(Playing):
    """Two Russian dubs whose IDs change between episodes, with device
    choices persisted between launches. No real audio devices are used."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        season = os.path.join(self.tmp.name, "Season")
        os.mkdir(season)
        self.first = os.path.join(season, "Episode 1.mkv")
        self.second = os.path.join(season, "Episode 2.mkv")
        for path, order in ((self.first, (0, 1, 2)), (self.second, (2, 0, 1))):
            args = ["ffmpeg", "-v", "error", "-i", TONES, "-map", "0:v"]
            for index in order:
                args += ["-map", "0:a:" + str(index)]
            args += ["-c", "copy"]
            for index, source in enumerate(order):
                args += [f"-metadata:s:a:{index}", "language=" + ("en" if source == 0 else "rus"),
                         f"-metadata:s:a:{index}", "title=" + ("Original" if source == 0 else f"Dub {source}")]
            subprocess.run(args + [path], check=True)
        self.runs = 0
        self.start(self.first, self.second)

    def start(self, *files, names="Pair A|Pair B", extra=()):
        self.runs += 1
        run = os.path.join(self.tmp.name, str(self.runs))
        os.mkdir(run)
        self.mpv = Mpv(run, "--start=3", "--pause", "--audio-channels=quad",
                       "--script-opts-append=dual_audio-auto=yes",
                       "--script-opts-append=dual_audio-mode=pairs",
                       "--script-opts-append=dual_audio-pair_names=" + names,
                       *extra, *files, src=None, store=self.tmp.name)
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") is not None)

    def tearDown(self):
        self.mpv.close()
        self.tmp.cleanup()

    def test_dub_survives_reordered_tracks_and_restart(self):
        self.message("set-track", 2, 3)  # Russian Dub 2, not the first Russian track
        self.expect((1, 3))
        self.next_file()
        self.expect((2, 1))  # Dub 2 now has ID 1
        self.mpv.close()
        self.start(self.second)
        self.expect((2, 1))  # same season, even when only one episode is opened
        self.mpv.close()
        self.start(self.first, self.second, names="Pair B|Pair A")
        self.expect((3, 1))  # headphone order reversed

    def test_file_choice_overrides_later_playlist_choice(self):
        self.message("set-track", 2, 3)
        self.expect((1, 3))
        self.next_file()
        self.message("set-track", 2, 3)  # Dub 1 for Episode 2 only
        self.expect((2, 3))
        self.mpv.command("playlist-prev")
        self.mpv.wait_for(lambda: self.mpv.get("playlist-pos") == 0)
        self.expect((1, 3))  # Episode 1's own Dub 2 choice wins
        self.mpv.close()
        self.start(self.first, self.second)
        self.expect((1, 3))
        self.next_file()
        self.expect((2, 3))

    def test_preferred_language_persists_for_new_movies(self):
        self.message("set-language", 1, "ru")
        self.message("set-language", 2, "eng")
        self.expect((2, 1))
        self.mpv.wait_for(lambda: [s["language"] for s in self.state()["sides"]] == ["ru", "en"])
        self.mpv.close()
        self.start(TONES, names="Pair B|Pair A")
        self.expect((1, 2))
        with open(os.path.join(self.tmp.name, "tracks.json")) as f:
            saved = json.load(f)
        self.assertEqual(saved["Pair A"]["language"], "ru")
        self.assertEqual(saved["Pair B"]["language"], "en")

    def test_preference_change_clears_current_context_pick(self):
        self.message("set-track", 2, 3)
        self.expect((1, 3))
        self.message("set-language", 2, "en")
        self.expect((1, 1))
        self.next_file()
        self.expect((2, 2))
        self.message("set-track", 2, 1)  # a deliberate file override is still possible
        self.expect((2, 1))
        self.mpv.close()
        self.start(self.second)
        self.expect((2, 1))

    def test_missing_language_does_not_replace_preference(self):
        self.message("set-language", 1, "rus")
        self.message("set-language", 2, "en")
        self.expect((2, 1))
        missing = os.path.join(self.tmp.name, "no-russian.mkv")
        subprocess.run(["ffmpeg", "-v", "error", "-i", TONES, "-map", "0:v", "-map", "0:a:0",
                        "-map", "0:a:2", "-c", "copy", missing], check=True)
        self.mpv.close()
        self.start(missing)
        self.expect((2, 1))  # English goes to its device; the other gets available German
        self.assertEqual(self.state()["sides"][0]["language"], "ru")
        self.mpv.close()
        self.start(self.first)
        self.expect((2, 1))  # Russian returns without another manual pick

    def test_untagged_tracks_keep_their_identity(self):
        self.mpv.close()
        path = os.path.join(self.tmp.name, "untagged.mkv")
        subprocess.run(["ffmpeg", "-v", "error", "-i", TONES, "-map", "0", "-c", "copy",
                        "-metadata:s:a:0", "language=und", "-metadata:s:a:1", "language=und",
                        "-metadata:s:a:2", "language=und", path], check=True)
        self.start(path, path)
        self.message("set-track", 2, 3)
        self.expect((1, 3))
        self.next_file()
        self.expect((1, 3))

    def test_track_memory_can_be_disabled(self):
        self.mpv.close()
        run = os.path.join(self.tmp.name, "disabled")
        os.mkdir(run)
        self.mpv = Mpv(run, "--start=3", "--pause", "--script-opts-append=dual_audio-auto=yes",
                       "--script-opts-append=dual_audio-tracks_file=", self.first, src=None, store=False)
        self.mpv.wait_for(lambda: self.state() and self.state()["active"])
        self.message("set-track", 2, 3)
        self.expect((1, 3))
        self.assertFalse(os.path.exists(os.path.join(run, "tracks.json")))


class Live(unittest.TestCase):
    """Loudness and delay changed during playback, as heard: samples/sync.mkv
    rendered to a WAV, with the changes sent over IPC while paused at the
    start. mpv has filtered what its pcm output buffers by then (~1.4 s), so
    the beeps from 3 s on carry the change."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def render(self, *messages, opts=(), then=()):
        """The WAV of playing with `messages` sent, then the IPC commands
        in `then`."""
        out = os.path.join(self.tmp.name, "out.wav")
        mpv = Mpv(self.tmp.name, "--ao=pcm", "--ao-pcm-file=" + out, "--untimed", "--pause", "--end=7",
                  "--script-opts-append=dual_audio-auto=yes", *opts, src=SYNC)
        try:
            mpv.wait_for(lambda: mpv.get("seeking") is False)  # started: audio went through the filters
            time.sleep(0.2)
            graph = mpv.get("lavfi-complex")
            mpv.events.clear()  # those of the start
            for m in messages:
                mpv.command("script-message-to", "dual_audio", *map(str, m))
            want = {(m[0][4:], m[1]): m[2] for m in messages}  # e.g. ("delay", side): ms
            mpv.wait_for(lambda: all(mpv.get("user-data/dual_audio")["sides"][i - 1][k] == v
                                     for (k, i), v in want.items()))
            time.sleep(0.3)  # time for a seek, if one came
            # Live: the graph stays, nothing seeks.
            self.assertEqual(mpv.get("lavfi-complex"), graph)
            self.assertEqual([e for e in mpv.events if e in ("seek", "playback-restart")], [])
            self.audio_delay = mpv.get("audio-delay")
            for command in then:
                mpv.command(*command)
            mpv.command("set_property", "pause", False)
            mpv.proc.wait(timeout=60)
        finally:
            mpv.close()
        return out

    def assert_after(self, first, then, ms, skip=3):
        """Leaving out the first `skip` beeps, each beep of `then` starts `ms`
        after the one of `first`."""
        self.assertEqual(len(first), len(then))
        self.assertGreater(len(first), skip + 2)
        for a, b in zip(first[skip:], then[skip:]):
            self.assertAlmostEqual((b - a) * 1000, ms, delta=2)

    def test_delay(self):
        left, right = analyze.onsets(self.render(("set-delay", 2, 200)))
        self.assert_after(left, right, 200)
        for t in left:  # the other side stays with the picture: on the second
            self.assertAlmostEqual(t, round(t), delta=0.002)

    def test_negative_delay(self):
        # The left ear 200 ms early: the right is delayed instead, and the
        # sound's timestamps move 200 ms earlier, not mpv's audio-delay.
        # That shows at the end: --end=7 cuts by timestamp, so the WAV runs
        # to 7.2 s of the sound, a beep more on the left.
        out = self.render(("set-delay", 1, -200))
        left, right = analyze.onsets(out)
        self.assert_after(left[:-1], right, 200)
        n, pcm = analyze.decode(out)
        self.assertAlmostEqual(len(pcm) / n / analyze.RATE, 7.2, delta=0.005)
        self.assertEqual(self.audio_delay, 0)

    def test_loudness(self):
        left, right = analyze.peaks(self.render(("set-gain", 2, -6)), start=3)
        self.assertAlmostEqual(right / left, 10 ** (-6 / 20), delta=0.01)

    def test_pairs(self):
        # Both channels of a pair change together.
        out = self.render(("set-delay", 2, 150), ("set-gain", 1, -6),
                          opts=("--script-opts-append=dual_audio-mode=pairs", "--audio-channels=quad"))
        fl, fr, bl, br = analyze.onsets(out)
        self.assert_after(fl, fr, 0)
        self.assert_after(fl, bl, 150)
        self.assert_after(fl, br, 150)
        fl, fr, bl, br = analyze.peaks(out, start=3)
        self.assertAlmostEqual(fl / bl, 10 ** (-6 / 20), delta=0.01)
        self.assertAlmostEqual(fr / br, 10 ** (-6 / 20), delta=0.01)

    def test_kept_after_seek(self):
        # mpv rebuilds the filter after a seek; the live values come along.
        # The WAV then starts at 3 s and holds four beeps. The first is left
        # out: should mpv get to it before the script hears of the seek, it
        # still has the old values.
        out = self.render(("set-delay", 2, 200), ("set-gain", 2, -6), then=[("seek", 3, "absolute")])
        left, right = analyze.onsets(out)
        self.assertEqual(len(left), 4)
        self.assert_after(left, right, 200, skip=1)
        left, right = analyze.peaks(out, start=0.5)
        self.assertAlmostEqual(right / left, 10 ** (-6 / 20), delta=0.01)

    def test_kept_after_moving_a_pair(self):
        # Moving a pair to other headphones reopens the audio output, and
        # mpv builds the filter anew; a live change made before comes along.
        # The pcm output starts its WAV again then, so it holds only what
        # plays after the move.
        helper, _ = fake_helper(self.tmp.name)
        out = self.render(("set-gain", 2, -6), ("set-device", 1, "Other Phones"),
                          opts=("--script-opts-append=dual_audio-mode=pairs", "--audio-channels=quad",
                                "--script-opts-append=dual_audio-pair_names=Pair A|Pair B",
                                "--script-opts-append=dual_audio-helper=" + helper))
        fl, fr, bl, br = analyze.peaks(out)
        self.assertAlmostEqual(bl / fl, 10 ** (-6 / 20), delta=0.01)
        self.assertAlmostEqual(br / fr, 10 ** (-6 / 20), delta=0.01)

    def test_output_brings_its_settings(self):
        # In ears mode another output device has settings of its own (saved
        # before here), and they are heard once mpv plays to it. The WAV
        # starts again there too.
        with open(os.path.join(self.tmp.name, "devices.json"), "w") as f:
            json.dump({"coreaudio/test-uid (right ear)": {"gain": -6, "delay": 0}}, f)
        out = self.render(then=[("set_property", "audio-device", "coreaudio/test-uid")])
        left, right = analyze.peaks(out, start=3)
        self.assertAlmostEqual(right / left, 10 ** (-6 / 20), delta=0.01)


def calibrate(mpv, on=True):
    """Calibrate delay... (on) or Done (off), as the bubbaTV menu does it."""
    mpv.command("script-binding", "dual_audio/calibrate")
    mpv.wait_for(lambda: mpv.get("user-data/dual_audio")["calibrating"] == on)


class Pattern(unittest.TestCase):
    """The sync pattern as seen and heard: calibrating from 3.0 s, where the
    movie is paused, rendered to a WAV up to 8 s. A --vf filter logs each
    frame's brightness, which gives the flashes' timestamps; the WAV ends at
    the 8 s timestamp, which gives those of the beeps."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def render(self, *messages, opts=()):
        """The flashes and each channel's beeps (s) with `messages` sent before calibrating."""
        out, frames = os.path.join(self.tmp.name, "out.wav"), os.path.join(self.tmp.name, "frames.txt")
        mpv = Mpv(self.tmp.name, "--ao=pcm", "--ao-pcm-file=" + out, "--untimed", "--pause", "--start=3",
                  "--end=8", "--vf=lavfi=[signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=%s]" % frames,
                  "--script-opts-append=dual_audio-auto=yes", *opts)
        try:
            mpv.wait_for(lambda: mpv.get("seeking") is False)
            time.sleep(0.2)  # audio went through the filters: the delays change live, as in Live
            for m in messages:
                mpv.command("script-message-to", "dual_audio", *map(str, m))
            mpv.wait_for(lambda: all(mpv.get("user-data/dual_audio")["sides"][m[1] - 1]["delay"] == m[2]
                                     for m in messages))
            calibrate(mpv)
            mpv.proc.wait(timeout=60)  # unpaused: it plays to --end
        finally:
            mpv.close()
        # "frame:N pts:P pts_time:T" lines, each followed by "lavfi.signalstats.YAVG=Y".
        lines = read(frames).splitlines()
        white = [(float(frame.split("pts_time:")[1]), float(y.split("=")[1]) > 200)
                 for frame, y in zip(lines[::2], lines[1::2])]
        flashes = [t for (t, w), (_, before) in zip(white, [(0, False)] + white) if w and not before]
        n, pcm = analyze.decode(out)
        start = 8 - len(pcm) / n / analyze.RATE
        return flashes, [[t + start for t in ch] for ch in analyze.onsets(out)]

    def assert_beeps(self, beeps, flashes, ms):
        """Each flash's beep comes `ms` after it: those that are in the WAV."""
        want = [t + ms / 1000 for t in flashes if 3 <= t + ms / 1000 < 8]
        self.assertEqual(len(beeps), len(want), beeps)
        for b, w in zip(beeps, want):
            self.assertAlmostEqual(b, w, delta=0.002)

    def test_flash_and_beep_together(self):
        flashes, beeps = self.render()
        self.assertEqual(flashes[:5], [3, 4, 5, 6, 7])  # once a second from where the movie was
        for channel in beeps:
            self.assert_beeps(channel, flashes, 0)

    def test_each_side_its_delay(self):
        # As during the movie: later, or (negative) earlier than the flash,
        # from the first beep on.
        for delays in ((0, 150), (-200, 0)):
            flashes, beeps = self.render(*(("set-delay", i + 1, d) for i, d in enumerate(delays)))
            for channel, ms in zip(beeps, delays):
                self.assert_beeps(channel, flashes, ms)

    def test_pairs(self):
        # Both channels of a pair; the earlier pair is late too.
        flashes, beeps = self.render(("set-delay", 1, 50), ("set-delay", 2, 150),
                                     opts=("--script-opts-append=dual_audio-mode=pairs", "--audio-channels=quad"))
        self.assertEqual(len(beeps), 4)
        for channel, ms in zip(beeps, (50, 50, 150, 150)):
            self.assert_beeps(channel, flashes, ms)


class Calibration(Playing):
    """Calibrating while the movie is paused at 3.0 s with subtitles, then
    Done. The playlist holds the sample twice."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        subs = os.path.join(self.tmp.name, "subs.srt")
        srt(subs, "Line")
        self.start(TONES, "--sub-file=" + subs)

    def start(self, movie, *opts):
        self.mpv = Mpv(self.tmp.name, "--start=3", "--pause", "--script-opts-append=dual_audio-auto=yes",
                       *opts, movie, src=movie)
        self.expect((1, 2))

    def tearDown(self):
        self.mpv.close()
        self.tmp.cleanup()

    def props(self, *names):
        return [self.mpv.get(n) for n in names]

    def test_done_puts_the_movie_back(self):
        graph = self.mpv.get("lavfi-complex")
        self.mpv.wait_for(lambda: self.mpv.get("sub-text") == "Line 3")
        calibrate(self.mpv)
        self.assertIn("aevalsrc", self.mpv.get("lavfi-complex"))  # the pattern, not the tracks
        self.assertEqual(self.props("pause", "vid", "aid", "sub-visibility", "secondary-sub-visibility"),
                         [False, False, False, False, False])
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") > 3.5)
        self.message("set-delay", 2, 120)  # live, as during the movie
        self.wait_sides([(0, 0), (0, 120)])
        self.assertIn("aevalsrc", self.mpv.get("lavfi-complex"))
        calibrate(self.mpv, False)
        self.expect((1, 2))  # at 3.0 s
        self.assertEqual(self.mpv.get("lavfi-complex"), graph)
        self.assertEqual(self.props("pause", "vid", "sub-visibility", "secondary-sub-visibility"),
                         [True, 1, True, True])
        self.mpv.wait_for(lambda: self.mpv.get("sub-text") == "Line 3")
        self.af_has("delays=0|120")
        with open(os.path.join(self.tmp.name, "devices.json")) as f:  # remembered for the headphones
            self.assertEqual(json.load(f), {"system output (right ear)": {"gain": 0, "delay": 120}})

    def test_while_playing(self):
        # Done goes on playing, from where calibrating started.
        self.mpv.command("set_property", "pause", False)
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") > 3.3)
        calibrate(self.mpv)
        pos = self.mpv.get("time-pos")
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") > pos + 1)
        calibrate(self.mpv, False)
        self.mpv.wait_for(lambda: abs((self.mpv.get("time-pos") or 0) - pos) < 0.3)
        self.assertFalse(self.mpv.get("pause"))
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") > pos + 1)
        self.assertLess(abs(self.mpv.get("avsync")), 0.1)

    def test_picture_size(self):
        # The pattern has the movie's size, so the window keeps its own.
        small = os.path.join(self.tmp.name, "small.mkv")
        subprocess.run(["ffmpeg", "-v", "error", "-i", TONES, "-map", "0", "-vf", "scale=320:180", "-c:v", "libx264",
                        "-preset", "ultrafast", "-c:a", "copy", small], check=True)
        self.mpv.close()
        self.start(small)
        calibrate(self.mpv)
        self.mpv.wait_for(lambda: "aevalsrc" in self.mpv.get("lavfi-complex"))
        self.mpv.wait_for(lambda: self.props("video-params/w", "video-params/h") == [320, 180])

    def test_seeking(self):
        # The pattern starts again where a seek lands, rather than being
        # generated (and dropped) all the way there; Done still returns to
        # where calibrating started.
        calibrate(self.mpv)
        self.mpv.command("seek", 10, "absolute")
        self.mpv.wait_for(lambda: "asetpts=PTS+10/TB[ao]" in self.mpv.get("lavfi-complex"))
        self.mpv.wait_for(lambda: 10 <= (self.mpv.get("time-pos") or 0) < 11)
        calibrate(self.mpv, False)
        self.expect((1, 2))

    def test_track_change_ends_it(self):
        # A new graph ends calibrating: the movie comes back as it was.
        calibrate(self.mpv)
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") > 3.5)
        self.press("}", (1, 3))
        self.assertFalse(self.state()["calibrating"])
        self.assertEqual(self.props("pause", "vid", "sub-visibility"), [True, 1, True])

    def test_next_file_ends_it(self):
        calibrate(self.mpv)
        self.mpv.command("playlist-next")
        self.mpv.wait_for(lambda: self.mpv.get("playlist-pos") == 1)
        self.expect((1, 2))
        self.assertFalse(self.state()["calibrating"])
        self.assertEqual(self.props("pause", "vid", "sub-visibility"), [True, 1, True])


class Devices(unittest.TestCase):
    """dualplay -2's headphones: a pair moving to others (as the bubbaTV menu
    asks) and dropping out mid-movie (as the menu reports). A fake helper
    stands in for tools/audio-pairs; its calls are checked instead."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.helper, self.calls = fake_helper(self.tmp.name)
        self.log = os.path.join(self.tmp.name, "mpv.log")
        self.start()

    def start(self, *opts):
        self.mpv = Mpv(self.tmp.name, "--start=3", "--pause", "--audio-channels=quad",
                       "--audio-device=coreaudio/com.multilingual-player.audio-pairs",
                       "--ao-pcm-file=" + os.path.join(self.tmp.name, "missing", "audio.wav"),
                       "--log-file=" + self.log,
                       "--script-opts-append=dual_audio-auto=yes",
                       "--script-opts-append=dual_audio-mode=pairs",
                       "--script-opts-append=dual_audio-pair_names=Nikita’s AirPods|Friend, Pro",
                       "--script-opts-append=dual_audio-helper=" + self.helper, *opts)
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") is not None)

    def tearDown(self):
        self.mpv.close()
        self.tmp.cleanup()

    def message(self, *args):
        self.mpv.command("script-message-to", "dual_audio", *map(str, args))

    def sides(self):
        return self.mpv.get("user-data/dual_audio")["sides"]

    def devices(self):
        return [s["device"] for s in self.sides()]

    def wait_logged(self, text):
        self.mpv.wait_for(lambda: text in read(self.log))

    def af_has(self, *parts):
        self.mpv.wait_for(lambda: all(p in af_graph(self.mpv) for p in parts))

    def wait_lost(self, lost):
        self.mpv.wait_for(lambda: [s.get("lost", False) for s in self.sides()] == lost)

    def toggle(self, enabled):
        self.mpv.command("keypress", "a")
        self.mpv.wait_for(lambda: self.mpv.get("user-data/dual_audio")["enabled"] == enabled)

    def wait_output(self, device, channels):
        self.mpv.wait_for(lambda: self.mpv.get("audio-device") == device
                         and self.mpv.get("audio-channels") == channels
                         and (self.mpv.get("audio-out-params") or {}).get("channels") == channels
                         and self.mpv.get("current-ao") == "null")

    def fail_output(self):
        # Fail a real AO without touching CoreAudio: PCM cannot write into a
        # nonexistent directory. Returning to null alone does not revive it.
        self.mpv.command("set_property", "ao", "pcm")
        self.mpv.command("ao-reload")
        self.mpv.wait_for(lambda: self.mpv.get("current-ao") is None)
        self.mpv.command("set_property", "ao", "null")

    def test_single_output_and_back(self):
        self.message("set-track", 1, 3)
        self.mpv.wait_for(lambda: self.sides()[0]["track"] == 3)
        self.message("set-gain", 2, -6)
        self.message("set-delay", 2, 120)
        self.mpv.wait_for(lambda: (self.sides()[1]["gain"], self.sides()[1]["delay"]) == (-6, 120))
        self.toggle(False)
        self.wait_output("auto", "stereo")
        self.assertEqual(self.mpv.get("lavfi-complex"), "")
        self.assertFalse(any(f.get("label") == "dual_audio" for f in self.mpv.get("af")))
        self.assertEqual(self.mpv.get("aid"), 3)
        self.mpv.command("set_property", "pause", False)
        self.message("outputs", "Nikita’s AirPods", "MacBook Pro Speakers")
        self.wait_lost([False, True])
        self.assertFalse(self.mpv.get("pause"))
        self.assertEqual(self.calls(), [])
        self.message("outputs", "Nikita’s AirPods", "Friend, Pro", "MacBook Pro Speakers")
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") > 3.5)
        self.assertEqual(self.calls(), [])  # reconnection while off must not rebuild
        self.mpv.command("set_property", "pause", True)
        pos = self.mpv.get("time-pos")
        self.toggle(True)
        self.mpv.wait_for(lambda: len(self.calls()) == 1)
        self.wait_lost([False, False])
        self.wait_output("coreaudio/com.multilingual-player.audio-pairs", "quad")
        self.assertEqual([s["track"] for s in self.sides()], [3, 2])
        self.af_has("volume@dual_audio2=-6dB", "delays=0|0|120|120")
        self.assertAlmostEqual(self.mpv.get("time-pos"), pos, delta=0.05)
        self.assertTrue(self.mpv.get("pause"))

    def test_enable_rebuilds_without_device_notifications(self):
        self.message("outputs", "Nikita’s AirPods")
        self.wait_lost([False, True])
        self.toggle(False)
        # Both are available to the helper again, but no outputs message arrived.
        self.toggle(True)
        self.mpv.wait_for(lambda: self.calls() == ["create|Nikita’s AirPods|Friend, Pro"])
        self.wait_lost([False, False])
        self.wait_output("coreaudio/com.multilingual-player.audio-pairs", "quad")

    def test_disable_after_output_failure(self):
        self.fail_output()
        self.toggle(False)
        self.wait_output("auto", "stereo")
        self.assertEqual(self.mpv.get("aid"), 1)
        self.assertAlmostEqual(self.mpv.get("time-pos"), 3.0, delta=0.05)

    def test_reconnect_after_output_failure(self):
        self.message("outputs", "Nikita’s AirPods")
        self.wait_lost([False, True])
        self.fail_output()
        self.message("outputs", "Nikita’s AirPods", "Friend, Pro")
        self.wait_lost([False, False])
        self.wait_output("coreaudio/com.multilingual-player.audio-pairs", "quad")
        self.assertIn("[aid1]", self.mpv.get("lavfi-complex"))
        self.assertIn("[aid2]", self.mpv.get("lavfi-complex"))
        self.mpv.wait_for(lambda: abs(self.mpv.get("time-pos") - 3.0) < 0.05)

    def test_disable_during_rebuild(self):
        self.message("set-device", 2, "Other Phones")
        self.mpv.wait_for(lambda: len(self.calls()) == 1)
        self.toggle(False)
        self.wait_output("auto", "stereo")
        self.mpv.wait_for(lambda: self.devices() == ["Nikita’s AirPods", "Other Phones"])
        self.wait_output("auto", "stereo")
        self.assertEqual(self.mpv.get("lavfi-complex"), "")
        self.assertFalse(self.mpv.get("user-data/dual_audio")["active"])

    def test_disable_during_dropout_grace(self):
        self.mpv.close()
        self.start("--script-opts-append=dual_audio-dropout_grace=0.5")
        self.mpv.command("set_property", "pause", False)
        self.message("outputs", "Nikita’s AirPods")
        self.wait_lost([False, True])
        self.toggle(False)
        self.wait_output("auto", "stereo")
        position = self.mpv.get("time-pos")
        time.sleep(0.8)
        self.assertFalse(self.mpv.get("pause"))
        self.assertGreater(self.mpv.get("time-pos"), position + 0.3)
        self.assertEqual(self.calls(), [])

    def test_disable_during_resume_countdown(self):
        self.mpv.close()
        self.start("--script-opts-append=dual_audio-dropout_grace=0.2",
                   "--script-opts-append=dual_audio-resume_countdown=1")
        self.mpv.command("set_property", "pause", False)
        self.message("outputs", "Nikita’s AirPods")
        self.mpv.wait_for(lambda: self.mpv.get("pause"))
        self.message("outputs", "Nikita’s AirPods", "Friend, Pro")
        self.wait_logged("reconnected, playing on")
        self.wait_lost([False, False])
        self.toggle(False)
        self.wait_output("auto", "stereo")
        time.sleep(1.3)
        self.assertTrue(self.mpv.get("pause"))
        self.assertEqual(len(self.calls()), 1)

    def test_disable_while_calibrating_with_missing_pair(self):
        self.mpv.command("set_property", "pause", False)
        calibrate(self.mpv)
        self.message("outputs", "Nikita’s AirPods")
        self.wait_lost([False, True])
        self.toggle(False)
        self.wait_output("auto", "stereo")
        self.assertFalse(self.mpv.get("user-data/dual_audio")["calibrating"])
        self.assertFalse(self.mpv.get("pause"))

    def test_move_a_pair(self):
        self.message("set-device", 2, "Other Phones")
        self.mpv.wait_for(lambda: self.devices() == ["Nikita’s AirPods", "Other Phones"])
        self.assertEqual(self.calls(), ["create|Nikita’s AirPods|Other Phones"])
        self.mpv.wait_for(lambda: ao_inits(self.log) == 2)  # ao-reload: on to the new device
        self.wait_logged("Other Phones: [2] Russian 1000Hz 5.1 (rus)")
        self.assertAlmostEqual(self.mpv.get("time-pos"), 3.0, delta=0.05)
        # The other pair's headphones: the two pairs swap theirs.
        self.message("set-device", 1, "Other Phones")
        self.mpv.wait_for(lambda: self.devices() == ["Other Phones", "Nikita’s AirPods"])
        self.assertEqual(self.calls()[-1], "create|Other Phones|Nikita’s AirPods")

    def test_moves_wait_their_turn(self):
        # The second, asked for while the helper still builds the first,
        # starts from where the first leaves the pairs.
        self.message("set-device", 1, "Other Phones")
        self.message("set-device", 2, "Nikita’s AirPods")
        self.mpv.wait_for(lambda: len(self.calls()) == 2)
        self.assertEqual(self.calls(), ["create|Other Phones|Friend, Pro",
                                        "create|Other Phones|Nikita’s AirPods"])
        self.mpv.wait_for(lambda: self.devices() == ["Other Phones", "Nikita’s AirPods"])

    def test_track_choices_follow_a_device_move(self):
        self.message("set-track", 2, 3)
        self.mpv.wait_for(lambda: [s["track"] for s in self.sides()] == [1, 3])
        self.message("set-device", 1, "Friend, Pro")
        self.mpv.wait_for(lambda: self.devices() == ["Friend, Pro", "Nikita’s AirPods"])
        self.mpv.wait_for(lambda: [s["track"] for s in self.sides()] == [3, 1])
        self.assertAlmostEqual(self.mpv.get("time-pos"), 3.0, delta=0.05)

    def test_helper_fails(self):
        self.message("set-device", 2, "Broken Phones")
        self.wait_logged('could not switch the headphones | "Broken" matches 0 outputs')
        self.assertEqual(self.devices(), ["Nikita’s AirPods", "Friend, Pro"])
        self.assertEqual(ao_inits(self.log), 1)

    def test_settings_follow_a_move(self):
        # Loudness and delay stay with the headphones: after a swap they are
        # on the other pair, and headphones new to a pair bring their own.
        self.message("set-gain", 2, -6)
        self.mpv.wait_for(lambda: [s["gain"] for s in self.sides()] == [0, -6])
        self.message("set-device", 1, "Friend, Pro")
        self.mpv.wait_for(lambda: self.devices() == ["Friend, Pro", "Nikita’s AirPods"])
        self.af_has("volume@dual_audio1=-6dB", "volume@dual_audio2=0dB")
        self.assertEqual([s["gain"] for s in self.sides()], [-6, 0])
        self.message("set-delay", 2, 120)  # Nikita's
        self.mpv.wait_for(lambda: [s["delay"] for s in self.sides()] == [0, 120])
        self.message("set-device", 2, "Other Phones")
        self.mpv.wait_for(lambda: self.devices() == ["Friend, Pro", "Other Phones"])
        self.af_has("volume@dual_audio1=-6dB", "volume@dual_audio2=0dB", "delays=0|0|0|0")
        self.mpv.wait_for(lambda: abs(self.mpv.get("time-pos") - 3.0) < 0.05)
        with open(os.path.join(self.tmp.name, "devices.json")) as f:
            self.assertEqual(json.load(f), {"Friend, Pro": {"gain": -6, "delay": 0},
                                            "Nikita’s AirPods": {"gain": 0, "delay": 120}})

    def test_dropout_and_back(self):
        self.mpv.command("set_property", "pause", False)
        self.message("outputs", "Nikita’s AirPods", "MacBook Pro Speakers")
        self.mpv.wait_for(lambda: self.mpv.get("pause"))
        self.wait_logged("Pair 2 (Friend, Pro) dropped out, so the movie is paused.")
        self.wait_lost([False, True])
        self.assertEqual(self.calls(), [])
        self.message("outputs", "Nikita’s AirPods", "Friend, Pro", "MacBook Pro Speakers")
        self.mpv.wait_for(lambda: self.calls() == ["create|Nikita’s AirPods|Friend, Pro"])
        self.wait_logged("Pair 2 (Friend, Pro) reconnected, playing on")
        self.mpv.wait_for(lambda: ao_inits(self.log) == 2)
        self.wait_lost([False, False])
        self.assertTrue(self.mpv.get("pause"))  # countdown before playing on
        self.mpv.wait_for(lambda: not self.mpv.get("pause"))

    def test_second_dropout_cancels_resume(self):
        self.mpv.close()
        self.start("--script-opts-append=dual_audio-dropout_grace=0.2",
                   "--script-opts-append=dual_audio-resume_countdown=1")
        self.mpv.command("set_property", "pause", False)
        self.message("outputs", "Nikita’s AirPods")
        self.mpv.wait_for(lambda: self.mpv.get("pause"))
        self.message("outputs", "Nikita’s AirPods", "Friend, Pro")
        self.wait_logged("reconnected, playing on")
        self.wait_lost([False, False])
        self.message("outputs", "Nikita’s AirPods")
        self.wait_lost([False, True])
        time.sleep(1.3)  # the original countdown must not resume missing headphones
        self.assertTrue(self.mpv.get("pause"))
        self.assertEqual(len(self.calls()), 1)
        self.message("outputs", "Nikita’s AirPods", "Friend, Pro")
        self.wait_lost([False, False])
        self.mpv.wait_for(lambda: not self.mpv.get("pause"))
        self.assertEqual(len(self.calls()), 2)

    def test_dropout_does_not_resume_user_pause(self):
        self.message("outputs", "Nikita’s AirPods")
        self.wait_logged("dropped out, so the movie is paused.")
        self.message("outputs", "Nikita’s AirPods", "Friend, Pro")
        self.wait_logged("reconnected. | Press Space to resume.")
        time.sleep(3.2)
        self.assertTrue(self.mpv.get("pause"))

    def test_dropout_during_rebuild_stays_missing(self):
        self.mpv.close()
        with open(self.helper, "r") as f:
            helper = f.read().replace("sleep 0.2", "sleep 0.6")
        with open(self.helper, "w") as f:
            f.write(helper)
        self.start("--script-opts-append=dual_audio-dropout_grace=0.2",
                   "--script-opts-append=dual_audio-resume_countdown=1")
        self.mpv.command("set_property", "pause", False)
        self.message("outputs", "Nikita’s AirPods")
        self.mpv.wait_for(lambda: self.mpv.get("pause"))
        self.message("outputs", "Nikita’s AirPods", "Friend, Pro")
        self.mpv.wait_for(lambda: len(self.calls()) == 1)
        # The output disappears before the successful helper callback arrives.
        self.message("outputs", "Nikita’s AirPods")
        self.mpv.wait_for(lambda: ao_inits(self.log) == 2)
        time.sleep(1.3)
        self.wait_lost([False, True])
        self.assertTrue(self.mpv.get("pause"))
        self.message("outputs", "Nikita’s AirPods", "Friend, Pro")
        self.wait_lost([False, False])
        self.mpv.wait_for(lambda: not self.mpv.get("pause"))

    def test_dropout_while_calibrating(self):
        # Done puts back the pause state from before, but headphones that
        # dropped out meanwhile keep the movie paused.
        self.mpv.command("set_property", "pause", False)
        calibrate(self.mpv)
        self.message("outputs", "Nikita’s AirPods", "MacBook Pro Speakers")
        self.mpv.wait_for(lambda: self.mpv.get("pause"))
        calibrate(self.mpv, False)
        self.assertTrue(self.mpv.get("pause"))


class AudioRecovery(unittest.TestCase):
    """A missing PCM output directory fails like a device that cannot open,
    and creating it lets real mpv audio recover without real headphones."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.helper, self.calls = fake_helper(self.tmp.name)
        self.output = os.path.join(self.tmp.name, "missing")
        self.log = os.path.join(self.tmp.name, "mpv.log")
        self.mpv = None

    def start(self, *opts):
        self.mpv = Mpv(self.tmp.name, "--start=3", "--ao=pcm",
                       "--ao-pcm-file=" + os.path.join(self.output, "out.wav"),
                       "--log-file=" + self.log, "--audio-channels=quad",
                       "--script-opts-append=dual_audio-auto=yes",
                       "--script-opts-append=dual_audio-mode=pairs",
                       "--script-opts-append=dual_audio-pair_names=Pair A|Pair B",
                       "--script-opts-append=dual_audio-helper=" + self.helper, *opts)
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") is not None)

    def tearDown(self):
        if self.mpv:
            self.mpv.close()
        self.tmp.cleanup()

    def test_failed_output_retries_and_stays_paused(self):
        self.start()
        self.mpv.wait_for(lambda: "no sound, the headphones' device would not open" in read(self.log), timeout=10)
        self.assertEqual(len(self.calls()), 3)
        self.assertIsNone(self.mpv.get("current-ao"))
        self.assertTrue(self.mpv.get("pause"))
        position = self.mpv.get("time-pos")
        time.sleep(1.2)
        self.assertEqual(len(self.calls()), 3)
        self.assertAlmostEqual(self.mpv.get("time-pos"), position, delta=0.05)

    def test_output_recovers_after_retry(self):
        self.start()
        # Initial open and first rebuild both fail; the second rebuild can open it.
        self.mpv.wait_for(lambda: read(self.log).count("Failed to initialize audio driver 'pcm'") >= 2)
        self.assertTrue(self.mpv.get("pause"))
        os.mkdir(self.output)
        self.mpv.wait_for(lambda: self.mpv.get("current-ao") == "pcm")
        self.mpv.wait_for(lambda: not self.mpv.get("pause"))
        self.assertEqual(len(self.calls()), 2)
        position = self.mpv.get("time-pos")
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") > position + 0.2)

    def test_recovery_keeps_user_pause(self):
        self.start("--pause")
        os.mkdir(self.output)
        self.mpv.wait_for(lambda: self.mpv.get("current-ao") == "pcm")
        time.sleep(1.2)  # allow the recovery check to run
        self.assertTrue(self.mpv.get("pause"))
        self.assertEqual(len(self.calls()), 1)

    def test_failed_helper_retries_are_bounded(self):
        self.start("--script-opts-append=dual_audio-pair_names=Pair A|Broken Phones")
        self.mpv.wait_for(lambda: "no sound, the headphones' device would not open" in read(self.log), timeout=10)
        self.assertEqual(len(self.calls()), 3)
        self.assertTrue(self.mpv.get("pause"))


class LastPlayback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.last = os.path.join(self.tmp.name, "last.json")
        self.option = "--script-opts-append=dual_audio-last_file=" + self.last

    def tearDown(self):
        self.tmp.cleanup()

    def test_finished_movie_is_not_resumable(self):
        subprocess.run(BASE + ["--ao=null", "--start=19", self.option, TONES], check=True, timeout=15)
        last = json.loads(read(self.last))
        self.assertTrue(last.get("finished"))
        self.assertEqual(last["index"], 0)
        self.assertEqual(last["playlist"], [TONES])

    def test_finished_playlist_is_not_resumable(self):
        subprocess.run(BASE + ["--ao=null", "--start=19", self.option, TONES, SYNC], check=True, timeout=15)
        last = json.loads(read(self.last))
        self.assertTrue(last.get("finished"))
        self.assertEqual(last["index"], 1)
        self.assertEqual(last["playlist"], [TONES, SYNC])

    def test_intermediate_eof_and_quit_keep_next_episode_resumable(self):
        mpv = Mpv(self.tmp.name, "--start=19", self.option, TONES, src=SYNC)
        try:
            mpv.wait_for(lambda: mpv.get("playlist-pos") == 1)
            mpv.command("set_property", "pause", True)
            mpv.command("seek", 5, "absolute+exact")
            mpv.wait_for(lambda: abs((mpv.get("time-pos") or 0) - 5) < 0.05)
            mpv.quit()
            last = json.loads(read(self.last))
            self.assertFalse(last.get("finished", False))
            self.assertEqual(last["index"], 1)
            self.assertEqual(last["playlist"], [TONES, SYNC])
            self.assertAlmostEqual(last["time"], 5, delta=0.05)
        finally:
            mpv.close()


class Subs(unittest.TestCase):
    """Subtitles keep coming after the audio graph is rebuilt. The cache is kept
    small so mpv has to go on reading them, as it does with a long movie."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        subs = os.path.join(self.tmp.name, "subs.srt")
        srt(subs, "Line")
        movie = os.path.join(self.tmp.name, "movie.mkv")
        subprocess.run(["ffmpeg", "-v", "error", "-i", TONES, "-i", subs, "-map", "0", "-map", "1",
                        "-c", "copy", "-c:s", "srt", movie], check=True)
        self.mpv = Mpv(self.tmp.name, "--sid=1", "--script-opts=dual_audio-auto=yes",
                       "--cache=no", "--demuxer-readahead-secs=0.5", "--demuxer-max-bytes=1MiB",
                       "--demuxer-max-back-bytes=0", src=movie)
        self.mpv.wait_for(lambda: self.mpv.get("sub-text"))

    def tearDown(self):
        self.mpv.close()
        self.tmp.cleanup()

    def assert_subs_keep_coming(self):
        start = self.mpv.get("time-pos")
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") > start + 2.5, timeout=15)
        self.assertRegex(self.mpv.get("sub-text"), r"^Line \d+$")

    def test_cycle(self):
        self.mpv.command("keypress", "}")
        self.assert_subs_keep_coming()

    def test_toggle_off(self):
        self.mpv.command("keypress", "a")
        self.assert_subs_keep_coming()

    def test_calibrate(self):
        calibrate(self.mpv)
        calibrate(self.mpv, False)
        self.assert_subs_keep_coming()


FOLLOW = "--script-opts-append=dual_audio-follow_subs=yes"


class FollowSubs(Playing):
    """Subtitles that follow the pairs. The movie has forced English, Russian
    and English subtitles; the next file only Russian and English, in other
    slots. Line N of a track says "<word> N", so at 3.0s the full English
    track shows "Eng 3" and the forced one "Forced 3"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.movie = self.make("movie.mkv", ("eng", "Forced"), ("rus", "Rus"), ("eng", "Eng"))
        self.next = self.make("next.mkv", ("rus", "Rus"), ("eng", "Eng"))
        self.mpv = None

    def tearDown(self):
        if self.mpv:
            self.mpv.close()
        self.tmp.cleanup()

    def make(self, name, *subs):
        """The tones plus subtitle tracks (language, word); word "Forced" marks one forced."""
        cmd, tags = ["ffmpeg", "-v", "error", "-i", TONES], []
        for i, (lang, word) in enumerate(subs):
            path = os.path.join(self.tmp.name, "%s.%d.srt" % (name, i))
            srt(path, word)
            cmd += ["-i", path]
            tags += ["-map", str(i + 1), "-metadata:s:s:%d" % i, "language=" + lang,
                     "-disposition:s:%d" % i, "forced" if word == "Forced" else "0"]
        out = os.path.join(self.tmp.name, name)
        subprocess.run(cmd + ["-map", "0", *tags, "-c", "copy", "-c:s", "srt", out], check=True)
        return out

    def start(self, *opts):
        self.mpv = Mpv(self.tmp.name, "--start=3", "--pause", "--script-opts-append=dual_audio-auto=yes",
                       *opts, self.movie, src=self.next)
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") is not None)

    def shows(self, main, top):
        """Wait for these lines at 3.0s: the main subtitles (bottom), then the
        secondary ones (top); None = no line."""
        want = [main and main + " 3", top and top + " 3"]
        self.mpv.wait_for(lambda: [self.mpv.get("sub-text"), self.mpv.get("secondary-sub-text")] == want)

    def ids(self):
        return [self.mpv.get("sid"), self.mpv.get("secondary-sid")]

    def test_off_by_default(self):
        self.start("--sid=2")
        self.shows("Rus", None)
        self.press("}", (1, 3))
        self.shows("Rus", None)
        self.assertFalse(self.state()["follow_subs"])

    def test_track_changes_and_swap(self):
        self.start(FOLLOW)
        self.expect((1, 2))
        self.shows("Eng", "Rus")    # the full English track, not the forced one
        self.assertEqual(self.ids(), [3, 2])
        self.press("}", (1, 3))
        self.shows("Eng", None)     # no German subtitles: the top stays empty
        self.press("{", (2, 3))
        self.shows("Rus", None)
        self.press("y", (3, 2))
        self.shows(None, "Rus")     # ... and so does the bottom
        self.message("set-track", 1, 2)
        self.expect((2, 2))
        self.shows("Rus", None)     # both hear Russian: one line is enough
        self.assertTrue(self.state()["follow_subs"])

    def test_next_file(self):
        self.start(FOLLOW)
        self.press("y", (2, 1))
        self.shows("Rus", "Eng")
        self.next_file()
        self.expect((2, 1))
        self.shows("Rus", "Eng")
        self.assertEqual(self.ids(), [1, 2])  # found by language, not by id

    def test_own_pick_stops_following(self):
        self.start(FOLLOW)
        self.shows("Eng", "Rus")
        self.mpv.command("keypress", "v")      # hiding them is no pick
        self.mpv.command("keypress", "Alt+v")
        self.press("}", (1, 3))
        self.assertEqual(self.ids(), [3, False])
        self.mpv.command("keypress", "j")      # mpv's next subtitle track: a pick
        self.mpv.wait_for(lambda: not self.state()["follow_subs"])
        picked = self.ids()
        self.press("{", (2, 3))
        self.press("y", (3, 2))
        self.assertEqual(self.ids(), picked)

    def test_switch_off_and_dual_audio_off(self):
        # Either way the top line goes, and the bottom one stays: the first
        # pair's language is what plays with dual audio off.
        self.start(FOLLOW)
        self.shows("Eng", "Rus")
        self.mpv.command("script-binding", "dual_audio/follow-subs")
        self.shows("Eng", None)
        self.mpv.wait_for(lambda: not self.state()["follow_subs"])  # published just after
        self.mpv.command("script-binding", "dual_audio/follow-subs")
        self.shows("Eng", "Rus")
        self.expect((1, 2))         # still at 3.0s
        self.press("a", None)
        self.shows("Eng", None)
        self.press("a", (1, 2))
        self.shows("Eng", "Rus")

    def test_calibrate(self):
        # Hidden while the pattern plays; hiding them is no pick of one's own.
        self.start(FOLLOW)
        self.shows("Eng", "Rus")
        calibrate(self.mpv)
        self.assertEqual([self.mpv.get("sub-visibility"), self.mpv.get("secondary-sub-visibility")], [False, False])
        calibrate(self.mpv, False)
        self.expect((1, 2))
        self.shows("Eng", "Rus")
        self.assertEqual([self.mpv.get("sub-visibility"), self.mpv.get("secondary-sub-visibility")], [True, True])
        self.assertTrue(self.state()["follow_subs"])

    def test_keep_coming_after_graph_change(self):
        # A swap rebuilds the graph: both subtitles must be selected anew
        # (see Subs). A loudness change before it is live and leaves them be.
        self.start(FOLLOW, "--cache=no", "--demuxer-readahead-secs=0.5", "--demuxer-max-bytes=1MiB",
                   "--demuxer-max-back-bytes=0")
        self.shows("Eng", "Rus")
        self.mpv.command("set_property", "pause", False)
        self.message("set-gain", 1, -3)
        self.wait_sides([(-3, 0), (0, 0)])
        self.mpv.command("keypress", "y")
        self.mpv.wait_for(lambda: [s.get("track") for s in self.state()["sides"]] == [2, 1])
        start = self.mpv.get("time-pos")
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") > start + 2.5, timeout=15)
        self.assertRegex(self.mpv.get("sub-text"), r"^Rus \d+$")
        self.assertRegex(self.mpv.get("secondary-sub-text"), r"^Eng \d+$")


class Menu(unittest.TestCase):
    """The bubbaTV menu plugin, built with swiftc against this mpv's headers.
    It mirrors the menu it builds as text in user-data/bubbatv/menu."""

    @classmethod
    def setUpClass(cls):
        cls.prefix = os.path.dirname(os.path.dirname(os.path.realpath(shutil.which("mpv"))))
        cls.header = os.path.join(cls.prefix, "include", "mpv", "client.h")
        if not shutil.which("swiftc") or not os.path.exists(cls.header):
            raise unittest.SkipTest("needs swiftc and mpv's headers")
        cls.build = tempfile.TemporaryDirectory()
        cls.plugin = os.path.join(cls.build.name, "bubbatv.so")
        cls.swiftc("-emit-library", "-o", cls.plugin, "-Xlinker", "-undefined", "-Xlinker", "dynamic_lookup")

    @classmethod
    def swiftc(cls, *args, sources=()):
        subprocess.run(["swiftc", "-O", MENU, *sources, "-import-objc-header", cls.header, *args],
                       check=True, timeout=300)

    @classmethod
    def harness(cls):
        # Built against libmpv: clicks the real menu items.
        path = os.path.join(cls.build.name, "menu_clicks")
        if not os.path.exists(path):
            cls.swiftc("-o", path, "-L", os.path.join(cls.prefix, "lib"), "-lmpv",
                       sources=[os.path.join(ROOT, "tests", "menu_clicks", "main.swift")])
        return path

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.start()

    def start(self, *opts, env=None):
        self.mpv = Mpv(self.tmp.name, "--script=" + self.plugin, "--start=3", "--pause",
                       "--script-opts-append=dual_audio-mode=pairs",
                       "--script-opts-append=dual_audio-pair_names=Nikita’s AirPods|Friend, Pro",
                       *opts, env=env)
        # The script has published its state, so its keys are bound: a key
        # pressed before that goes nowhere.
        self.mpv.wait_for(lambda: self.mpv.get("user-data/dual_audio"))

    def outputs(self, *names):
        """The fake list of connected outputs the plugin reads (BUBBATV_DEVICES)."""
        path = os.path.join(self.tmp.name, "outputs")
        with open(path + ".new", "w", encoding="utf-8") as f:
            f.write("\n".join(names) + "\n")
        os.replace(path + ".new", path)  # never half a list
        return path

    def tearDown(self):
        self.mpv.close()
        self.tmp.cleanup()

    def menu(self):
        return self.mpv.get("user-data/bubbatv/menu") or ""

    def wait_menu(self, text):
        self.mpv.wait_for(lambda: text in self.menu())

    def test_in_menu_bar(self):
        self.mpv.wait_for(lambda: "Playback,bubbaTV,Window" in (self.mpv.get("user-data/bubbatv/menubar") or ""))

    def test_follows_the_state(self):
        self.wait_menu("Dual audio")
        self.assertNotIn("Pair 1", self.menu())  # off: just the switch
        self.mpv.command("keypress", "a")
        self.wait_menu("[x] Dual audio")
        self.assertIn("Pair 1\nDevice: Nikita’s AirPods (disabled)\nAudio track: [1] English 440Hz (eng)\n"
                      "  [x] [1] English 440Hz (eng)\n  [2] Russian 1000Hz 5.1 (rus)\n", self.menu())
        self.assertIn("Pair 2\nDevice: Friend, Pro (disabled)\nAudio track: [2] Russian", self.menu())
        self.assertIn("Swap pairs", self.menu())
        for msg in (("set-gain", 2, -6), ("set-delay", 1, 120), ("set-track", 2, 1)):
            self.mpv.command("script-message-to", "dual_audio", *map(str, msg))
        self.wait_menu("Pair 2\nDevice: Friend, Pro (disabled)\nAudio track: [1] English 440Hz (eng)\n"
                       "  [x] [1] English 440Hz (eng)\n  [2] Russian 1000Hz 5.1 (rus)\n"
                       "  [3] German 2500Hz (ger)\nLoudness -6 dB\nDelay 0 ms")
        self.assertIn("Loudness 0 dB\nDelay +120 ms", self.menu())

    def test_calibrate_item(self):
        self.mpv.command("keypress", "a")
        self.wait_menu("---\nCalibrate delay…\nSwap pairs")
        calibrate(self.mpv)
        self.wait_menu("---\nDone calibrating\nSwap pairs")
        self.assertIn("Pair 2\nDevice: Friend, Pro (disabled)\nAudio track:", self.menu())  # still set per pair
        calibrate(self.mpv, False)
        self.wait_menu("---\nCalibrate delay…\nSwap pairs")

    def test_seek_items(self):
        # Back / Forward come last, with dual audio off too.
        playback = "---\nPlayback\nBack 5 s\nForward 5 s"
        self.mpv.wait_for(lambda: self.menu() == "Dual audio\n" + playback)
        self.mpv.command("keypress", "a")
        self.wait_menu("Swap pairs\nSubtitles follow pairs\n" + playback)
        self.assertTrue(self.menu().endswith("Subtitles follow pairs\n" + playback))
        # Only while a file is loaded.
        self.mpv.close()
        self.mpv = Mpv(self.tmp.name, "--script=" + self.plugin, "--idle=yes", src=None)
        self.mpv.wait_for(lambda: self.menu() == "Dual audio")
        self.mpv.command("loadfile", TONES)
        self.mpv.wait_for(lambda: self.menu() == "Dual audio\n" + playback)
        self.mpv.command("stop")
        self.mpv.wait_for(lambda: self.menu() == "Dual audio")

    def test_seek_items_step(self):
        self.mpv.close()
        self.start("--script-opts-append=dual_audio-seek_step=10")
        self.wait_menu("Playback\nBack 10 s\nForward 10 s")

    def test_follow_subs_item(self):
        self.mpv.command("keypress", "a")
        self.wait_menu("Swap pairs\nSubtitles follow pairs")
        self.mpv.command("script-binding", "dual_audio/follow-subs")
        self.wait_menu("Swap pairs\n[x] Subtitles follow pairs")

    def test_playback_section(self):
        # With 2+ files, dual audio on or off: previous / next file and the
        # playlist by file name - with the folder where two share a name -
        # after Back / Forward.
        self.mpv.wait_for(lambda: self.menu() == "Dual audio\n---\nPlayback\nBack 5 s\nForward 5 s")  # one file
        for name in ("S1/Ep 1.mkv", "S1/Ep 2.mkv", "S2/Ep 1.mkv"):
            self.mpv.command("loadfile", episode(os.path.join(self.tmp.name, name)), "append")
        self.wait_menu("Dual audio\n---\nPlayback\nBack 5 s\nForward 5 s\nPrevious file (disabled)\nNext file\n"
                       "Files\n  [x] tones.mkv\n  Ep 1.mkv (S1)\n  Ep 2.mkv\n  Ep 1.mkv (S2)")
        self.mpv.command("keypress", "a")  # below the dual audio settings
        self.wait_menu("Subtitles follow pairs\n---\nPlayback\nBack 5 s\nForward 5 s\nPrevious file (disabled)\n")
        self.mpv.command("playlist-play-index", 3)
        self.wait_menu("Playback\nBack 5 s\nForward 5 s\nPrevious file\nNext file (disabled)\nFiles\n"
                       "  tones.mkv\n  Ep 1.mkv (S1)\n  Ep 2.mkv\n  [x] Ep 1.mkv (S2)")
        self.assertTrue(self.menu().endswith("[x] Ep 1.mkv (S2)"))

    def test_playback_without_script(self):
        # The files don't need dual_audio.lua (the plugin reads mpv's
        # playlist); Back / Forward do.
        self.mpv.close()
        files = [episode(os.path.join(self.tmp.name, "Ep %d.mkv" % i)) for i in (1, 2)]
        self.mpv = Mpv(self.tmp.name, "--script=" + self.plugin, "--pause", *files, src=None,
                       cmd=["mpv", "--no-config", "--vo=null", "--really-quiet"])
        self.mpv.wait_for(lambda: self.menu() == "Dual audio (disabled)\ndual_audio.lua is not loaded (disabled)\n"
                          "---\nPlayback\nPrevious file (disabled)\nNext file\nFiles\n  [x] Ep 1.mkv\n  Ep 2.mkv")

    def test_playback_clicks(self):
        # Next file, a pick from Files and Previous file in the real menu.
        files = [episode(os.path.join(self.tmp.name, "Ep %d.mkv" % i)) for i in (1, 2, 3)]
        out = subprocess.run([self.harness(), "playlist", SCRIPT, os.path.join(self.tmp.name, "clicks.json"), *files],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("ok previous file", out.stdout)

    def test_clicks(self):
        # Clicks the real menu items and checks the graph, and the position
        # after Back / Forward.
        devices = os.path.join(self.tmp.name, "clicks.json")
        out = subprocess.run([self.harness(), TONES, SCRIPT, devices], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("ok forward 5 s with dual audio off", out.stdout)  # the last check
        with open(devices) as f:  # the loudness click, remembered
            self.assertEqual(json.load(f), {"system output (right ear)": {"gain": 1, "delay": 0}})

    def test_device_menu(self):
        # Under dualplay -2 (the helper set) each pair's Device row lists the
        # connected outputs and follows them as headphones come and go.
        helper, calls = fake_helper(self.tmp.name)
        outputs = self.outputs("Nikita’s AirPods", "Friend, Pro", "Other Phones")
        self.mpv.close()
        self.start("--script-opts-append=dual_audio-auto=yes",
                   "--script-opts-append=dual_audio-helper=" + helper,
                   env=dict(os.environ, BUBBATV_DEVICES=outputs))
        self.wait_menu("Pair 1\nDevice: Nikita’s AirPods\n  [x] Nikita’s AirPods\n  Friend, Pro\n"
                       "  Other Phones\nAudio track:")
        self.assertIn("Pair 2\nDevice: Friend, Pro\n  Nikita’s AirPods\n  [x] Friend, Pro\n"
                      "  Other Phones\nAudio track:", self.menu())
        self.mpv.command("set_property", "pause", False)
        self.outputs("Nikita’s AirPods", "Other Phones")
        self.wait_menu("Pair 2\nDevice: Friend, Pro — disconnected\n  Nikita’s AirPods\n  Other Phones\n")
        self.mpv.wait_for(lambda: self.mpv.get("pause"))
        self.outputs("Nikita’s AirPods", "Friend, Pro", "Other Phones")
        self.mpv.wait_for(lambda: calls() == ["create|Nikita’s AirPods|Friend, Pro"])
        self.wait_menu("Pair 2\nDevice: Friend, Pro\n  Nikita’s AirPods\n  [x] Friend, Pro\n")
        self.assertTrue(self.mpv.get("pause"))

    def test_device_clicks(self):
        # Choosing headphones in the real menu: the helper rebuilds the
        # combined device, and mpv reopens its audio output each time.
        helper, calls = fake_helper(self.tmp.name)
        outputs = self.outputs("Pair A", "Pair B", "Other Phones")
        log = os.path.join(self.tmp.name, "clicks.log")
        devices = os.path.join(self.tmp.name, "clicks.json")
        out = subprocess.run([self.harness(), TONES, SCRIPT, devices, helper, log], capture_output=True,
                             text=True, timeout=60, env=dict(os.environ, BUBBATV_DEVICES=outputs))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(calls(), ["create|Pair A|Other Phones", "create|Other Phones|Pair A"])
        self.assertEqual(ao_inits(log), 3)


def dualplay_copy(root):
    """dualplay copied to root, where the fake helper is the compiled
    tools/audio-pairs: the real one is out of its reach. Returns the helper's
    calls and the environment to run it with."""
    shutil.copy(DUALPLAY, root)
    os.makedirs(os.path.join(root, "scripts"))
    os.symlink(SCRIPT, os.path.join(root, "scripts", "dual_audio.lua"))
    os.makedirs(os.path.join(root, "tools", ".build"))
    _, calls = fake_helper(os.path.join(root, "tools", ".build"))
    home = os.path.join(root, "mpv")  # MPV_HOME: keeps ~/.config/mpv out of it
    os.makedirs(os.path.join(home, "scripts"))
    # A menu "installed" there: dualplay neither builds nor loads it, and
    # --no-config keeps mpv from loading it.
    open(os.path.join(home, "scripts", "bubbatv.so"), "w").close()
    return calls, dict(os.environ, MPV_HOME=home)


class Dualplay(unittest.TestCase):
    """dualplay -2, copied to a directory of its own (dualplay_copy)."""

    def test_rebuilt_device_removed_on_exit(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        calls, env = dualplay_copy(root)
        mpv = Mpv(root, "--pause", src=None,
                  cmd=[os.path.join(root, "dualplay"), "-2", TONES, "--no-config", "--vo=null",
                       "--really-quiet"],
                  env=env)
        self.addCleanup(mpv.sock.close)
        self.addCleanup(mpv.quit)
        mpv.wait_for(lambda: [s.get("device") for s in (mpv.get("user-data/dual_audio") or {})
                              .get("sides", [])] == ["Pair A", "Pair B"])
        self.assertIn(mpv.get("hwdec"), ("auto", ["auto"]))
        self.assertEqual(mpv.get("options/audio-buffer"), 0.5)
        # dualplay gives the script the helper, to move a pair to other headphones.
        mpv.command("script-message-to", "dual_audio", "set-device", "2", "Other Phones")
        mpv.wait_for(lambda: calls() == ["create", "create|Pair A|Other Phones"])
        mpv.quit()
        self.assertEqual(calls(), ["create", "create|Pair A|Other Phones", "destroy"])
        self.assertEqual(mpv.proc.returncode, 0)

    def test_performance_defaults_can_be_overridden(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _, env = dualplay_copy(tmp.name)
        mpv = Mpv(tmp.name, "--pause", src=None,
                  cmd=[os.path.join(tmp.name, "dualplay"), "-2", TONES, "--no-config", "--vo=null",
                       "--hwdec=no", "--audio-buffer=0.8", "--really-quiet"], env=env)
        self.addCleanup(mpv.sock.close)
        self.addCleanup(mpv.quit)
        mpv.wait_for(lambda: mpv.get("time-pos") is not None)
        self.assertIn(mpv.get("hwdec"), ("no", ["no"]))
        self.assertEqual(mpv.get("options/audio-buffer"), 0.8)


class Folders(unittest.TestCase):
    """dualplay with folders of episodes (a copy, see dualplay_copy): each
    becomes its videos in episode order, and mpv gets the whole playlist
    up front."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.root)
        self.calls, self.env = dualplay_copy(self.root)
        self.mpv = None

    def tearDown(self):
        if self.mpv:
            self.mpv.quit()
            self.mpv.sock.close()
        self.tmp.cleanup()

    def folder(self, name, *files):
        """A folder with these files ("Extras/x.mkv" in a subfolder): videos
        are short episodes, the others (and hidden ones) empty."""
        path = os.path.join(self.tmp.name, name)
        os.makedirs(path)
        for f in files:
            if f.lower().endswith(VIDEOS) and not os.path.basename(f).startswith("."):
                episode(os.path.join(path, f))
            else:
                open(os.path.join(path, f), "w").close()
        return path

    def play(self, *args):
        """dualplay ARGS... --pause; the playlist mpv got."""
        self.mpv = Mpv(self.tmp.name, "--pause", src=None, env=self.env,
                       cmd=[os.path.join(self.root, "dualplay"), *args, "--no-config", "--vo=null", "--really-quiet"])
        return [e["filename"] for e in self.mpv.get("playlist")]

    def test_folder(self):
        # Any case of .mkv; not hidden files (macOS's "._" ones), others, or
        # subfolders: in a season folder those hold extras and samples.
        season = self.folder("Season 1", "Episode 10.mkv", "Episode 2.mkv", "episode 1.MKV", "._Episode 3.mkv",
                             "Episode 2.srt", "notes.txt", "Extras/Making of.mkv", "Sample/sample.mkv")
        self.assertEqual(self.play("-l", "ger", season + "/"),  # with the slash tab completion adds
                         [os.path.join(season, n) for n in ("episode 1.MKV", "Episode 2.mkv", "Episode 10.mkv")])
        self.assertTrue(self.mpv.get("pause"))  # mpv's options still go after it, dualplay's before
        self.mpv.wait_for(lambda: (self.mpv.get("user-data/dual_audio") or {}).get("active"))
        self.assertEqual(self.mpv.get("user-data/dual_audio")["sides"][0]["track"], 3)

    def test_other_video_types(self):
        # .mp4 and the rest too, mixed in one episode order; subtitles and
        # other files still stay out.
        season = self.folder("Season 2", "E05.mp4", "E05.srt", "E01.MP4", "E03.mov", "E02.m4v", "E04.avi",
                             "E06.webm", "E07.mkv", "cover.jpg", "._E08.mp4")
        self.assertEqual(self.play(season),
                         [os.path.join(season, n) for n in ("E01.MP4", "E02.m4v", "E03.mov", "E04.avi", "E05.mp4",
                                                            "E06.webm", "E07.mkv")])

    def test_folders_and_files(self):
        # Each folder's episodes take its place; the arguments keep their order.
        s1, s2 = self.folder("S1", "E2.mkv", "E1.mkv"), self.folder("S2", "E10.mkv", "E9.mkv")
        movie = episode(os.path.join(self.tmp.name, "movie.mkv"))
        self.assertEqual(self.play(s2, movie, s1), [os.path.join(s2, "E9.mkv"), os.path.join(s2, "E10.mkv"), movie,
                                                    os.path.join(s1, "E1.mkv"), os.path.join(s1, "E2.mkv")])

    def test_no_videos(self):
        # A clear error before anything starts: with -2 not even the device.
        show = self.folder("Show", "notes.txt", "Episode 1.srt", "._Episode 1.mp4", "Season 1/Episode 1.mkv")
        out = subprocess.run([os.path.join(self.root, "dualplay"), "-2", show, "--no-config"],
                             capture_output=True, text=True, timeout=60, env=self.env)
        self.assertEqual((out.returncode, out.stderr),
                         (1, "dualplay: no videos in %s (its subfolders are not searched)\n" % show))
        self.assertEqual(self.calls(), [])


class App(unittest.TestCase):
    """bubbaTV.app, built by tools/make-app.sh. --dry-run prints the dualplay
    command it would run. The dry runs use a copy of the app pointed at a fake
    dualplay, whose -L prints the outputs each test needs."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("swiftc"):
            raise unittest.SkipTest("needs swiftc")
        cls.build = tempfile.TemporaryDirectory()
        cls.app = os.path.join(cls.build.name, "bubbaTV.app")
        # Built from a shell without Homebrew in its PATH: the app still finds mpv.
        cls.path = "/usr/bin:/bin:" + os.path.join(cls.build.name, "bin")
        # Stands in for Launch Services' lsregister: it only notes its arguments.
        cls.registered = os.path.join(cls.build.name, "registered")
        cls.lsregister = os.path.join(cls.build.name, "lsregister")
        with open(cls.lsregister, "w") as f:
            f.write('#!/bin/sh\necho "$@" >> "%s"\n' % cls.registered)
        os.chmod(cls.lsregister, 0o755)
        cls.make_app(cls.app)

    @classmethod
    def make_app(cls, app, **env):
        subprocess.run([os.path.join(ROOT, "tools", "make-app.sh"), app],
                       env=dict(os.environ, PATH=cls.path, LSREGISTER=cls.lsregister, **env),
                       check=True, capture_output=True, timeout=300)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = os.path.join(self.tmp.name, "dualplay")
        with open(self.fake, "w") as f:  # -L prints dualplay.outputs and keeps its PATH in dualplay.path
            f.write('#!/bin/sh\n[ "$1" = -L ] || exit 1\necho "$PATH" > "$0.path"\ncat "$0.outputs"\n')
        os.chmod(self.fake, 0o755)
        self.outputs("  MacBook Pro Speakers (2 ch)")

    def tearDown(self):
        self.tmp.cleanup()

    def outputs(self, *lines):
        with open(self.fake + ".outputs", "w") as f:
            f.write("".join(line + "\n" for line in lines))

    def info(self):
        with open(os.path.join(self.app, "Contents", "Info.plist"), "rb") as f:
            return plistlib.load(f)

    def dry_run(self, *files, **keys):
        """The app's dry run, with BubbaTV* Info.plist keys set (dualplay: the fake)."""
        app = os.path.join(self.tmp.name, "bubbaTV.app")
        if not os.path.exists(app):
            shutil.copytree(self.app, app, symlinks=True)
        for key, value in dict({"Dualplay": self.fake}, **keys).items():
            subprocess.run(["plutil", "-replace", "BubbaTV" + key, "-string", value,
                            os.path.join(app, "Contents", "Info.plist")], check=True)
        return subprocess.run([os.path.join(app, "Contents", "MacOS", "bubbaTV"), "--dry-run", *files],
                              capture_output=True, text=True, timeout=60)

    def test_open_with_right_away(self):
        # Built into an Applications folder, the app is registered with Launch
        # Services at once, so Finder offers it under Open With before it was
        # ever opened. The build in a temp dir above was not registered.
        self.assertFalse(os.path.exists(self.registered))
        home = os.path.join(self.tmp.name, "home")
        app = os.path.join(home, "Applications", "bubbaTV.app")
        self.make_app(app, HOME=home)
        with open(self.registered) as f:
            self.assertEqual(f.read(), "-f %s\n" % app)

    def test_bundle(self):
        subprocess.run(["plutil", "-lint", os.path.join(self.app, "Contents", "Info.plist")],
                       check=True, capture_output=True)
        # Signed as a whole, so nothing may be written into it: dualplay builds in tools/.build.
        subprocess.run(["codesign", "--verify", "--strict", self.app], check=True, capture_output=True)
        info = self.info()
        self.assertTrue(os.access(os.path.join(self.app, "Contents", "MacOS", info["CFBundleExecutable"]), os.X_OK))
        self.assertEqual(info["BubbaTVDualplay"], os.path.join(ROOT, "dualplay"))
        self.assertEqual(info["BubbaTVPath"], self.path + ":/opt/homebrew/bin:/usr/local/bin")
        self.assertEqual(info["CFBundleIconFile"], "bubbaTV.icns")
        self.assertGreater(os.path.getsize(os.path.join(self.app, "Contents", "Resources", "bubbaTV.icns")), 1000)
        self.assertFalse(info["BubbaTVBundledPlayer"])
        version = subprocess.check_output([os.path.join(self.app, "Contents", "MacOS", "bubbaTV"), "--version"], text=True)
        self.assertEqual(version.strip(), "bubbaTV 0.1.0")
        # Offered for movies and folders of episodes, never their default app.
        types = info["CFBundleDocumentTypes"]
        self.assertEqual({t["LSHandlerRank"] for t in types}, {"Alternate"})
        self.assertLessEqual({"public.movie", "org.matroska.mkv", "org.webmproject.webm", "public.folder"},
                             {u for t in types for u in t["LSItemContentTypes"]})
        self.assertEqual({t["UTTypeIdentifier"] for t in info["UTImportedTypeDeclarations"]},
                         {"org.matroska.mkv", "org.webmproject.webm"})

    def test_rebuild_replaces_only_bubbatv(self):
        # make-app.sh builds over an older bubbaTV, even one whose build failed,
        # but not over another app given by mistake.
        def make(app, path=os.environ["PATH"]):
            return subprocess.run([os.path.join(ROOT, "tools", "make-app.sh"), app], env=dict(os.environ, PATH=path),
                                  capture_output=True, text=True, timeout=300)
        old = os.path.join(self.tmp.name, "old", "bubbaTV.app")
        shutil.copytree(self.app, old, symlinks=True)
        out = make(old)
        self.assertEqual(out.returncode, 0, out.stderr)
        broken = os.path.join(self.tmp.name, "broken")  # a swiftc that fails
        os.makedirs(broken)
        with open(os.path.join(broken, "swiftc"), "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(os.path.join(broken, "swiftc"), 0o755)
        self.assertEqual(make(old, broken + ":/usr/bin:/bin").returncode, 1)
        out = make(old)
        self.assertEqual(out.returncode, 0, out.stderr)
        other = os.path.join(self.tmp.name, "Other.app", "Contents", "Info.plist")
        os.makedirs(os.path.dirname(other))
        with open(other, "wb") as f:
            plistlib.dump({"CFBundleIdentifier": "com.example.other"}, f)
        out = make(os.path.join(self.tmp.name, "Other.app"))
        self.assertEqual(out.returncode, 1)
        self.assertIn("is not bubbaTV", out.stderr)
        self.assertTrue(os.path.exists(other))

    def test_standalone_bundle_survives_relocation_without_build_sources(self):
        app = os.path.join(self.tmp.name, "Standalone.app")
        runtime_path = os.path.dirname(shutil.which("mpv")) + ":/usr/bin:/bin:/usr/sbin:/sbin"
        subprocess.run([os.path.join(ROOT, "tools", "make-app.sh"), "--standalone", app], check=True,
                       capture_output=True, timeout=300,
                       env=dict(os.environ, BUBBATV_DISTRIBUTION="homebrew", BUBBATV_RUNTIME_PATH=runtime_path,
                                MPV_HOME=os.path.join(self.tmp.name, "profile")))
        moved = os.path.join(self.tmp.name, "Moved player.app")
        shutil.move(app, moved)
        with open(os.path.join(moved, "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f)
        self.assertTrue(info["BubbaTVBundledPlayer"])
        self.assertEqual(info["BubbaTVDualplay"], "")
        self.assertEqual(info["BubbaTVDistribution"], "homebrew")
        self.assertEqual(info["BubbaTVPath"], runtime_path)
        player = os.path.join(moved, "Contents", "Resources", "Player")
        self.assertFalse(os.path.exists(os.path.join(player, "tools", "audio-pairs.swift")))
        self.assertFalse(os.path.exists(os.path.join(player, "tools", "bubbatv-menu.swift")))
        for binary in ("audio-pairs", "bubbatv.so"):
            self.assertTrue(os.path.exists(os.path.join(player, "tools", ".build", binary)))
        output = subprocess.check_output([os.path.join(moved, "Contents", "MacOS", "bubbaTV"),
                                          "--dry-run", TONES], text=True)
        self.assertEqual(shlex.split(output)[0], os.path.join(player, "dualplay"))
        subprocess.run([os.path.join(player, "dualplay"), "-B"], check=True, timeout=60,
                       env=dict(os.environ, PATH="/usr/bin:/bin"))
        subprocess.run([os.path.join(player, "dualplay"), TONES, "--no-config", "--vo=null", "--ao=null",
                        "--end=0.2", "--really-quiet"], check=True, timeout=30,
                       env=dict(os.environ, PATH=runtime_path, MPV_HOME=os.path.join(self.tmp.name, "profile")))
        subprocess.run(["codesign", "--verify", "--strict", moved], check=True, capture_output=True)

    def test_two_pairs_with_two_bluetooth_outputs(self):
        for bluetooth, pairs in ((0, ""), (1, ""), (2, "-2 "), (3, "")):
            self.outputs("  MacBook Pro Speakers (2 ch)",
                         *("  AirPods %d (2 ch, Bluetooth)" % i for i in range(bluetooth)))
            out = self.dry_run(TONES)
            self.assertEqual((out.returncode, out.stdout),
                             (0, "%s %s%s --quiet --msg-time\n" % (self.fake, pairs, TONES)))

    def test_dualplay_gets_the_baked_path(self):
        path = os.path.dirname(shutil.which("mpv")) + ":/usr/bin:/bin"
        self.assertEqual(self.dry_run(TONES, Path=path).returncode, 0)
        with open(self.fake + ".path") as f:
            self.assertEqual(f.read(), path + "\n")

    def test_files_in_episode_order(self):
        # Several files make a playlist; the dry run quotes them for the shell.
        d = self.tmp.name
        out = self.dry_run(*(os.path.join(d, n) for n in ("Episode 10.mkv", "Episode 2.mkv", "it's 1.mkv")))
        self.assertEqual(out.stdout, "%s '%s/Episode 2.mkv' '%s/Episode 10.mkv' '%s/it'\\''s 1.mkv' --quiet --msg-time\n"
                         % (self.fake, d, d, d))

    def test_nothing_to_play(self):
        out = self.dry_run()
        self.assertEqual(out.returncode, 1)
        self.assertIn("Nothing to play", out.stderr)

    def test_mpv_missing(self):
        out = self.dry_run(TONES, Path="/usr/bin:/bin")
        self.assertEqual(out.returncode, 1)
        self.assertIn("mpv is not installed", out.stderr)
        self.assertIn("brew install mpv", out.stderr)

    def test_dualplay_missing(self):
        out = self.dry_run(TONES, Dualplay=os.path.join(self.tmp.name, "moved", "dualplay"))
        self.assertEqual(out.returncode, 1)
        self.assertIn("tools/make-app.sh", out.stderr)

    def test_real_dualplay_lists_outputs(self):
        # dualplay -L builds the helper (here in a copy of the checkout) and
        # lists the outputs without mpv or a file; the app runs it the same way.
        repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(os.path.join(repo, "tools"))
        shutil.copy(os.path.join(ROOT, "dualplay"), repo)
        shutil.copy(os.path.join(ROOT, "tools", "audio-pairs.swift"), os.path.join(repo, "tools"))
        out = subprocess.run([os.path.join(repo, "dualplay"), "-L"], capture_output=True, text=True,
                             timeout=300, env=dict(os.environ, PATH="/usr/bin:/bin"))
        self.assertEqual(out.returncode, 0, out.stderr)
        for line in out.stdout.splitlines():
            self.assertRegex(line, r"^  .+ \(\d+ ch(, Bluetooth)?\)$")
        out = self.dry_run(TONES, Dualplay=os.path.join(repo, "dualplay"))
        self.assertRegex(out.stdout, "^%s (-2 )?%s --quiet --msg-time\n$"
                         % (re.escape(os.path.join(repo, "dualplay")), re.escape(TONES)))


class AppHome(unittest.TestCase):
    """Native home-window lifecycle, persistent recents, and playlist IPC."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("swiftc"):
            raise unittest.SkipTest("needs swiftc")
        cls.build = tempfile.TemporaryDirectory()
        cls.app = os.path.join(cls.build.name, "HomeTests.app")
        macos = os.path.join(cls.app, "Contents", "MacOS")
        os.makedirs(macos)
        cls.binary = os.path.join(macos, "bubbaTV")
        subprocess.run(["swiftc", "-O", "-D", "BUBBATV_TESTING", "-o", cls.binary,
                        *(os.path.join(ROOT, "tools", name) for name in
                          ("bubbatv-app.swift", "bubbatv-home.swift", "bubbatv-library.swift")),
                        os.path.join(ROOT, "tests", "app_home", "main.swift")], check=True, timeout=300)
        cls.fake = os.path.join(cls.build.name, "dualplay")
        with open(cls.fake, "w") as f:
            f.write('#!/bin/sh\nif [ "$1" = -L ]; then echo "  Speakers (2 ch)"; exit 0; fi\n'
                    'mkdir -p "$MPV_HOME"\nprintf "%s\\n" "$@" > "$MPV_HOME/launch-args"\nsleep 1\n')
        os.chmod(cls.fake, 0o755)
        with open(os.path.join(ROOT, "tools", "bubbatv-app.plist"), "rb") as f:
            info = plistlib.load(f)
        info.update(BubbaTVDualplay=cls.fake, BubbaTVPath=os.environ["PATH"])
        with open(os.path.join(cls.app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump(info, f)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def check(self, mode):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([self.binary, mode, tmp], capture_output=True, text=True, timeout=30,
                                    env=dict(os.environ, MPV_HOME=os.path.join(tmp, "profile")))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("ok ", result.stdout)

    def test_history_persists_and_only_shows_available_media(self):
        self.check("history")

    def test_homebrew_update_uses_package_manager_instead_of_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = os.path.join(tmp, "HomeTests.app")
            shutil.copytree(self.app, app)
            plist = os.path.join(app, "Contents", "Info.plist")
            with open(plist, "rb") as f:
                info = plistlib.load(f)
            info["BubbaTVDistribution"] = "homebrew"
            with open(plist, "wb") as f:
                plistlib.dump(info, f)
            result = subprocess.run([os.path.join(app, "Contents", "MacOS", "bubbaTV"), "homebrew-update", tmp],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_continue_keeps_the_current_file_when_earlier_files_disappear(self):
        self.check("resume")

    def test_home_hides_for_playback_and_returns_when_queue_ends(self):
        self.check("window")

    def test_continue_launches_the_saved_position_and_surviving_episode(self):
        self.check("window-resume")

    def test_more_media_is_appended_to_the_same_player_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            mpv = Mpv(tmp, "--pause", "--idle=yes")
            try:
                mpv.wait_for(lambda: mpv.get("time-pos") is not None)
                result = subprocess.run([self.binary, "queue", tmp, mpv.path], capture_output=True, text=True, timeout=30,
                                        env=dict(os.environ, MPV_HOME=os.path.join(tmp, "profile")))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual([os.path.basename(e["filename"]) for e in mpv.get("playlist")],
                                 ["tones.mkv", "Episode 2.MP4", "Episode 10.mkv"])
                self.assertEqual(mpv.get("playlist-pos"), 0)
                self.assertTrue(mpv.get("pause"))
            finally:
                mpv.close()


if __name__ == "__main__":
    if not os.path.exists(TONES):
        subprocess.run([os.path.join(ROOT, "tools", "make-samples.sh")], check=True)
    unittest.main(verbosity=2)
