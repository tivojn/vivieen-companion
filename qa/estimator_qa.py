"""Measure the ESTIMATED aligner against ground truth, and tune it there.

The acoustic metric in align_qa.py compares a track to an RMS envelope, which is
noisy: openness and loudness are only loosely related, so it tops out around
r=0.45 even for a track that is correct by construction. Tuning against it would
be tuning against its noise.

There is a better instrument available. Kokoro's track comes from its own
duration predictor, so for Kokoro audio we have the TRUE phoneme timing. Run the
estimator over that same audio and the two tracks can be compared directly - no
acoustics in the middle, no metric ceiling. That turns 'does it look aligned' into
'how many milliseconds off is each viseme onset', which is a number worth tuning.

Cached to /tmp so the parameter sweep does not re-synthesise 12 clips per trial.
"""
import os, sys, asyncio, itertools
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, ROOT)

CACHE = "/tmp/viv_estimator_cache.npz"

SENTS = [
    "The market is wrong, but you knew that.",
    "Positioning is stretched and nobody wants to say it out loud.",
    "Carry works until it doesn't, and then it works in reverse, violently.",
    "I would rather be early and bored than late and liquidated.",
    "Volatility is cheap here. That is not the same as being wrong.",
    "Everyone is long the same trade and calling it diversification.",
    "The curve inverted, the Fed blinked, and nothing actually changed.",
    "Risk is what is left over when you think you have thought of everything.",
    "Dollar strength is a policy choice, not an accident.",
    "If the carry is nine percent and the vol is twenty, you are not being paid.",
    "No.",
    "Emerging market debt looks cheap until the currency moves against you, "
    "and then it looks exactly as expensive as it always was.",
]

OPEN = {"sil": .0, "PP": .0, "FF": .15, "SS": .2, "CH": .3, "TH": .3,
        "nn": .3, "DD": .35, "kk": .35, "RR": .35, "ou": .35,
        "ih": .45, "E": .7, "oh": .8, "aa": 1.0}
RATE = 200


def curve(track, dur):
    n = max(4, int(dur * RATE))
    op = np.zeros(n)
    for i, (t, v) in enumerate(track):
        t2 = track[i + 1][0] if i + 1 < len(track) else dur
        a, b = int(t * RATE), int(t2 * RATE)
        if b > a:
            op[max(0, a):max(0, b)] = OPEN.get(v, .4)
    return op


def compare(est, ref, dur):
    """-> (correlation, best lag ms, median onset error ms)"""
    a, b = curve(est, dur), curve(ref, dur)
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    if a.std() < 1e-9 or b.std() < 1e-9:
        return 0.0, 0, 999.0
    r = float(np.corrcoef(a, b)[0, 1])
    lags = np.arange(-40, 41)
    cc = [float(np.corrcoef(np.roll(a, int(L)), b)[0, 1]) for L in lags]
    lag = int(lags[int(np.argmax(cc))]) * 5

    # Onset error: for every ground-truth event, the nearest estimated event
    # carrying the SAME viseme. Nearest-in-time alone would flatter a track that
    # simply has many events.
    errs = []
    for t, v in ref:
        cand = [abs(t2 - t) for t2, v2 in est if v2 == v]
        if cand:
            errs.append(min(cand))
    return r, lag, float(np.median(errs) * 1000) if errs else 999.0


async def build_cache():
    import providers as P
    import align
    print("synthesising ground truth with Kokoro…")
    cfg = dict(P.DEFAULTS["tts"])
    data = {}
    for i, s in enumerate(SENTS):
        y, al = await P.speak(s, cfg)
        ref, dur, tier = align.build(s, y, al)
        assert tier == "exact", tier
        data[f"y{i}"] = y
        data[f"r{i}"] = np.array(ref, dtype=object)
        print(f"  {i:2d}  {dur:5.2f}s  {len(ref):3d} true events   {s[:46]}")
    np.savez(CACHE, **{k: v for k, v in data.items() if k.startswith("y")},
             refs=np.array([data[f"r{i}"] for i in range(len(SENTS))], dtype=object),
             allow_pickle=True)
    print(f"cached -> {CACHE}")


