# Walk cycle loop spec — why every walk style breaks, and the fix

Measured from `walk-alpha.mov` (32 frames, 24 fps, ProRes 4444, 256×384, style: *runway catwalk*).

---

## 1. What is actually wrong\n
The clip contains **exactly one step. A loop needs two.**

Two independent estimators agree:

| method | full gait cycle |
|---|---|
| legs-passing → legs-passing (×2) | 67.1 frames |
| stride length ÷ ground speed | 60.9 frames |
| **consensus** | **≈ 64 frames = 2.67 s** |

Rendered: **32 frames = 50.0 % of one gait cycle.**

### Why one step is not half a loop

This is the part that makes it counter-intuitive, and it is why the bug survived review:

- **Legs repeat every HALF cycle.** After one step the silhouette looks nearly identical — just with left and right legs swapped. In a side view you can barely tell. So the legs *almost* get away with it.
- **Arms repeat every FULL cycle.** The near arm swings forward on step 1 and backward on step 2. A half-cycle clip only ever contains the forward half.

Measured on the audience-side hand, relative to the hip line:

- travels **70 px in front** of the hip ✅
- travels **1 px behind** the hip ❌

The arm swings forward, returns to the hip, and the clip ends. On loop it teleports **24.8 px** — **6.9× a normal frame step** — back to the start of the forward swing. That is the flaw you are seeing.

There is also a smaller second defect: the clip stops ~1.5 frames *short* of even a clean half cycle (feet are still 32 px apart at frame 31, closing at 15 px/frame). So the legs stutter slightly too — silhouette IoU at the seam is 0.783 vs 0.906 typical.

---

## 2. Why it hits every walk style

The frame budget is **hard-coded at 32**, but the cadence is **set by the style**. Nothing ties them together, so the fraction of a cycle you capture is effectively arbitrary per style:

```
coverage = 32 frames / (cadence-derived cycle length)
```

Runway catwalk is a slow, deliberate stride — ~45 steps/min, a 2.67 s cycle — so 32 frames at 24 fps lands on 50 %. A brisk walk would land somewhere else entirely. **Every style is wrong; they are just wrong by different amounts.** Catwalk happens to be wrong by exactly half, which is the most visible failure mode.

---

## 3. The invariant to enforce

> The N rendered frames must sample phase `0/N, 1/N, … (N−1)/N` of **one full gait cycle (two steps)**.
> Frame N is identical to frame 0 by definition and **must not be rendered**.

That second sentence matters: rendering the closing pose as an extra frame gives a 1-frame stutter at the seam. Sample the half-open interval `[0, 1)`, never `[0, 1]`.

---

## 4. Two ways to satisfy it

### Option A — keep 32 frames, vary playback fps  *(recommended)*

Keeps the 8×4 atlas budget intact. The generator must produce a **full two-step cycle**, resampled to 32 frames; playback rate then encodes the style's cadence.

```
playback_fps = 32 / cycle_seconds
cycle_seconds = 120 / cadence_steps_per_min
```

| style | cadence (steps/min) | cycle | playback fps @ 32 frames |
|---|---|---|---|
| runway catwalk | 45 | 2.67 s | **12** |
| slow stroll | 70 | 1.71 s | 19 |
| casual walk | 100 | 1.20 s | 27 |
| brisk walk | 120 | 1.00 s | 32 |
| power walk | 140 | 0.86 s | 37 |

For this asset: **32 frames played at 12 fps**, not 24.

### Option B — keep 24 fps, vary frame count

Smoother, but the atlas size changes per style: `N = round(24 × cycle_seconds)`. Catwalk → 64 frames.

---

## 5. Generation prompt changes

The generator is being asked for "a walking animation", which gives you whatever fragment fits the duration. Ask for the cycle explicitly:

**Add:**
- "Exactly **two full steps**: left foot passes right, then right foot passes left, returning to the identical starting pose."
- "**Seamless loop** — the final frame must flow into the first frame with no jump."
- "Both arms complete a full swing: each hand passes **in front of and behind** the hip."
- "Camera locked. Character walks **in place**, no drift."

**Set duration from cadence, not from the frame budget.** A catwalk stride cannot fit in 1.33 s — that request is physically impossible to satisfy, so the model truncated it. Give it 2.7 s and resample down.

---

## 6. Horizon-walk scroll speed (bonus — prevents foot skate)

Measured ground travel: 3.49 px/frame at a 345 px figure → **223 px per full cycle ≈ 0.65 × figure height**.

```
scroll_px_per_sec = 0.65 × character_height_px / cycle_seconds
```

If the horizon translation per loop does not match this, the feet skate even with a perfect cycle. For this asset at 345 px and 2.67 s: **83 px/s**.

---

## 7. Gate it in CI

`walkloop.py` measures any generated clip and fails the three things that break loops:

```bash
python3 walkloop.py walk-alpha.mov --fps 24 --chart report.png
```

| check | threshold |
|---|---|
| cycle coverage | 0.97 – 1.03 |
| seam jump vs typical frame step | ≤ 1.6× |
| arm back/forward swing ratio | ≥ 0.45 |

Current asset fails all three. Run it on every style before shipping.

---

## 8. About this specific file

`walk-alpha.mov` **cannot be repaired into a true loop.** Half the gait cycle was never rendered, and the missing half is not recoverable from the frames that exist — in a side view the second step is the same motion with near/far limbs swapped, which is not an image transform.

Specifically, do **not** try to patch it with:

- **Ping-pong / boomerang** — reverses the gait; heel-toe mechanics run backwards.
- **Horizontal mirroring** — flips the walk direction.
- **Trimming to a clean half cycle** — the legs would loop fine, but the arm period is a *full* cycle, so the arm still snaps every loop. This is the trap: it looks like a fix and isn't.

Regenerate at ≈ 2.7 s of motion covering two steps, resample to 32 frames, play at 12 fps.
