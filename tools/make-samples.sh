#!/usr/bin/env bash
# Generates test MKVs with multiple language-tagged audio tracks.
#   samples/tones.mkv  - each track is a distinct sine tone (used by tests/test_dual_audio.py)
#   samples/speech.mkv - English + Russian TTS speech (for listening with AirPods)
#   samples/sync.mkv   - flash + beep once a second (to judge lip sync)
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p samples
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

DUR=20

# Track 1: 440 Hz stereo AAC (eng). Track 2: 1000 Hz 5.1 AC3 (rus) - movies often
# ship 5.1, so this exercises the downmix. Track 3: 2500 Hz stereo AAC (ger).
# A keyframe every 10 s (-g 250): the tests tell an exact seek from one that
# lands on a keyframe.
ffmpeg -loglevel error -y \
  -f lavfi -i "testsrc2=size=640x360:rate=25:duration=$DUR" \
  -f lavfi -i "sine=frequency=440:duration=$DUR" \
  -f lavfi -i "sine=frequency=1000:duration=$DUR" \
  -f lavfi -i "sine=frequency=2500:duration=$DUR" \
  -filter_complex "[1]aformat=channel_layouts=stereo[a1];[2]aformat=channel_layouts=5.1[a2];[3]aformat=channel_layouts=stereo[a3]" \
  -map 0:v -map "[a1]" -map "[a2]" -map "[a3]" \
  -c:v libx264 -preset ultrafast -pix_fmt yuv420p -g 250 \
  -c:a:0 aac -c:a:1 ac3 -c:a:2 aac \
  -metadata:s:a:0 language=eng -metadata:s:a:0 title="English 440Hz" \
  -metadata:s:a:1 language=rus -metadata:s:a:1 title="Russian 1000Hz 5.1" \
  -metadata:s:a:2 language=ger -metadata:s:a:2 title="German 2500Hz" \
  samples/tones.mkv

en="This is the English audio track. You should hear it in your left ear. "
ru="Это русская звуковая дорожка. Вы должны слышать её в правом ухе. "
say -v Samantha -o "$tmp/en.aiff" "$en$en$en"
say -v Milena -o "$tmp/ru.aiff" "$ru$ru$ru"

ffmpeg -loglevel error -y \
  -f lavfi -i "testsrc2=size=640x360:rate=25:duration=$DUR" \
  -i "$tmp/en.aiff" -i "$tmp/ru.aiff" \
  -map 0:v -map 1:a -map 2:a -t $DUR \
  -c:v libx264 -preset ultrafast -pix_fmt yuv420p -c:a aac -ac 2 -ar 48000 \
  -metadata:s:a:0 language=eng -metadata:s:a:0 title="English" \
  -metadata:s:a:1 language=rus -metadata:s:a:1 title="Русский" \
  samples/speech.mkv

# Lip-sync check: the screen flashes white and both tracks beep at the same
# moment, once a second (440 Hz on eng, 1000 Hz on rus).
beep() { echo "aevalsrc='if(lt(mod(t,1),0.1),0.5*sin(2*PI*$1*t),0)':s=48000:c=stereo:d=$DUR"; }
ffmpeg -loglevel error -y \
  -f lavfi -i "color=black:size=640x360:rate=25:duration=$DUR,drawbox=color=white:t=fill:enable='lt(mod(t,1),0.1)'" \
  -f lavfi -i "$(beep 440)" -f lavfi -i "$(beep 1000)" \
  -map 0:v -map 1:a -map 2:a \
  -c:v libx264 -preset ultrafast -pix_fmt yuv420p -c:a aac \
  -metadata:s:a:0 language=eng -metadata:s:a:0 title="Beep 440Hz" \
  -metadata:s:a:1 language=rus -metadata:s:a:1 title="Beep 1000Hz" \
  samples/sync.mkv

ls -lh samples/*.mkv
