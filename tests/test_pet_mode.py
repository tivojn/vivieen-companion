import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from studio import body, cutout, face, generate, motion


ROOT = Path(__file__).resolve().parents[1]


class PetInputBridgeTests(unittest.TestCase):
    def test_alpha_hit_tracking_works_while_another_app_is_frontmost(self):
        main = (ROOT / "electron" / "main.cjs").read_text()
        preload = (ROOT / "electron" / "preload.cjs").read_text()
        renderer = (ROOT / "web" / "index.html").read_text()
        self.assertIn("screen.getCursorScreenPoint()", main)
        self.assertIn("vivieen:pet-pointer", main)
        # Off-window coordinates now flow through (for cursor gaze) with an
        # `inside` flag; the renderer restores the {-1,-1} hit sentinel itself
        # so click-through behaviour is unchanged.
        self.assertIn(
            "x: point.x - bounds.x, y: point.y - bounds.y, inside,", main)
        self.assertIn("onPetPointer", preload)
        self.assertIn("HAS_GLOBAL_PET_POINTER", renderer)
        self.assertIn("SHELL.onPetPointer", renderer)
        self.assertIn(
            "pointer=inside?{x:Number(point.x),y:Number(point.y)}:{x:-1,y:-1};",
            renderer)
        self.assertIn("noteCursor(Number(point.x),Number(point.y));", renderer)
        self.assertIn("cursorGazeFor", renderer)
        self.assertIn("hitConfidence", renderer)
        self.assertIn("ctx.getImageData", renderer)

    def test_pet_gestures_share_one_button_without_conflicts(self):
        # pointerdown only ARMS a gesture: drag starts after the slop (from
        # the press point, so nothing jumps), a held HEAD press becomes
        # push-to-talk, and a quick release is an acknowledged tap. The head
        # is resolved by sampling the real head mask through the scene
        # transforms, never a hit box.
        renderer = (ROOT / "web" / "index.html").read_text()
        self.assertIn("function pointerOnHead", renderer)
        self.assertIn("petHitCtx.drawImage(HEADMASK,0,0", renderer)
        self.assertIn("const TAP_SLOP_PX=4,HEAD_HOLD_MS=200,TAP_MS=350;", renderer)
        self.assertIn("SHELL.beginPetDrag({screenX:gesture.sx,screenY:gesture.sy});", renderer)
        self.assertIn("function startPetTalk", renderer)
        self.assertIn("function petTapReaction", renderer)
        self.assertIn("cv.addEventListener('pointercancel'", renderer)
        # Drag must never start once push-to-talk is live.
        self.assertIn("if(SHELL&&gesture&&!dragging&&!gesture.ptt&&", renderer)

    def test_head_press_drives_enconvo_voice_hotkey(self):
        # A held head presses EnConvo's right-Option voice hotkey for real
        # (held down while held, up on release) via the key-tap helper; the
        # answer comes back through the monitor, not Vivieen's local chat.
        renderer = (ROOT / "web" / "index.html").read_text()
        preload = (ROOT / "electron" / "preload.cjs").read_text()
        main = (ROOT / "electron" / "main.cjs").read_text()
        package = json.loads((ROOT / "package.json").read_text())
        self.assertIn("SHELL.petVoiceKey('down')", renderer)
        self.assertIn("SHELL.petVoiceKey('up')", renderer)
        self.assertIn("petVoiceKey", preload)
        self.assertIn("vivieen:pet-voice-key", main)
        self.assertIn("'key-tap'", main)
        self.assertIn("accessibility-permission-missing", main)
        self.assertIn("build:key-tap", package["scripts"])
        self.assertIn("build:key-tap", package["scripts"]["build:native"])
        swift = (ROOT / "electron" / "native" / "key_tap.swift").read_text()
        self.assertIn("flagsChanged", swift)
        self.assertIn("maskAlternate", swift)

    def test_unfollowed_head_press_is_her_own_hold_to_talk(self):
        # The EnConvo hotkey only fires while the monitor follows EnConvo.
        # Unfollowed, the same head hold records through Vivieen's configured
        # ASR, answers through her own Settings models, and opens the chat
        # bar (Hold to talk + text field) for typed follow-ups.
        renderer = (ROOT / "web" / "index.html").read_text()
        self.assertIn(
            "if(monitorEnabled){SHELL.petVoiceKey('down');return;}", renderer)
        self.assertIn(
            "if(monitorEnabled){SHELL.petVoiceKey('up');return;}", renderer)
        marker = renderer.index("function startPetTalk")
        window = renderer[marker:renderer.index("function stopPetTalk")]
        self.assertIn("classList.add('chat-open')", window)
        # Forced: the hidden chat button's disabled flag stays latched in pet
        # mode until a health poll lands, and the head must not wait for it.
        self.assertIn("startRec(true)", window)
        stop = renderer[renderer.index("function stopPetTalk"):]
        stop = stop[:stop.index("function petTapReaction")]
        self.assertIn("stopRec()", stop)
        # canPetTalk no longer requires the hotkey bridge when unfollowed.
        marker = renderer.index("function canPetTalk")
        window = renderer[marker:marker + 420]
        self.assertIn("monitorEnabled?", window)
        # The menu row reflects that both modes talk from the head.
        main = (ROOT / "electron" / "main.cjs").read_text()
        self.assertIn("followingEnconvo ? 'hold head' : 'hold head or type'", main)
        # Listening must be visible (soundwave pill + pulsing field), a mic
        # refusal must surface in the field, and the open bar must never
        # swallow drag / taps / the right-click menu on the avatar herself.
        self.assertIn('id="listenWave"', renderer)
        self.assertIn("classList.add('listening')", renderer)
        self.assertIn("classList.remove('listening')", renderer)
        self.assertIn("flashPrompt('Microphone blocked", renderer)
        # The face mask alone is not the head: people hold the CROWN, so a
        # skull circle around the nose (sized by the nose-to-neck span)
        # classifies the crown as HAIR - which arms hold-to-talk exactly
        # like the head (verified live on the desktop 2026-07-31) and gives
        # the double-tap Moves gesture its own zone.
        self.assertIn(
            "Math.hypot(point.x-nose[0],point.y-nose[1])<=radius)return 'hair'",
            renderer)
        self.assertIn("part,head:part==='head'||part==='hair'", renderer)
        # A release before the microphone resolves must not leave a ghost
        # recording running.
        self.assertIn("if(!recWanted){", renderer)
        # The shell forces click-through when the cursor leaves the window;
        # with the chat bar pinning petHit true the renderer never re-sent
        # the flag on re-entry and the window went permanently dead. The
        # interactive flag must be re-asserted on every outside->inside
        # transition.
        self.assertIn(
            "if(inside&&!pointerWasInside&&petHit", renderer)
        # An open chat bar must not defeat Click-Through Gaps (only its
        # actual controls claim the window) and must not count as engagement
        # by itself (or the 10s stillness Edge Idle never triggers again
        # after a head-talk leaves the bar open). Typing in it still does.
        self.assertIn("function pointerOverChatControls", renderer)
        self.assertIn(
            "const modalHit=pointerOverChatControls()||", renderer)
        # Unfollowed, the bar is hover-revealed chrome: chat-visible is
        # driven per-frame, a hidden bar claims no screen region, and the
        # heartbeat re-send heals any shell/renderer click-through desync
        # (the stuck-input-until-dragged bug).
        self.assertIn("classList.toggle('chat-visible'", renderer)
        self.assertIn("chat-open.chat-visible #bar{opacity:1", renderer)
        self.assertIn("!classes.contains('chat-visible'))return false", renderer)
        self.assertIn("if(now-lastHitSentAt>500){lastHitSentAt=now;\n"
                      "      if(SHELL&&typeof SHELL.setPetHit==='function')"
                      "SHELL.setPetHit(petHit);}", renderer)
        # Live dictation: timesliced recording streams interim transcripts
        # into the input field through the configured dictation model.
        self.assertIn("rec.start(280)", renderer)
        self.assertIn("function interimTranscribe", renderer)
        self.assertIn("if(recording&&r.text){txt.value=", renderer)
        self.assertIn(
            "(document.hasFocus()&&document.activeElement===txt)||txt.value.trim())lastEngagedAt=now;",
            renderer)
        self.assertNotIn(
            "document.documentElement.classList.contains('chat-open'))lastEngagedAt",
            renderer)
        marker = renderer.index("html.electron.pet.chat-open #bar{")
        self.assertIn("pointer-events:none", renderer[marker:marker + 220])
        self.assertIn("#bar #manual{pointer-events:auto}", renderer)
        # The input must never sit ON the avatar (2026-08-01, zoomed-out
        # screenshot: the field covered the chin; full-body would put it on
        # the legs). With the chat bar or listening chip up, the camera
        # reserves a measured lane at the window bottom and the avatar
        # glides up into the remaining height.
        self.assertIn("function petBottomReserve", renderer)
        self.assertIn("barHeight+18", renderer)
        self.assertIn("petLaneNow+=(petBottomReserve()-petLaneNow)*0.22", renderer)
        self.assertIn("Math.min(cv.width/crop.w,avail/crop.h)*margin", renderer)
        # Feet anchor at the EXACT window bottom - the shell rests that
        # edge on the Dock's top line, and the old 0.5% reserve read as
        # the figure hovering above the Dock (owner, 2026-08-01).
        self.assertIn("PET.roam?avail-(crop.y+crop.h)*scale", renderer)
        # Pointing at where the bar LIVES wakes it: without this, a cursor
        # aimed straight at the EnConvo mark (no detour over the avatar)
        # found an invisible bar that never revealed itself, so hover and
        # click both read as dead. Waking still claims no clicks.
        self.assertIn("function pointerOverBarZone", renderer)
        self.assertIn("||recording||pointerOverBarZone()||", renderer)
        # De-coupled pet mode keeps the bar minimal: no EnConvo mark (the
        # right-click menu's "Couple to EnConvo" is the way back); the
        # button and its hover label still serve the windowed view.
        self.assertIn("html.electron.pet #bar #monitor{display:none}", renderer)
        self.assertIn('id="monitorTip"', renderer)
        self.assertNotIn('title="Use EnConvo"', renderer)
        # The menu speaks the user's vocabulary: couple / de-couple.
        self.assertIn("'De-couple from EnConvo' : 'Couple to EnConvo'", main)
        # The listening hint is the quiet chip: dot + slim bars + the word.
        self.assertIn('id="listenWaveBars"', renderer)
        self.assertIn('id="listenWaveText"', renderer)
        # The field is a one-line-until-needed textarea: Enter sends,
        # Shift+Enter breaks the line, and every programmatic value change
        # re-sizes it.
        self.assertIn('<textarea id="txt"', renderer)
        self.assertIn("function autoSizeTxt", renderer)
        self.assertIn("e.key==='Enter'&&!e.shiftKey", renderer)
        # The shell claims the chat controls AUTHORITATIVELY from its own
        # 32ms cursor poll: the renderer-side claim (rAF -> hysteresis ->
        # IPC) could stall - a stuck dragging flag froze the pointer feed -
        # and the window went permanently deaf to clicks while hover kept
        # working via forwarded moves. The renderer only reports where the
        # visible controls ARE; hidden bars report empty so desktop clicks
        # in the gap stay click-through.
        self.assertIn("function reportControlRects", renderer)
        self.assertIn("SHELL.setPetControlRects(rects)", renderer)
        preload = (ROOT / "electron" / "preload.cjs").read_text()
        self.assertIn("vivieen:pet-control-rects", preload)
        self.assertIn("target.setHit(true, 'controls')", main)
        # A drag in flight owns the window: forcing click-through while the
        # cursor outruns the moving bounds lost the mouseup that ends the
        # drag. Recording pins the claim the same way, and a window-level
        # release rescues a drag whose canvas pointerup was lost.
        self.assertIn("target.setHit(true, 'drag-pinned')", main)
        self.assertIn("pointerOverChatControls()||recording||", renderer)
        self.assertIn("if(!dragging)return;", renderer)
        # recoverCompanion goes through setPetHit so the dedupe flag stays
        # honest - a direct setIgnoreMouseEvents desynced it.
        self.assertIn("setPetHit(true, 'recover')", main)
        # Clicking the field must make the window KEY: acceptFirstMouse
        # delivers clicks into an inactive window WITHOUT activating it, so
        # the caret never blinked whenever Vivieen was not frontmost - the
        # "randomly dead input field".
        self.assertIn("vivieen:pet-focus", preload)
        self.assertIn("app.focus({ steal: true })", main)
        self.assertIn("SHELL.focusPetWindow()", renderer)

    def test_articulation_travels_between_mouth_shapes(self):
        # Measured 2026-07-31: with the 18ms cut, EVERY transition on a
        # realistic phoneme track had zero visible in-between frames at
        # 60fps - a slideshow. The aperture-aligned dissolve (both plates
        # warped to one shared lip gap so dental rows coincide mid-fade)
        # buys 45-105ms of real travel: zero instant cuts, ~3 in-between
        # frames per transition.
        renderer = (ROOT / "web" / "index.html").read_text()
        self.assertIn("const XFADE=0.055;", renderer)
        self.assertIn("const APERTURE_V=", renderer)
        self.assertIn("function fadeFor(from,to){", renderer)
        self.assertIn("const EXTERNAL_XFADE=0.085;", renderer)
        # Both plates get their OWN mouth-band stretch toward the shared gap.
        self.assertIn("const plate=(image,extra)=>", renderer)
        self.assertIn("mouthWarp?mouthWarp.prev:0", renderer)
        self.assertIn("mouthWarp?mouthWarp.cur:0", renderer)
        # Short vowels undershoot and held vowels breathe with loudness.
        self.assertIn("curReduce=0.62+0.38*(dwell/0.11);", renderer)
        self.assertIn("apCur*=0.86+0.20*Math.min(1,lvl*6);", renderer)
        # The track QA invariant: dissolve midpoint meets the audio event.
        self.assertIn("const VISUAL_LEAD=XFADE*0.5;", renderer)

    def test_gaze_grid_carries_directed_glances(self):
        # The iris grid keeps its quarter-pixel VOR centre and gains coarse
        # flanks wide enough that eyes-following-cursor is visible at chat
        # scale; old runtime bundles are rebaked on activation.
        from studio import expression
        self.assertGreaterEqual(max(expression.GAZE_DX), 9.0)
        self.assertLessEqual(min(expression.GAZE_DX), -9.0)
        self.assertIn(0.25, [round(b - a, 3) for a, b in zip(
            expression.GAZE_DX, expression.GAZE_DX[1:])])
        self.assertGreaterEqual(max(expression.GAZE_DY), 2.0)
        server = (ROOT / "server" / "app.py").read_text()
        self.assertIn("RUNTIME_VERSION = 15", server)
        export_source = (ROOT / "studio" / "export.py").read_text()
        self.assertIn("dict(v=15,", export_source)

    def test_body_parts_are_classified_and_react(self):
        # Clicks resolve to the nearest baked bone segment (head stays
        # mask-exact); each part answers with its own baked warp reaction.
        renderer = (ROOT / "web" / "index.html").read_text()
        export_source = (ROOT / "studio" / "export.py").read_text()
        self.assertIn("function canvasToBody", renderer)
        self.assertIn("function petBodyPart", renderer)
        # Portrait-mode avatars count the whole cutout as head, so
        # hold-to-talk works before any body is generated.
        self.assertIn("if(!(BODY&&HEADMASK&&M.body))return 'head';", renderer)
        self.assertIn("function startLimbReaction", renderer)
        self.assertIn("drawLimbReaction(now);", renderer)
        self.assertIn("petTapReaction(part);", renderer)
        self.assertIn("let part=active.part||'body';", renderer)
        self.assertIn("M.body.reactions", renderer)
        self.assertIn("_publish_body_extras", export_source)
        self.assertIn("react_", export_source)
        from studio import limbs
        self.assertGreaterEqual(limbs.STATES, 5)

    def test_idle_walk_hover_choreography(self):
        # Ten quiet seconds settle the standing pet into the Edge Idle loop;
        # a double leg tap starts the Horizon Walk; hovering either animation
        # brings the live standing avatar back.
        renderer = (ROOT / "web" / "index.html").read_text()
        self.assertIn("function standingIdleActive", renderer)
        self.assertIn("STANDING_IDLE_AFTER_MS=10000", renderer)
        self.assertIn("if(standingIdleActive(now)){", renderer)
        self.assertIn("DOUBLE_TAP_MS=IS_IOS?650:450", renderer)
        self.assertIn("function petDoubleTap", renderer)
        # The old anywhere-on-her dblclick walk handler must stay gone: it
        # fired alongside the part verbs, so a chest double-tap meant to
        # raise opacity also sent her walking.
        self.assertNotIn("syncShellState(await SHELL.setPetRoam(true))", renderer)
        self.assertIn(".then(state=>syncShellState(state)).catch(()=>{});", renderer)
        self.assertIn("Promise.resolve(SHELL.setPetRoam(true))", renderer)
        # Double-tap verbs: chest raises opacity, a foot lowers it (floored so
        # she can never vanish), and the idle leans docked in the window's
        # own corner - right for the active avatar, left for a second one.
        self.assertIn("SHELL.setPetOpacity(Math.min(1,", renderer)
        self.assertIn("SHELL.setPetOpacity(Math.max(.15,", renderer)
        self.assertIn("SHELL.dockPet()", renderer)
        self.assertIn("drawMotionClip('idle',now,PET_SIDE)", renderer)
        main = (ROOT / "electron" / "main.cjs").read_text()
        preload = (ROOT / "electron" / "preload.cjs").read_text()
        self.assertIn("vivieen:pet-dock", main)
        self.assertIn("dockedPetBounds(size, area, 0)", main)
        self.assertIn("dockPet", preload)
        # Menu rows split name (left) from gesture hint (right): a native
        # macOS Menu reserves the right column for accelerators, so the
        # menu is our own window (web/menu.html) fed a {name, hint} spec.
        for name, hint in (("Talk", "hold head"), ("Walk", "2×tap leg"),
                           ("Opacity +", "2×tap chest"),
                           ("Opacity −", "2×tap foot")):
            self.assertIn(f"name: '{name}', hint: '{hint}'", main)
        self.assertIn("function showMenuWindow", main)
        self.assertIn("vivieen:menu-spec", main)
        menu_page = (ROOT / "web" / "menu.html").read_text()
        self.assertIn("hint.className='hint'", menu_page)
        self.assertIn("text-align:right", menu_page)
        # The menu follows the app theme: the choice Settings saved (same
        # origin, same localStorage key) wins, system scheme otherwise.
        self.assertIn("localStorage.getItem('vivieen-theme')", menu_page)
        self.assertIn(":root[data-theme=dark]", menu_page)
        package = (ROOT / "package.json").read_text()
        self.assertIn('"menu.html"', package)
        app_source = (ROOT / "server" / "app.py").read_text()
        self.assertIn('@app.get("/menu")', app_source)
        self.assertIn("ROAM_HOVER_STOP_MS", renderer)
        self.assertIn("SHELL.setPetRoam(false)", renderer)
        # The stillness clock resets on every kind of attention.
        self.assertIn("lastEngagedAt=now;", renderer)

    def test_continuous_size_expands_native_alpha_window(self):
        main = (ROOT / "electron" / "main.cjs").read_text()
        renderer = (ROOT / "web" / "index.html").read_text()
        appearance = (ROOT / "web" / "appearance.html").read_text()
        appearance_preload = (ROOT / "electron" / "appearance-preload.cjs").read_text()
        bounds = (ROOT / "electron" / "pet-window-bounds.cjs").read_text()
        server = (ROOT / "server" / "app.py").read_text()
        package = json.loads((ROOT / "package.json").read_text())
        self.assertIn("PET_BASE_SIZE", main)
        self.assertIn("PET_ZOOM_RANGE", main)
        self.assertIn("petBoundsForZoom", main)
        self.assertIn("enableLargerThanScreen: true", main)
        self.assertIn("boundsForPetZoom", main)
        self.assertIn("current.x + (current.width - width) / 2", bounds)
        self.assertNotIn("Math.min(area.width", main)
        self.assertNotIn("Math.min(area.height", main)
        self.assertIn("mainWindow.setBounds(bounds, false)", main)
        self.assertIn("Size & Opacity…", main)
        self.assertNotIn("petZoomItems", main)
        self.assertNotIn("petOpacityItems", main)
        self.assertIn('id="size" type="range" min="25" max="400"', appearance)
        self.assertIn('id="opacity" type="range" min="0" max="100"', appearance)
        self.assertIn("setSize", appearance_preload)
        self.assertIn("setOpacity", appearance_preload)
        self.assertNotIn("*(PET.roam?1:(PET.zoom||1))", renderer)
        self.assertIn("confirmPetEventHit", renderer)
        self.assertIn("following-enconvo #bar{display:none!important}", renderer)
        self.assertIn('@app.get("/appearance")', server)
        web_filter = next(
            entry["filter"] for entry in package["build"]["extraResources"]
            if entry.get("from") == "web")
        self.assertIn("appearance.html", web_filter)
        self.assertIn("bubble.html", web_filter)

    def test_live_pinch_zoom_stays_in_sync_and_scales_animations(self):
        main = (ROOT / "electron" / "main.cjs").read_text()
        preload = (ROOT / "electron" / "preload.cjs").read_text()
        renderer = (ROOT / "web" / "index.html").read_text()
        appearance = (ROOT / "web" / "appearance.html").read_text()
        appearance_preload = (ROOT / "electron" / "appearance-preload.cjs").read_text()
        bounds = (ROOT / "electron" / "pet-window-bounds.cjs").read_text()
        # the pinch drives the window every frame instead of a trailing debounce
        self.assertIn("vivieen:pet-zoom-live", main)
        self.assertIn("vivieen:pet-zoom-live", preload)
        self.assertIn("applyPetZoomLive", main)
        self.assertIn("setPetZoomLive", renderer)
        self.assertNotIn("setTimeout(()=>SHELL.setPetZoom(PET.zoom),160)", renderer)
        # one anchor per gesture, so rounding cannot walk the window sideways
        self.assertIn("boundsForPetZoomAtAnchor", bounds)
        self.assertIn("petZoomAnchor(mainWindow.getBounds())", main)
        # the alpha probe is a GPU readback: once per gesture, not per event
        self.assertIn("if(!zoomGesture&&!confirmPetEventHit(event))return;", renderer)
        # a shell echo must never rewind a pinch that is still in flight
        self.assertIn("zoomEchoUntil", renderer)
        # the panel adopts whatever the pinch left behind, focus or not
        self.assertNotIn("document.activeElement!==size", appearance)
        self.assertIn("getState", appearance)
        self.assertIn("visibilitychange", appearance)
        # edge idle and horizon walk resize on their own slider
        self.assertIn('id="motion" type="range" min="50" max="300"', appearance)
        self.assertIn("setMotionSize", appearance_preload)
        self.assertIn("vivieen:set-pet-roam-zoom", main)
        self.assertIn("PET_ROAM_ZOOM_RANGE", main)
        self.assertIn("petRoamSize()", main)
        self.assertIn("petRoamZoom", main)
        self.assertIn("roamZoomValue", renderer)
        self.assertIn("syncMotionProfile", renderer)

    def test_horizon_walk_requires_alpha_motion_and_restores_live_standing(self):
        main = (ROOT / "electron" / "main.cjs").read_text()
        preload = (ROOT / "electron" / "preload.cjs").read_text()
        renderer = (ROOT / "web" / "index.html").read_text()
        self.assertIn("Horizon Walk Along Dock", main)
        self.assertIn("PET_LEDGE_HOLD_MS", main)
        self.assertIn("state.petHomeBounds", main)
        self.assertIn("petMotionReady", main)
        self.assertIn("setPetEngaged", main)
        self.assertIn("petRoamRuntime.mode = petRoamRuntime.resumeMode || 'walk';", main)
        self.assertIn("petRoamRuntime.resumeMode = petRoamRuntime.mode.startsWith('ledge-')", main)
        self.assertIn("setPetMotionReady", preload)
        self.assertIn("setPetEngaged", preload)
        self.assertIn("drawMotionClip", renderer)
        self.assertIn("MOTION={walk:null,idle:null,move:null}", renderer)
        self.assertIn("speaking||recording||petHit", renderer)
        self.assertIn("elapsed / Math.max(0.1, petRoamRuntime.cycleSeconds)", main)
        self.assertIn("motionTravelDelta", main)
        self.assertIn("Number.isFinite(petRoamRuntime.x)", main)
        self.assertIn("petRoamRuntime.x = x", main)
        self.assertIn("travelOffsets", main)
        self.assertIn("clip.travel_offsets", renderer)
        self.assertIn("phase*clip.frames", renderer)
        self.assertIn("edge==='right'", renderer)
        self.assertIn("clip.edge_anchors", renderer)
        self.assertIn("anchors.left_frames", renderer)
        self.assertIn("wallPadding=3", renderer)
        self.assertIn("const motionReady=Boolean(MOTION.walk);", renderer)
        # The idle ships as GPU-decoded VP9-alpha video when the baking
        # ffmpeg can encode it; the walk keeps its frame-exact atlas, and
        # every consumer tolerates either shape.
        self.assertIn("def _encode_alpha_stream",
                      (ROOT / "studio" / "motion.py").read_text())
        self.assertIn('clip["alpha_stream"] = f"assets/{stream_name}"',
                      (ROOT / "studio" / "export.py").read_text())
        self.assertIn(
            'clip.get("sheets") or clip.get("alpha_stream")',
            (ROOT / "server" / "app.py").read_text())
        self.assertIn("for(const src of streams){", renderer)
        self.assertIn("if(clip.video){", renderer)
        self.assertIn("backgroundThrottling: false", main)
        self.assertNotIn("stride*direction*width", renderer)
        self.assertNotIn("PET_ROAM_SPEED", main)

    def test_update_check_offers_the_newer_github_dmg(self):
        # Check-and-click, not a silent installer: the app is signed but not
        # notarized, so it POINTS at the newer DMG instead of replacing its
        # own binary. Background check on launch + every 6h in packaged
        # builds only; a menu row answers on demand in both menus.
        main = (ROOT / "electron" / "main.cjs").read_text()
        self.assertIn("function checkForUpdates", main)
        self.assertIn("releases/latest", main)
        self.assertIn("/\\.dmg$/i", main)
        # Install, not just download: the app fetches the DMG itself (no
        # quarantine attribute that way), verifies the new bundle deeply
        # AND requires the same signing team as the running build, swaps
        # it into /Applications, and relaunches. Any failure falls back
        # to opening the DMG; so does a copy not living in /Applications.
        self.assertIn("function installUpdate", main)
        self.assertIn("async function codesignTeam", main)
        self.assertIn("'--verify', '--deep', '--strict'", main)
        self.assertIn("signature team mismatch", main)
        self.assertIn("app.getAppPath().startsWith('/Applications/')", main)
        self.assertIn("'Install Update' : 'Download Update'", main)
        self.assertIn("scheduleUpdateChecks()", main)
        self.assertIn("if (!app.isPackaged) return", main)
        self.assertIn("'Check for Updates…', click", main)
        self.assertIn("name: 'Check for Updates…'", main)
        # The real comparator, run under node.
        import subprocess
        script = (
            "const s=require('fs').readFileSync(process.argv[1],'utf8');"
            "const m=s.match(/function versionNewer[\\s\\S]*?\\n}/);"
            "const fn=new Function('return '+m[0])();"
            "console.log(JSON.stringify([fn('0.7.0','0.6.0'),fn('v0.6.1','0.6.0'),"
            "fn('0.6.0','0.6.0'),fn('0.5.9','0.6.0'),fn('1.0.0','0.9.9')]));"
        )
        out = subprocess.run(
            ["node", "-e", script, str(ROOT / "electron" / "main.cjs")],
            capture_output=True, text=True)
        self.assertEqual(json.loads(out.stdout), [True, True, False, False, True])

    def test_enconvo_is_default_and_double_click_uses_detached_bubble(self):
        main = (ROOT / "electron" / "main.cjs").read_text()
        native = (ROOT / "electron" / "native" / "enconvo_audio_tap.swift").read_text()
        preload = (ROOT / "electron" / "preload.cjs").read_text()
        renderer = (ROOT / "web" / "index.html").read_text()
        server = (ROOT / "server" / "app.py").read_text()
        bubble = (ROOT / "web" / "bubble.html").read_text()
        self.assertIn("followEnconvo: true", main)
        self.assertIn("--trigger-right-option", main)
        self.assertIn("CGPreflightPostEventAccess", native)
        self.assertIn("virtualKey: CGKeyCode(61)", native)
        self.assertIn("triggerEnconvoVoiceCommand", preload)
        self.assertIn("SHELL.triggerEnconvoVoiceCommand()", renderer)
        self.assertIn("showSpeechBubble", renderer)
        self.assertIn("chat-open #log{display:none}", renderer)
        self.assertIn("following-enconvo #bar{display:none!important}", renderer)
        self.assertIn('@app.get("/bubble")', server)
        self.assertIn('id="text"', bubble)
        # A reply that arrives as markdown must READ as markdown - the
        # bubble carries the same DOM-built renderer as the chat caption
        # (textContent only, so a reply can never inject markup), and the
        # shell keeps newlines (its old whitespace-collapse flattened
        # lists and fences into one unparseable line).
        self.assertIn("function renderMarkdown", bubble)
        self.assertIn("renderMarkdown(value, text)", bubble)
        # Long replies scroll instead of clamping, the window accepts mouse
        # events so the wheel can reach it, and reading holds the auto-hide.
        self.assertIn("overflow-y:auto", bubble)
        self.assertNotIn("-webkit-line-clamp", bubble)
        self.assertIn("vivieen:bubble-hold", main)
        self.assertIn("function holdSpeechBubble", main)
        self.assertNotIn("bubbleWindow.setIgnoreMouseEvents(true)", main)
        marker = main.index("function showSpeechBubble")
        self.assertIn("replace(/\\r\\n?/g, '\\n')", main[marker:marker + 400])
        self.assertNotIn("replace(/\\s+/g, ' ')", main[marker:marker + 400])


