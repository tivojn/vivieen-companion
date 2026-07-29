# Follow EnConvo — why the mouth skips words

**Symptom:** with Follow EnConvo on, the voice says every word but the mouth only
moves on some of them. Whole phrases come out with a frozen face.

**Verdict:** not a timing/offset bug. The follow path is *structurally* discarding
83% of the mouth movement it computes. Measured, not guessed.

Harness: `/tmp/vv_probe.py` — faithful ports of the Swift `AudioFeatureAnalyzer`,
the JS `byExternalEnergy()` classifier, and the JS `stabiliseExternalViseme()`
stabiliser, run over the real capture `qa/proof/enconvo-driven-voice.wav`
(18.62 s, 10.2 s voiced, 80 syllable nuclei -> 7.8 syll/s).

---

## The measurement

| | |
|---|---|
| Speech rate in the clip | 7.8 syllables/s -> **~168 viseme changes expected** |
| Classifier proposed | **234** viseme changes |
| Stabiliser confirmed | **39** -> 3.8 changes/s |
| Reached the face | **17%** |
| Longest frozen mouth *while actively speaking* | **2,432 ms** (@13.4 s) |
| Next worst | 1,600 ms @5.4 s · 1,152 ms @17.4 s · 1,088 ms @8.5 s · 1,056 ms @10.9 s |

2.4 seconds of continuous speech at 7.8 syll/s is roughly **8–10 words spoken with
a motionless mouth**. That is the reported bug, reproduced numerically.

---

## Root cause 1 — the confirmation gate is unreachable

`web/index.html:648` `stabiliseExternalViseme()` requires **3 consecutive identical
candidate packets** (`minimumSamples`) before it will move the mouth.

Measured candidate stream: **235 runs over 582 packets, mean run 2.48 packets (79 ms).**

> **70% of candidate runs are shorter than 3 packets** — they are mathematically
> incapable of ever confirming, no matter how long the audio plays.

The counter resets to 1 on every disagreement (`externalCandidateSamples=1`), so it
is reset **190 times** in 18 seconds. During fast passages it never reaches 3 and
the pose is never updated at all — the mouth locks on the last confirmed shape and
rides through several words.

Blocked-by breakdown: sample-count 78 · confirm-time 100 · 110 ms hold 13.
The 110 ms `EXTERNAL_MIN_HOLD_MS` is *not* the main culprit.

## Root cause 2 — the classifier dithers on its own thresholds

Why are the runs so short? `byExternalEnergy()` (`web/index.html:1055`) uses hard
cutoffs with **no hysteresis**, and the signals sit right on top of them:

- **36% of all speech packets** sit within ±10% of the `low>0.58` boundary — the
  most-used branch.
- 15% straddle `mid>0.40`; 9% straddle `amp>0.045` and `amp>0.05`.

Worse, the largest single category of viseme flips is **`low>0.58` -> `low>0.58`
(51 flips)**: the *spectral* branch did not change at all. Only the amplitude
sub-gate flipped, swapping `aa`<->`oh` because RMS wobbled across 0.045 mid-vowel.
Same pattern for `SS`<->`CH` on `high>0.54` (12 flips). A further 32 events are
A->B->A flicker where B lasted <=2 packets (14% of all runs).

**These flips carry no articulatory information — they are amplitude ripple being
read as a new mouth shape, and each one resets the confirmation counter.**

## Root cause 3 — the packet rate is 32 ms, not the 25 ms the design assumes

`enconvo_audio_tap.swift:235` throttles to `>= 25 ms`, but emission only happens on
buffer boundaries. With ~10.7 ms buffers that quantises to **every 3rd buffer = 32 ms
measured**. The renderer comment at `web/index.html:628` explicitly assumes "~25ms".

3 packets x 32 ms = 64 ms minimum before *any* change, plus the 110 ms floor between
changes -> a hard ceiling of ~9 mouth changes/s against speech that needs 12–16.

---

## Why tuning the constants will not fix it

Sensitivity sweep on the real capture:

| Configuration | Changes | Rate |
|---|---|---|
| shipped | 39 | 3.8/s |
| samples 3->2 | 39 | 3.8/s |
| hold 110->60 ms | 41 | 4.0/s |
| confirm 55->35 ms | 39 | 3.8/s |
| samples 2 + hold 60 + confirm 35 | 41 | 4.0/s |
| **samples 1** + hold 40 + confirm 25 | **114** | 11.2/s |

Loosening all three constants together buys **2 extra mouth movements in 18 seconds.**
Only removing the consecutive-agreement requirement entirely recovers the rate —
because the input stream itself dithers, *any* consecutive-agreement rule collapses.

**The fix belongs in the classification, not the confirmation.**

---

## Root cause 4 — the expressive range is tiny (separate, also real)

The avatar ships **15 visemes**. The follow path can only ever emit **8**, and in
practice showed **5**.

