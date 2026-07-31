# Vivieen packaging plan — size, models, and the Pro-matte download

*2026-07-31 · measured against the v0.5.0 DMG (227.5 MB)*

## 1. Where the app stands today

The shipped app is **fully self-contained**: nothing downloads at first run,
and no model is required post-install.

- **Alpha cutting in the DMG uses macOS Vision** (the `person-cutout` Swift
  helper) plus the pure-code whiteness refiner (`_refine_white_matte`).
  Vision ships with macOS → **0 bytes** in the DMG.
- **RVM (Robust Video Matting)** — the higher-quality torch matte added
  2026-07-31 — is **dev-only by design**: `_rvm_runtime()` in
  `studio/motion.py` probes for torch and silently falls back to Vision.
  Two reasons it is not bundled:
  1. torch is 409 MB installed;
  2. **RVM is GPL-3.0** — bundling it in the DMG would attach GPL
     obligations to the whole distribution. A user-initiated download keeps
     the shipped app GPL-free.
- The MLX voice stack (mlx, mlx_whisper — 179 MB+) and torch's dependency
  tree are already excluded via the `extraResources` filter in
  `package.json`.

### Measured composition (what actually ships, uncompressed)

| Component | Size | Verdict |
|---|---|---|
| Electron framework | ~230 MB | fixed cost; ~85 MB compressed — the floor |
| opencv-**contrib**-python (`cv2`) | 138 MB | overbuilt: we use **no** contrib modules and **no** GUI calls (verified by grep) |
| scipy | 53 MB | transitive only — nothing in `server/` or `studio/` imports it |
| mediapipe | 51 MB | needed: the 478-landmark face model behind build/calibration/lip-sync |
| Python 3.12 runtime | 44 MB | needed |
| matplotlib + fontTools | 34 MB | mediapipe metadata baggage; we never call its drawing utils |
| numpy, PIL, ffmpeg, misc | ~35 MB | needed |

## 2. Phase 1 — static pruning (no UX change, no downloader)

Target: **227 MB → ~150–170 MB DMG.**

1. **Swap `opencv-contrib-python` → `opencv-python-headless`** in
   `requirements-electron.lock` (relock with `uv pip compile
   --generate-hashes`, WITHOUT `--python-platform` — the flag stopped
   resolving for torch). mediapipe declares `opencv-contrib-python` but only
   needs the cv2 API; pip's resolver is not consulted in the staged
   site-packages copy, so the headless build satisfies it.
2. **Exclude from `extraResources`**: `scipy`, `matplotlib`, `fontTools`,
   `kiwisolver`, `cycler`, `pyparsing`, `dateutil` (matplotlib's tail).
   Gate: a **packaged-app smoke test** that runs the bundled Python with the
   pruned site-packages and executes `import mediapipe` + one
   `build_keyframe` pass — mediapipe imports matplotlib only through its
   drawing utils, but this must be *proven* against the shipped layout, not
   assumed (add to `scripts/` beside the GPL audit).
3. Keep the existing GPL-free audit green.

Rules of thumb kept from earlier packaging work: UDBZ DMG, voice stack
excluded, Developer ID "THE GREAT LIONHEART PTE. LTD.", not notarized.

## 3. Phase 2 — "Pro matte (RVM)" download-on-demand (opt-in)

Not a first-run requirement — an upgrade card. The app is complete without
it; a failed or skipped download can never break a launch.

- **What downloads** (real, current numbers):
  - `torch-2.13.0-cp312-macosx_14_0_arm64.whl` — **111.2 MB**
  - `torchvision-0.28.0-cp312-macosx_14_0_arm64.whl` — **1.9 MB**
  - `rvm_mobilenetv3.pth` weights + pinned RVM code snapshot — **~16 MB**
  - Total ≈ **130 MB download, ~460 MB on disk**, one-time.
- **Where**: `~/Library/Application Support/Vivieen/pro-matte/` — wheels are
  zips, extracted without pip; the backend prepends the directory to
  `sys.path` when present. `VIVIEEN_NO_RVM` stays as the kill switch.
- **Integrity**: pinned URLs + sha256 for every artifact (PyPI wheel hashes,
  a pinned RVM commit tarball, the release .pth checksum). No live
  `torch.hub` fetch in the packaged flow.
- **UX** (per the 2026-07-31 request): a card in Settings → Models showing
  size up front + the GPL-3.0 notice; download runs as a server job
  reporting **bytes done / total, %, speed, and ETA** through the existing
  jobbox progress UI; resumable via HTTP Range on failure.
- **Payoff moment**: when the download completes, RVM becomes the matte
  backend automatically and the card offers one-click **Re-cut** — the
  retained raw takes reprocess through `motion.recut()` at zero generation
  cost, so existing walk/idle/move sets visibly clean up (measured edge
  softness 1.33 vs 1.08 for Vision).

## 4. Explicitly NOT planned

- Bundling torch/RVM in the DMG (size + GPL).
- Moving mediapipe behind a download — it is required for the core avatar
  build, and 51 MB does not justify a mandatory first-run download with its
  offline/notarization failure modes.
- Any first-run blocking download. Vision remains the always-works default.

## 5. Order of work

1. Phase 1 pruning + packaged smoke test + relock → release as the next
   DMG (this is where the size drop lands).
2. Phase 2 Pro-matte flow (server download job + Settings card + progress +
   re-cut hook) → its own release.