class MotionPipelineTests(unittest.TestCase):
    @staticmethod
    def _synthetic_pose(root_x, phase):
        def point(x, y):
            return {"x": x, "y": y, "confidence": 1.0}

        arm = math.sin(phase) * 16
        leg = -math.sin(phase) * 24
        return {
            "joints": {
                "nose": point(root_x, 20),
                "neck": point(root_x, 45),
                "root": point(root_x, 105),
                "left_shoulder": point(root_x - 4, 52),
                "right_shoulder": point(root_x + 4, 52),
                "left_elbow": point(root_x - 4 + arm * 0.55, 75),
                "right_elbow": point(root_x + 4 - arm * 0.55, 75),
                "left_wrist": point(root_x - 4 + arm, 98),
                "right_wrist": point(root_x + 4 - arm, 98),
                "left_hip": point(root_x - 3, 108),
                "right_hip": point(root_x + 3, 108),
                "left_knee": point(root_x - 3 + leg * 0.55, 145),
                "right_knee": point(root_x + 3 - leg * 0.55, 145),
                "left_ankle": point(root_x - 3 + leg, 188),
                "right_ankle": point(root_x + 3 - leg, 188),
            }
        }

    def test_media_commands_inherit_selected_models_and_pose_references(self):
        image_provider = {
            "route": "open_ai/create",
            "model": "gpt-image-2",
        }
        image_command = motion._image_command(
            image_provider, ["/body.png", "/pose.png"], "/out", "idle", "prompt")
        reference_index = image_command.index("--reference_images")
        self.assertEqual(
            image_command[reference_index + 1:reference_index + 3],
            ["/body.png", "/pose.png"])
        self.assertEqual(
            image_command[image_command.index("--model") + 1], "gpt-image-2")
        self.assertNotIn("--credentials", image_command)

        video_provider = {
            "name": "x_ai",
            "model": "grok-imagine-video",
        }
        video_command = motion._video_command(
            video_provider, "/idle.png", "/out", "idle", "prompt")
        self.assertEqual(
            video_command[video_command.index("--model") + 1],
            "grok-imagine-video")
        self.assertEqual(
            video_command[video_command.index("--mode") + 1], "image-to-video")
        # "2:3" is not a native xAI video grid; a non-native request resamples
        # the subject through the model's internal grid and the idle figure
        # drifts wide. The idle therefore runs on native 9:16 end to end.
        self.assertEqual(
            video_command[video_command.index("--aspect_ratio") + 1], "9:16")
        walk_command = motion._video_command(
            video_provider, "/walk.png", "/out", "walk-source", "prompt")
        self.assertEqual(
            walk_command[walk_command.index("--aspect_ratio") + 1], "16:9")
        self.assertNotIn("--credentials", video_command)

    def test_walk_prompt_keeps_office_gait_compact(self):
        keyframe = motion._walk_keyframe_prompt("existing outfit")
        video = motion._walk_video_prompt()
        self.assertEqual(motion.WALK_FPS, 24)
        self.assertIn("one ordinary shoe-length step", keyframe)
        self.assertIn("both wrists between the hip seam and mid-thigh", keyframe)
        self.assertIn("This is not a runway performance", keyframe)
        self.assertIn("Do NOT use a flat side profile", keyframe)
        self.assertIn("BOTH complete arms, elbows, wrists, and hands", keyframe)
        self.assertIn("narrow background gap around each wrist", keyframe)
        self.assertIn("canonical RIGHT-SIDE full-body plate", keyframe)
        self.assertIn("canonical HD head", keyframe)
        # The office style now ships as an authored in-place loop: the
        # keyframe is the exact first and final frame, and the treadmill
        # contract replaces the runway crossing.
        self.assertIn("correct contralateral coordination", video)
        self.assertIn("IN PLACE", video)
        self.assertIn("EXACT first frame and the EXACT final frame", video)
        self.assertIn("input keyframe in every frame", video)
        self.assertIn("NORMAL charming office walk", video)
        self.assertIn("one-sided partial cycles", video)
        self.assertIn("same-side arm-and-leg motion", video)
        self.assertNotIn("camera-left to camera-right", video)
        # The runway frame remains landscape for traversal styles, because
        # measured portrait traversal runs walk in place and cannot be paced.
        default_frame = motion.resolve_walk_frame()
        self.assertEqual("landscape", default_frame["id"])
        crossing = "{}% to {}% at constant speed".format(*default_frame["crossing"])
        self.assertNotIn(crossing, video)
        self.assertIn("color flicker", video)
        # White studio plates: matting runs through macOS Vision person
        # segmentation (semantic, not color-keyed), so light skin, blonde
        # hair, and white wardrobe survive and there is no green spill to
        # fringe the contour. The chroma-key path remains for legacy takes.
        self.assertIn("pure white", keyframe)
        self.assertIn("pure white", video)
        self.assertEqual(motion.MOTION_VERSION, 9)

    def test_walk_style_presets_change_generation_and_validation(self):
        self.assertEqual(
            {"office", "runway", "stroll", "power", "promenade", "cartwheel"},
            set(motion.WALK_STYLE_PRESETS),
        )
        office = motion.resolve_walk_style()
        runway = motion.resolve_walk_style("runway")
        cartwheel = motion.resolve_walk_style({"id": "cartwheel"})
        self.assertEqual("office-gait", office["validation"])
        self.assertEqual("stylized-gait", runway["validation"])
        self.assertEqual("traversal", cartwheel["validation"])

        office_keyframe = motion._walk_keyframe_prompt("existing outfit", office)
        runway_keyframe = motion._walk_keyframe_prompt("existing outfit", runway)
        cartwheel_video = motion._walk_video_prompt(cartwheel)
        self.assertNotEqual(office_keyframe, runway_keyframe)
        self.assertIn("Runway catwalk", runway_keyframe)
        self.assertIn("narrow crossover track", runway_keyframe)
        # Assert the loop contract, not the prose. Two failure modes have already
        # been observed and both are unloopable: one cartwheel then standing still,
        # and nonstop tumbling that never stands up. A traversal clip therefore
        # needs repetition AND a repeated upright anchor to cut the loop on.
        self.assertIn("lateral cartwheel", cartwheel_video)
        self.assertIn("REPEATING TRAVERSAL LOOP", cartwheel_video)
        self.assertIn("upright", cartwheel_video)
        self.assertIn("twice", cartwheel_video)
        with self.assertRaisesRegex(ValueError, "unknown Horizon Walk style"):
            motion.resolve_walk_style("moonwalk")

    def test_custom_walk_style_is_a_free_in_place_gait(self):
        description = "hop forward on one foot with both arms stretched out"
        style = motion.resolve_walk_style("custom", description)
        self.assertEqual("custom", style["id"])
        self.assertEqual("free", style["validation"])
        self.assertEqual("loop", motion.walk_mode(style))
        self.assertEqual(description, style["prompt"])
        # The saved receipt round-trips through motion.json, so the dict form
        # must rebuild the same style, and the receipt must carry the prompt:
        # two different custom gaits may not share one cache signature.
        receipt = motion._walk_style_receipt(style)
        self.assertEqual(description, receipt["prompt"])
        self.assertEqual(style, motion.resolve_walk_style(receipt))
        self.assertNotIn("prompt", motion._walk_style_receipt("office"))
        # The user's text leads both prompts, and the two-step walking
        # contract must not override a gait that is not a two-footed walk.
        keyframe = motion._walk_keyframe_prompt("existing outfit", style)
        video = motion._walk_video_prompt(style)
        self.assertIn(description, keyframe)
        self.assertIn(description, video)
        self.assertIn("REPEATED CYCLES", video)
        self.assertNotIn("TWO-STEP GAIT CYCLE", video)
        with self.assertRaisesRegex(ValueError, "at least 12 characters"):
            motion.resolve_walk_style("custom", "hop")

    def test_idle_video_runs_on_native_nine_sixteen_plate(self):
        plate = motion.IDLE_PLATE
        self.assertEqual("9:16", plate["aspect_ratio"])
        self.assertEqual(plate["width"] * 16, plate["height"] * 9)
        with tempfile.TemporaryDirectory() as workspace:
            source = os.path.join(workspace, "keyframe.png")
            frame = np.zeros((1536, 1024, 3), np.uint8)
            frame[:, :] = (55, 170, 42)
            cv2.rectangle(
                frame, (390, 90), (640, 1460), (20, 30, 190),
                thickness=cv2.FILLED)
            cv2.imwrite(source, frame)
            destination = os.path.join(workspace, "idle-loop-keyframe.png")
            motion._idle_loop_keyframe(source, destination, lambda *_: None)
            composed = cv2.imread(destination, cv2.IMREAD_COLOR)
        self.assertEqual(
            (plate["height"], plate["width"]), composed.shape[:2])
        # White plate in the corners, subject bottom-anchored at the floor.
        self.assertTrue(bool(np.all(composed[4, 4] >= 245)), composed[4, 4])
        rows = np.where((composed[:, :, 2] > 120) & (composed[:, :, 1] < 90))[0]
        self.assertGreater(rows.size, 0)
        self.assertLessEqual(plate["floor"] - int(rows.max()), 4)
        self.assertLessEqual(int(rows.max()), plate["floor"])

    def test_green_screen_key_removes_background_and_green_spill(self):
        height, width = 180, 120
        frame = np.zeros((height, width, 3), np.uint8)
        for row in range(height):
            frame[row, :, :] = (55 + row // 6, 170 - row // 8, 42 + row // 10)
        cv2.rectangle(frame, (43, 20), (76, 158), (20, 30, 190), thickness=cv2.FILLED)
        cv2.rectangle(frame, (48, 80), (71, 150), (18, 43, 20), thickness=cv2.FILLED)
        cv2.circle(frame, (59, 20), 12, (78, 125, 186), thickness=cv2.FILLED)

        self.assertGreater(motion._green_screen_purity(frame), 0.8)
        self.assertTrue(motion._is_green_screen([frame, frame.copy()]))
        rgba = motion._chroma_key_frame(frame)

        self.assertEqual(int(rgba[10, 10, 3]), 0)
        self.assertEqual(int(rgba[90, 59, 3]), 255)
        self.assertGreater(int(rgba[20, 59, 3]), 240)
        np.testing.assert_array_equal(rgba[90, 59, :3], frame[90, 59])

        skin = np.array([[[80, 130, 190, 255]]], np.uint8)
        spill = np.array([[[20, 200, 30, 255]]], np.uint8)
        np.testing.assert_array_equal(motion._despill_green(skin), skin)
        self.assertLess(int(motion._despill_green(spill)[0, 0, 1]), 200)

        cleaned = motion._despill_green(rgba)
        quality = motion._color_fidelity_quality([rgba], [cleaned])
        self.assertTrue(quality["valid"], quality)
        changed = cleaned.copy()
        changed[40:60, 50:70, 1] = 0
        quality = motion._color_fidelity_quality([rgba], [changed])
        self.assertFalse(quality["valid"])

    def test_pose_aligned_matte_preserves_approved_source_rgb(self):
        matte = np.zeros((32, 32, 4), dtype=np.uint8)
        matte[8:24, 6:14, 3] = 255
        color = np.full((32, 32, 3), 210, dtype=np.uint8)
        color[10:26, 10:18] = (40, 70, 220)
        validation = np.zeros((32, 32, 4), dtype=np.uint8)
        validation[10:26, 10:18, 3] = 255

        source_points = {
            "neck": (8, 9), "root": (8, 20),
            "left_shoulder": (6, 11), "right_shoulder": (12, 11),
        }
        matte_pose = {"joints": {
            name: {"x": x, "y": y, "confidence": 1.0}
            for name, (x, y) in source_points.items()
        }}
        color_pose = {"joints": {
            name: {"x": x + 4, "y": y + 2, "confidence": 1.0}
            for name, (x, y) in source_points.items()
        }}

        processed, alignment, quality = motion._pose_aligned_color_authority(
            [matte], [matte_pose], [color], [color_pose], [validation])

        self.assertTrue(alignment["valid"])
        self.assertEqual(alignment["iou_min"], 1.0)
        self.assertTrue(quality["valid"])
        self.assertEqual(quality["authority"], "approved-original-source-rgb")
        self.assertFalse(quality["green_spill_checked"])
        np.testing.assert_array_equal(
            processed[0][14, 14, :3], color[14, 14])
        self.assertEqual(int(processed[0][0, 0, 3]), 0)
        np.testing.assert_array_equal(
            processed[0][0, 0, :3], np.zeros(3, dtype=np.uint8))

    def test_approved_walk_reprocess_preserves_idle_and_backs_up_walk(self):
        with tempfile.TemporaryDirectory() as root:
            avatar_dir = os.path.join(root, "avatar")
            motion_dir = os.path.join(avatar_dir, "motion")
            raw_dir = os.path.join(motion_dir, "raw")
            os.makedirs(raw_dir)
            original_source = os.path.join(raw_dir, "walk-original-source.mp4")
            matte_source = os.path.join(raw_dir, "walk-source.mp4")
            Path(original_source).write_bytes(b"original")
            Path(matte_source).write_bytes(b"matte")
            old_walk = {
                "source_loop": [30, 54],
                "pose_quality": {"valid": True},
                "sheets": [{"image": "walk-0.png"}],
                "poster": "walk-poster.png",
                "alpha_video": "walk-alpha.mov",
            }
            idle = {"poster": "idle-poster.png", "receipt": "keep"}
            Path(os.path.join(motion_dir, "motion.json")).write_text(json.dumps({
                "v": 5,
                "walk": old_walk,
                "idle": idle,
            }))
            old_assets = {
                "walk-0.png": b"old sheet",
                "walk-poster.png": b"old poster",
                "walk-alpha.mov": b"old preview",
            }
            for name, contents in old_assets.items():
                Path(os.path.join(motion_dir, name)).write_bytes(contents)

            new_walk = {
                "source_loop": [30, 54],
                "source_authority": "approved-original-source-rgb",
                "sheets": [{"image": "walk-0.png"}],
                "poster": "walk-poster.png",
                "alpha_video": "walk-alpha.mov",
            }

            def process(*arguments):
                stage = arguments[4]
                Path(os.path.join(stage, "walk-0.png")).write_bytes(b"new sheet")
                Path(os.path.join(stage, "walk-poster.png")).write_bytes(b"new poster")
                Path(os.path.join(stage, "walk-alpha.mov")).write_bytes(b"new preview")
                return new_walk

            with mock.patch.object(
                    motion, "_process_approved_original_walk", side_effect=process):
                result = motion.reprocess_approved_walk(
                    avatar_dir, original_source, matte_source=matte_source)

            installed = json.loads(
                Path(os.path.join(motion_dir, "motion.json")).read_text())
            self.assertEqual(installed["v"], motion.MOTION_VERSION)
            self.assertEqual(installed["idle"], idle)
            self.assertEqual(installed["walk"], new_walk)
            self.assertEqual(
                Path(os.path.join(motion_dir, "walk-0.png")).read_bytes(),
                b"new sheet")
            for name, contents in old_assets.items():
                self.assertEqual(
                    Path(os.path.join(result["backup"], name)).read_bytes(),
                    contents)
            backup_metadata = json.loads(
                Path(os.path.join(result["backup"], "motion.json")).read_text())
            self.assertEqual(backup_metadata["walk"], old_walk)
            self.assertEqual(backup_metadata["idle"], idle)

    def test_gray_studio_is_not_mistaken_for_green_screen(self):
        frame = np.full((100, 160, 3), 235, np.uint8)
        cv2.rectangle(frame, (60, 10), (100, 90), (25, 35, 180), thickness=cv2.FILLED)
        self.assertLess(motion._green_screen_purity(frame), 0.1)
        self.assertFalse(motion._is_green_screen([frame]))

    def test_idle_wall_contact_requires_back_and_raised_heel_alignment(self):
        bounds = [20, 10, 80, 180]

        def frame_with_heel(heel_x):
            frame = np.zeros((220, 130, 4), np.uint8)
            frame[38:90, 28:88, :3] = (25, 45, 180)
            frame[38:90, 28:88, 3] = 255
            frame[120:145, heel_x:82, :3] = (20, 22, 24)
            frame[120:145, heel_x:82, 3] = 255
            return frame

        aligned = [frame_with_heel(28) for _index in range(12)]
        quality = motion._wall_contact_quality(aligned, bounds)
        self.assertTrue(quality["available"])
        self.assertTrue(quality["valid"], quality)
        self.assertEqual(quality["back_contact_x"], quality["raised_heel_contact_x"])

        raised_heel_forward = [frame_with_heel(70) for _index in range(12)]
        quality = motion._wall_contact_quality(raised_heel_forward, bounds)
        self.assertFalse(quality["valid"])
        self.assertIn("raised heel", quality["reason"])
        grounded_quality = motion._edge_contact_quality(
            raised_heel_forward, bounds)
        self.assertTrue(grounded_quality["valid"], grounded_quality)

        source = [frame_with_heel(28) for _index in range(24)]
        source.extend(frame_with_heel(70) for _index in range(16))
        selected, start, end, quality = motion._select_idle_wall_loop(
            source, None, 12)
        self.assertEqual(start, 0)
        self.assertLess(end, 38)
        self.assertEqual(len(selected), end - start)
        self.assertTrue(quality["valid"], quality)

    def test_pose_reference_is_geometry_only(self):
        prompt = motion._idle_keyframe_prompt("existing outfit", True)
        self.assertIn("pose geometry only", prompt)
        self.assertIn("do not copy its person", prompt)
        self.assertIn("canonical FRONT full-body plate", prompt)
        self.assertIn("canonical HD head", prompt)
        self.assertIn("knee lifts to hip height", prompt)
        self.assertIn("Never substitute an upright tree pose", prompt)
        self.assertIn("canonical LEFT-EDGE pose", prompt)
        self.assertIn("raised shoe's heel", prompt)
        self.assertIn("same wall line", prompt)
        self.assertIn("raised heel drift forward", motion._idle_video_prompt())
        source = (ROOT / "studio" / "motion.py").read_text()
        self.assertNotIn("idle-pose-reference.png", source)
        self.assertIn('"retained": False', source)

    def test_pose_presets_and_custom_prompt_are_geometry_only(self):
        self.assertEqual(6, len(motion.IDLE_POSE_PRESETS))
        grounded = motion.resolve_idle_pose("folded-cross")
        self.assertEqual("edge", grounded["validation"])
        prompt = motion._idle_keyframe_prompt(
            "existing outfit", False, grounded)
        self.assertIn("arms folded calmly", prompt)
        self.assertIn("controls geometry only", prompt)
        self.assertIn("never identity, wardrobe, styling, age, or gender", prompt)

        direction = "dance an upbeat little routine for the audience"
        custom = motion.resolve_idle_pose("custom", direction)
        self.assertEqual("custom", custom["id"])
        self.assertEqual(direction, custom["prompt"])
        # A custom act is FREE: the user's text leads, the loop contract
        # carries the seam, and the wall-lean wrapper that drowned "dance"
        # into a lean must never wrap it again.
        self.assertEqual("free", custom["validation"])
        video = motion._idle_video_prompt(custom)
        self.assertIn(direction, video)
        self.assertIn("EXACT first frame and the EXACT final frame", video)
        self.assertNotIn("wall", video)
        self.assertNotIn("living hold", video)
        keyframe = motion._idle_keyframe_prompt("outfit", False, custom)
        self.assertIn(direction, keyframe)
        self.assertNotIn("LEFT-EDGE pose", keyframe)
        with self.assertRaisesRegex(ValueError, "at least 12 characters"):
            motion.resolve_idle_pose("custom", "lean")
        with self.assertRaisesRegex(ValueError, "unknown edge-idle pose"):
            motion.resolve_idle_pose("unknown")

    def test_alpha_frames_pack_into_transparent_runtime_atlas(self):
        frames = []
        for offset in range(4):
            frame = np.zeros((motion.TARGET_HEIGHT, motion.TARGET_WIDTH, 4), np.uint8)
            frame[40:120, 30 + offset:90 + offset, :3] = (20, 40, 220)
            frame[40:120, 30 + offset:90 + offset, 3] = 255
            frames.append(frame)
        with tempfile.TemporaryDirectory() as directory:
            sheets = motion._pack_sheets(frames, directory, "walk")
            atlas = cv2.imread(
                os.path.join(directory, sheets[0]["image"]), cv2.IMREAD_UNCHANGED)
        self.assertEqual(atlas.shape[2], 4)
        self.assertEqual(int(atlas[50, 40, 3]), 255)
        self.assertEqual(int(atlas[10, 10, 3]), 0)

    def test_temporal_repair_preserves_motion_and_repairs_dropout(self):
        frames = [np.zeros((50, 50, 4), np.uint8) for _index in range(3)]
        for frame in frames:
            frame[8:42, 18:32, :3] = (30, 40, 180)
            frame[8:42, 18:32, 3] = 255
        frames[0][24:42, 10:18, :3] = (30, 40, 180)
        frames[0][24:42, 10:18, 3] = 255
        frames[2][24:42, 10:18, :3] = (30, 40, 180)
        frames[2][24:42, 10:18, 3] = 255
        frames[1][10:18, 32:42, :3] = (30, 40, 180)
        frames[1][10:18, 32:42, 3] = 255
        hole = np.array([[22, 27], [28, 27], [25, 34]], np.int32)
        for frame in frames:
            frame[20:24, 22:28, 3] = 96
            cv2.fillConvexPoly(frame, hole, (245, 245, 245, 0))
        repaired = motion._stabilise_segmented(frames)
        self.assertEqual(int(repaired[1][30, 12, 3]), 255)
        self.assertEqual(int(repaired[1][14, 36, 3]), 255)
        self.assertEqual(int(repaired[1][22, 24, 3]), 255)
        self.assertEqual(int(repaired[1][30, 25, 3]), 255)
        self.assertLess(float(repaired[1][30, 25, :3].mean()), 180)

    def test_motion_aligned_repair_restores_rgb_without_background_trails(self):
        frames = []
        poses = []
        for index, shift in enumerate((0, 5, 10)):
            frame = np.zeros((220, 100, 4), np.uint8)
            frame[20:195, 40 + shift:60 + shift, :3] = (25, 45, 175)
            frame[20:195, 40 + shift:60 + shift, 3] = 255
            frames.append(frame)
            poses.append(self._synthetic_pose(50 + shift, -math.pi / 2 + index * 0.15))
        frames[1][95:105, 50:56, :3] = (245, 245, 245)
        frames[1][95:105, 50:56, 3] = 0

        repaired = motion._stabilise_segmented(frames, poses)

        self.assertEqual(int(repaired[1][100, 53, 3]), 255)
        self.assertLess(float(repaired[1][100, 53, :3].mean()), 120)
        self.assertEqual(int(repaired[1][100, 40, 3]), 0)

    def test_pose_cycle_quality_rejects_incomplete_arm_swing(self):
        complete = [
            self._synthetic_pose(50 + index * 4, -math.pi / 2 + 2 * math.pi * index / 24)
            for index in range(25)
        ]
        quality = motion._pose_cycle_metrics(complete, 0, 24)
        self.assertTrue(quality["available"])
        self.assertTrue(quality["valid"], quality)
        self.assertGreaterEqual(quality["arm_crossings"], 2)
        self.assertLess(quality["contralateral_correlation"], -0.9)
        self.assertTrue(quality["sides"]["left"]["arm_available"])
        self.assertTrue(quality["sides"]["right"]["arm_available"])

        raised_foot = [
            self._synthetic_pose(50 + index * 4, -math.pi / 2 + 2 * math.pi * index / 24)
            for index in range(25)
        ]
        for pose in raised_foot:
            pose["joints"]["right_ankle"]["y"] = 85
        quality = motion._pose_cycle_metrics(raised_foot, 0, 24)
        self.assertFalse(quality["valid"])
        self.assertIn("swing foot lifts too high", quality["reason"])

        # Wrist height is taste, not physics: raised hands never invalidate a
        # window, they only cost a style penalty so lower-handed windows win
        # when the footage offers both.
        raised_hands = [
            self._synthetic_pose(50 + index * 4, -math.pi / 2 + 2 * math.pi * index / 24)
            for index in range(25)
        ]
        for pose in raised_hands:
            pose["joints"]["left_wrist"]["y"] = 55
            pose["joints"]["right_wrist"]["y"] = 55
        quality = motion._pose_cycle_metrics(raised_hands, 0, 24)
        self.assertTrue(quality["valid"], quality)
        self.assertGreater(quality["style_penalty"], 0)
        self.assertEqual(0, motion._pose_cycle_metrics(complete, 0, 24)["style_penalty"])

        one_sided = [
            self._synthetic_pose(50 + index * 4, -math.pi / 2 + 2 * math.pi * index / 24)
            for index in range(25)
        ]
        for pose in one_sided:
            pose["joints"]["left_wrist"]["confidence"] = 0
            pose["joints"]["left_elbow"]["confidence"] = 0
        quality = motion._pose_cycle_metrics(one_sided, 0, 24)
        self.assertFalse(quality["valid"])
        self.assertIn("left arm tracking unavailable", quality["reason"])
        self.assertTrue(quality["sides"]["right"]["arm_available"])

        incomplete = [
            self._synthetic_pose(50 + index * 4, -math.pi / 2 + math.pi * index / 24)
            for index in range(25)
        ]
        quality = motion._pose_cycle_metrics(incomplete, 0, 24)
        self.assertTrue(quality["available"])
        self.assertFalse(quality["valid"])
        self.assertIn("arm", quality["reason"])

    def test_extremity_gate_rejects_a_disappearing_hand(self):
        poses = [
            self._synthetic_pose(50, -math.pi / 2 + 2 * math.pi * index / 24)
            for index in range(25)
        ]
        frames = [np.zeros((220, 100, 4), np.uint8) for _index in range(25)]
        joints = ("left_wrist", "right_wrist", "left_ankle", "right_ankle")
        for frame, pose in zip(frames, poses):
            alpha = np.zeros(frame.shape[:2], np.uint8)
            for joint in joints:
                point = pose["joints"][joint]
                cv2.circle(
                    alpha,
                    (round(point["x"]), round(point["y"])),
                    5,
                    255,
                    thickness=cv2.FILLED,
                )
            frame[:, :, 3] = alpha
        quality = motion._extremity_integrity(frames, poses, 0, 24)
        self.assertTrue(quality["valid"], quality)

        point = poses[12]["joints"]["left_wrist"]
        alpha = frames[12][:, :, 3].copy()
        cv2.circle(
            alpha,
            (round(point["x"]), round(point["y"])),
            7,
            0,
            thickness=cv2.FILLED,
        )
        frames[12][:, :, 3] = alpha
        quality = motion._extremity_integrity(frames, poses, 0, 24)
        self.assertFalse(quality["valid"])
        self.assertEqual(quality["missing_frames"], 1)

    def test_source_trajectory_drives_each_continuous_frame(self):
        anchors = np.arange(25, dtype=np.float64) * 5 + 100
        profile = motion._trajectory_profile(anchors, 0, 24, 24, 0.5)
        self.assertIsNotNone(profile)
        self.assertEqual(profile["speed_method"], "source-root-trajectory")
        self.assertTrue(profile["continuous_source_frames"])
        self.assertAlmostEqual(profile["ground_speed"], 60, delta=0.1)
        self.assertEqual(len(profile["travel_offsets"]), 24)
        self.assertTrue(all(
            left <= right for left, right in zip(
                profile["travel_offsets"], profile["travel_offsets"][1:])))

    def test_gait_metrics_convert_stride_to_ground_speed(self):
        frames = []
        for separation in (42, 52, 62, 52):
            frame = np.zeros((motion.TARGET_HEIGHT, motion.TARGET_WIDTH, 4), np.uint8)
            frame[28:330, 112:144, :3] = (30, 40, 180)
            frame[28:330, 112:144, 3] = 255
            left = 128 - separation // 2
            right = 128 + separation // 2
            frame[300:350, left - 8:left + 8, 3] = 255
            frame[300:350, right - 8:right + 8, 3] = 255
            frames.append(frame)
        metrics = motion._gait_metrics(frames, 24, [58, 20, 140, 330])
        self.assertAlmostEqual(metrics["cycle_seconds"], 4 / 24, places=3)
        self.assertGreater(metrics["stride_pixels"], 100)
        self.assertAlmostEqual(
            metrics["ground_speed"],
            metrics["stride_pixels"] / metrics["cycle_seconds"],
            delta=0.2,
        )

    def test_normalised_frames_drop_near_zero_alpha_halo(self):
        frame = np.zeros((120, 80, 4), np.uint8)
        frame[14:108, 16:66, :3] = 245
        frame[14:108, 16:66, 3] = 10
        frame[18:104, 20:62, :3] = (25, 45, 180)
        frame[18:104, 20:62, 3] = 255
        normalised, _bounds = motion._normalise_frames([frame, frame.copy()])
        alpha = normalised[0][:, :, 3]
        self.assertEqual(int(((alpha > 0) & (alpha < 16)).sum()), 0)
        self.assertEqual(int(normalised[0][:, :, :3][alpha == 0].max()), 0)

        sample = np.zeros((2, 2, 4), np.uint8)
        sample[:, :, :3] = (0, 255, 0)
        sample[0, 0] = (0, 0, 240, 255)
        resized = motion._resize_rgba_premultiplied(sample, (1, 1))
        self.assertGreater(int(resized[0, 0, 2]), 230)
        self.assertEqual(int(resized[0, 0, 1]), 0)


class BodyProviderTests(unittest.TestCase):
    def test_head_edit_is_hd_head_only_and_inherits_provider(self):
        provider = {
            "name": "open_ai", "route": "open_ai/create", "model": "gpt-image-2",
        }
        command = generate._head_command(
            provider, "/portrait.png", "/out", "high")
        self.assertEqual(command[3], "open_ai/create")
        self.assertEqual(command[command.index("--mode") + 1], "edit")
        self.assertEqual(command[command.index("--input_fidelity") + 1], "high")
        self.assertEqual(command[command.index("--size") + 1], "1024x1024")
        self.assertNotIn("--credentials", command)
        self.assertNotIn("--model", command)
        self.assertIn("No shoulders, collarbones, chest, torso", generate.HEAD_PROMPT)
        self.assertIn("no clothing", generate.HEAD_PROMPT.lower())

        gemini = generate._head_command({
            "name": "gemini", "route": "gemini/create",
            "model": "google/gemini-3-pro-image",
        }, "/portrait.png", "/out", "high")
        self.assertEqual(gemini[3], "gemini/create")
        self.assertEqual(gemini[gemini.index("--aspectRatio") + 1], "1:1")
        self.assertEqual(gemini[gemini.index("--imageSize") + 1], "2K")

    def test_body_prompt_uses_editable_direction_with_decency_floor(self):
        custom = "A scarlet tailored suit with restrained gold hardware."
        prompt = body._prompt({
            "style": "editorial", "pose": "confident", "prompt": custom,
        })
        self.assertIn(custom, prompt)
        self.assertIn("DECENCY FLOOR", prompt)
        self.assertIn("proper, decent, and intentionally fashionable", prompt)
        self.assertNotIn(body.DEFAULT_BODY_PROMPT, prompt)

        preset = body._prompt({"style": "photorealistic", "pose": "relaxed"})
        self.assertIn(body.DEFAULT_BODY_PROMPT, preset)
        self.assertIn("opaque", body.DEFAULT_BODY_PROMPT)

        side = body._prompt(
            {"style": "photorealistic", "pose": "relaxed"}, view="side")
        back = body._prompt(
            {"style": "photorealistic", "pose": "relaxed"}, view="back")
        self.assertIn("canonical RIGHT-SIDE view", side)
        self.assertIn("approved front body plate", side)
        self.assertIn("canonical BACK view", back)
        self.assertIn("face remains completely out of view", back)
        self.assertIn("never a triptych", side)

    def test_body_generation_prefers_canonical_head_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            keyframe = os.path.join(directory, "keyframe.png")
            head = os.path.join(directory, "head.png")
            Path(keyframe).write_bytes(b"keyframe")
            self.assertEqual(body._identity_reference(directory), keyframe)
            Path(head).write_bytes(b"head")
            self.assertEqual(body._identity_reference(directory), head)

    def test_body_build_publishes_front_side_and_back_transactionally(self):
        provider = {
            "name": "open_ai", "route": "open_ai/create",
            "title": "OpenAI", "model": "gpt-image-2",
        }
        commands = []
        random = np.random.default_rng(7)

        def generate(command, **_arguments):
            commands.append(command)
            output_dir = command[command.index("--output_dir") + 1]
            file_name = command[command.index("--file_name") + 1]
            generated = os.path.join(output_dir, file_name + ".png")
            cv2.imwrite(
                generated,
                random.integers(0, 256, (180, 120, 3), dtype=np.uint8))
            return mock.Mock(
                returncode=0, stderr="",
                stdout=json.dumps({"paths": [generated]}))

        def render(_source, destination, **_arguments):
            plate = np.zeros((240, 160, 4), np.uint8)
            plate[12:228, 38:122, :3] = (30, 50, 180)
            plate[12:228, 38:122, 3] = 255
            return bool(cv2.imwrite(destination, plate))

        def head_mask(_image, _landmarks, destination):
            mask = np.zeros((240, 160, 4), np.uint8)
            mask[20:90, 55:105, 3] = 255
            cv2.imwrite(destination, mask)

        with tempfile.TemporaryDirectory() as directory:
            keyframe = np.full((256, 256, 3), 127, np.uint8)
            cv2.imwrite(os.path.join(directory, "keyframe.png"), keyframe)
            cv2.imwrite(os.path.join(directory, "head.png"), keyframe)
            landmarks = np.zeros((478, 2), np.float32)
            with (
                    mock.patch.object(body, "default_provider", return_value=provider),
                    mock.patch.object(body.subprocess, "run", side_effect=generate),
                    mock.patch.object(body.cutout, "render", side_effect=render),
                    mock.patch.object(
                        body, "_face_transform",
                        return_value=(
                            np.array([[1, 0, 0], [0, 1, 0]], np.float32),
                            {"scale": 1.0}, landmarks)),
                    mock.patch.object(body, "_head_mask", side_effect=head_mask),
                    mock.patch.object(body, "_seam_tone_match")):
                metadata = body.build(
                    directory,
                    {"style": "photorealistic", "pose": "relaxed"},
                    log=lambda _message: None)

            body_dir = os.path.join(directory, "body")
            self.assertEqual(len(commands), 3)
            self.assertEqual(metadata["v"], 3)
            self.assertEqual(list(metadata["views"]), ["front", "side", "back"])
            self.assertEqual(metadata["motion_reference"]["walk_view"], "side")
            for view in body.BODY_VIEWS:
                self.assertTrue(os.path.isfile(
                    os.path.join(body_dir, f"body-{view}.png")))
                self.assertTrue(os.path.isfile(os.path.join(
                    body_dir, metadata["views"][view]["source"])))
            self.assertTrue(os.path.isfile(os.path.join(body_dir, "body.png")))
            for command in commands[1:]:
                reference_index = command.index("--reference_images")
                references = command[
                    reference_index + 1:command.index("--output_dir")]
                self.assertEqual(len(references), 2)
                self.assertTrue(references[0].endswith("head.png"))
                self.assertIn("source-front", references[1])

    def test_body_studio_exposes_prefilled_editable_prompt(self):
        settings = (ROOT / "web" / "settings.html").read_text()
        server = (ROOT / "server" / "app.py").read_text()
        self.assertIn('id="body-prompt"', settings)
        self.assertIn('id="body-prompt-reset"', settings)
        self.assertIn("BODY_STATE.default_prompt", settings)
        self.assertIn("wardrobe.cached_prompt(directory)", server)
        self.assertIn("wardrobe.preset_prompt()", server)
        self.assertIn('"/api/avatar/body/prompt"', server)
        self.assertIn("tailorBodyPrompt", settings)
        self.assertIn('data-body-view="front"', settings)
        self.assertIn('data-body-view="side"', settings)
        self.assertIn('data-body-view="back"', settings)
        self.assertIn("generated side body automatically", settings)
        self.assertIn('data-idle-pose="back-heel"', settings)
        self.assertIn('data-idle-pose="side-cross"', settings)
        self.assertIn("High heel touch", settings)
        self.assertIn("knee raised · heel to wall", settings)
        self.assertIn("Low heel touch", settings)
        self.assertIn("heel lifted behind", settings)
        for style in ("office", "runway", "stroll", "power", "promenade", "cartwheel"):
            self.assertIn(f'data-walk-style="{style}"', settings)
        self.assertIn('id="body-walk-generate"', settings)
        self.assertIn('id="body-idle-generate"', settings)
        # The single Remove buttons became per-set libraries.
        self.assertNotIn('id="body-walk-remove"', settings)
        self.assertNotIn('id="body-idle-remove"', settings)
        self.assertIn('id="body-walk-sets"', settings)
        self.assertIn('id="body-idle-sets"', settings)
        self.assertIn('id="body-set-list"', settings)
        self.assertIn('"/api/avatar/motion/set/activate"', server)
        self.assertIn('"/api/avatar/motion/set/remove"', server)
        self.assertIn('"/api/avatar/body/set/activate"', server)
        self.assertIn('"/api/avatar/body/set/remove"', server)
        self.assertNotIn('id="body-motion-generate"', settings)
        self.assertIn('id="body-motion-prompt"', settings)
        self.assertNotIn('id="body-motion-reference"', settings)
        self.assertIn("walk_style", settings)
        self.assertIn("pose_prompt", settings)

    @mock.patch("studio.body.subprocess.run")
    def test_saved_selection_wins_over_static_provider_default(self, run):
        run.side_effect = [
            mock.Mock(returncode=0, stdout=json.dumps({
                "selected": "image_create|open_ai",
            }), stderr=""),
            mock.Mock(returncode=0, stdout=json.dumps({
                "title": "OpenAI",
                "modelName": "gpt-image-2",
                "description": "OpenAI image generation",
            }), stderr=""),
        ]
        provider = body.default_provider()
        self.assertEqual(provider["name"], "open_ai")
        self.assertEqual(provider["model"], "gpt-image-2")
        self.assertEqual(provider["route"], "open_ai/create")
        self.assertEqual(
            run.call_args_list[0].args[0][-2:], ["--includes", "selected"])
        self.assertEqual(
            run.call_args_list[1].args[0][-3:],
            ["title", "modelName", "description"])

    @mock.patch("studio.body.subprocess.run")
    def test_video_default_reads_saved_xai_selection(self, run):
        run.side_effect = [
            mock.Mock(returncode=0, stdout=json.dumps({
                "selected": "video_create|x_ai",
            }), stderr=""),
            mock.Mock(returncode=0, stdout=json.dumps({
                "title": "xAI",
                "modelName": "grok-imagine-video",
                "description": "xAI video generation",
            }), stderr=""),
        ]
        provider = body.default_video_provider()
        self.assertEqual(provider["name"], "x_ai")
        self.assertEqual(provider["model"], "grok-imagine-video")

    def test_flash_lite_uses_supported_one_k_output(self):
        provider = {
            "route": "gemini/create",
            "model": "google/gemini-3.1-flash-lite-image",
        }
        command = body._provider_command(provider, "/face.png", "/out", "prompt")
        self.assertEqual(command[command.index("--imageSize") + 1], "1K")
        self.assertNotIn("--credentials", command)

    def test_body_command_accepts_identity_and_front_body_references(self):
        provider = {"route": "open_ai/create", "model": "gpt-image-2"}
        command = body._provider_command(
            provider, ["/head.png", "/front.png"], "/out", "prompt",
            file_name="body-source-side")
        reference_index = command.index("--reference_images")
        self.assertEqual(
            command[reference_index + 1:reference_index + 3],
            ["/head.png", "/front.png"])
        self.assertEqual(
            command[command.index("--file_name") + 1], "body-source-side")

    def test_motion_context_prefers_generated_side_body_for_horizon_walk(self):
        with tempfile.TemporaryDirectory() as directory:
            body_dir = os.path.join(directory, "body")
            os.makedirs(body_dir)
            Path(directory, "head.png").write_bytes(b"head")
            Path(body_dir, "source-front.png").write_bytes(b"front")
            Path(body_dir, "source-side.png").write_bytes(b"side")
            Path(body_dir, "body.json").write_text(json.dumps({
                "views": {
                    "front": {"source": "source-front.png"},
                    "side": {"source": "source-side.png"},
                },
                "options": {"prompt": "tailored look"},
            }))
            image_provider = {
                "command_key": "image_create|open_ai", "name": "open_ai",
                "route": "open_ai/create", "title": "OpenAI", "model": "image",
            }
            video_provider = {
                "command_key": "video_create|x_ai", "name": "x_ai",
                "title": "xAI", "model": "video",
            }
            with (
                    mock.patch.object(body, "default_provider", return_value=image_provider),
                    mock.patch.object(body, "default_video_provider", return_value=video_provider)):
                context = motion._build_context(directory, None)
        self.assertTrue(context["body_sources"]["walk"].endswith("source-side.png"))
        self.assertTrue(context["body_sources"]["idle"].endswith("source-front.png"))
        self.assertEqual(context["body_reference_views"]["walk"], "side")

    def test_gemini_pro_keeps_two_k_output(self):
        provider = {
            "route": "gemini/create",
            "model": "google/gemini-3-pro-image",
        }
        command = body._provider_command(provider, "/face.png", "/out", "prompt")
        self.assertEqual(command[command.index("--imageSize") + 1], "2K")

    def test_provider_reported_path_is_accepted_outside_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            image = os.path.join(directory, "provider-output.png")
            cv2.imwrite(image, np.full((80, 80, 3), 127, np.uint8))
            with open(image, "ab") as handle:
                handle.write(b"x" * 5000)
            result = body._generated_file(
                os.path.join(directory, "empty"),
                0,
                json.dumps({"paths": [image]}),
            )
            self.assertEqual(result, image)


    def test_body_studio_surfaces_motion_files_with_native_save_as(self):
        settings = (ROOT / "web" / "settings.html").read_text()
        server = (ROOT / "server" / "app.py").read_text()
        main = (ROOT / "electron" / "main.cjs").read_text()
        preload = (ROOT / "electron" / "preload.cjs").read_text()
        self.assertIn('id="body-mode-tabs"', settings)
        self.assertIn('id="body-motion-library"', settings)
        self.assertIn('id="body-motion-canvas"', settings)
        self.assertIn('data-motion-save', settings)
        self.assertIn('function startBodyMotionCycle', settings)
        self.assertIn('function saveBodyMotionAsset', settings)
        self.assertIn('"motion_assets": _motion_asset_catalog', server)
        self.assertIn("vivieen:save-motion-asset", main)
        self.assertIn("resolveMotionAsset", main)
        self.assertIn("fs.realpathSync", main)
        self.assertIn("showSaveDialog", main)
        self.assertIn("copyFile", main)
        self.assertIn("saveMotionAsset", preload)


class PetMatteTests(unittest.TestCase):
    def test_edge_decontamination_uses_subject_color(self):
        image = np.zeros((40, 40, 4), np.uint8)
        image[6:34, 6:34, :3] = (245, 245, 245)
        image[6:34, 6:34, 3] = 120
        image[8:32, 8:32, :3] = (30, 40, 170)
        image[8:32, 8:32, 3] = 255
        image[5:35, 20, :3] = (10, 200, 30)
        image[5:35, 20, 3] = 255
        cleaned = cutout._decontaminate_edges(image.copy())
        self.assertLess(float(cleaned[6:34, 6:34, :3].mean()), 180)
        self.assertGreater(int(cleaned[20, 21, 2]), 150)
        np.testing.assert_array_equal(
            cleaned[5:35, 20, :3], image[5:35, 20, :3])

    def test_head_mask_excludes_shoulders_below_chin(self):
        canvas = np.full((240, 240, 4), 255, np.uint8)
        landmarks = np.zeros((478, 2), np.float32)
        angles = np.linspace(0, 2 * np.pi, len(face.FACE_OVAL), endpoint=False)
        ellipse = np.column_stack((120 + 48 * np.cos(angles), 92 + 65 * np.sin(angles)))
        landmarks[face.FACE_OVAL] = ellipse
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "head-mask.png")
            body._head_mask(canvas, landmarks, path)
            alpha = cv2.imread(path, cv2.IMREAD_UNCHANGED)[:, :, 3]
        self.assertGreater(int(alpha[80, 120]), 245)
        # The neck gate RAMPS in over 0.28 face-heights instead of switching
        # in one row - the binary switch drew a horizontal border line at
        # chin height through the side hair (carol, 2026-08-01). Mid-ramp a
        # side column is partially kept; once engaged it is excluded.
        self.assertLess(int(alpha[200, 35]), 8)
        self.assertGreater(int(alpha[175, 35]), 40)
        self.assertLess(int(alpha[175, 35]), 220)
        self.assertGreater(int(alpha[165, 120]), 20)

    def test_live_talk_does_not_answer_its_own_voice(self):
        # Owner, 2026-08-02: speaker bleed re-entered the mic and the
        # provider transcribed her own reply and answered it. While her
        # audio is playing (plus a 350ms room tail), mic frames go up as
        # SILENCE - the stream stays continuous for the server's VAD -
        # unless the mic is decisively louder than bleed (rms >= 0.09),
        # which is a real interruption and passes through for barge-in.
        renderer = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("session.sources.size>0||performance.now()<session.echoGuardUntil",
                      renderer)
        self.assertIn("if(guarded&&session.lastRms<0.09){", renderer)
        self.assertIn("session.ws.send(new ArrayBuffer(data.byteLength));return;",
                      renderer)
        # The guard opens when her audio ARRIVES - covering the playback
        # race - and holds through a 450ms room tail.
        self.assertIn("session.echoGuardUntil=Math.max(", renderer)

    def test_live_talk_rides_the_existing_speech_machinery(self):
        # Realtime conversation (2026-08-01): mic PCM streams to the server
        # bridge as binary frames; provider audio plays through the SAME
        # actx + analyser as turn-based replies, so speaking, level() and
        # the byEnergy mouth work unmodified. Barge-in flushes the queue;
        # the menu toggles it; EnConvo coupling and live talk are mutually
        # exclusive over the voice channel.
        renderer = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("async function startLiveTalk()", renderer)
        self.assertIn("addModule('/live-worklet.js')", renderer)
        worklet = (ROOT / "web" / "live-worklet.js").read_text(encoding="utf-8")
        self.assertIn("registerProcessor('viv-live-capture'", worklet)
        app_src = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/live-worklet.js")', app_src)
        self.assertIn("'/live/voice'", renderer)
        self.assertIn("localSource=src;speaking=true;track=[];", renderer)
        self.assertIn("function liveFlush(session)", renderer)
        self.assertIn("De-couple from EnConvo first", renderer)
        self.assertIn("SHELL.onLiveToggle(()=>{void startLiveTalk();});", renderer)
        preload = (ROOT / "electron" / "preload.cjs").read_text(encoding="utf-8")
        self.assertIn("'vivieen:live-toggle'", preload)
        main = (ROOT / "electron" / "main.cjs").read_text(encoding="utf-8")
        self.assertIn("liveTalkActive ? 'End live talk' : 'Live talk'", main)
        self.assertIn("'vivieen:live-active'", main)
        self.assertIn("if(event.key==='Escape'&&LIVE)stopLiveTalk('ended');",
                      renderer)
        # The auth token must reach WEBSOCKET upgrades too: comparing full
        # origins left ws:// unmatched and both realtime endpoints 403'd.
        self.assertIn("target.protocol === 'http:' || target.protocol === 'ws:'",
                      main)
        settings = (ROOT / "web" / "settings.html").read_text(encoding="utf-8")
        self.assertIn('id="blk-live"', settings)
        self.assertIn("collectLiveBlock();", settings)
        # Voice pickers for both providers: xAI's five built-ins served
        # statically, ElevenLabs voices fetched through the server (the
        # key never reaches the browser); an ElevenLabs voice change
        # recreates the agent, which bakes its voice in at creation.
        self.assertIn("function fillLiveVoices", settings)
        # And chosen by EAR: a preview button per provider plays a short
        # sample through the server (xAI TTS REST; ElevenLabs ships
        # preview clips with its roster).
        self.assertIn("function playLivePreview", settings)
        self.assertIn('@app.get("/api/live/voice-preview")',
                      (ROOT / "server" / "app.py").read_text(encoding="utf-8"))
        app_src = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/api/live/voices")', app_src)
        self.assertIn("XAI_LIVE_VOICES", app_src)
        self.assertIn('live["eleven_agent_id"] = ""\n' if False else
                      'if "eleven_voice_id" in live and live["eleven_voice_id"] != previous_voice:',
                      app_src)

    def test_standby_sips_power_instead_of_gulping(self):
        # Power audit 2026-08-01: the renderer burned 115% CPU (+20% GPU
        # helper) while she just stood there - the full compositing
        # pipeline ran at ProMotion's 120Hz, a GPU->CPU getImageData
        # readback fired at 30Hz under a stationary cursor, control rects
        # forced a layout flush every frame, and the shell pushed 31
        # pointer IPCs a second for an unmoved point. Standby now paces to
        # 30fps (lively states get 60), the readback and rect reports are
        # cached/throttled, and a stationary cursor sends nothing.
        renderer = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function frameInterval(now)", renderer)
        self.assertIn("if(now-lastFrameAt<frameInterval(now))return;", renderer)
        self.assertIn("if(lively)return 1000/(powerOnBattery?30:60)-2;", renderer)
        self.assertIn("if(still&&now-hitSampleAt<250){", renderer)
        self.assertIn("if(now-lastRectsAt>140){lastRectsAt=now;reportControlRects();}",
                      renderer)
        main = (ROOT / "electron" / "main.cjs").read_text(encoding="utf-8")
        self.assertIn("const pointerLastSent = { pet: null, buddy: null };", main)
        self.assertIn("|| Date.now() - previous.at > 250;", main)
        self.assertIn("if (sendNow) window.webContents.send('vivieen:pet-pointer', localPoint);",
                      main)
        # Round 3 (owner: "burning too much battery still"): while a
        # looping alpha-WebM take is on screen (edge idle, stillness, a
        # move show) the <video> element shows DIRECTLY - compositor
        # playback, no per-frame canvas copy - and the loop drops to 10fps
        # bookkeeping; hit-tests use the video's rect. Walk stays on
        # canvas (stride-synced to window travel). Unattended standby
        # fades to 15fps after a minute; on battery every cap halves.
        self.assertIn("function showDomVideo(kind,edgeOverride)", renderer)
        self.assertIn("function hideDomVideo()", renderer)
        self.assertIn("if(motionKind==='idle'&&showDomVideo('idle'))return;",
                      renderer)
        self.assertIn("if(showDomVideo('idle',PET_SIDE))return;", renderer)
        self.assertIn("if(!clipOwns)renderFaceSurface(", renderer)
        self.assertIn("if(domVideo.el&&!petHit)return 1000/10-2;", renderer)
        self.assertIn("const unattended=now-lastEngagedAt>60000;", renderer)
        self.assertIn("powerOnBattery?(unattended?10:20):(unattended?15:30)",
                      renderer)
        self.assertIn("domVideo.el.getBoundingClientRect()", renderer)
        main_source = (ROOT / "electron" / "main.cjs").read_text(encoding="utf-8")
        self.assertIn("powerMonitor.isOnBattery()", main_source)
        self.assertIn("powerMonitor.on('on-battery', broadcastState);", main_source)
        # Second pass, the structural half (115% -> ~25% measured): the
        # face surfaces rasterise at the scale the compositor actually
        # samples instead of always-1024 (consumers sample them back into
        # keyframe space, so drawing coordinates are untouched), and
        # assets decode once into GPU-resident ImageBitmaps.
        self.assertIn("faceScaleHint*1.15", renderer)
        self.assertIn("faceOutScale=surfaceW/ref.width;", renderer)
        self.assertIn("0,0,FACE_KEY.w,FACE_KEY.h)", renderer)
        self.assertIn("createImageBitmap(i).then(res).catch(()=>res(i));", renderer)

    def test_chat_placeholder_fits_the_field_it_sits_in(self):
        # The roam-sized chat bar is far narrower than the docked one, and
        # the full placeholder clipped mid-sentence (owner screenshot,
        # 2026-08-01). The placeholder now adapts to the field's width.
        renderer = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function fitTxtPlaceholder()", renderer)
        self.assertIn("'Hold head to talk…'", renderer)
        self.assertIn("'Type…'", renderer)
        self.assertIn("fitTxtPlaceholder();", renderer)

    def test_roam_and_edge_idle_never_leave_the_screen(self):
        # Owner rule 2026-08-01: the edge idle held at a screen corner must
        # never poke out of the screen - at most the figure fills the work
        # area top to bottom, feet above the Dock (the window is bottom-
        # anchored, so an over-tall roam zoom pushed the HEAD past the
        # screen top). Every path that sizes a roam window - pet start,
        # pet zoom resize, buddy start, and the docked stillness idle -
        # clamps through the same helper, width scaling with height.
        main = (ROOT / "electron" / "main.cjs").read_text(encoding="utf-8")
        self.assertIn("function clampRoamSizeToArea(size, area)", main)
        self.assertIn("const scale = area.height / size.height;", main)
        self.assertEqual(main.count("clampRoamSizeToArea("), 5)
        self.assertIn("clampRoamSizeToArea(petRoamSize(zoom), area)", main)
        # (the stillness dock later moved to roam scale - see
        # test_stillness_idle_docks_small_and_restores_on_wake)

    def test_accessibility_failures_open_the_settings_pane_directly(self):
        # Owner request 2026-08-01: the old bubble described the System
        # Settings path in words and left the user to walk it. Both voice-
        # key failure paths now deep-link straight to Privacy & Security →
        # Accessibility, where the only remaining step is the switch.
        main = (ROOT / "electron" / "main.cjs").read_text(encoding="utf-8")
        self.assertIn("function openAccessibilitySettings()", main)
        self.assertIn(
            "x-apple.systempreferences:com.apple.preference.security", main)
        self.assertIn("?Privacy_Accessibility", main)
        self.assertEqual(main.count("openAccessibilitySettings();"), 2)
        self.assertIn("flip the switch next '\n        + 'to Vivieen", main)

    def test_coupling_is_a_plain_toggle_with_no_pitch(self):
        # The EnConvo explainer (card window, narration, first-launch
        # trigger) was removed on 2026-08-01 - "keep the app simple and
        # clean". Coupling is a plain toggle again from every entry point,
        # and none of the intro machinery may linger.
        main = (ROOT / "electron" / "main.cjs").read_text(encoding="utf-8")
        self.assertIn("click: () => setEnconvoMonitoring(!followingEnconvo)",
                      main)
        self.assertIn("click: (item) => setEnconvoMonitoring(item.checked)",
                      main)
        self.assertIn("(_event, value) => setEnconvoMonitoring(value)", main)
        for relic in ("coupleToEnconvo", "showEnconvoIntroWindow",
                      "maybeShowLaunchIntro", "enconvoIntroSeen",
                      "enconvo-intro.m4a", "intro-preload"):
            self.assertNotIn(relic, main)
        self.assertNotIn('@app.get("/intro")',
                         (ROOT / "server" / "app.py").read_text(encoding="utf-8"))
        self.assertFalse((ROOT / "web" / "intro.html").exists())
        self.assertFalse((ROOT / "electron" / "intro-preload.cjs").exists())

    def test_cursor_head_follow_is_a_whisper_not_a_swing(self):
        # Tuned DOWN twice on the live desktop (2026-08-01): 12/5/1.0 swung
        # the whole head with the pointer, and even 4.5/2/0.4 slid the head
        # visibly against the static body plate - the mask contour itself
        # read as moving. The iris carries the glance; the head barely
        # breathes toward it.
        renderer = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("h.x+=attend*cursorDir.x*1.4;", renderer)
        self.assertIn("h.y+=attend*cursorDir.y*0.6;", renderer)
        self.assertIn("h.r+=attend*cursorDir.x*0.12;", renderer)

    def test_click_through_ships_enabled_for_existing_installs(self):
        # Owner, 2026-08-02: click-through-the-gaps is the default - the
        # v3 appearance adoption flips it on once for installs where it
        # was off, and it remains a per-user toggle afterwards.
        main = (ROOT / "electron" / "main.cjs").read_text(encoding="utf-8")
        self.assertIn("if (Number(saved.appearanceDefaultVersion || 0) < 3) {", main)
        self.assertIn("next.petClickThrough = true;", main)

    def test_whole_figure_ships_by_default_and_on_recovery(self):
        # Owner, 2026-08-02 (fresh-Mac report): the 'half' default view
        # read as "her legs are missing" and hid the feet/leg click
        # targets. Full body is the default (v4 adoption covers existing
        # installs), and Cmd+Shift+0 recovery resets to the whole figure
        # at a zoom that provably fits the display - blind zoom=1
        # overflowed short screens once enableLargerThanScreen removed
        # the OS clamp.
        main = (ROOT / "electron" / "main.cjs").read_text(encoding="utf-8")
        self.assertIn("petView: 'full',", main)
        self.assertIn("appearanceDefaultVersion: 4,", main)
        self.assertIn("if (Number(saved.appearanceDefaultVersion || 0) < 4) {", main)
        self.assertIn("next.petView = 'full';", main)
        self.assertIn("state.petView = 'full';", main)
        # startup, buddy startup, AND recovery all fit the zoom to the area
        self.assertGreaterEqual(main.count("fitPetZoomToArea("), 3)
        self.assertIn("fitPetZoomToArea(\n      PET_BASE_SIZE, PET_NORMAL_MINIMUM, state.petZoom, area, PET_DOCK_MARGIN),\n    PET_ZOOM_RANGE);\n  const size = petZoomSize(PET_BASE_SIZE, PET_NORMAL_MINIMUM, state.petZoom);\n  mainWindow.setBounds(dockedPetBounds(size, area, PET_DOCK_MARGIN));", main)

    def test_iphone_pairing_is_off_by_default_and_token_persists(self):
        # Pocket Mirror (2026-08-02): the iOS app reaches the same server
        # over the LAN. Remote access is opt-in (loopback-only otherwise),
        # the auth token persists across launches so pairing survives a
        # restart (0600 file, delete to revoke), and both websocket auth
        # gates accept the pairing cookie alongside the Electron header.
        main = (ROOT / "electron" / "main.cjs").read_text(encoding="utf-8")
        self.assertIn("remoteAccess: false,", main)
        self.assertIn("state && state.remoteAccess ? '0.0.0.0' : HOST", main)
        self.assertIn("function persistentBackendToken()", main)
        self.assertIn("{ mode: 0o600 }", main)
        self.assertIn("'iPhone on This Network'", main)
        self.assertIn("'Pair iPhone…'", main)
        server = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
        self.assertEqual(server.count("_client_token(client)"), 2)
        self.assertIn("_client_token(request)", server)

    def test_phone_talks_wechat_style_and_struts_in_frame(self):
        # Owner design talk (2026-08-02): head-hold is a desk idiom. On the
        # phone the mic toggle swaps the field for one big hold-to-talk bar
        # (press records, release sends, slide up cancels unheard); finger
        # double-taps get a wider window and the whole head owns the dance;
        # a leg double-tap plays the walk take in place (catwalk) since
        # there is no desktop to roam; captions are glass, capped at 30vh,
        # dimmed while she speaks; feet keep a lane above the input row.
        renderer = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="talkbar"', renderer)
        self.assertIn('id="micmode"', renderer)
        self.assertIn("recDiscard=holdCancel;", renderer)
        self.assertIn("if(recDiscard){", renderer)
        self.assertIn("DOUBLE_TAP_MS=IS_IOS?650:450;", renderer)
        self.assertIn("if(IS_IOS&&part==='head')part='hair';", renderer)
        self.assertIn("function toggleWalkShow()", renderer)
        self.assertIn("if(IS_IOS)return false;",
                      renderer)
        self.assertIn("--caption-expanded-height:30vh", renderer)
        self.assertIn("html.ios.her-speaking #her{opacity:.72}", renderer)
        self.assertIn("if(!de.classList.contains('pet')&&!IS_IOS)return 0;",
                      renderer)

    def test_motion_video_ships_hevc_twin_and_never_hangs_boot(self):
        # iOS WebKit cannot decode VP9-alpha webm, and an unsupported webm
        # fires NEITHER canplaythrough nor error - boot hung at "booting"
        # on the iPhone (Pocket Mirror, 2026-08-02). The runtime bundle
        # carries the HEVC-alpha .mov twin, the renderer picks by
        # canPlayType, and the probe times out instead of hanging.
        export = (ROOT / "studio" / "export.py").read_text(encoding="utf-8")
        self.assertIn('clip["alpha_stream_hevc"] = f"assets/{hevc_name}"',
                      export)
        server = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
        self.assertIn('clip.get("alpha_stream_hevc")', server)
        self.assertIn("RUNTIME_VERSION = 15", server)
        renderer = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("canPlayType('video/mp4; codecs=\"hvc1\"')", renderer)
        self.assertIn("const bail=setTimeout(()=>res(false),6000);", renderer)

    def test_motion_clips_always_fit_the_whole_figure(self):
        # Same report: under a partial view the clip camera scaled for the
        # crop while the bottom anchor pinned the full-body feet to the
        # frame - only the legs stayed on screen. Motion takes are whole-
        # figure by definition, so their fit ignores the chosen view.
        renderer = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function viewCrop(meta,width,height,forceView){", renderer)
        self.assertIn("petCamera({bounds},width,height,'full')", renderer)
        self.assertIn("petCamera(meta,width,height,'full')", renderer)

    def test_canvas_backing_store_is_capped_at_display_size(self):
        # Companion mode made the window ~3000pt tall and the full-res
        # backing store a ~27-megapixel canvas - the per-frame composite
        # outran the frame budget and the mouth lagged the voice (owner,
        # 2026-08-02). The backing store caps near the display's pixel
        # size (off-screen body needs no pixels; on-screen stays 1:1) and
        # every css<->canvas conversion goes through cvScale().
        renderer = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function cvScale()", renderer)
        self.assertIn("const back=Math.min(1,capW/(innerWidth*d),capH/(innerHeight*d));",
                      renderer)
        self.assertNotIn("x=Math.floor(pointer.x*ratio),y=Math.floor(pointer.y*ratio);"
                         .replace("ratio", "devicePixelRatio"), renderer)
        self.assertGreaterEqual(renderer.count("cvScale()"), 7)

    def test_cmd_shift_9_enlarges_and_places_without_framing(self):
        # Final semantics (owner, 2026-08-02, after a full rollback): the
        # view is NEVER touched - no bust, no crop. Cmd+Shift+9 raises the
        # zoom to max and places the window per the reference (crown ~1/3
        # down, face ~84% across, body off the bottom edge); a second
        # press restores the prior zoom and bounds; both shortcuts force
        # 100% opacity. enableLargerThanScreen unclamps the oversized
        # window - macOS otherwise silently re-frames her on every resize.
        main = (ROOT / "electron" / "main.cjs").read_text(encoding="utf-8")
        self.assertIn("enableLargerThanScreen: true,", main)
        self.assertIn("const companion = 'CommandOrControl+Shift+9';", main)
        self.assertNotIn("state.petView = 'bust';", main)
        self.assertIn("state.petZoom = PET_ZOOM_RANGE.max;", main)
        self.assertIn("area.width * 0.84 - size.width / 2", main)
        self.assertIn("area.height * 0.23", main)
        self.assertIn("if (companionHold) {", main)
        self.assertEqual(main.count("applyPetOpacity(1);"), 2)

    def test_stillness_idle_docks_small_and_restores_on_wake(self):
        # Owner, 2026-08-02: the standing stillness idle used the full
        # docked pet size - a big cutout parked in the corner, reading as
        # 'the old approach'. It now docks at roam scale (held bounds
        # remembered), feet anchored to the window bottom even in the
        # docked non-roam camera, and undock restores the exact prior
        # bounds the moment attention returns.
        main = (ROOT / "electron" / "main.cjs").read_text(encoding="utf-8")
        self.assertIn("if (!preDockBounds) preDockBounds = mainWindow.getBounds();",
                      main)
        self.assertIn("clampRoamSizeToArea(petRoamSize(), area)", main)
        self.assertIn("'vivieen:pet-undock'", main)
        renderer = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("SHELL.undockPet==='function')SHELL.undockPet();", renderer)
        self.assertIn("camera.y=cv.height-(bounds[1]+bounds[3])*camera.scale;",
                      renderer)

    def test_facial_rebuild_keeps_the_full_body_set(self):
        # vvn, 2026-08-02: a calibration rebuild published a runtime with
        # NO body and NO motion. export() resolved body/ and motion/
        # against `d` - which during a recompose is the temporary rig
        # stage holding only keyframe+visemes - so every facial rebuild
        # silently stripped the full-body set (carol's 'body no longer
        # attached' included). Persistent assets now resolve against the
        # avatar's HOME dir regardless of the export source.
        source = (ROOT / "studio" / "export.py").read_text(encoding="utf-8")
        self.assertIn("home = reg.adir(slug)", source)
        self.assertIn('body_dir = os.path.join(home, "body")', source)
        self.assertIn("_publish_motion(home, dest, log)", source)

    def test_missing_runtime_layers_heal_themselves(self):
        # A publish swaps the runtime directory under a reloading pet: one
        # fetch lands in the gap, resolves null, and the avatar silently
        # loses its body until the next manual reload (carol, 2026-08-01 -
        # "the full-body is no longer attached"). The loader retries the
        # missing layers with fresh cache-busters until they heal, and
        # re-arms the roam engine when the walk clip arrives late.
        renderer = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("heal=${Date.now()}", renderer)
        self.assertIn("if(M.body&&!BODY)BODY=await reload(M.body.image);", renderer)
        self.assertIn("if(M.body&&!HEADMASK)HEADMASK=await reload(M.body.head_mask);", renderer)
        self.assertIn("if(M.cutout&&!CUTOUT)CUTOUT=await reload(M.cutout.src);", renderer)
        self.assertIn("M.motion.walk&&!MOTION.walk", renderer)

    def test_body_plate_tone_matches_the_portrait_along_the_seam(self):
        # A dissolve cannot hide a brightness difference - it spreads it
        # into a gradient band. The body plate's low frequencies shift
        # toward the warped portrait, but ONLY around the transition line
        # (band term peaks at the 50/50 mix) and ONLY in the side-hair
        # columns: whole-mask weighting washed the chest with portrait
        # brightness, and neck correction painted a light collar.
        source = (ROOT / "studio" / "body.py").read_text(encoding="utf-8")
        self.assertIn("def _seam_tone_match", source)
        self.assertIn("handover * (1.0 - handover) * 4.0", source)
        self.assertIn("(xs - 0.38) / 0.22", source)
        self.assertIn("_seam_tone_match(\n            os.path.join(stage, \"body.png\")", source)
        self.assertIn("def masked_blur", source)


class PocketBarAndToolsTests(unittest.TestCase):
    """The phone's messenger bar and the coupled lane's hands."""

    def setUp(self):
        self.renderer = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.settings = (ROOT / "web" / "settings.html").read_text(encoding="utf-8")

    def test_the_emoji_panel_is_a_panel_not_a_squeezed_flex_sibling(self):
        # Inside #row (display:flex) the strip was just another child,
        # crushed to a sliver beside the buttons. It belongs under the bar,
        # the way WeChat and Telegram put it.
        photo = self.renderer.index('id="chatPhoto"')
        panel = self.renderer.index('<div id="emojirow">')
        # #manual and #row both close before the panel opens.
        # #manual and #row both close before the panel opens, and the panel
        # is the last thing in #bar.
        self.assertEqual(self.renderer[photo:panel].count("</div>"), 2)
        self.assertIn('<div id="emojirow"></div>\n</div>', self.renderer)

    def test_the_file_input_stays_rendered_so_the_plus_can_click_it(self):
        # WebKit refuses a programmatic .click() on a display:none input,
        # which is why attaching a photo did nothing on the phone.
        self.assertIn('<input type="file" id="chatPhoto" accept="image/*">',
                      self.renderer)
        self.assertIn("#chatPhoto{position:fixed;left:-9999px", self.renderer)

    def test_the_rail_steps_aside_for_the_emoji_panel(self):
        # A raised bar put the rail's lowest button on top of the plus -
        # every attempt to attach opened the zoom sliders instead.
        self.assertIn("html.ios.emoji-open #rail{opacity:0;pointer-events:none}",
                      self.renderer)

    def test_settings_knows_it_is_on_a_phone(self):
        # Without the flag the tabs fell off the right edge and the sticky
        # header scrolled away on the first swipe (height:100% caps the
        # sticky containing block at one viewport).
        self.assertIn("link.href='/settings?ios=1'", self.renderer)
        self.assertIn("html.ios,html.ios body{height:auto;min-height:100%",
                      self.settings)
        # Fixed, not sticky: body needs overflow containment on a narrow
        # screen, and that makes body its own scrollport - a sticky header
        # then scrolls away on the first swipe and strands the tabs.
        self.assertIn("html.ios header{position:fixed;top:0", self.settings)
        self.assertIn("html.ios main{padding:calc(112px", self.settings)
        self.assertIn("html.ios nav{", self.settings)
        self.assertIn("html.ios input,html.ios textarea,html.ios select{font-size:16px}",
                      self.settings)

    def test_a_produced_file_becomes_a_card_the_thread_can_play(self):
        self.assertIn("function threadAttachments(list)", self.renderer)
        self.assertIn("threadAttachments(r.media);", self.renderer)

    def test_only_media_under_known_roots_is_ever_served(self):
        import sys
        sys.path.insert(0, str(ROOT / "server"))
        import app

        with tempfile.TemporaryDirectory() as home:
            downloads = os.path.join(home, "Downloads")
            os.makedirs(downloads)
            good = os.path.join(downloads, "clip.mp4")
            script = os.path.join(downloads, "run.sh")
            for path in (good, script):
                with open(path, "wb") as handle:
                    handle.write(b"x")
            # This is about who may be served, not about codecs.
            with mock.patch.object(app, "_enconvo_roots",
                                   return_value=[os.path.realpath(downloads)]), \
                    mock.patch.object(app, "_phone_playable",
                                      side_effect=lambda value: value):
                self.assertTrue(app._enconvo_share(good))
                # Not media: never served, whatever the agent claims.
                self.assertIsNone(app._enconvo_share(script))
                # Outside the roots, and files that do not exist.
                self.assertIsNone(app._enconvo_share("/etc/passwd"))
                self.assertIsNone(
                    app._enconvo_share(os.path.join(downloads, "ghost.mp4")))

                text, cards = app._enconvo_media(f"Saved to {good} for you.")
                self.assertEqual(len(cards), 1)
                self.assertIn(f"[clip.mp4]({cards[0]['url']})", text)
                # An existing markdown link keeps its label, swaps its target.
                linked, _ = app._enconvo_media(f"Here: [the clip]({good})")
                self.assertIn(f"[the clip](api/enconvo/file/", linked)
                self.assertNotIn("[clip.mp4](api", linked)

    def test_the_pocket_app_talks_to_agents_the_way_a_channel_does(self):
        # EnConvo's IM channels POST the agent's own command route -
        # /<extension>/<command>, no /api - as an event stream. The agent
        # then picks and runs its own tools. Nothing here tells it how.
        source = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
        self.assertIn('f"{ENCONVO_HOST}/{extension}/{command}"', source)
        self.assertIn('"Accept": "text/event-stream"', source)
        self.assertIn('"runType": "command"', source)
        self.assertIn('"input_text": message', source)
        # run_mode is THE switch. Mavis's saved config says "chat", which is
        # a brain with no hands through every route - which is exactly why
        # the lane looked toolless. "agent" is where the tool belt lives.
        self.assertIn('"run_mode": "agent"', source)
        # And no coaching: the coupled agent gets the owner's message
        # VERBATIM - its tool belt is EnConvo's business. (Uncoupled
        # Vivieen has her own directives; that brain is ours.)
        self.assertIn("key, session, request.message, safe_files", source)
        self.assertNotIn("request.message + _", source)

    def test_a_command_key_is_understood_however_it_is_written(self):
        import sys
        sys.path.insert(0, str(ROOT / "server"))
        import app

        for value in ("main", "agent|main", "agent/main"):
            self.assertEqual(app._enconvo_command_key(value), "agent|main")
        self.assertEqual(app._enconvo_command_key("agent|Gq3x"), "agent|Gq3x")

    def test_delivered_files_are_read_from_the_delivery_call(self):
        # EnConvo agents hand artifacts over through delivery/present_files
        # and are told NOT to repeat the path in prose, so this is the only
        # place a produced file is ever named.
        import sys
        sys.path.insert(0, str(ROOT / "server"))
        import app

        with tempfile.TemporaryDirectory() as home:
            made = os.path.join(home, "shot.png")
            with open(made, "wb") as handle:
                handle.write(b"x")
            steps = [
                {"flowRunStatus": "success",
                 "flowParams": json.dumps({
                     "path": "image_create/features/open_ai/create"}),
                 "output": {"paths": [made]}},
                # Arguments stream in character by character; mid-flight the
                # title is a single letter. Only the finished call counts.
                {"flowRunStatus": "input-streaming",
                 "flowParams": json.dumps({
                     "path": "delivery/present_files",
                     "params": {"deliverables": [
                         {"type": "file", "url": made, "title": "W"}]}})},
                {"flowRunStatus": "success",
                 "flowParams": json.dumps({
                     "path": "delivery/present_files",
                     "params": {"deliverables": [
                         {"type": "file", "url": made,
                          "title": "Winter Greenhouse"}]}})},
            ]
            found = app._enconvo_step_files(steps)
            self.assertEqual(len(found), 1)
            # The tool reports a path; the delivery carries her name for it.
            self.assertEqual(found[0]["title"], "Winter Greenhouse")
            # A file that does not exist is not a deliverable...
            self.assertEqual(app._enconvo_step_files(
                [{"flowParams": json.dumps({
                    "path": "delivery/present_files",
                    "params": {"deliverables": [
                        {"url": os.path.join(home, "ghost.png")}]}})}]),
                [])
            # ...and one outside the served roots never becomes a card,
            # however confidently the agent hands it over.
            self.assertIsNone(app._enconvo_share("/etc/passwd"))
            self.assertIsNone(app._enconvo_share("/usr/bin/env"))

    def test_the_thread_narrates_the_way_a_verbose_channel_does(self):
        import sys
        sys.path.insert(0, str(ROOT / "server"))
        import app

        # EnConvo's own rule (launch_channel.js): announce a call that has
        # STARTED, never a hidden one, never the channel's own plumbing,
        # and label it with the agent's description of what it is doing.
        running = {"type": "flow_step", "flowRunStatus": "running",
                   "flowName": "local_api", "flowId": "a",
                   "title": "Untitled",
                   "flowParams": json.dumps({
                       "description": "Generate fox in snow image",
                       "path": "image_create/features/open_ai/create"})}
        self.assertEqual(app._enconvo_step_note(running)["text"],
                         "Generate fox in snow image")
        # Not yet started, hidden, or the channel talking to itself.
        for tweak in ({"flowRunStatus": "input-streaming"},
                      {"hide": True},
                      {"flowParams": json.dumps(
                          {"path": "im_channels/reply"})}):
            self.assertIsNone(app._enconvo_step_note({**running, **tweak}))
        # Falls back to the step's own title when it has no description.
        bare = {**running, "flowParams": json.dumps({"path": ""})}
        self.assertEqual(app._enconvo_step_note(bare)["text"], "Untitled")

        source = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
        # Verbose, like a Telegram channel - and streamed, so a long job
        # reads as work rather than a frozen app.
        self.assertIn('"im_verbose": True', source)
        self.assertIn('media_type="text/event-stream"', source)
        self.assertIn('{"type": "typing"}', source)
        # Her sentence as she writes it, not one silent blob at the end.
        self.assertIn('{"type": "say", "text": piece}', source)
        self.assertIn("work.addText(event.text)", self.renderer)
        # And a turn nobody is listening to gets cancelled, not orphaned.
        self.assertIn("turn.cancel()", source)

    def test_the_channel_takes_the_same_slash_commands(self):
        # /new /stop /audio /verbose /status, exactly EnConvo's set.
        for command in ("/new", "/newsession", "/stop", "/audio",
                        "/verbose", "/status"):
            self.assertIn(f"'{command}'", self.renderer, command)
        # A command never reaches the agent.
        self.assertIn("if(enconvoSlash(text)){", self.renderer)
        # /stop actually aborts the request in flight.
        self.assertIn("ENCONVO.abort=new AbortController();", self.renderer)
        self.assertIn("ENCONVO.abort.abort();", self.renderer)
        # The thread reads the stream rather than waiting for one blob.
        self.assertIn("response.body.getReader()", self.renderer)
        self.assertIn("work.addStep(event.text)", self.renderer)

    def test_muted_means_muted_lips_included(self):
        # No sound, no articulation - lips moving over silence read as a
        # glitch, not a courtesy (owner, 2026-08-03).
        self.assertIn("if(window.IOS_MUTED)return'sil';", self.renderer)

    def test_live_talk_uses_the_native_microphone_bridge(self):
        # WKWebView's getUserMedia in the Simulator is a MOCK device - a
        # ~155Hz hum at 10% amplitude, never the real mic. "She can't hear
        # me" was literally true: she was hearing a tone. AVAudioEngine is
        # the real microphone on device AND in the Simulator.
        self.assertIn("webkit.messageHandlers.mic", self.renderer)
        self.assertIn("window.__vivMicData=", self.renderer)
        self.assertIn("liveSendPcm(session,", self.renderer)
        swift = (ROOT / "ios" / "Vivieen" / "MicDriver.swift").read_text()
        self.assertIn("AVAudioEngine", swift)
        self.assertIn("installTap", swift)
        self.assertIn("AVAudioConverter", swift)
        glue = (ROOT / "ios" / "Vivieen" / "CompanionWebView.swift").read_text()
        self.assertIn('name: "mic"', glue)
        self.assertIn('body.hasPrefix("start:")', glue)
        # Hang-up also stops the native capture.
        self.assertIn("webkit.messageHandlers.mic.postMessage('stop')",
                      self.renderer)

    def test_the_silence_hangup_measures_the_owner_not_the_line(self):
        source = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
        self.assertIn("LIVE_SILENCE_HANGUP_S = 15", source)
        # Only a voice resets the clock: the phone streams continuously
        # (zeroed frames while she speaks, room tone while nobody does) and
        # counting those meant the quiet-line hangup could never fire.
        self.assertIn("> 0.012", source)
        # And her own speaker bleed must not look like a voice: the echo
        # guard opens when audio ARRIVES, covering the playback race.
        self.assertIn("session.echoGuardUntil=Math.max(", self.renderer)

    def test_the_thread_never_crams_its_cards(self):
        # flex:none, or a full thread SHRINKS every card to a sliver
        # instead of scrolling (owner screenshot, 2026-08-03).
        self.assertIn("flex:none;\n  width:100%", self.renderer)
        # And an empty VAD turn - "…" - never earns a bubble.
        self.assertIn("/[\\p{L}\\p{N}]/u.test(m.text)", self.renderer)

    def test_the_wave_is_a_meter_not_a_loop(self):
        # The Listening chip's bars follow the REAL microphone level -
        # a loop that ignores the microphone is a lie about listening.
        self.assertIn("window.MIC_LEVEL=0;", self.renderer)
        self.assertIn("window.MIC_LEVEL=session.lastRms;", self.renderer)
        # PTT rides an analyser on the same stream the recorder captures.
        self.assertIn("meter.getByteTimeDomainData(sample);", self.renderer)
        # And no keyframe loop remains on the bars.
        self.assertNotIn("@keyframes listenWave", self.renderer)

    def test_a_swipe_across_her_opens_the_avatar_deck(self):
        # Two switchable looks, both owner-picked: noir (dark cascade)
        # and sorbet (pastel arc fan). Cards come from the registry,
        # tapping Use activates and reloads.
        self.assertIn('id="avfan"', self.renderer)
        self.assertIn("localStorage.getItem('viv-carousel')", self.renderer)
        self.assertIn("rotate(${d*13}deg)", self.renderer)   # sorbet arc
        self.assertIn("rotate(${d*-7}deg)", self.renderer)   # noir cascade
        self.assertIn("api/avatar/activate", self.renderer)
        self.assertIn("a.slug===feed.active", self.renderer)
        # A swipe, not a tap: face play keeps taps, the deck takes drags.
        self.assertIn("Math.abs(dx)>70&&Math.abs(dy)<48", self.renderer)

    def test_the_relay_is_opt_in_allow_listed_and_blind(self):
        # Internet reach, the OpenClaw way: both ends dial out to a dumb
        # mailbox. Nothing starts unless the relay-url file exists, the
        # Mac agent replays only an allow-list, and the relay never sees
        # the pairing token - only a hash of it.
        import sys
        sys.path.insert(0, str(ROOT / "server"))
        import relay_agent

        with tempfile.TemporaryDirectory() as empty:
            with mock.patch.object(relay_agent, "SUPPORT", empty):
                self.assertIsNone(relay_agent.start("8777"))
        self.assertIn("/api/enconvo/", relay_agent._ALLOWED_PREFIXES)
        self.assertNotIn("/api/settings", str(relay_agent._ALLOWED_PREFIXES))
        source = (ROOT / "server" / "relay_agent.py").read_text()
        self.assertIn('hashlib.sha256(b"viv-relay:" + token.encode())', source)
        relay = (ROOT / "relay" / "api" / "relay.js").read_text()
        # Trust-on-first-use pinning, and boxes that expire.
        self.assertIn("channel claimed by another key", relay)
        self.assertIn('"EXPIRE", key, "900"', relay)
        # The engine only ever starts it behind the opt-in file.
        app_source = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
        self.assertIn("import relay_agent", app_source)

    def test_secrets_live_in_the_vault_never_on_disk(self):
        # EnConvo's leaf, the Mac's machinery: keys go to the Keychain
        # (a JSON vault file under test), config.json keeps only the
        # marker, load() weaves the real value back in memory, and
        # __clear__ deletes the vault entry - not just the marker.
        import importlib
        import sys
        sys.path.insert(0, str(ROOT / "server"))
        with tempfile.TemporaryDirectory() as work:
            environ = {"VIVIEEN_DATA_DIR": work,
                       "VIVIEEN_CONFIG": os.path.join(work, "config.json"),
                       "VIVIEEN_VAULT_FILE": os.path.join(work, "vault.json")}
            with mock.patch.dict(os.environ, environ):
                with open(environ["VIVIEEN_CONFIG"], "w") as handle:
                    json.dump({"llm": {"provider": "openai",
                                       "api_key": "sk-plain-42"}}, handle)
                import credentials
                import providers
                importlib.reload(credentials)
                importlib.reload(providers)
                cfg = providers.load()
                self.assertEqual(cfg["llm"]["api_key"], "sk-plain-42")
                disk = json.load(open(environ["VIVIEEN_CONFIG"]))
                self.assertEqual(disk["llm"]["api_key"], "@keychain")
                providers.save({"llm": {"api_key": "__clear__"}})
                self.assertEqual(providers.load()["llm"]["api_key"], "")
                self.assertEqual(json.load(open(
                    environ["VIVIEEN_VAULT_FILE"])), {})
        importlib.reload(credentials)
        importlib.reload(providers)

    def test_the_catalogue_covers_the_market_in_every_slot(self):
        import sys
        sys.path.insert(0, str(ROOT / "server"))
        import providers

        ids = {kind: {p["id"] for p in options}
               for kind, options in providers.PROVIDERS.items()}
        # Every slot leads with EnConvo Global Default...
        for kind in ("llm", "tts", "stt", "image", "video"):
            self.assertEqual(providers.PROVIDERS[kind][0]["id"], "enconvo",
                             kind)
        # ...and the market follows, on your own keys.
        self.assertLessEqual({"mistral", "together", "fireworks", "perplexity",
                              "moonshot", "qwen", "zhipu", "cerebras",
                              "nvidia"}, ids["llm"])
        self.assertLessEqual({"deepgram", "cartesia"}, ids["tts"])
        self.assertLessEqual({"deepgram", "elevenlabs"}, ids["stt"])
        self.assertLessEqual({"openai", "gemini", "xai", "stability", "bfl"},
                             ids["image"])
        self.assertLessEqual({"openai", "gemini", "luma", "runway"},
                             ids["video"])
        # The OpenAI-shape fleet actually routes through the one adapter.
        for pid in ("mistral", "together", "qwen", "cerebras"):
            self.assertIn(pid, providers.OPENAI_SHAPE)
        # And the new slots exist in the defaults with EnConvo first.
        self.assertEqual(providers.DEFAULTS["image"]["provider"], "enconvo")
        self.assertEqual(providers.DEFAULTS["video"]["provider"], "enconvo")

    def test_uncoupled_vivieen_has_her_own_hands(self):
        # The directive-in-prompt design is legitimate HERE: this brain is
        # ours, so its tool belt is ours to strap on.
        source = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
        self.assertIn("<<viv:image", source)
        self.assertIn("<<viv:video", source)
        self.assertIn('cfg["persona"]["system"] + _OWN_TOOLS', source)
        self.assertIn("media_gen.generate_image", source)
        self.assertIn("media_gen.generate_video", source)
        # The result is a card, and a failure is a sentence - never silence.
        self.assertIn('result["media"] = cards', source)
        self.assertIn("but the provider said", source)
        self.assertIn("threadAttachments(r.media);", self.renderer)
        # EnConvo default naming quirk stays fixed: gemini-enconvo creates
        # through features/gemini/create.
        media = (ROOT / "server" / "media_gen.py").read_text(encoding="utf-8")
        self.assertIn('name.replace("-enconvo", "")', media)

    def test_media_tools_never_inherit_the_engines_stdin(self):
        # ffmpeg and ffprobe read stdin; the engine's is a pipe nobody
        # closes, and an inherited one hangs the probe until it times out.
        source = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
        self.assertIn("stdin=subprocess.DEVNULL", source)
        self.assertIn('"-nostdin"', source)
        # And a probe that will not answer means re-encode, not ship an
        # unplayable card.
        self.assertIn("re-encoding to be safe", source)

    def test_served_files_answer_range_requests(self):
        # Safari refuses to scrub a video whose source ignores ranges.
        source = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
        self.assertIn('"Accept-Ranges": "bytes"', source)


if __name__ == "__main__":
    unittest.main()
