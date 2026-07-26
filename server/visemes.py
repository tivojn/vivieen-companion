"""Turn Kokoro's own duration-predictor output into a timed viseme track.

Kokoro is a StyleTTS2 derivative: it predicts a per-phoneme frame count
(`pred_dur`) and upsamples by it to make audio. That array IS the forced
alignment - no second model, no guessing from the waveform.

Measured on this build: hop = 600 samples @ 24 kHz = exactly 25 ms/frame, and
len(pred_dur) == len(phonemes) + 2 (BOS/EOS pads). sum(pred_dur) * 25 ms equals
the audio length to the sample, which is what makes the mapping trustworthy.

Viseme names follow the Oculus / Meta XR 15-target set:
    sil PP FF TH DD kk CH SS nn RR aa E ih oh ou
"""
import numpy as np

VISEMES = ["sil", "PP", "FF", "TH", "DD", "kk", "CH", "SS",
           "nn", "RR", "aa", "E", "ih", "oh", "ou"]

SR, HOP = 24000, 600
FRAME = HOP / SR                      # 0.025 s

# Measured: Kokoro's BOS pad over-predicts the leading silence by a varying
# 30-85 ms (mean 52), so the mouth always arrived slightly LATE. Mouth-late is
# the perceptually objectionable direction, and real articulators anticipate -
# lips seal for /p/ before the burst is audible. Leading the track corrects the
# measured bias and models that anticipation at the same time.
LEAD = 0.045

# misaki en-US IPA -> Oculus viseme
SINGLE = {
    "p": "PP", "b": "PP", "m": "PP",
    "f": "FF", "v": "FF",
    "\u03b8": "TH", "\u00f0": "TH",
    "t": "DD", "d": "DD",
    "k": "kk", "g": "kk", "\u0261": "kk", "\u014b": "kk",
    "\u0283": "CH", "\u0292": "CH", "\u02a7": "CH", "\u02a4": "CH",
    "s": "SS", "z": "SS",
    "n": "nn", "l": "nn",
    "\u0279": "RR", "r": "RR", "\u025a": "RR", "\u025d": "RR", "\u025c": "RR",
    "\u0251": "aa", "\u00e6": "aa", "\u028c": "aa", "a": "aa", "\u0250": "aa",
    "\u025b": "E", "e": "E",
    "\u026a": "ih", "i": "ih", "\u1d7b": "ih", "j": "ih", "y": "ih",
    "\u0259": "ih", "\u1d4a": "ih",          # schwa: small relaxed opening
    "\u0254": "oh", "o": "oh",
    "u": "ou", "\u028a": "ou", "w": "ou",
}

# misaki writes diphthongs as single ASCII letters. Real mouths travel through
# two shapes for these, so we emit both - this is a large part of "natural".
DIPH = {
    "A": ("E", "ih"),    # eɪ  say
    "I": ("aa", "ih"),   # aɪ  my
    "W": ("aa", "ou"),   # aʊ  now
    "Y": ("oh", "ih"),   # ɔɪ  boy
    "O": ("oh", "ou"),   # oʊ  go
}

# Stress marks AND the space between words fold their duration into the next
# phoneme. A space is NOT silence - words run together in a phrase, and emitting
# sil there snaps the mouth shut on every word boundary, which reads as a stutter.
CARRY = "\u02c8\u02cc "
SILCH = ".,!?;:\u2014\u2026()"            # real pauses only
INHERIT = "h"                            # /h/ borrows the next vowel's shape
# These consonants are articulated behind the lips. A morph-target rig can move
# the tongue without touching the lips; a photographic sprite bank cannot. A
# dedicated bitmap therefore creates a false visible mouth event for something
# the camera should barely see. Borrow the following visible lip posture instead
# (anticipatory coarticulation), exactly as /h/ already does.
COARTICULATE = set("tdkgɡŋnl")


def _chunk(phonemes: str, pred_dur) -> tuple[list, float]:
    pd = np.asarray(pred_dur, dtype=np.float64).ravel()
    body = pd[1:1 + len(phonemes)]
    t = float(pd[0]) * FRAME                 # leading silence from the BOS pad
    ev: list[list] = [[0.0, "sil"]]
    carry = 0.0
    for ch, d in zip(phonemes, body):
        dur = float(d) * FRAME
        if ch in CARRY:
            carry += dur
            continue
        dur += carry
        carry = 0.0
        if ch in DIPH:
            a, b = DIPH[ch]
            ev.append([t, a])
            ev.append([t + dur * 0.6, b])
        elif ch in SILCH:
            ev.append([t, "sil"])
        elif ch in INHERIT or ch in COARTICULATE:
            ev.append([t, None])
        else:
            ev.append([t, SINGLE.get(ch, "ih")])
        t += dur
    return ev, float(pd.sum()) * FRAME


