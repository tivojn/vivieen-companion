# The iOS hands map — every road Apple opens, gates, or closes

What a normal App Store app (Vivieen Pocket) may operate on iOS, verified
2026-08-05 against the iOS 26 SDK era. Four tiers: **open** (just code),
**ask-the-owner** (runtime permission dialog), **Apple-gated** (entitlement
Apple grants per-app on request), **closed** (no public API — the Mac
side is the workaround where one exists).

## Tier 1 — Personal data, behind a one-time permission dialog

| Area | Framework | Can do | Vivieen status |
|---|---|---|---|
| Calendar | EventKit | read/create/update/delete events, any calendar | ✅ shipped (hands) |
| Reminders | EventKit | read/create/complete/delete, lists, due dates | ✅ shipped (hands) |
| Contacts | Contacts | full cards read/write: phones, emails, addresses, birthdays, relations, orgs; **note field is Apple-gated** (see tier 3) | ✅ shipped (hands) |
| Photos | PhotosKit / PHPhotoLibrary | read/search/save photos & videos, albums, metadata; "limited access" picker variant | strong candidate — "find the photo of…" |
| Location | Core Location | current position, geofences, significant-change wake-ups | candidate (pairs with Maps/Weather) |
| Health & fitness | HealthKit | read/write steps, workouts, sleep, heart, nutrition… (per-type consent) | candidate — "how did I sleep?" |
| Microphone / camera | AVFoundation | capture | ✅ already used (PTT, selfie) |
| Speech-to-text | Speech (SFSpeechRecognizer) | on-device transcription, free, offline | candidate solo fallback below Soniox |
| Music | MusicKit / MediaPlayer | search/play Apple Music + library, playlists | candidate — "play something calm" |
| Home | HomeKit | read/control accessories, scenes ("turn off the lights") | candidate |
| Motion | Core Motion | steps, activity type | minor |
| Bluetooth / NFC | Core Bluetooth, Core NFC | peripherals, tag reading | niche |
| Local network | Network + multicast permission | LAN discovery | ✅ already used (pairing) |
| Clipboard | UIPasteboard | read (triggers paste banner) / write | trivial to add |
| Notifications | UserNotifications | local + push, actions, time-sensitive | candidate — reminders that speak |

## Tier 2 — Files

| Road | What it reaches | Notes |
|---|---|---|
| App sandbox (FileManager) | app's own container | unrestricted |
| App's iCloud container (ubiquity / CloudKit) | app-private iCloud storage | syncs across the owner's devices — good for her memory |
| Document picker (UIDocumentPickerViewController) | **any file/folder the owner picks**: On My iPhone, iCloud Drive, Dropbox/Drive providers | user gesture per grant; security-scoped bookmarks make a picked FOLDER permanently reachable — pick once, hands forever |
| Files app at large | ❌ no blanket browse | Apple's rule: the owner points, the app touches |

Practical design: a "give her a folder" button (picker → bookmark) buys
persistent read/write on a real folder tree — the closest iOS gets to
"local files."

## Tier 3 — Apple-gated entitlements (apply, then it works)

| Capability | Entitlement / program | What it unlocks |
|---|---|---|
| Contact notes | com.apple.developer.contacts.notes ([request form](https://developer.apple.com/contact/request/contact-note-field)) | the note field on contact cards — Debug/simulator build already carries it; TestFlight needs Apple's yes |
| Wallet transactions | FinanceKit (financekit entitlement) | read Apple Card / Apple Cash / Savings transactions & balances — "what did I spend this week?" |
| Screen time | FamilyControls + DeviceActivity + ManagedSettings | app-usage reports, app limits/shields |
| CarPlay | category entitlements | car dashboard presence |
| Critical alerts | critical-alerts entitlement | notifications that break through silent mode |
| Sensor research | SensorKit | research-grade sensors (research programs only) |
| VPN / content filter | Network Extension flavors | traffic-level features |

## Tier 4 — System integration that needs no data permission

| Area | Framework | Why it matters for her |
|---|---|---|
| **On-device LLM** | **Foundation Models (iOS 26)** | the Apple Intelligence ~3B model, free, offline, no key — guided generation + tool calling; iOS 27 adds image input and server-model bridging. A REAL solo brain that needs no synced key and no Mac. |
| Siri / Shortcuts | App Intents | expose her actions ("Hey Siri, ask Vivieen…"), Spotlight semantic index, interactive widgets/controls; `shortcuts://run-shortcut?name=…` runs an owner-made shortcut — the indirect road into hundreds of apps |
| Translation | Translation framework | on-device translation, free |
| Weather | WeatherKit | forecast by location (500k calls/mo free) |
| Maps & places | MapKit | geocoding, POI search, directions ETA — no key |
| Vision / OCR | Vision, VisionKit | read text out of photos/screenshots |
| TTS | AVSpeechSynthesizer | free offline voices (below her real voices, but never mute) |
| Widgets / Live Activities | WidgetKit, ActivityKit | her face and state on the lock screen / Dynamic Island |
| Wallet passes | PassKit | add tickets/cards she's given |
| Compose sheets | MessageUI, `mailto:`/`sms:`/`tel:`/`facetime:` | pre-filled mail/SMS/call the OWNER taps send on — the only phone-side mail/SMS |
| Safari extension | Safari Web Extensions | act inside Safari pages (own extension UI) |
| iMessage app | Messages framework | stickers / mini-app inside the Messages composer (not reading chats) |
| Auth | LocalAuthentication | Face ID gate for dangerous hands (send/delete) |

## Closed — no public API, and the honest workaround

| Wish | iOS reality | The road that exists |
|---|---|---|
| Apple Mail mailbox | ❌ nothing (compose sheet only) | ✅ **shipped**: Mac-side AppleScript mail hands via /reply |
| Apple Notes | ❌ nothing | Mac-side AppleScript (same pattern as mail) — candidate |
| Messages/SMS/WhatsApp/WeChat content | ❌ read/send closed; SMS filter ext. can only classify unknown senders | compose sheets; WhatsApp `wa.me` prefill |
| Safari history/bookmarks | ❌ | own in-app browser, or Mac-side |
| Wallpaper / ringtones / system settings | ❌ (deep links into Settings only) | — |
| Other apps' screens (computer-use) | ❌ no accessibility automation API on iOS at all | Shortcuts bridge is the only lever; real computer-use stays on the Mac |
| Shell / arbitrary code | ❌ no fork/exec/JIT | the Mac remains the terminal |
| Call audio / recording | ❌ (system call recording has no API) | — |

## Reading the map for Vivieen

The split-brain architecture is exactly right for this table: **whatever
iOS closes, the Mac usually opens** (mail shipped; Notes is the same
five-file pattern away), and whatever iOS opens goes into AgentHands on
the phone. Highest-value next hands, in rough order: Foundation Models
as the keyless solo brain, Photos, the folder-bookmark file hands,
HealthKit, App Intents/Siri exposure, MusicKit, HomeKit, WeatherKit+
MapKit, and — with one Apple form each — contact notes and FinanceKit.