def load():
    d = np.load(CACHE, allow_pickle=True)
    ys = [d[f"y{i}"] for i in range(len(SENTS))]
    refs = [[[float(t), str(v)] for t, v in r] for r in d["refs"]]
    return ys, refs


def measure(method, lead, w, pw, ys, refs, verbose=False):
    import align
    align.METHOD = method
    align.EST_LEAD = lead
    align.W_ENERGY, align.ENV_POW = w, pw
    rows = []
    for i, (y, ref) in enumerate(zip(ys, refs)):
        dur = len(y) / align.SR
        est, _t, _tier = align.build(SENTS[i], y, None)
        r, lag, err = compare(est, ref, dur)
        rows.append((r, lag, err))
        if verbose:
            print(f"  {i:2d} {dur:5.2f}s  r={r:+.3f}  lag {lag:+4d}ms  "
                  f"onset {err:5.0f}ms   {SENTS[i][:40]}")
    return rows


def summarise(rows, indices=None):
    q = rows if indices is None else [rows[i] for i in indices]
    return {
        "r": float(np.mean([x[0] for x in q])),
        "worst": float(np.min([x[0] for x in q])),
        "lag": float(np.mean([abs(x[1]) for x in q])),
        "onset": float(np.median([x[2] for x in q])),
    }


def sweep():
    """Validate the constants actually shipped in align.py.

    The small grid is retained only to prove the selected point is on a plateau
    and to run leave-one-out. Selection is by onset error, then correlation -
    onset is the direct ground-truth metric and correlation is only a tie-break.
    """
    ys, refs = load()
    naive_rows = measure("uniform", 0.0, 0.0, 1.0, ys, refs)
    shipped_rows = measure("energy", -.095, .7, .5, ys, refs)
    naive, shipped = summarise(naive_rows), summarise(shipped_rows)

    print("\nGROUND-TRUTH REGRESSION  (12 Kokoro clips; pred_dur is truth)")
    print("  method             mean r   worst r   |lag|   median onset")
    print(f"  naive stretch      {naive['r']:+.3f}   {naive['worst']:+.3f}   "
          f"{naive['lag']:5.1f}   {naive['onset']:6.1f} ms")
    print(f"  shipped energy     {shipped['r']:+.3f}   {shipped['worst']:+.3f}   "
          f"{shipped['lag']:5.1f}   {shipped['onset']:6.1f} ms")

    grid = list(itertools.product(
        [-.11, -.095, -.08, -.065, -.05, -.035],
        [.35, .45, .5, .6, .7],
        [.5, .65, .8],
    ))
    cache = {g: measure("energy", *g, ys, refs) for g in grid}
    all_indices = list(range(len(SENTS)))

    def rank(g, indices):
        s = summarise(cache[g], indices)
        return s["onset"], -s["r"]

    best = min(grid, key=lambda g: rank(g, all_indices))
    best_score = summarise(cache[best])
    loo = []
    for holdout in all_indices:
        train = [i for i in all_indices if i != holdout]
        selected = min(grid, key=lambda g: rank(g, train))
        loo.append(cache[selected][holdout][2])
    loo_onset = float(np.median(loo))

    print(f"\n  grid optimum       lead {best[0]*1000:+.0f}ms  w={best[1]:.2f}  "
          f"pow={best[2]:.2f}  onset {best_score['onset']:.1f} ms")
    print(f"  leave-one-out      median onset {loo_onset:.1f} ms  "
          f"(gap {loo_onset-best_score['onset']:+.1f} ms)")
    print(f"  improvement        {naive['onset']-shipped['onset']:.1f} ms / "
          f"{100*(naive['onset']-shipped['onset'])/naive['onset']:.0f}% lower onset error")

    assert shipped["onset"] <= naive["onset"] * .65, (naive, shipped)
    assert shipped["worst"] > 0.10, shipped
    assert abs(loo_onset - best_score["onset"]) < 12, loo_onset

    print("\nper-sentence at shipped settings:")
    measure("energy", -.095, .7, .5, ys, refs, verbose=True)
    return shipped


if __name__ == "__main__":
    if not os.path.exists(CACHE) or "--rebuild" in sys.argv:
        asyncio.run(build_cache())
    sweep()