Candidate distribution over 582 packets:
`aa` 266 (46%) · `oh` 93 · `CH` 76 · `sil` 70 · `ou` 54 · `SS` 17 · `ih` 3 · `E` 3

**`PP`, `FF`, `TH`, `DD`, `nn`, `kk`, `RR` are never emitted at all** — every /m/,
/b/, /p/, /f/, /v/ renders as an open vowel. The mouth never closes on a bilabial.
Even perfectly timed, this would read as wrong.

Cause: the band split is two **first-order** one-poles at 520 Hz and 2400 Hz
(`enconvo_audio_tap.swift:186`). At 6 dB/octave the bands overlap so heavily that F1
and F2 cannot be separated, so most vowels collapse into the same branch.

---

## How this shipped

`qa/enconvo_monitor_qa.js::testExternalVisemeStability` asserts only that jitter is
*rejected* — its own assertion strings are `"single-packet spectrum changes do not
flip the mouth"` and `"re-reading one packet on render frames cannot confirm a pose"`.

There is **no test asserting that a genuine fast articulation reaches the face.**
The suite is one-sided, so tightening the gate always looked like a pass. The test
also feeds packets at 25 ms spacing, which the helper does not actually deliver.

---

## Recommended fixes, in order of payoff

1. **Stop letting amplitude choose viseme identity.** Remove the `amp>0.045` /
   `amp>0.035` / `amp>0.05` sub-gates from `byExternalEnergy()`. Amplitude should
   drive **jaw opening** — a continuous parameter the renderer already has via
   `level()` — not shape selection. This alone kills 63 of the identity flips.
   The comment at `web/index.html:632` already states this intent ("the jaw envelope
   stays immediate; only categorical texture swaps are paced"); the classifier
   violates it.

2. **Replace classify-then-confirm with score-and-smooth.** Compute a continuous
   score per viseme each packet, EMA the scores (~40–50 ms), take the argmax. A
   stable argmax over smoothed scores changes when articulation changes, not when a
   signal grazes a boundary — and it needs no consecutive-sample counter.

3. **Add hysteresis** to any threshold that survives: separate enter/exit bands
   (e.g. enter `low>0.60`, exit `low<0.56`).

4. **Drop the emission throttle from 25 ms to ~10 ms** (`enconvo_audio_tap.swift:235`).
   One extra JSON line per buffer is negligible and triples temporal resolution.

5. **Give the classifier real bands.** A small FFT, or 4–5 steeper (4th-order)
   bands, so rounded and closed shapes become reachable.

6. **Best fix if it is available at all:** EnConvo knows the text it is speaking.
   If the companion can obtain the utterance text, `align.py`'s `estimated` tier is
   measured at **67 ms median onset error** — far better than any blind audio
   classifier can do. Audio-only should be the fallback, not the primary path.

7. **Add the missing test:** drive the stabiliser with the real capture and assert a
   floor — e.g. ">= 60% of proposed changes reach the face" and "no frozen-mouth
   interval > 400 ms while active". Without a responsiveness assertion the gate will
   drift tight again.

---

## Fix shipped (2026-07-29, branch `lipsync-median-vote`)

Two of the fixes above landed, plus the missing test:

- **Fix 1** — `byExternalEnergy()` no longer gates viseme identity on
  amplitude; shapes come from spectral ratios alone and loudness keeps
  driving jaw opening through `lvl` in `jawFor()`. Measured alone:
  rendered changes 39 → 56, worst freeze 2,432 ms → 800 ms.
- **Fix 2 (variant)** — instead of EMA'd scores, `stabiliseExternalViseme()`
  now keeps the last 5 candidate packets and renders the **majority vote
  centred two packets back** (~64 ms of mouth lag, under the ~125 ms
  video-late threshold). Dither runs of 1–2 packets lose the vote; real
  syllables (~4 packets) always pass. The consecutive-sample counter,
  hold and confirm timers are gone. Ties go to the centre packet. The
  tail self-flushes because inactive packets keep voting `sil`.
- **Fix 7** — `qa/enconvo_replay_qa.js` replays the real capture through
  the *shipped* classifier and stabiliser (extracted verbatim from
  `web/index.html`) and asserts ≥6 mouth changes/s, ≥40% of proposals
  rendered, and no mid-speech freeze over 800 ms. It runs in `npm test`.
  `qa/enconvo_monitor_qa.js` now feeds 32 ms packets and asserts that a
  3-packet syllable always reaches the face.

Measured on `qa/proof/enconvo-driven-voice.wav`, both fixes combined:
**39 → 141 rendered changes (7.9/s against 7.8 syllables/s spoken), 17% →
55% of proposals, worst freeze 2,432 ms → 448 ms.**

Deliberately not fixed here: `PP FF TH DD nn` are still never proposed —
bilabial closure needs richer bands or text alignment (fixes 5/6), and
the 25 ms tap throttle (fix 4) is untouched. Those remain open.
