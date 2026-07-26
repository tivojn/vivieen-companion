"""Build a timed viseme track for ANY TTS engine.

Engines fall into tiers and mouth quality follows the tier, so the UI reports
which one is in use rather than pretending they are equal.

  exact     Kokoro. It predicts a per-phoneme frame count and upsamples by it,
            so pred_dur IS a forced alignment. visemes.py consumes it directly.
  timed     ElevenLabs (character timestamps), Edge when it sends word marks.
  anchored  Edge in practice: sentence spans from the engine, estimate inside.
  estimated OpenAI, Gemini, macOS say. Audio and nothing else.

The estimated tier is where the work is, and it is measured rather than guessed:
qa/estimator_qa.py runs the estimator over KOKORO audio, where the true phoneme
timing is known, and reports the median viseme-onset error in milliseconds. Every
constant below came out of that harness. Do not tune them by eye - the whole
point of the harness is that this is not eye-judgeable.

Two estimators were built and both were measured:

  energy    Match cumulative expected loudness against the cumulative measured
            envelope and invert.  SHIPPED.  median onset 67 ms.

  nucleus   Vowels are the loud peaks, so find the envelope's local maxima, pin
            each vowel to one, and interpolate the consonants between. This is
            how a simple forced aligner bootstraps and it should have won.
            MEASURED AND REJECTED: 124 ms, worse than the naive flat stretch
            in places and worse than `energy` everywhere. Peak count and
            syllable count disagree too often - unstressed vowels merge into a
            neighbour, breaths and stop bursts add peaks that are not syllables -
            and every mismatch shifts the whole anchor sequence by one, which is
            a much bigger error than the smooth mistiming it was meant to fix.
            Deleted rather than left switchable: a losing branch that still runs
            is a thing someone re-enables by taste later.

Where it lands, against the ground truth of 12 clips:

    naive flat stretch      median onset 119 ms   mean r +0.124
    shipped (energy)        median onset  67 ms   mean r +0.341
    leave-one-out           median onset  71 ms   (+4 ms - not overfitted)

44% less onset error than a flat stretch. Still five times looser than the exact
tier, which is why the UI names the tier instead of implying they are equal.
"""
import numpy as np
import visemes as VZ

SR = 24000
_g2p = None

# ---- set by qa/estimator_qa.py against Kokoro ground truth --------------------
METHOD = "energy"      # "energy" | "uniform"
W_ENERGY = 0.7         # how far to trust the envelope integral over a flat fit
ENV_POW = 0.5          # envelope compression before integrating

# NEGATIVE, deliberately. The harness reports a positive lag when the track
# LEADS the truth, and at EST_LEAD=0 the estimate already led by ~48 ms: table
# durations front-load a sentence, so the estimate spends its time budget too
# early. VZ.LEAD then adds another 45 ms of anticipation on top, which is right
# for an exact track and too much for this one. This takes that back out. The
# optimum is a plateau (-65 to -110 ms all within 4 ms of best), not a spike.
EST_LEAD = -0.095
# Engine-supplied sentence boundaries are already on the audio clock. Their
# measured optimum is the original 35 ms articulation anticipation, not the
# audio-only estimator's duration-table correction.
ANCHOR_LEAD = 0.035

# ---------------------------------------------------------------- phoneme kit

DIPHS = set(VZ.DIPH)
VOWELS = set("\u0251\u00e6\u028ca\u0250\u025be\u026ai\u1d7b\u0259\u1d4a"
             "\u0254ou\u028a\u025a\u025d\u025c")
STOPS = set("pbtdkg\u0261")
FRICS = set("fv\u03b8\u00f0sz\u0283\u0292h")
AFFR = set("\u02a7\u02a4")
NASAL = set("mn\u014b")
LIQ = set("l\u0279rjw")
VOICED = set("bdg\u0261v\u00f0z\u0292")

DUR = {"diph": .155, "vowel": .105, "stop": .058, "affr": .095,
       "fric": .095, "nasal": .068, "liq": .068, "other": .075}

LOUD = {"diph": 1.0, "vowel": 1.0, "liq": .70, "nasal": .55, "affr": .30,
        "fric_v": .40, "fric": .22, "stop_v": .15, "stop": .08, "other": .30}

