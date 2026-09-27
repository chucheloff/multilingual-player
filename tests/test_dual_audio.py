#!/usr/bin/env python3
"""End-to-end tests: run mpv with the plugin and check what reaches each channel.

Run: python3 tests/test_dual_audio.py   (needs mpv + ffmpeg; builds samples if missing)
"""
import json
import os
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

    def __init__(self, tmpdir, *opts):
        self.path = os.path.join(tmpdir, "mpv.sock")
        self.proc = subprocess.Popen(BASE + ["--ao=null", "--input-ipc-server=" + self.path,
                                             *opts, TONES])
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
            return g.startswith("[aid%d]" % expect[0]) and "[aid%d]" % expect[1] in g
        self.mpv.wait_for(routed)
        self.mpv.wait_for(lambda: abs((self.mpv.get("time-pos") or 0) - 3.0) < 0.05)

    def test_toggle_cycle_swap(self):
        self.press("a", (1, 2))     # on: current track left, next right
        self.press(")", (1, 3))     # right ear -> next track
        self.press(")", (1, 2))     # wraps around, skipping the left ear's track
        self.press("(", (3, 2))     # left ear -> next free track
        self.press("y", (2, 3))     # swap
        self.press("a", None)       # off
        self.assertEqual(self.mpv.get("aid"), 2)  # left-ear language keeps playing

    def test_choice_carries_to_next_file(self):
        self.press("a", (1, 2))
        self.press(")", (1, 3))
        self.mpv.command("playlist-next")
        self.mpv.wait_for(lambda: self.mpv.get("playlist-pos") == 1)
        self.expect((1, 3))

    def test_balance(self):
        self.press("a", (1, 2))
        self.mpv.command("keypress", "k")
        self.mpv.wait_for(lambda: "volume=-2dB[l]" in self.mpv.get("lavfi-complex"))
        self.mpv.command("keypress", "h")
        self.mpv.command("keypress", "h")
        self.mpv.wait_for(lambda: "volume=-2dB[r]" in self.mpv.get("lavfi-complex"))
        self.assertIn("volume=0dB[l]", self.mpv.get("lavfi-complex"))

    def test_av_sync_while_playing(self):
        self.press("a", (1, 2))
        self.mpv.command("set_property", "pause", False)
        time.sleep(3)
        self.assertLess(abs(self.mpv.get("avsync")), 0.1)


if __name__ == "__main__":
    if not os.path.exists(TONES):
        subprocess.run([os.path.join(ROOT, "tools", "make-samples.sh")], check=True)
    unittest.main(verbosity=2)
