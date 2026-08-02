#!/bin/zsh
# Put Vivieen Pocket Mirror on a real iPhone, one command:
#
#   ./scripts/iphone-install.sh            # plugged-in (or wifi-paired) iPhone
#   ./scripts/iphone-install.sh --ipa-only # just produce build/Vivieen.ipa
#
# ONE-TIME SETUP (cannot be automated - it needs your Apple ID password):
#   Xcode -> Settings -> Accounts -> "+" -> sign in with the Apple ID for
#   team X7R8N6MMSU (THE GREAT LIONHEART PTE. LTD.). That lets Xcode mint
#   the free development certificate and provisioning profile on demand.
#
# On the phone after the first install: Settings -> General -> VPN &
# Device Management -> trust the developer certificate. A development
# install runs for 1 year on a paid team (7 days on a free Apple ID).
set -euo pipefail
cd "$(dirname "$0")/.."

TEAM="${VIVIEEN_TEAM:-X7R8N6MMSU}"
ARCHIVE=build/vivieen.xcarchive
EXPORT=build/ipa

command -v xcodegen >/dev/null || { echo "xcodegen missing (brew install xcodegen)"; exit 1; }
(cd ios && xcodegen generate >/dev/null)

echo "▸ archiving for device (automatic signing, team $TEAM)…"
xcodebuild -project ios/Vivieen.xcodeproj -scheme Vivieen \
  -destination "generic/platform=iOS" -configuration Release \
  -archivePath "$ARCHIVE" archive -allowProvisioningUpdates \
  DEVELOPMENT_TEAM="$TEAM" CODE_SIGN_STYLE=Automatic \
  CODE_SIGNING_REQUIRED=YES CODE_SIGNING_ALLOWED=YES \
  CODE_SIGN_IDENTITY="Apple Development" -quiet

echo "▸ exporting the .ipa…"
xcodebuild -exportArchive -archivePath "$ARCHIVE" \
  -exportPath "$EXPORT" -exportOptionsPlist ios/ExportOptions.plist \
  -allowProvisioningUpdates -quiet
mkdir -p build && cp -f "$EXPORT"/*.ipa build/Vivieen.ipa
echo "✓ build/Vivieen.ipa"

[[ "${1:-}" == "--ipa-only" ]] && exit 0

echo "▸ looking for an iPhone (plug in via USB, or wifi-paired)…"
DEVICE=$(xcrun devicectl list devices 2>/dev/null \
  | awk '/iPhone/ && /(connected|available)/ {print $NF; exit}')
if [[ -z "$DEVICE" ]]; then
  echo "No iPhone found. Plug it in (trust this Mac on the phone), then:"
  echo "  xcrun devicectl device install app --device <UDID> build/Vivieen.ipa"
  exit 1
fi
echo "▸ installing on $DEVICE…"
xcrun devicectl device install app --device "$DEVICE" build/Vivieen.ipa
echo "✓ installed - open Vivieen on the phone, then pair it with this Mac"
echo "  (menu bar -> Vivieen -> Pair iPhone…)."
