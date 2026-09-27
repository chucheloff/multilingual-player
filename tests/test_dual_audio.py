#!/usr/bin/env python3
"""End-to-end tests: run mpv with the plugin and check what reaches each channel.

Run: python3 tests/test_dual_audio.py   (needs mpv + ffmpeg; builds samples if missing)
"""
import json
import os
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
TONES = os.path.join(ROOT, "samples", "tones.mkv")  # 1=eng 440Hz, 2=rus 1000Hz 5.1, 3=ger 2500Hz
BASE = ["mpv", "--no-config", "--script=" + SCRIPT, "--vo=null", "--really-quiet"]


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
    """A real-time mpv instance driven over its JSON IPC socket."""

    def __init__(self, tmpdir, *opts, src=TONES):
        self.path = os.path.join(tmpdir, "mpv.sock")
        self.proc = subprocess.Popen(BASE + ["--ao=null", "--input-ipc-server=" + self.path,
                                             *opts, src])
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

    def close(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)
        self.sock.close()


class Keys(unittest.TestCase):
    """Key bindings while playing. Paused at 3.0s so any position jump shows up.
    The playlist holds the sample twice, standing in for two episodes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mpv = Mpv(self.tmp.name, "--start=3", "--pause", TONES)
        self.mpv.wait_for(lambda: self.mpv.get("time-pos") is not None)

    def tearDown(self):
        self.mpv.close()
        self.tmp.cleanup()

    def press(self, key, expect):
        self.mpv.command("keypress", key)
        self.expect(expect)

    def expect(self, expect):
        """Wait until the graph routes aid L / aid R (None = off) at 3.0s."""
        def routed():
            g = self.mpv.get("lavfi-complex")
            if expect is None:
                return g == ""
            ids = [int(i) for i in re.findall(r"\[aid(\d+)\]", g)]
            return (ids * 2)[:2] == list(expect)  # a track both sides play appears once
        self.mpv.wait_for(routed)
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


class Subs(unittest.TestCase):
    """Subtitles keep coming after the audio graph is rebuilt. The cache is kept
    small so mpv has to go on reading them, as it does with a long movie."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        srt = os.path.join(self.tmp.name, "subs.srt")
        with open(srt, "w") as f:  # one line per second, no gaps
            f.write("\n".join("%d\n00:00:%02d,000 --> 00:00:%02d,000\nLine %d\n" % (i + 1, i, i + 1, i)
                              for i in range(20)))
        movie = os.path.join(self.tmp.name, "movie.mkv")
        subprocess.run(["ffmpeg", "-v", "error", "-i", TONES, "-i", srt, "-map", "0", "-map", "1",
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
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mpv = Mpv(self.tmp.name, "--script=" + self.plugin, "--start=3", "--pause",
                       "--script-opts-append=dual_audio-mode=pairs",
                       "--script-opts-append=dual_audio-pair_names=Nikita’s AirPods|Friend, Pro")

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

    def test_clicks(self):
        # Built against libmpv: clicks the real menu items and checks the graph.
        harness = os.path.join(self.build.name, "menu_clicks")
        self.swiftc("-o", harness, "-L", os.path.join(self.prefix, "lib"), "-lmpv", sources=[os.path.join(ROOT, "tests", "menu_clicks", "main.swift")])
        out = subprocess.run([harness, TONES, SCRIPT], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("ok dual audio off", out.stdout)


if __name__ == "__main__":
    if not os.path.exists(TONES):
        subprocess.run([os.path.join(ROOT, "tools", "make-samples.sh")], check=True)
    unittest.main(verbosity=2)
