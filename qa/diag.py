"""Two questions the QA run raised.

1. Edge TTS produced audio but zero word boundaries, so its 'timed' tier silently
   degraded to a single silent event - worse than the estimate it was supposed
   to beat. Find out what the stream actually yields.
2. The estimated path peaked at +55 ms. One sentence is not a bias, it is an
   anecdote; measure across several to see whether the offset is a constant
   worth correcting or noise worth leaving alone.
"""
import os, sys, asyncio
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, ROOT)
import providers as P
import align
from align_qa import score, TEXT  # noqa

SENTS = [
    "The market is wrong, but you knew that.",
    "Positioning is stretched and nobody wants to say it out loud.",
    "Carry works until it doesn't, and then it works in reverse, violently.",
    "I would rather be early and bored than late and liquidated.",
    "Volatility is cheap here. That is not the same as being wrong.",
]


async def edge_probe():
    print("=" * 66)
    print("1. what does edge-tts actually stream?")
    try:
        import edge_tts
        print("   version:", getattr(edge_tts, "__version__", "unknown"))
    except Exception as e:
        print("   not installed:", e)
        return
    com = edge_tts.Communicate("Testing one two three, the market is wrong.",
                               "en-US-AvaNeural")
    kinds, marks, nbytes = {}, [], 0
    async for ch in com.stream():
        t = ch.get("type")
        kinds[t] = kinds.get(t, 0) + 1
        if t == "audio":
            nbytes += len(ch.get("data") or b"")
        else:
            marks.append(ch)
    print("   chunk types:", kinds, f"({nbytes} audio bytes)")
    if marks:
        print("   first non-audio chunk:", {k: v for k, v in marks[0].items()})
    else:
        print("   NO boundary chunks at all")


async def bias_probe():
    print("=" * 66)
    print("2. is the estimated-path offset a constant?")
    print("   engine/voice          sentence  lag(ms)   r")
    lags = []
    for pid, voice in [("system", "Samantha"), ("system", "Daniel")]:
        cfg = dict(P.DEFAULTS["tts"]); cfg["provider"] = pid; cfg["voice"] = voice
        for i, s in enumerate(SENTS):
            try:
                y, _al = await P.speak(s, cfg)
                track, dur, tier = align.build(s, y, None)   # force the estimate
                lag, r, _ = score(track, y)
                lags.append(lag)
                print(f"   {pid}/{voice:<12} {i}         {lag:+5d}   {r:.3f}")
            except Exception as e:
                print(f"   {pid}/{voice} {i} failed: {str(e)[:60]}")
    if lags:
        a = np.array(lags, float)
        print(f"\n   mean {a.mean():+.0f} ms   median {np.median(a):+.0f} ms   "
              f"sd {a.std():.0f} ms   n={len(a)}")
        print("   -> " + ("stable bias, correct it" if abs(a.mean()) > 1.5 * a.std() / max(1, len(a) ** .5)
                          else "within noise, leave it"))


async def main():
    await edge_probe()
    await bias_probe()

asyncio.run(main())
