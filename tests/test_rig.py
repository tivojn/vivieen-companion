import hashlib
import os
import tempfile
import unittest
from unittest import mock

import numpy as np

import cv2

from studio import anatomy, build, compose, face, measure, rig, visemes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RigProfileTests(unittest.TestCase):
    def test_all_controls_span_zero_to_one_hundred_with_safe_bands(self):
        schema = rig.public_schema()["controls"]
        # Owner calibration 2026-08-01: defaults are the proven live profile,
        # sliders run to 150 (transfer alphas clip at 1.0 - saturation, not
        # extrapolation), green bands embrace the proven values.
        expected_safe = {
            "lips": (80.0, 120.0),
            "jaw": (25.0, 110.0),
            "cheeks": (0.0, 110.0),
            "nasolabial": (0.0, 110.0),
            "nose": (0.0, 110.0),
        }
        for name, safe_limits in expected_safe.items():
            self.assertEqual(
                (schema[name]["minimum"], schema[name]["maximum"]),
                (0.0, 150.0),
            )
            self.assertEqual(
                (schema[name]["safe_minimum"], schema[name]["safe_maximum"]),
                safe_limits,
            )

    def test_profile_is_normalized_and_dental_lock_is_mandatory(self):
        profile = rig.normalize({
            "lips": 88,
            "jaw": 41,
            "cheeks": 15,
            "nasolabial": 22,
            "nose": 4,
            "teeth_lock": True,
            "preset": "subtle",
        })
        self.assertEqual(profile["nose"], 4.0)
        self.assertEqual(profile["preset"], "subtle")
        with self.assertRaisesRegex(ValueError, "identity lock"):
            rig.normalize({"teeth_lock": False})
        with self.assertRaisesRegex(ValueError, "identity lock"):
            rig.normalize({"lower_teeth_lock": False})
        locks = rig.public_schema()["locks"]
        self.assertTrue(locks["upper_teeth"])
        self.assertTrue(locks["lower_teeth"])
        self.assertEqual(rig.VERSION, 3)

    def test_red_experimental_values_are_allowed_for_anatomy_qa(self):
        profile = rig.normalize({
            "lips": 0, "jaw": 100, "cheeks": 100,
            "nasolabial": 100, "nose": 100,
        })
        self.assertEqual(profile["lips"], 0.0)
        self.assertEqual(profile["nose"], 100.0)

    def test_values_outside_the_control_span_are_rejected(self):
        for name in rig.CONTROLS:
            for value in (-1, 151):
                with self.subTest(name=name, value=value), \
                     self.assertRaises(ValueError):
                    rig.normalize({name: value})

    def test_single_canonical_dental_pose_is_valid(self):
        self.assertEqual(
            anatomy._comparison_metrics([]),
            ((None, 0), (None, 1.0), (None, 0.0)),
        )

    def test_dental_band_reaches_past_the_inner_lip_polygon(self):
        # gary66 `ah` (2026-08-01): the inner-mouth polygon traced the lip
        # line THROUGH the teeth - 14732 enamel px in the mouth, 174 inside
        # the polygon. The lock inpainted a sliver, pasted the donor row
        # lower, and both rows showed ("doubled teeth shadow"). The band
        # grows the cavity vertically but stays inside the outer-lip hull.
        landmarks = np.zeros((478, 2), np.float32)
        landmarks[compose.INNER_MOUTH] = [40, 50]
        for offset, point in enumerate(compose.INNER_MOUTH):
            landmarks[point] = [30 + offset, 46 + (offset % 3) * 4]
        landmarks[face.OUTER_LIP] = [40, 50]
        for offset, point in enumerate(face.OUTER_LIP):
            landmarks[point] = [24 + offset * 2, 30 + (offset % 5) * 10]
        cavity = compose._mouth_cavity((100, 100, 3), landmarks)
        band = compose._dental_band((100, 100, 3), landmarks, cavity)
        self.assertGreater(int(np.count_nonzero(band)),
                           int(np.count_nonzero(cavity)))
        hull = np.zeros_like(band)
        cv2.fillPoly(hull, [cv2.convexHull(
            landmarks[face.OUTER_LIP].astype(np.int32))], 255)
        self.assertEqual(int(np.count_nonzero(band & ~hull)), 0)

    def test_tooth_donor_is_the_most_complete_row_not_the_first(self):
        # SS (clenched, lip-shaded, 483px) used to win over eh's wide bright
        # row (865px) purely by list position, so every frame got a dull
        # beige paste. Selection now scans all candidates for the largest
        # detected master.
        source = open(os.path.join(ROOT, "studio", "compose.py"),
                      encoding="utf-8").read()
        self.assertIn("pixels >= MIN_TEETH_PIXELS[row] and pixels > best_pixels",
                      source)
        self.assertIn("_dental_band(donor.shape, donor_lm)", source)

    def test_teeth_lock_gains_strength_and_donor_override_controls(self):
        # Owner request 2026-08-01 (carol, upper TH 765px / lower TH 478px):
        # the auto-elected donor carried a minor defect with no recourse.
        # The dental lock gets a control surface - per-row donor overrides
        # ("auto" default, validated against the candidate list) and a
        # strength slider whose 100 is today's exact full paste; below 100
        # each frame's own render blends back in. All advisory: an override
        # without detected enamel falls back to the election, never a veto.
        schema = rig.public_schema()["controls"]
        self.assertEqual(
            (schema["teeth"]["minimum"], schema["teeth"]["maximum"]),
            (0, 100))
        self.assertEqual(
            (schema["teeth"]["safe_minimum"], schema["teeth"]["safe_maximum"]),
            (100, 100))
        profile = rig.normalize({"teeth": 60, "upper_teeth_donor": "eh"})
        self.assertEqual(profile["teeth"], 60.0)
        self.assertEqual(profile["upper_teeth_donor"], "eh")
        self.assertEqual(profile["lower_teeth_donor"], "auto")
        self.assertTrue(profile["teeth_lock"])
        self.assertTrue(profile["upper_teeth_lock"])
        self.assertEqual(anatomy._experimental_keys(profile), ["teeth"])
        with self.assertRaisesRegex(ValueError, "upper_teeth_donor"):
            rig.normalize({"upper_teeth_donor": "blink"})
        # The candidate lists live in rig (normalize validates overrides and
        # compose imports rig, not the reverse); compose aliases them.
        self.assertIs(compose.DENTAL_DONORS, rig.DENTAL_DONORS)
        source = open(os.path.join(ROOT, "studio", "compose.py"),
                      encoding="utf-8").read()
        self.assertIn("work = img.astype(np.float32) * (1.0 - strength) "
                      "+ work * strength", source)
        self.assertIn("dental lock released: strength 0", source)
        self.assertIn("ADVISORY {row} donor override", source)
        app_source = open(os.path.join(ROOT, "server", "app.py"),
                          encoding="utf-8").read()
        self.assertIn('teeth: float = _rig_control_field("teeth")', app_source)
        self.assertIn('upper_teeth_donor: str = _dental_donor_field("upper")',
                      app_source)

    def test_donor_override_wins_and_falls_back_without_enamel(self):
        candidates = [
            ("SS", "image-SS", "landmarks-SS", "master-SS", 483),
            ("eh", "image-eh", "landmarks-eh", "master-eh", 865),
            ("TH", "image-TH", "landmarks-TH", "master-TH", 0),
        ]
        self.assertEqual(
            compose._elect_tooth_donor(candidates, "upper", "auto")[0], "eh")
        self.assertEqual(
            compose._elect_tooth_donor(candidates, "upper", "SS")[0], "SS")
        # A chosen frame with no detected enamel: advisory fallback to the
        # election, the rebuild proceeds.
        self.assertEqual(
            compose._elect_tooth_donor(candidates, "upper", "TH")[0], "eh")

    def test_brow_envelope_uses_ascending_smoothstep_edges(self):
        # _smoothstep clamps its denominator to 1e-6, so reversed edges
        # degenerate into a hard step. The brow's vertical envelope shipped
        # reversed: 1.0 in the forehead, 0.0 over the hair - every baked
        # strip came out ~15% opaque and the brows never visibly moved.
        from studio import expression
        ys = np.array([473.0, 531.0, 560.0], np.float32)  # hairline..brow hair
        up = expression._smoothstep(546 - 1.7 * 43, 546 - 0.35 * 43, ys)
        self.assertLess(float(up[0]), 0.05)      # fades out at the hairline
        self.assertGreater(float(up[2]), 0.95)   # full strength over the hair
        source = open(os.path.join(ROOT, "studio", "expression.py"),
                      encoding="utf-8").read()
        self.assertIn("up = _smoothstep(btop - 1.7 * span, btop - 0.35 * span, ys)",
                      source)

    def test_dental_rows_partition_at_the_lip_midline(self):
        cavity = np.full((10, 12), 255, np.uint8)
        landmarks = np.zeros((478, 2), np.float32)
        landmarks[13, 1] = 4
        landmarks[14, 1] = 6
        upper = compose._row_zone(cavity, landmarks, "upper") > 0
        lower = compose._row_zone(cavity, landmarks, "lower") > 0
        self.assertEqual(int(np.count_nonzero(upper)), 72)
        self.assertEqual(int(np.count_nonzero(lower)), 48)
        self.assertFalse(np.any(upper & lower))
        self.assertTrue(np.all(upper | lower))

    def test_degenerate_lower_row_transform_follows_the_jaw_translation(self):
        donor = np.zeros((478, 2), np.float32)
        target = donor.copy()
        target[compose.LOWER_MOUTH_ANCHORS] = [4.5, 8.0]
        transform = compose._lower_row_transform(donor, target)
        np.testing.assert_allclose(
            transform,
            np.array([[1.0, 0.0, 4.5], [0.0, 1.0, 8.0]], np.float32),
        )

    def test_connected_tooth_surface_extension_is_canonical(self):
        actual = np.zeros((8, 8), dtype=bool)
        actual[1:4, 1:4] = True
        anchor = np.zeros_like(actual)
        anchor[1:3, 1:3] = True
        self.assertEqual(anatomy._disconnected_fraction(actual, anchor), 0.0)

    def test_disconnected_tooth_component_is_measured(self):
        actual = np.zeros((8, 8), dtype=bool)
        actual[1:3, 1:3] = True
        actual[5:7, 5:7] = True
        anchor = np.zeros_like(actual)
        anchor[1:3, 1:3] = True
        self.assertEqual(anatomy._disconnected_fraction(actual, anchor), 0.5)
        self.assertEqual(
            anatomy._disconnected_fraction(
                actual, anchor, ignore_components_at_most=4),
            0.0,
        )

    def test_speech_articulation_excludes_eyelid_source_frame(self):
        self.assertEqual(len(visemes.SPEECH_ORDER), 15)
        self.assertNotIn("blink", visemes.SPEECH_ORDER)

    def test_articulation_failure_reports_measured_width(self):
        message = build._articulation_failure({
            "name": "oo", "ratio": 0.004, "max_ratio": 0.06,
            "width_ratio": 0.96, "want_width": 0.82,
        })
        self.assertEqual(
            message,
            "oo width 0.96x neutral is outside 0.70-0.94 (target 0.82)",
        )

    def test_aperture_tolerance_only_covers_landmark_jitter(self):
        self.assertTrue(measure._aperture_within_limit(0.091, 0.09))
        self.assertFalse(measure._aperture_within_limit(0.093, 0.09))

    def test_gaze_warp_never_tears_the_iris(self):
        # 'Iris kinda ruptured' at far-left cursor (2026-08-01): the lash
        # guard hard-zeroed the eyeball mask, and under a 9px iris shift
        # the warp sheared the iris across that cliff in single-pixel
        # steps - on EVERY avatar's extreme gaze tiles, only visible once
        # the face filled the screen. The guard is soft-lifted now (the
        # lash line itself still holds at zero), and the travel table
        # scales down for genuinely small irises.
        source = open(os.path.join(ROOT, "studio", "expression.py"),
                      encoding="utf-8").read()
        self.assertNotIn("alpha[guard > 0] = 0.0", source)
        self.assertIn("soft_guard = cv2.GaussianBlur(guard", source)
        self.assertIn("gaze travel scaled to", source)
        self.assertIn("radius / 17.0", source)
        # Root cause, diagnosed by the user: the landmark ring measures
        # only the VISIBLE iris, so the rigid zone ended inside the real
        # disc and moved part of it. The whole iris now travels as one
        # body (rigid to 1.45r), and cursor-following uses only a sliver
        # of the travel where every tile is clean.
        self.assertIn("_smoothstep(1.45 * r, 2.3 * r, d)", source)
        renderer = open(os.path.join(ROOT, "web", "index.html"),
                        encoding="utf-8").read()
        self.assertIn(".dxs.map(Math.abs)):1.5)*0.28", renderer)

    def test_brows_slider_drives_runtime_and_forehead(self):
        # The 'half zombie' fix (2026-08-01): brows and forehead animate
        # with speech. The Brows slider is a live runtime amplitude carried
        # on rig_profile in the runtime manifest; the renderer scales both
        # gesture size and cadence by it, and the forehead skin band shifts
        # with the brow value instead of staying frozen under moving strips.
        self.assertIn("brows", rig.CONTROLS)
        # Eyebrows and forehead are SEPARATE sliders (2026-08-01), and the
        # baked brow travel reaches real human range - the old +3.5px
        # ceiling was measured live as imperceptible on screen.
        self.assertIn("forehead", rig.CONTROLS)
        self.assertEqual(rig.CONTROLS["brows"]["label"], "Eyebrows")
        # Owner-tuned (2026-08-01) once the strips actually rendered: the
        # gesture units were calibrated against invisible 15%-alpha strips,
        # so 55 reads theatrical at full opacity. 8 is the resting default.
        self.assertEqual(rig.CONTROLS["brows"]["default"], 10)
        self.assertEqual(rig.PRESETS["natural"]["brows"], 10)
        self.assertLessEqual(rig.CONTROLS["brows"]["safe_minimum"], 10)
        # The natural preset IS the owner's proven profile - a fresh upload
        # builds ready to talk, no rebuild-to-100% ritual.
        self.assertEqual(rig.PRESETS["natural"],
                         dict(lips=100, jaw=97, cheeks=100, brows=10,
                              forehead=100, nasolabial=100, nose=100))
        self.assertEqual(rig.CONTROLS["forehead"]["label"], "Forehead")
        from studio import expression
        self.assertEqual(max(expression.BROW_DY), 9.5)
        for preset in rig.PRESETS.values():
            self.assertIn("brows", preset)
            self.assertIn("forehead", preset)
        renderer = open(os.path.join(ROOT, "web", "index.html"),
                        encoding="utf-8").read()
        self.assertIn("const browGain=", renderer)
        # Gains read through liveRig(): the panel's dragged value when one
        # is broadcast for this avatar, else the published rig_profile.
        self.assertIn("Math.min(3,liveRig('brows')/55)", renderer)
        self.assertIn("(M&&M.rig_profile&&Number(M.rig_profile[key]))||55", renderer)
        # Idle life (owner request 2026-08-01): standing by, the face does
        # more than blink - a breathing-rate jaw sway plus occasional soft
        # lip press / parting / swallow, washed in at 15-30% alpha from the
        # pose-locked viseme plates so it never reads as speech.
        self.assertIn("function idleMouthFor", renderer)
        self.assertIn("function idleJawDrift", renderer)
        self.assertIn("speaking?shape:idleJawDrift(now)", renderer)
        # Retuned twice against the live desktop (2026-08-01): first pass
        # sat below notice, second pass reads but stayed lips-closed. Now
        # the parted shapes ('ih', 'E') run strong enough that a hint of
        # teeth deliberately shows, presses/swallows wash at 42-64%, and
        # the jaw and cheeks lean into each gesture through idleEnv().
        self.assertIn("r<0.25?'PP':r<0.50?'ih':r<0.65?'E':r<0.85?'SS':'nn'",
                      renderer)
        self.assertIn("?0.32+Math.random()*0.16:0.42+Math.random()*0.22",
                      renderer)
        self.assertIn("function idleEnv", renderer)
        self.assertIn("0.34*idleEnv(now)", renderer)
        self.assertIn("0.38*idleEnv(now)", renderer)
        self.assertIn("plate(idleWash.img,mouthWarp?mouthWarp.cur:0)", renderer)
        self.assertIn("/Math.max(0.55,browGain())", renderer)
        # Brow and forehead are SEPARATE tissues (2026-08-01): the strips
        # flick fast and asymmetric; the forehead runs its own damped
        # follower - engaged only past a real raise, saturating, barely
        # following a frown, settling a beat behind.
        self.assertIn("function foreheadFor", renderer)
        self.assertIn("foreheadFor(bv);", renderer)
        self.assertIn("target>foreheadLift?0.10:0.045", renderer)
        # Three sovereign tissues (2026-08-01): forehead warp pinned at the
        # brow line and absorbed at the hairline; brow strips repaint their
        # own boxes; from the brow line DOWN the plate is identity - the
        # earlier band stretched across the eye region while eye tiles
        # stamped at fixed positions, splitting the eyes horizontally.
        self.assertIn("Math.min(M.brow.l.box[1],M.brow.r.box[1])", renderer)
        self.assertIn("Math.max(-6,Math.min(2,-foreheadLift*1.15))", renderer)
        self.assertIn("[browTop,Ym,browTop+dy,Ym+dy]", renderer)
        self.assertIn("const foreheadGain=", renderer)
        self.assertIn("const browRange=", renderer)
        # Eyebrows/Forehead apply LIVE: the panel hands dragged values to
        # the desk through same-origin storage, scoped to the avatar slug;
        # the published profile is the baseline when no override exists.
        self.assertIn("vivieen-live-rig", renderer)
        self.assertIn("LIVE_RIG.slug===M.avatar.slug", renderer)
        settings_src = open(os.path.join(ROOT, "web", "settings.html"),
                            encoding="utf-8").read()
        self.assertIn("key === 'brows' || key === 'forehead'", settings_src)
        self.assertIn("localStorage.setItem('vivieen-live-rig'", settings_src)
        # Flexible brows (2026-08-01): accents ride the SPEECH RHYTHM
        # (every other stressed vowel onset), the pair can knit toward
        # centre or spread via a second baked axis, single-brow gestures
        # exist, and the calibration panel's landscape answers the
        # Eyebrows/Forehead sliders through their own regions.
        self.assertIn("browBeat=!browBeat;", renderer)
        self.assertIn("function browSqueezeFor", renderer)
        self.assertIn("browBiasL", renderer)
        self.assertIn("nearest(sqs,browSqueezeFor(performance.now()))", renderer)
        from studio import expression as expr
        self.assertEqual(expr.BROW_SQ, [-1.8, 0.0, 2.4])
        self.assertIn("brows", rig.REGION_GROUPS)
        rig_source = open(os.path.join(ROOT, "studio", "rig.py"),
                          encoding="utf-8").read()
        self.assertIn('regions["forehead"]', rig_source)
        settings = open(os.path.join(ROOT, "web", "settings.html"),
                        encoding="utf-8").read()
        self.assertIn("'forehead', 'brows', 'cheeks'", settings)
        export_source = open(os.path.join(ROOT, "studio", "export.py"),
                             encoding="utf-8").read()
        self.assertIn('sqs=expr["brow"].get("sqs"', export_source)
        app_src = open(os.path.join(ROOT, "server", "app.py"),
                       encoding="utf-8").read()
        self.assertIn('forehead: float = _rig_control_field("forehead")', app_src)
        app_source = open(os.path.join(ROOT, "server", "app.py"),
                          encoding="utf-8").read()
        self.assertIn('brows: float = _rig_control_field("brows")', app_source)

    def test_upper_lip_floor_tracks_the_lips_slider(self):
        # Fourth live rejection 2026-08-01: lips at 0 hit 'nose lock
        # suppresses upper lip 0.0%' against a hardcoded 78% floor. The
        # invariant is relative - the lip weight must track the slider -
        # so a deliberate low-lips profile passes and a nose mask that
        # eats requested lip motion still fails.
        source = open(os.path.join(ROOT, "studio", "anatomy.py"),
                      encoding="utf-8").read()
        self.assertNotIn("max(78.0", source)
        self.assertIn('max(0.0, profile["lips"] - 3.0)', source)
        self.assertIn("lips target", source)
        # Sixth live rejection: Nose at 100% could never pass min(12,
        # nose+2). Under experimental profiles the nose, lip, and shadow
        # checks flag with the suggested green band instead of raising;
        # green-band profiles still raise, with the suggestion included.
        self.assertIn("def flag(message, suggestion):", source)
        self.assertIn("structure_warnings", source)
        self.assertIn("speech mask reaches the nose", source)
        settings = open(os.path.join(ROOT, "web", "settings.html"),
                        encoding="utf-8").read()
        self.assertIn("line.includes('ADVISORY')", settings)

    def test_dental_qa_is_advisory_under_experimental_targets(self):
        # Third live rejection 2026-08-01: 'nn non-canonical upper pixels
        # 35.0%' at folds 100 / jaw 100. Sliders outside their green bands
        # are the user's declared intent to trade canonical anatomy for
        # expression, so the canonical-teeth thresholds turn ADVISORY there
        # (reported, logged, recorded on the manifest). Green-band profiles
        # keep every strict gate.
        self.assertEqual(anatomy._experimental_keys(rig.PRESETS["natural"]), [])
        self.assertEqual(
            anatomy._experimental_keys(
                dict(rig.PRESETS["natural"], jaw=130, nasolabial=130)),
            ["jaw", "nasolabial"])
        source = open(os.path.join(ROOT, "studio", "anatomy.py"),
                      encoding="utf-8").read()
        self.assertIn("if violations and not advisory:", source)
        self.assertIn("advisory=advisory", source)
        self.assertIn("dental_warnings", source)
        self.assertIn("ADVISORY past canonical bounds", source)

    def test_missing_dental_donor_is_reported_not_fatal(self):
        # The live rejection 2026-08-01, second act: a face whose speech
        # shapes never expose a full tooth row failed with 'no canonical
        # upper/lower dental-row donor' - though canonicalize_teeth itself
        # skips the lock gracefully in exactly that case. The QA reports
        # missing rows in its summary now instead of vetoing the rebuild.
        source = open(os.path.join(ROOT, "studio", "anatomy.py"),
                      encoding="utf-8").read()
        self.assertNotIn("no canonical", source)
        self.assertIn("missing_dental_rows=missing", source)
        self.assertIn("row not visible (lock skipped)", source)

    def test_calibration_gate_warns_on_mild_overshoot_and_stops_broken(self):
        # The live rejection 2026-08-01: TH at 0.109 against a 0.09 target
        # vetoed the whole rebuild, though the sliders advertise full,
        # deliberately experimental control. Mild overshoot (<=1.35x the
        # target) now publishes with a warning; anatomically broken shapes
        # still stop the rebuild.
        source = open(
            os.path.join(ROOT, "studio", "build.py"), encoding="utf-8").read()
        marker = source.index("def recompose_avatar")
        window = source[marker:marker + 4200]
        self.assertIn("- published with this ", window)
        # The final contract (2026-08-01, after seven live rejections): a
        # REBUILD of retained renders never blocks on articulation or
        # profile-shaped QA, green band or red - everything publishes as
        # ADVISORY lines naming the suggested green bands.
        self.assertIn("never blocks on articulation", window)
        self.assertIn("soft_overs = list(over)", window)
        self.assertIn("_band_suggestion(experimental)", window)
        self.assertEqual(
            build._band_suggestion(["nose"]),
            "Nose base and nostrils 0–110%")
        anatomy_source = open(os.path.join(ROOT, "studio", "anatomy.py"),
                              encoding="utf-8").read()
        self.assertIn("advisory = True", anatomy_source)
        self.assertIn(
            'over_articulated=[row["name"] for row in soft_overs]', source)
        # TH at 0.109 vs 0.09: soft (0.109 <= 0.09*1.35+eps). At 0.13: hard.
        self.assertLessEqual(0.109, 0.09 * 1.35 + measure.APERTURE_DETECTOR_EPSILON)
        self.assertGreater(0.13, 0.09 * 1.35 + measure.APERTURE_DETECTOR_EPSILON)


class PublishTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_avatars = build.AVATARS
        build.AVATARS = self.temp.name
        self.slug = "test-avatar"
        self.directory = build.adir(self.slug)
        os.makedirs(self.directory)
        self._write_artifacts(self.directory, b"live")
        build.write_manifest(self.slug, {"status": "ready", "version": 1})

    def tearDown(self):
        build.AVATARS = self.original_avatars
        self.temp.cleanup()

    @staticmethod
    def _write_artifacts(root, content):
        for artifact in build.RIG_ARTIFACTS:
            path = os.path.join(root, artifact)
            if "." in artifact:
                with open(path, "wb") as handle:
                    handle.write(content + artifact.encode())
            else:
                os.makedirs(path)
                with open(os.path.join(path, "payload.bin"), "wb") as handle:
                    handle.write(content + artifact.encode())

    @staticmethod
    def _digest(path):
        sha = hashlib.sha256()
        if os.path.isfile(path):
            with open(path, "rb") as handle:
                sha.update(handle.read())
        else:
            for root, directories, files in os.walk(path):
                directories.sort()
                for name in sorted(files):
                    full = os.path.join(root, name)
                    sha.update(os.path.relpath(full, path).encode())
                    with open(full, "rb") as handle:
                        sha.update(handle.read())
        return sha.hexdigest()

    def _state(self):
        names = list(build.RIG_ARTIFACTS) + ["manifest.json"]
        return {
            name: self._digest(os.path.join(self.directory, name))
            for name in names
        }

    def test_mid_publish_failure_restores_exact_live_bytes(self):
        stage = tempfile.mkdtemp(prefix=".rig-stage-", dir=self.directory)
        self._write_artifacts(stage, b"stage")
        baseline = self._state()
        real_replace = os.replace
        staged_moves = 0

        def fail_third_staged_move(source, target):
            nonlocal staged_moves
            if stage in source:
                staged_moves += 1
                if staged_moves == 3:
                    raise OSError("injected publish failure")
            return real_replace(source, target)

        with mock.patch.object(
                build.os, "replace", side_effect=fail_third_staged_move):
            with self.assertRaisesRegex(OSError, "injected publish failure"):
                build._publish_stage(
                    self.slug, stage,
                    {"status": "ready", "version": 2},
                )
        self.assertEqual(self._state(), baseline)
        leftovers = [
            name for name in os.listdir(self.directory)
            if name.startswith(".rig-live-")
        ]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
