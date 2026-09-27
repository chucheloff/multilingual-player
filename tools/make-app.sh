#!/usr/bin/env bash
# make-app.sh - build bubbaTV.app: drop a movie on it to play it with dualplay.
#   tools/make-app.sh [APP]   (default tools/.build/bubbaTV.app), e.g.
#   tools/make-app.sh ~/Applications/bubbaTV.app
# The app runs the dualplay of this checkout: its path is baked in, so the app
# follows changes here, and the helpers dualplay compiles stay in tools/.build
# instead of landing inside the signed bundle. Move the checkout and the app
# needs building again.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
app=${1:-$repo/tools/.build/bubbaTV.app}
case $app in
  *.app) ;;
  *) echo "make-app.sh: $app: the name has to end in .app" >&2; exit 1 ;;
esac
command -v swiftc >/dev/null || { echo "make-app.sh: needs swiftc - xcode-select --install" >&2; exit 1; }

rm -rf "$app"
mkdir -p "$app/Contents/MacOS"
swiftc -O -o "$app/Contents/MacOS/bubbaTV" "$repo/tools/bubbatv-app.swift"
plist="$app/Contents/Info.plist"
cp "$repo/tools/bubbatv-app.plist" "$plist"
plutil -replace BubbaTVDualplay -string "$repo/dualplay" "$plist"
# Apps started from the Dock get a bare PATH: keep this shell's, which finds
# mpv, and Homebrew's in case mpv comes later.
plutil -replace BubbaTVPath -string "$PATH:/opt/homebrew/bin:/usr/local/bin" "$plist"
codesign --force --sign - "$app"
echo "built $app"
