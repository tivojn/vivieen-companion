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
        self.assertIn("mainWindow.webContents.send('vivieen:pet-pointer', { x: -1, y: -1 });", main)
        self.assertIn("onPetPointer", preload)
        self.assertIn("HAS_GLOBAL_PET_POINTER", renderer)
        self.assertIn("SHELL.onPetPointer", renderer)
        self.assertIn("hitConfidence", renderer)
        self.assertIn("ctx.getImageData", renderer)

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
        self.assertIn("MOTION={walk:null,idle:null}", renderer)
        self.assertIn("speaking||recording||petHit", renderer)
        self.assertIn("elapsed / Math.max(0.1, petRoamRuntime.cycleSeconds)", main)
        self.assertIn("motionTravelDelta", main)
        self.assertIn("Number.isFinite(petRoamRuntime.x)", main)
        self.assertIn("petRoamRuntime.x = x", main)
        self.assertIn("travelOffsets", main)
        self.assertIn("clip.travel_offsets", renderer)
        self.assertIn("phase*clip.frames", renderer)
        self.assertIn("ROAM.edge==='right'", renderer)
        self.assertIn("clip.edge_anchors", renderer)
        self.assertIn("anchors.left_frames", renderer)
        self.assertIn("wallPadding=3", renderer)
        self.assertIn("const motionReady=Boolean(MOTION.walk);", renderer)
        self.assertIn("backgroundThrottling: false", main)
        self.assertNotIn("stride*direction*width", renderer)
        self.assertNotIn("PET_ROAM_SPEED", main)

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
        self.assertEqual(
            video_command[video_command.index("--aspect_ratio") + 1], "2:3")
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
        self.assertIn("narrow green-screen gap around each wrist", keyframe)
        self.assertIn("canonical RIGHT-SIDE full-body plate", keyframe)
        self.assertIn("canonical HD head", keyframe)
        self.assertIn("Use correct contralateral coordination", video)
        self.assertIn("both shoulders, sleeves, elbows, wrists, and hands remain naturally readable", video)
        self.assertIn("forward → back → return", video)
        self.assertIn("input keyframe in every frame", video)
        self.assertIn("NORMAL, charming office walk", video)
        self.assertIn("Each wrist stays continuously between the hip seam and mid-thigh", video)
        self.assertIn("one-sided partial cycles", video)
        self.assertIn("same-side arm-and-leg motion", video)
        self.assertIn("15% to 85% at constant speed", video)
        self.assertIn("color flicker", video)
        self.assertIn("chroma-key green", keyframe)
        self.assertIn("chroma-key green", video)
        self.assertEqual(motion.MOTION_VERSION, 8)

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
        self.assertIn("exactly one clean lateral cartwheel", cartwheel_video)
        self.assertIn("finish upright", cartwheel_video)
        with self.assertRaisesRegex(ValueError, "unknown Horizon Walk style"):
            motion.resolve_walk_style("moonwalk")

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

        direction = "Lean one shoulder on the wall with crossed ankles."
        custom = motion.resolve_idle_pose("custom", direction)
        self.assertEqual("custom", custom["id"])
        self.assertEqual(direction, custom["prompt"])
        self.assertIn(direction, motion._idle_video_prompt(custom))
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

        raised_hands = [
            self._synthetic_pose(50 + index * 4, -math.pi / 2 + 2 * math.pi * index / 24)
            for index in range(25)
        ]
        for pose in raised_hands:
            pose["joints"]["left_wrist"]["y"] = 55
            pose["joints"]["right_wrist"]["y"] = 55
        quality = motion._pose_cycle_metrics(raised_hands, 0, 24)
        self.assertFalse(quality["valid"])
        self.assertIn("hand rises above the waist", quality["reason"])

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
                    mock.patch.object(body, "_head_mask", side_effect=head_mask)):
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
        self.assertIn('id="body-walk-remove"', settings)
        self.assertIn('id="body-idle-remove"', settings)
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
        self.assertLess(int(alpha[175, 35]), 8)
        self.assertGreater(int(alpha[165, 120]), 20)


if __name__ == "__main__":
    unittest.main()
