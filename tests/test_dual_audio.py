#!/usr/bin/env python3
"""End-to-end tests: run mpv with the plugin and check what reaches each channel.

Run: python3 tests/test_dual_audio.py   (needs mpv + ffmpeg; builds samples if missing)
"""
import json
import os
import plistlib
import re
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


class Render(unittest.TestCase):
    """Render 3s of playback to a WAV and inspect the channels."""

    def render(self, *opts, src=TONES):
        out = os.path.join(self.tmp.name, "out.wav")
        subprocess.run(BASE + ["--no-video", "--ao=pcm", "--ao-pcm-file=" + out,
                               "--untimed", "--end=3", *opts, src], check=True, timeout=60)
        return analyze.tones(out)

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

    def test_pairs_mode_stereo_per_pair(self):
        # Channels 1-2 -> first headphone pair, 3-4 -> second.
        self.assertEqual(self.render("--script-opts=dual_audio-auto=yes,dual_audio-mode=pairs",
                                     "--audio-channels=quad"), [440, 440, 1000, 1000])

    def test_pairs_mode_names_and_stereo_warning(self):
        out = subprocess.run(
            BASE[:-1] + ["--no-video", "--ao=pcm", "--ao-pcm-file=" + os.path.join(self.tmp.name, "o.wav"),
                         "--untimed", "--end=1", "--audio-channels=stereo",
                         "--msg-level=all=no,dual_audio=info",
                         "--script-opts-append=dual_audio-auto=yes",
                         "--script-opts-append=dual_audio-mode=pairs",
                         "--script-opts-append=dual_audio-pair_names=Nikita’s AirPods|Friend, Pro", TONES],
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


class Mpv:
    """A real-time mpv instance driven over its JSON IPC socket. `cmd` can put
    dualplay in front of it (with the file in `cmd` and src=None)."""

    def __init__(self, tmpdir, *opts, src=TONES, cmd=BASE, env=None):
        self.path = os.path.join(tmpdir, "mpv.sock")
        if os.path.exists(self.path):  # left by an mpv before
            os.unlink(self.path)
        self.proc = subprocess.Popen(cmd + ["--ao=null", "--input-ipc-server=" + self.path,
                                            *opts, *([src] if src else [])],
                                     env=env, stdout=subprocess.DEVNULL)
        deadline = time.time() + 10
        while not os.path.exists(self.path):
            if time.time() > deadline:
                raise RuntimeError("mpv did not open its IPC socket")
            time.sleep(0.05)
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(self.path)
        self.buf = b""
        self.next_id = 0

    def command(self, *args):
        self.next_id += 1
        self.sock.sendall(json.dumps({"command": list(args), "request_id": self.next_id}).encode() + b"\n")
        while True:
            while b"\n" not in self.buf:
                self.buf += self.sock.recv(65536)
            line, self.buf = self.buf.split(b"\n", 1)
            reply = json.loads(line)
            if reply.get("request_id") == self.next_id:
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

    def test_toggle_cycle_swap(self):
        self.press("a", (1, 2))     # on: current track left, next right
        self.press("}", (1, 3))     # right ear -> next track
        self.press("}", (1, 1))     # wraps around to the left ear's track: both hear it
        self.assertIn("[aid1]asplit=2", self.mpv.get("lavfi-complex"))
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
        self.mpv.wait_for(lambda: "volume=-2dB[l]" in self.mpv.get("lavfi-complex"))
        self.mpv.command("keypress", "h")
        self.mpv.command("keypress", "h")
        self.mpv.wait_for(lambda: "volume=-2dB[r]" in self.mpv.get("lavfi-complex"))
        self.assertIn("volume=0dB[l]", self.mpv.get("lavfi-complex"))

    def test_menu_messages(self):
        # What the bubbaTV menu sends, and the state it reads back.
        self.press("a", (1, 2))
        self.assertEqual([t["id"] for t in self.state()["tracks"]], [1, 2, 3])
        self.message("set-track", 2, 1)
        self.expect((1, 1))
        self.message("set-track", 1, 3)
        self.expect((3, 1))
        self.message("set-gain", 2, -6)
        self.mpv.wait_for(lambda: "volume=-6dB[r]" in self.mpv.get("lavfi-complex"))
        self.message("set-delay", 2, 150)
        self.mpv.wait_for(lambda: "adelay=150:all=1,volume=-6dB[r]" in self.mpv.get("lavfi-complex"))
        self.expect((3, 1))
        sides = self.state()["sides"]
        self.assertEqual([(s["track"], s["gain"], s["delay"]) for s in sides],
                         [(3, 0, 0), (1, -6, 150)])
        self.assertEqual([s["name"] for s in sides], ["Left ear", "Right ear"])

    def test_negative_delay_moves_audio_delay(self):
        # The graph can only hold a side back: the other side is delayed
        # instead, and mpv's own audio-delay (the user's 0.1 s here) moves
        # everything earlier - and comes back when dual audio goes off.
        self.mpv.command("set_property", "audio-delay", 0.1)
        self.press("a", (1, 2))
        self.message("set-delay", 1, -200)
        self.mpv.wait_for(lambda: "adelay=200:all=1,volume=0dB[r]" in self.mpv.get("lavfi-complex"))
        self.assertNotIn("adelay", self.mpv.get("lavfi-complex").split("[l]")[0])
        self.assertAlmostEqual(self.mpv.get("audio-delay"), -0.1)
        self.expect((1, 2))
        self.press("a", None)
        self.assertAlmostEqual(self.mpv.get("audio-delay"), 0.1)

    def test_av_sync_while_playing(self):
        self.press("a", (1, 2))
        self.mpv.command("set_property", "pause", False)
        time.sleep(3)
        self.assertLess(abs(self.mpv.get("avsync")), 0.1)


class Devices(unittest.TestCase):
    """dualplay -2's headphones: a pair moving to others (as the bubbaTV menu
    asks) and dropping out mid-movie (as the menu reports). A fake helper
    stands in for tools/audio-pairs; its calls are checked instead."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.helper, self.calls = fake_helper(self.tmp.name)
        self.log = os.path.join(self.tmp.name, "mpv.log")
        self.mpv = Mpv(self.tmp.name, "--start=3", "--pause", "--audio-channels=quad",
                       "--log-file=" + self.log,
                       "--script-opts-append=dual_audio-auto=yes",
                       "--script-opts-append=dual_audio-mode=pairs",
                       "--script-opts-append=dual_audio-pair_names=Nikita’s AirPods|Friend, Pro",
                       "--script-opts-append=dual_audio-helper=" + self.helper)
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

    def wait_lost(self, lost):
        self.mpv.wait_for(lambda: [s.get("lost", False) for s in self.sides()] == lost)

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

    def test_helper_fails(self):
        self.message("set-device", 2, "Broken Phones")
        self.wait_logged('could not switch the headphones | "Broken" matches 0 outputs')
        self.assertEqual(self.devices(), ["Nikita’s AirPods", "Friend, Pro"])
        self.assertEqual(ao_inits(self.log), 1)

    def test_dropout_and_back(self):
        self.mpv.command("set_property", "pause", False)
        self.message("outputs", "Nikita’s AirPods", "MacBook Pro Speakers")
        self.mpv.wait_for(lambda: self.mpv.get("pause"))
        self.wait_logged("Pair 2 (Friend, Pro) dropped out, so the movie is paused.")
        self.wait_lost([False, True])
        self.assertEqual(self.calls(), [])
        self.message("outputs", "Nikita’s AirPods", "Friend, Pro", "MacBook Pro Speakers")
        self.mpv.wait_for(lambda: self.calls() == ["create|Nikita’s AirPods|Friend, Pro"])
        self.wait_logged("Pair 2 (Friend, Pro) reconnected. | Press Space to resume.")
        self.mpv.wait_for(lambda: ao_inits(self.log) == 2)
        self.wait_lost([False, False])
        self.assertTrue(self.mpv.get("pause"))  # resuming is up to the people watching


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
        self.assertFalse(self.state()["follow_subs"])
        self.mpv.command("script-binding", "dual_audio/follow-subs")
        self.shows("Eng", "Rus")
        self.expect((1, 2))         # still at 3.0s
        self.press("a", None)
        self.shows("Eng", None)
        self.press("a", (1, 2))
        self.shows("Eng", "Rus")

    def test_keep_coming_after_graph_change(self):
        # A loudness change rebuilds the graph but keeps the tracks: both
        # subtitles must be selected anew all the same (see Subs).
        self.start(FOLLOW, "--cache=no", "--demuxer-readahead-secs=0.5", "--demuxer-max-bytes=1MiB",
                   "--demuxer-max-back-bytes=0")
        self.shows("Eng", "Rus")
        self.mpv.command("set_property", "pause", False)
        self.message("set-gain", 1, -3)
        self.mpv.wait_for(lambda: "volume=-3dB[l]" in self.mpv.get("lavfi-complex"))
        start = self.mpv.get("time-pos")
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") > start + 2.5, timeout=15)
        self.assertRegex(self.mpv.get("sub-text"), r"^Eng \d+$")
        self.assertRegex(self.mpv.get("secondary-sub-text"), r"^Rus \d+$")


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

    def test_follow_subs_item(self):
        self.mpv.command("keypress", "a")
        self.wait_menu("Swap pairs\nSubtitles follow pairs")
        self.mpv.command("script-binding", "dual_audio/follow-subs")
        self.wait_menu("Swap pairs\n[x] Subtitles follow pairs")

    def test_clicks(self):
        # Clicks the real menu items and checks the graph.
        out = subprocess.run([self.harness(), TONES, SCRIPT], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("ok dual audio off", out.stdout)

    def test_device_menu(self):
        # Under dualplay -2 (the helper set) each pair's Device row lists the
        # connected outputs and follows them as headphones come and go.
        helper, calls = fake_helper(self.tmp.name)
        outputs = self.outputs("Nikita’s AirPods", "Friend, Pro", "Other Phones")
        self.mpv.close()
        self.start("--script-opts-append=dual_audio-helper=" + helper,
                   env=dict(os.environ, BUBBATV_DEVICES=outputs))
        self.mpv.command("keypress", "a")
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
        out = subprocess.run([self.harness(), TONES, SCRIPT, helper, log], capture_output=True,
                             text=True, timeout=60, env=dict(os.environ, BUBBATV_DEVICES=outputs))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(calls(), ["create|Pair A|Other Phones", "create|Other Phones|Pair A"])
        self.assertEqual(ao_inits(log), 3)


class Dualplay(unittest.TestCase):
    """dualplay -2, copied to a directory of its own where the fake helper
    is the compiled tools/audio-pairs: the real one is out of its reach."""

    def test_rebuilt_device_removed_on_exit(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
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
        mpv = Mpv(root, "--pause", src=None,
                  cmd=[os.path.join(root, "dualplay"), "-2", TONES, "--no-config", "--vo=null",
                       "--really-quiet"],
                  env=dict(os.environ, MPV_HOME=home))
        self.addCleanup(mpv.sock.close)
        self.addCleanup(mpv.quit)
        mpv.wait_for(lambda: [s.get("device") for s in (mpv.get("user-data/dual_audio") or {})
                              .get("sides", [])] == ["Pair A", "Pair B"])
        # dualplay gives the script the helper, to move a pair to other headphones.
        mpv.command("script-message-to", "dual_audio", "set-device", "2", "Other Phones")
        mpv.wait_for(lambda: calls() == ["create", "create|Pair A|Other Phones"])
        mpv.quit()
        self.assertEqual(calls(), ["create", "create|Pair A|Other Phones", "destroy"])
        self.assertEqual(mpv.proc.returncode, 0)


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
        subprocess.run([os.path.join(ROOT, "tools", "make-app.sh"), cls.app], env=dict(os.environ, PATH=cls.path),
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

    def test_bundle(self):
        subprocess.run(["plutil", "-lint", os.path.join(self.app, "Contents", "Info.plist")],
                       check=True, capture_output=True)
        # Signed as a whole, so nothing may be written into it: dualplay builds in tools/.build.
        subprocess.run(["codesign", "--verify", "--strict", self.app], check=True, capture_output=True)
        info = self.info()
        self.assertTrue(os.access(os.path.join(self.app, "Contents", "MacOS", info["CFBundleExecutable"]), os.X_OK))
        self.assertEqual(info["BubbaTVDualplay"], os.path.join(ROOT, "dualplay"))
        self.assertEqual(info["BubbaTVPath"], self.path + ":/opt/homebrew/bin:/usr/local/bin")
        # Offered for movies, never their default app.
        types = info["CFBundleDocumentTypes"]
        self.assertEqual({t["LSHandlerRank"] for t in types}, {"Alternate"})
        self.assertLessEqual({"public.movie", "org.matroska.mkv", "org.webmproject.webm"},
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

    def test_two_pairs_with_two_bluetooth_outputs(self):
        for bluetooth, pairs in ((0, ""), (1, ""), (2, "-2 "), (3, "")):
            self.outputs("  MacBook Pro Speakers (2 ch)",
                         *("  AirPods %d (2 ch, Bluetooth)" % i for i in range(bluetooth)))
            out = self.dry_run(TONES)
            self.assertEqual((out.returncode, out.stdout), (0, "%s %s%s\n" % (self.fake, pairs, TONES)))

    def test_dualplay_gets_the_baked_path(self):
        path = os.path.dirname(shutil.which("mpv")) + ":/usr/bin:/bin"
        self.assertEqual(self.dry_run(TONES, Path=path).returncode, 0)
        with open(self.fake + ".path") as f:
            self.assertEqual(f.read(), path + "\n")

    def test_files_in_episode_order(self):
        # Several files make a playlist; the dry run quotes them for the shell.
        d = self.tmp.name
        out = self.dry_run(*(os.path.join(d, n) for n in ("Episode 10.mkv", "Episode 2.mkv", "it's 1.mkv")))
        self.assertEqual(out.stdout, "%s '%s/Episode 2.mkv' '%s/Episode 10.mkv' '%s/it'\\''s 1.mkv'\n"
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
        self.assertRegex(out.stdout, "^%s (-2 )?%s\n$" % (re.escape(os.path.join(repo, "dualplay")), re.escape(TONES)))


if __name__ == "__main__":
    if not os.path.exists(TONES):
        subprocess.run([os.path.join(ROOT, "tools", "make-samples.sh")], check=True)
    unittest.main(verbosity=2)