PAUSE = {",": .20, ";": .24, ":": .24, ".": .34, "!": .34, "?": .34,
         "\u2014": .26, "\u2026": .40, "(": .12, ")": .12}


def _klass(ch):
    if ch in DIPHS:
        return "diph"
    if ch in VOWELS:
        return "vowel"
    if ch in AFFR:
        return "affr"
    if ch in STOPS:
        return "stop_v" if ch in VOICED else "stop"
    if ch in FRICS:
        return "fric_v" if ch in VOICED else "fric"
    if ch in NASAL:
        return "nasal"
    if ch in LIQ:
        return "liq"
    return "other"


def _dur(k):
    return DUR.get(k.replace("_v", ""), DUR["other"])


def phonemise(text):
    """Text -> misaki IPA, the alphabet visemes.py already maps. misaki is
    Kokoro's own G2P and is already installed, so the estimated path shares a
    front end with the exact path and only the TIMING differs."""
    global _g2p
    try:
        if _g2p is None:
            from misaki import en
            _g2p = en.G2P(trf=False, british=False, fallback=None)
        ps, _ = _g2p(text)
        if ps:
            return ps
    except Exception:
        pass
    return _crude(text)


def _crude(text):
    """Last resort if G2P is unavailable: spell it. Wrong in detail, but the
    mouth still opens on vowels and shuts on stops, which is most of the read."""
    m = {"a": "\u0251", "e": "\u025b", "i": "\u026a", "o": "\u0254", "u": "\u028c",
         "y": "\u026a", "c": "k", "q": "k", "x": "k"}
    return "".join(m.get(c, c) for c in text.lower()
                   if c.isalpha() or c == " " or c in PAUSE)


def _emit(ch, t, dur):
    if ch in VZ.DIPH:
        a, b = VZ.DIPH[ch]
        return [[t, a], [t + dur * .6, b]]
    if ch in VZ.SILCH:
        return [[t, "sil"]]
    if ch in VZ.INHERIT or ch in VZ.COARTICULATE:
        return [[t, None]]
    return [[t, VZ.SINGLE.get(ch, "ih")]]


def _events(ps, t0, span):
    """Lay phonemes across [t0, t0+span] by nominal weight. Used inside one word."""
    items = [(c, _klass(c)) for c in ps if c not in VZ.CARRY]
    if not items:
        return []
    w = np.array([_dur(k) for _, k in items])
    w = w / w.sum() * span
    ev, t = [], t0
    for (c, _k), d in zip(items, w):
        ev += _emit(c, t, d)
        t += d
    return ev


# ---------------------------------------------------------------- timed tier

def _from_words(marks, total):
    """marks: [(start_s, dur_s, word)]. The engine already said where every word
    is; we only distribute phonemes inside it and shut the mouth in real gaps."""
    ev = [[0.0, "sil"]]
    prev_end = 0.0
    for st, dur, word in marks:
        if st - prev_end > .12:
            ev.append([prev_end + .04, "sil"])
        ev += _events(phonemise(word), st, max(dur, .04))
        prev_end = st + dur
    ev.append([max(prev_end, total - .02), "sil"])
    return ev


def _chars_to_words(chars):
    out, cur, st, en = [], "", None, None
    for c, a, b in chars:
        if c.isspace():
            if cur:
                out.append((st, max(en - st, .04), cur))
            cur, st, en = "", None, None
            continue
        if st is None:
            st = float(a)
        en = float(b)
        cur += c
    if cur and st is not None:
        out.append((st, max(en - st, .04), cur))
    return out


# ---------------------------------------------------------------- estimated

