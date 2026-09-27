#!/usr/bin/env python3
"""Report which test tone dominates each channel of an audio file.

Usage: analyze.py FILE
Prints one line per channel: "ch<N> <freq>" where freq is the loudest of the
sample tones (440/1000/2500 Hz), or "silent". Uses ffmpeg for decoding and a
Goertzel filter for tone energy, so it needs nothing beyond the stdlib.
For samples/sync.mkv (a beep a second) onsets() and peaks() give when each
channel's beeps start (first_onsets(): the first one) and how loud they are.
"""
import array
import math
import subprocess
import sys

TONES = (440, 1000, 2500)
RATE = 48000
SKIP = RATE // 2       # skip the first 0.5s (decoder/filter warm-up)
WINDOW = RATE          # analyse 1s


def channel_count(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=channels", "-of", "csv=p=0", path])
    return int(out.strip())


def goertzel(samples, freq):
    k = 2 * math.cos(2 * math.pi * freq / RATE)
    s1 = s2 = 0.0
    for x in samples:
        s1, s2 = x + k * s1 - s2, s1
    return math.sqrt(max(s1 * s1 + s2 * s2 - k * s1 * s2, 0)) / len(samples)


def decode(path):
    """Channel count and interleaved float samples at RATE."""
    raw = subprocess.check_output(
        ["ffmpeg", "-v", "error", "-i", path, "-f", "f32le",
         "-ar", str(RATE), "-"])
    return channel_count(path), array.array("f", raw)


def onsets(path, level=0.05, gap=0.05):
    """Per channel, the times (s) where a beep starts: the signal gets past
    `level` after at least `gap` s below it."""
    n, pcm = decode(path)
    result = [[] for _ in range(n)]
    last = [-RATE] * n  # sample of each channel's last loud moment
    for i in range(len(pcm)):
        if abs(pcm[i]) > level:
            ch, t = i % n, i // n
            if t - last[ch] > gap * RATE:
                result[ch].append(t / RATE)
            last[ch] = t
    return result


def first_onsets(path):
    """Seconds into the file where each channel first gets loud (None if never),
    e.g. the first beep of samples/sync.mkv."""
    return [times[0] if times else None for times in onsets(path)]


def peaks(path, start=0.5):
    """Per channel, the loudest sample from `start` s on."""
    n, pcm = decode(path)
    return [max(map(abs, pcm[int(start * RATE) * n + ch::n])) for ch in range(n)]


def tones(path):
    """Loudest sample tone per channel (None where the channel is silent)."""
    n, pcm = decode(path)
    result = []
    for ch in range(n):
        start = SKIP * n + ch
        samples = pcm[start:start + WINDOW * n:n]
        energy = {f: goertzel(samples, f) for f in TONES}
        best = max(energy, key=energy.get)
        result.append(best if energy[best] > 1e-3 else None)
    return result


if __name__ == "__main__":
    for ch, tone in enumerate(tones(sys.argv[1]), 1):
        print(f"ch{ch} {tone or 'silent'}")
