import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from studio import body, cutout, face, motion


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
        server = (ROOT / "server" / "app.py").read_text()
        package = json.loads((ROOT / "package.json").read_text())
        self.assertIn("PET_BASE_SIZE", main)
        self.assertIn("PET_ZOOM_RANGE", main)
        self.assertIn("petBoundsForZoom", main)
        self.assertIn("Math.min(area.width", main)
        self.assertIn("Math.min(area.height", main)
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
        self.assertIn("low toe clearance", keyframe)
        self.assertIn("hands remain below the waist", keyframe)
        self.assertIn("no marching", keyframe)
        self.assertIn("correct contralateral human gait", video)
        self.assertIn("No same-side arm-and-leg advance", video)
        self.assertIn("15% to 85% of the frame", video)
        self.assertIn("color flicker", video)

    def test_pose_reference_is_geometry_only(self):
        prompt = motion._idle_keyframe_prompt("existing outfit", True)
        self.assertIn("pose geometry only", prompt)
        self.assertIn("do not copy its person", prompt)
        self.assertIn("fuchsia tailored blazer", prompt)
        self.assertIn("knee lifts to hip height", prompt)
        self.assertIn("never an upright tree pose", prompt)
        self.assertIn("canonical LEFT-EDGE pose", prompt)
        self.assertIn("away from the supporting edge", prompt)
        source = (ROOT / "studio" / "motion.py").read_text()
        self.assertNotIn("idle-pose-reference.png", source)
        self.assertIn('"retained": False', source)

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


class BodyProviderTests(unittest.TestCase):
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


class PetMatteTests(unittest.TestCase):
    def test_edge_decontamination_uses_subject_color(self):
        image = np.zeros((40, 40, 4), np.uint8)
        image[6:34, 6:34, :3] = (245, 245, 245)
        image[6:34, 6:34, 3] = 120
        image[8:32, 8:32, :3] = (30, 40, 170)
        image[8:32, 8:32, 3] = 255
        cleaned = cutout._decontaminate_edges(image.copy())
        self.assertLess(float(cleaned[6:34, 6:34, :3].mean()), 180)
        self.assertGreater(int(cleaned[20, 20, 2]), 150)

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