def _envelope(y, hop=120):
    """RMS at 5 ms, noise-floored. The floor matters: room tone in a cloud clip
    is never digital zero, and without subtracting it a 'silence' still
    accumulates energy and the pause logic quietly stops working."""
    n = max(1, len(y) // hop)
    e = np.sqrt(np.array([(y[i * hop:(i + 1) * hop] ** 2).mean()
                          for i in range(n)]) + 1e-12)
    e = np.maximum(e - np.percentile(e, 8), 0)
    m = e.max()
    return (e / m if m > 0 else e), hop / SR


def _items(text):
    out = []
    for c in phonemise(text):
        if c in VZ.CARRY and c != " ":
            continue
        if c == " ":
            out.append((c, "space", .030, 0.0))
        elif c in PAUSE:
            out.append((c, "sil", PAUSE[c], 0.0))
        else:
            k = _klass(c)
            out.append((c, k, _dur(k), LOUD.get(k, .3)))
    return out


def _times_energy(items, y, total):
    """Match cumulative expected loudness to the cumulative measured envelope.

    Time is not stretched uniformly: a pause costs no energy, so the track waits
    through it, and a fast clause consumes its energy quickly and is compressed.
    W_ENERGY blends this against a flat fit because the envelope alone overfits
    loud consonants; ENV_POW=0.5 (sqrt) stops one shouted vowel dominating the
    whole integral.
    """
    dur = np.array([d for _c, _k, d, _l in items], float)
    loud = np.array([l for _c, _k, _d, l in items], float)
    w = dur * (0.12 + 0.88 * loud)                      # floor: a run of stops
    cw = np.concatenate([[0.0], np.cumsum(w)])          # must not be zero-width
    if cw[-1] <= 0:
        return None
    cw = cw / cw[-1]

    env, step = _envelope(y)
    ce = np.concatenate([[0.0], np.cumsum(env ** ENV_POW)])
    if ce[-1] <= 0:
        ce = np.arange(len(ce), dtype=float)
    ce = ce / ce[-1]
    grid = np.arange(len(ce)) * step

    t = W_ENERGY * np.interp(cw, ce, grid) + (1 - W_ENERGY) * (cw * total)
    return np.maximum.accumulate(np.clip(t, 0, total))


def _times_uniform(items, total):
    dur = np.array([d for _c, _k, d, _l in items], float)
    cw = np.concatenate([[0.0], np.cumsum(dur)])
    return cw / cw[-1] * total if cw[-1] > 0 else cw


def _estimate(text, y, t0=0.0):
    total = len(y) / SR
    items = _items(text)
    if not items:
        return [[t0, "sil"]], total

    t = _times_energy(items, y, total) if METHOD == "energy" else None
    if t is None:
        t = _times_uniform(items, total)
    t = np.maximum.accumulate(np.clip(np.asarray(t, float), 0, total))

    ev = [[t0, "sil"]]
    for i, (c, k, _d, _l) in enumerate(items):
        if k == "space":
            continue
        if k == "sil":
            ev.append([t0 + t[i], "sil"])
            continue
        ev += _emit(c, t0 + t[i], max(t[i + 1] - t[i], .02))
    ev.append([t0 + total, "sil"])
    return ev, total


def _from_spans(spans, y):
    """Sentence anchors from the engine, estimate inside each one.

    Coarser than word boundaries, but it pins every clause to real audio so the
    estimate cannot accumulate drift across a paragraph - which is the failure
    that shows up as the mouth finishing before the voice does.
    """
    total = len(y) / SR
    ev, prev = [[0.0, "sil"]], 0.0
    for st, dur, sent in spans:
        a, b = int(max(0.0, st) * SR), int(min(total, st + dur) * SR)
        if b - a < SR // 20:
            continue
        if st - prev > .12:
            ev.append([prev + .04, "sil"])
        part, _ = _estimate(sent, y[a:b], t0=max(0.0, st))
        ev += part
        prev = min(total, st + dur)
    ev.append([max(prev, total - .02), "sil"])
    return ev


# ---------------------------------------------------------------- entry point

def build(text, y, alignment):
    """-> (track, seconds, tier). track is [[t, viseme], ...] for the runtime."""
    total = len(y) / SR
    kind = alignment[0] if alignment else None
    lead = VZ.LEAD
    try:
        if kind == "kokoro":
            track, dur = VZ.build(alignment[1])
            return track, dur, "exact"
        if kind == "words":
            ev, tier = _from_words(alignment[1], total), "timed"
        elif kind == "chars":
            ev, tier = _from_words(_chars_to_words(alignment[1]), total), "timed"
        elif kind == "spans":
            ev, tier = _from_spans(alignment[1], y), "anchored"
            lead += ANCHOR_LEAD
        else:
            ev, total = _estimate(text, y)
            tier = "estimated"
            lead += EST_LEAD
    except Exception as e:
        print("[viv] align fallback:", e, flush=True)
        ev, tier = [[0.0, "sil"]], "none"

    ev = [[max(0.0, t - lead), v] for t, v in sorted(ev, key=lambda q: q[0])]
    return VZ._resolve(ev), total, tier
