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
# An older bubbaTV gets replaced; another app under that name (a typo) stays.
bundle_id() { plutil -extract CFBundleIdentifier raw -o - "$1" 2>/dev/null; }
if [ -e "$app" ] && [ "$(bundle_id "$app/Contents/Info.plist")" != "$(bundle_id "$repo/tools/bubbatv-app.plist")" ]; then
  echo "make-app.sh: $app is not bubbaTV, leaving it alone" >&2; exit 1
fi

rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
# Info.plist first, so a build that fails halfway is still ours to replace.
plist="$app/Contents/Info.plist"
cp "$repo/tools/bubbatv-app.plist" "$plist"
plutil -replace BubbaTVDualplay -string "$repo/dualplay" "$plist"
# Apps started from the Dock get a bare PATH: keep this shell's, which finds
# mpv, and Homebrew's in case mpv comes later.
plutil -replace BubbaTVPath -string "$PATH:/opt/homebrew/bin:/usr/local/bin" "$plist"
swiftc -O -parse-as-library -o "$app/Contents/MacOS/bubbaTV" \
  "$repo/tools/bubbatv-app.swift" "$repo/tools/bubbatv-home.swift" "$repo/tools/bubbatv-library.swift"
icon_tmp=$(mktemp -d)
trap 'rm -rf "$icon_tmp"' EXIT
swift "$repo/tools/make-icon.swift" "$icon_tmp/bubbaTV.iconset"
iconutil --convert icns --output "$app/Contents/Resources/bubbaTV.icns" "$icon_tmp/bubbaTV.iconset"
codesign --force --sign - "$app"
# In an Applications folder, tell Launch Services right away, so Finder offers
# bubbaTV under Open With before it has ever been opened. Builds elsewhere
# (tools/.build, the tests' temp dirs) stay unregistered, or Open With would
# list every stale copy.
lsregister=${LSREGISTER:-/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister}
case $(cd "$(dirname "$app")" && pwd) in
  /Applications | "$HOME/Applications") "$lsregister" -f "$app" ;;
esac
echo "built $app"
