"""Does the mouth land where the sound is - for every engine, not just Kokoro?

Kokoro's track is derived from its own duration predictor, so it is exact by
construction and only serves as the ceiling here. The question worth asking is
how far the ESTIMATED path (cloud engines that return audio and nothing else)
falls below that ceiling, and whether warping the phoneme timeline against the
measured energy envelope actually beats naively stretching it to fit.

Method: turn the viseme track into a graded openness curve, cross-correlate it
against the audio's RMS envelope, and report the lag of the peak. A track that
matches the speech peaks at lag 0. A binary open/shut signal was useless for
this - both curves sit ~70% "on" and the correlation only sees silence gaps -
so openness is graded by how far the jaw actually drops for each viseme.
"""
import os, sys, asyncio, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, ROOT)
import providers as P
import align
import visemes as VZ

TEXT = ("The market is wrong, but you knew that. "
        "Positioning is stretched and nobody wants to say it out loud.")

OPENNESS = {"sil": .0, "PP": .0, "FF": .15, "SS": .2, "CH": .3, "TH": .3,
            "nn": .3, "DD": .35, "kk": .35, "RR": .35, "ou": .35,
            "ih": .45, "E": .7, "oh": .8, "aa": 1.0}
RATE = 200                      # 5 ms grid


def score(track, y):
    dur = len(y) / P.SR
    n = max(4, int(dur * RATE))
    op = np.zeros(n)
    for i, (t, v) in enumerate(track):
        t2 = track[i + 1][0] if i + 1 < len(track) else dur
        op[int(t * RATE):int(t2 * RATE)] = OPENNESS.get(v, .4)
    win = P.SR // RATE
    rms = np.sqrt(np.array([(y[i * win:(i + 1) * win] ** 2).mean()
                            for i in range(n)]) + 1e-12)
    env = rms / (rms.max() or 1)
    a = np.convolve(op, np.ones(5) / 5, "same")
    a = a - a.mean()
    b = env - env.mean()
    if a.std() < 1e-9 or b.std() < 1e-9:
        return 0, 0.0, op.mean()
    lags = np.arange(-40, 41)
    cc = [float(np.corrcoef(np.roll(a, L), b)[0, 1]) for L in lags]
    k = int(np.argmax(cc))
    return int(lags[k]) * 5, float(cc[k]), float(op.mean())


def naive(text, y):
    """The obvious implementation: lay phonemes down by nominal length and scale
    the whole thing to the clip. This is the baseline the energy warp has to beat."""
    total = len(y) / P.SR
    ps = align.phonemise(text)
    items = [c for c in ps if c not in VZ.CARRY or c == " "]
    items = [c for c in items if c != " "]
    if not items:
        return [[0.0, "sil"]]
    w = np.array([align._dur(align._klass(c)) for c in items])
    w = w / w.sum() * total
    ev, t = [[0.0, "sil"]], 0.0
    for c, d in zip(items, w):
        ev += align._emit(c, t, d)
        t += d
    ev.append([total, "sil"])
    ev = [[max(0.0, q - VZ.LEAD), v] for q, v in ev]
    return VZ._resolve(ev)


async def run(pid, **over):
    cfg = dict(P.DEFAULTS["tts"]); cfg["provider"] = pid; cfg.update(over)
    t0 = time.time()
    y, al = await P.speak(TEXT, cfg)
    el = time.time() - t0
    track, dur, tier = align.build(TEXT, y, al)
    lag, r, op = score(track, y)
    changes = len(track) / max(dur, .01)
    print(f"  {pid:<12} {tier:<10} {dur:5.2f}s  {len(track):3d} events "
          f"({changes:4.1f}/s)  lag {lag:+4d} ms  r={r:.3f}  synth {el:.1f}s")
    return dict(pid=pid, tier=tier, lag=lag, r=r, n=len(track), dur=dur, y=y)


async def main():
    print(f"text: {TEXT!r}\n")
    print("ENGINE       TIER       DUR    EVENTS          LAG        FIT")
    rows = []
    for pid, over in [("kokoro", {}), ("system", {"voice": "Samantha"}),
                      ("edge", {"voice": "en-US-AvaNeural"})]:
        try:
            rows.append(await run(pid, **over))
        except Exception as e:
            print(f"  {pid:<12} unavailable: {str(e)[:70]}")

    # The comparison that matters: same audio, energy-warped vs naive stretch.
    est = next((q for q in rows if q["tier"] == "estimated"), None)
    if est is not None:
        y = est["y"]
        w_lag, w_r, _ = score(align.build(TEXT, y, None)[0], y)
        n_lag, n_r, _ = score(naive(TEXT, y), y)
        print(f"\nsame {est['pid']} audio, two ways of estimating:")
        print(f"  energy-warped   lag {w_lag:+4d} ms   r={w_r:.3f}")
        print(f"  naive stretch   lag {n_lag:+4d} ms   r={n_r:.3f}")
        d = w_r - n_r
        print(f"  -> warp is {d:+.3f} on fit and {abs(n_lag)-abs(w_lag):+d} ms closer to zero lag")

    print("\nverdict:")
    for q in rows:
        ok = abs(q["lag"]) <= 25 and q["r"] >= .45
        fine = abs(q["lag"]) <= 60 and q["r"] >= .30
        print(f"  {q['pid']:<12} {'GOOD' if ok else 'ACCEPTABLE' if fine else 'POOR'}")

if __name__ == "__main__":
    asyncio.run(main())