def _resolve(ev: list) -> list:
    """Fill inherit slots from the next real shape, then drop repeats."""
    nxt = "sil"
    for e in reversed(ev):
        if e[1] is None:
            e[1] = nxt
        else:
            nxt = e[1]
    out: list = []
    for t, v in ev:
        if not out or out[-1][1] != v:
            out.append([round(float(t), 4), v])
    return out


def build(results) -> tuple[list, float]:
    """results: iterable of kokoro Result. Returns (track, total_seconds).

    track is [[t_seconds, viseme_name], ...] sorted, de-duplicated.
    """
    ev: list = []
    off = 0.0
    for r in results:
        audio = r.audio
        audio = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
        pd = r.pred_dur
        if pd is None:
            off += len(audio) / SR
            continue
        pd = pd.detach().cpu().numpy() if hasattr(pd, "detach") else np.asarray(pd)
        part, _span = _chunk(r.phonemes or "", pd)
        ev += [[t + off, v] for t, v in part]
        off += len(audio) / SR                # trust real audio length per chunk
    ev.append([off, "sil"])
    ev = [[max(0.0, t - LEAD), v] for t, v in ev]
    return _resolve(ev), off


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Self-test: does the track line up with where the audio actually has energy?
    # Cross-correlate mouth-open against the RMS envelope and report the best lag.
    # A well-aligned track peaks at lag 0.
    from kokoro import KPipeline
    import soundfile as sf

    TEXT = ("The market is wrong, but you knew that. "
            "Positioning is stretched and nobody wants to say it out loud.")
    pipe = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
    res = list(pipe(TEXT, voice="af_heart"))
    track, dur = build(res)

    audio = np.concatenate([
        (r.audio.detach().cpu().numpy() if hasattr(r.audio, "detach") else np.asarray(r.audio))
        for r in res])
    print(f"audio {len(audio)/SR:.3f}s   track span {dur:.3f}s   events {len(track)}")
    assert abs(len(audio) / SR - dur) < 0.02, "track span does not match audio"

    # Graded openness vs the real amplitude envelope. Vowels are loud and open,
    # closures are quiet and shut, so these two curves should track each other.
    # A thresholded binary signal was useless here: both sides sat ~70% "on",
    # so the correlation only saw the handful of silence gaps.
    OPENNESS = {"sil": .0, "PP": .0, "FF": .15, "SS": .2, "CH": .3, "TH": .3,
                "nn": .3, "DD": .35, "kk": .35, "RR": .35, "ou": .35,
                "ih": .45, "E": .7, "oh": .8, "aa": 1.0}
    RATE = 200                                    # 5 ms grid
    n = int(len(audio) / SR * RATE)
    op = np.zeros(n)
    for i, (t, v) in enumerate(track):
        t2 = track[i + 1][0] if i + 1 < len(track) else dur
        op[int(t * RATE):int(t2 * RATE)] = OPENNESS[v]

    win = SR // RATE
    rms = np.sqrt(np.array([
        (audio[i * win:(i + 1) * win] ** 2).mean() for i in range(n)]) + 1e-12)
    env = rms / rms.max()

    k = np.ones(5) / 5                            # mouths have inertia
    a = np.convolve(op, k, "same"); a -= a.mean()
    b = env - env.mean()
    lags = np.arange(-40, 41)                      # +/- 200 ms
    cc = [float(np.corrcoef(np.roll(a, L), b)[0, 1]) for L in lags]
    best = int(lags[int(np.argmax(cc))])
    sil_frac = sum(1 for _, v in track if v == "sil") / len(track)
    print(f"events {len(track)}  sil events {sil_frac*100:.0f}%  mean openness {op.mean():.2f}")
    print(f"best lag {best*5:+d} ms   r={max(cc):.3f}   (r at lag 0 = {cc[len(cc)//2]:.3f})")
    print("VERDICT:", "aligned" if abs(best) <= 4 else f"OFFSET {best*5:+d} ms - needs correction")

    sf.write("/tmp/viseme_selftest.wav", audio, SR)
    print("first 14 events:", track[:14])
