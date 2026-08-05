#!/bin/zsh
# Vivieen Pocket Mirror -> TestFlight, one command, no Xcode UI.
#
#   ASC_KEY_ID=XXXXXXXXXX ASC_ISSUER_ID=xxxx-xxxx ./scripts/testflight.sh
#
# ONE-TIME SETUP (only the owner can do this - it needs their Apple login):
#   App Store Connect → Users and Access → Integrations →
#   App Store Connect API → Team Keys → "+" → role App Manager.
#   Download the AuthKey_XXXX.p8 ONCE into ~/.appstoreconnect/private_keys/
#   and note the Key ID and Issuer ID shown on that page.
#
# The API key lets xcodebuild cloud-sign (no Apple ID session needed) and
# lets this script register the bundle id. After the upload, the build
# appears in App Store Connect → TestFlight in a few minutes; install the
# TestFlight app on the iPhone and it's one tap.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${ASC_KEY_ID:?set ASC_KEY_ID (from the Team Keys page)}"
: "${ASC_ISSUER_ID:?set ASC_ISSUER_ID (from the Team Keys page)}"
KEYS_DIR="$HOME/.appstoreconnect/private_keys"
ASC_KEY_PATH="${ASC_KEY_PATH:-$KEYS_DIR/AuthKey_${ASC_KEY_ID}.p8}"
[[ -f "$ASC_KEY_PATH" ]] || { echo "missing $ASC_KEY_PATH"; exit 1; }
TEAM="${VIVIEEN_TEAM:-X7R8N6MMSU}"

echo "▸ making sure the bundle id is registered…"
ASC_KEY_ID="$ASC_KEY_ID" ASC_ISSUER_ID="$ASC_ISSUER_ID" \
  ASC_KEY_PATH="$ASC_KEY_PATH" .venv/bin/python scripts/asc_bootstrap.py || true

(cd ios && xcodegen generate >/dev/null)
BUILD_NUMBER=$(date +%y%m%d%H%M)
ARCHIVE=build/vivieen-tf.xcarchive

# CODE_SIGN_IDENTITY must be forced here: project.yml pins it to "" for the
# simulator, and an UNSIGNED archive carries no entitlements - the export
# re-sign then applies only the profile's minimal set, so the App Group
# never reaches the device (verified against build 2608051328).
echo "▸ archiving (cloud signing via the API key, team $TEAM, build $BUILD_NUMBER)…"
xcodebuild -project ios/Vivieen.xcodeproj -scheme Vivieen \
  -destination "generic/platform=iOS" -configuration Release \
  -archivePath "$ARCHIVE" archive -allowProvisioningUpdates \
  -authenticationKeyPath "$ASC_KEY_PATH" \
  -authenticationKeyID "$ASC_KEY_ID" \
  -authenticationKeyIssuerID "$ASC_ISSUER_ID" \
  DEVELOPMENT_TEAM="$TEAM" CODE_SIGN_STYLE=Automatic \
  CODE_SIGNING_REQUIRED=YES CODE_SIGNING_ALLOWED=YES \
  CODE_SIGN_IDENTITY="Apple Development" \
  CURRENT_PROJECT_VERSION="$BUILD_NUMBER" -quiet

cat > build/ExportOptions-testflight.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>method</key><string>app-store-connect</string>
  <key>destination</key><string>upload</string>
  <key>teamID</key><string>$TEAM</string>
  <key>signingStyle</key><string>automatic</string>
  <key>manageAppVersionAndBuildNumber</key><false/>
</dict></plist>
PLIST

echo "▸ uploading to App Store Connect…"
xcodebuild -exportArchive -archivePath "$ARCHIVE" \
  -exportPath build/tf-export \
  -exportOptionsPlist build/ExportOptions-testflight.plist \
  -allowProvisioningUpdates \
  -authenticationKeyPath "$ASC_KEY_PATH" \
  -authenticationKeyID "$ASC_KEY_ID" \
  -authenticationKeyIssuerID "$ASC_ISSUER_ID"

echo "✓ uploaded. App Store Connect → TestFlight shows the build after"
echo "  processing (5-15 min). Export compliance is pre-answered in the"
echo "  app, so it goes straight to 'Ready to Test' for internal testers."