#!/usr/bin/env python3
"""
walkloop.py - measure whether a generated walk clip is a TRUE seamless gait loop.

usage:
    python3 walkloop.py <clip.mov|frames_dir> [--fps 24] [--chart out.png]

What it checks (the three things that break a horizon-walk loop):
  1. CYCLE COVERAGE  - does the clip span one FULL gait cycle (2 steps)?
  2. SEAM CONTINUITY - does the last frame flow into the first?
  3. ARM COMPLETENESS- does the near-side arm swing both in FRONT of and BEHIND the hip?

A walk loop is only valid if all three pass. Legs alone are not enough:
  legs repeat every HALF cycle, arms only every FULL cycle. A half-cycle clip
  looks almost right in the legs and visibly snaps in the arms.
"""
import sys, os, glob, subprocess, tempfile, argparse
import numpy as np
from PIL import Image


# ---------------------------------------------------------------- utilities
def load_frames(src):
    if os.path.isdir(src):
        files = sorted(glob.glob(os.path.join(src, '*.png')))
    else:
        d = tempfile.mkdtemp(prefix='walkloop_')
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', src,
                        '-vsync', '0', os.path.join(d, 'f%04d.png')], check=True)
        files = sorted(glob.glob(os.path.join(d, '*.png')))
    if not files:
        sys.exit(f'no frames found in {src}')
    return [np.asarray(Image.open(f).convert('RGBA'), float) for f in files]


def components(binimg, min_area=25):
    """4-connected components -> list of dicts, largest first."""
    lab = -np.ones(binimg.shape, np.int32)
    out, cur = [], 0
    ys, xs = np.nonzero(binimg)
    for sy, sx in zip(ys, xs):
        if lab[sy, sx] != -1:
            continue
        stack, pts = [(sy, sx)], []
        lab[sy, sx] = cur
        while stack:
            y, x = stack.pop()
            pts.append((y, x))
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ny, nx = y + dy, x + dx
                if (0 <= ny < binimg.shape[0] and 0 <= nx < binimg.shape[1]
                        and binimg[ny, nx] and lab[ny, nx] == -1):
                    lab[ny, nx] = cur
                    stack.append((ny, nx))
        p = np.array(pts)
        if len(pts) >= min_area:
            out.append(dict(area=len(pts), cx=p[:, 1].mean(), cy=p[:, 0].mean(),
                            x0=p[:, 1].min(), x1=p[:, 1].max(),
                            y0=p[:, 0].min(), y1=p[:, 0].max()))
        cur += 1
    return sorted(out, key=lambda b: -b['area'])


# ---------------------------------------------------------------- measurement
def measure(imgs):
    N = len(imgs)
    H, W = imgs[0].shape[:2]
    A = np.stack([im[:, :, 3] / 255.0 for im in imgs])
    C = np.stack([im[:, :, :3] / 255.0 for im in imgs])
    M = A > 0.5

    y_top = min(np.nonzero(M[i])[0].min() for i in range(N))
    y_bot = max(np.nonzero(M[i])[0].max() for i in range(N))
    BH = y_bot - y_top

    hip_x = float(np.mean([(A[i].sum(0) * np.arange(W)).sum() / A[i].sum()
                           for i in range(N)]))

    sy0 = int(y_bot - 0.14 * BH)                 # shoe band
    ay0, ay1 = int(y_top + 0.34 * BH), int(y_top + 0.60 * BH)   # hand band

    sep, footA, footB, hand_x, hand_y = [], [], [], [], []
    for i in range(N):
        # --- feet: bright, low-saturation shoe blobs -------------------
        sc, sm = C[i][sy0:], M[i][sy0:]
        lum = sc.mean(2)
        mx, mn = sc.max(2), sc.min(2)
        sat = (mx - mn) / np.maximum(mx, 1e-6)
        bs = components(sm & (lum > 0.62) & (sat < 0.42))[:2]
        if len(bs) == 2:
            b1, b2 = sorted(bs, key=lambda b: b['cx'])
            sep.append(b2['cx'] - b1['cx']); footA.append(b1['cx']); footB.append(b2['cx'])
        else:
            c = bs[0]['cx'] if bs else np.nan
            sep.append(0.0); footA.append(c); footB.append(c)

        # --- near hand / cuff: brightest blob in the mid band ----------
        ac, am = C[i][ay0:ay1], M[i][ay0:ay1]
        bs = components(am & (ac.mean(2) > 0.58), min_area=30)
        if bs:
            hand_x.append(bs[0]['cx']); hand_y.append(bs[0]['cy'])
        else:
            hand_x.append(np.nan); hand_y.append(np.nan)

    return dict(N=N, W=W, H=H, BH=BH, hip_x=hip_x,
                sep=np.array(sep), footA=np.array(footA), footB=np.array(footB),
                hand_x=np.array(hand_x, float), hand_y=np.array(hand_y, float),
                mask=M, alpha=A, rgb=C)


def crossings(sep):
    """sub-frame estimate of every 'legs passing' event (sep -> 0), incl. extrapolated."""
    ev = []
    n = len(sep)
    idx = [i for i in range(n) if sep[i] > 1]
    if not idx:
        return ev
    first, last = idx[0], idx[-1]
    # leading extrapolation
    if first + 2 < n:
        v = sep[first + 1] - sep[first]
        if v > 0.5:
            ev.append(first - sep[first] / v)
    for i in range(first, last):
        if sep[i] > 1 and sep[i + 1] <= 1:
            ev.append(i + sep[i] / max(sep[i] - sep[i + 1], 1e-6))
    # trailing extrapolation
    if last - 1 >= 0:
        v = sep[last - 1] - sep[last]
        if v > 0.5:
            ev.append(last + sep[last] / v)
    return ev


def analyse(m, fps):
    N = m['N']; sep = m['sep']; hip = m['hip_x']
    out = {}

    # ---- period estimator 1: passing-to-passing (half cycle) ----------
    ev = crossings(sep)
    half = None
    if len(ev) >= 2:
        half = float(np.mean(np.diff(ev)))
    out['events'] = ev
    out['T_half_passing'] = half
    out['T_full_passing'] = half * 2 if half else None

    # ---- period estimator 2: stride / ground-speed --------------------
    # planted foot slides backward at body speed; stride = 2 x max step length
    fb = m['footB']
    good = ~np.isnan(fb)
    slope = None
    if good.sum() > 6:
        k = np.argmax(np.nan_to_num(fb, nan=-1e9))
        tail = np.arange(k, N)[good[k:]]
        if len(tail) > 5:
            slope = abs(np.polyfit(tail, fb[tail], 1)[0])       # px / frame
    step_len = float(np.nanmax(sep))
    out['ground_px_per_frame'] = slope
    out['step_len_px'] = step_len
    out['T_full_stride'] = (2 * step_len / slope) if slope and slope > 1e-6 else None

    Ts = [t for t in (out['T_full_passing'], out['T_full_stride']) if t]
    T = float(np.mean(Ts)) if Ts else None
    out['T_full'] = T
    out['coverage'] = (N / T) if T else None

    # ---- seam continuity ---------------------------------------------
    hx = m['hand_x']
    d_typ = float(np.nanmedian(np.abs(np.diff(hx))))
    d_seam = float(abs(hx[0] - hx[-1]))
    out['hand_step_typical'] = d_typ
    out['hand_seam_jump'] = d_seam
    out['hand_seam_ratio'] = d_seam / d_typ if d_typ > 1e-6 else None

    M = m['mask']
    iou = lambda a, b: (a & b).sum() / max((a | b).sum(), 1)
    adj = [iou(M[i], M[i + 1]) for i in range(N - 1)]
    out['iou_typical'] = float(np.median(adj))
    out['iou_seam'] = float(iou(M[-1], M[0]))

    # ---- arm swing completeness --------------------------------------
    fwd = float(np.nanmax(hx) - hip)          # how far in front of hip
    back = float(hip - np.nanmin(hx))         # how far behind hip
    out['hip_x'] = hip
    out['arm_forward_px'] = fwd
    out['arm_back_px'] = back
    out['arm_symmetry'] = (back / fwd) if fwd > 1e-6 else None
    return out


def report(m, a, fps, name):
    N = m['N']
    L = []
    P = L.append
    P(f'WALK LOOP REPORT  -  {name}')
    P('=' * 62)
    P(f'frames           : {N}   @ {fps} fps  = {N/fps:.3f} s')
    P(f'figure height    : {m["BH"]:.0f} px   hip line x = {a["hip_x"]:.1f}')
    P('')
    P('1. CYCLE COVERAGE')
    ev = a['events']
    P(f'   legs-passing events at frame  : {[round(e,1) for e in ev]}')
    if a['T_full_passing']:
        P(f'   full cycle (passing method)   : {a["T_full_passing"]:.1f} frames'
          f'  = {a["T_full_passing"]/fps:.2f} s')
    if a['T_full_stride']:
        P(f'   full cycle (stride method)    : {a["T_full_stride"]:.1f} frames'
          f'  = {a["T_full_stride"]/fps:.2f} s')
        P(f'   ground speed / step length    : {a["ground_px_per_frame"]:.2f} px/f'
          f'  /  {a["step_len_px"]:.0f} px')
    if a['coverage'] is not None:
        cov = a['coverage']
        P(f'   >> clip covers {cov*100:.1f}% of one full gait cycle'
          f'   ({cov*2:.2f} steps)')
        P(f'   {"PASS" if 0.97 <= cov <= 1.03 else "FAIL"}'
          f'  (need 100% = exactly 2 steps)')
        if cov < 0.97 and a['T_full']:
            P(f'   -> fix: regenerate 2 full steps, then either {a['T_full']:.0f} frames at {fps} fps,'
              f' or REGENERATE a 2-step cycle as {N} frames played at {N/(a["T_full"]/fps):.1f} fps')
    P('')
    P('2. SEAM CONTINUITY  (last frame -> first frame)')
    P(f'   silhouette IoU  typical {a["iou_typical"]:.3f}   seam {a["iou_seam"]:.3f}')
    P(f'   hand travel     typical {a["hand_step_typical"]:.1f} px/f'
      f'   seam {a["hand_seam_jump"]:.1f} px')
    if a['hand_seam_ratio']:
        r = a['hand_seam_ratio']
        P(f'   >> seam jump is {r:.1f}x a normal frame step'
          f'   {"PASS" if r <= 1.6 else "FAIL"}  (need <= 1.6x)')
    P('')
    P('3. ARM SWING COMPLETENESS  (near-side arm)')
    P(f'   hand reaches {a["arm_forward_px"]:.0f} px IN FRONT of the hip line')
    P(f'   hand reaches {a["arm_back_px"]:.0f} px BEHIND the hip line')
    if a['arm_symmetry'] is not None:
        s = a['arm_symmetry']
        P(f'   >> back/forward ratio {s:.2f}'
          f'   {"PASS" if s >= 0.45 else "FAIL"}  (need >= 0.45)')
        if s < 0.45:
            P('   -> the audience-side arm never swings behind the body:')
            P('      the clip contains only the forward half of the arm swing.')
    P('')
    ok = (a['coverage'] and 0.97 <= a['coverage'] <= 1.03
          and a['hand_seam_ratio'] and a['hand_seam_ratio'] <= 1.6
          and a['arm_symmetry'] and a['arm_symmetry'] >= 0.45)
    P(f'VERDICT: {"LOOPS CLEANLY" if ok else "NOT A VALID LOOP"}')
    return '\n'.join(L)


def chart(m, a, fps, path):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    N = m['N']; T = a['T_full'] or N; x = np.arange(N)
    fig, ax = plt.subplots(3, 1, figsize=(11, 9.5), facecolor='#14141a')
    fig.suptitle('Walk loop diagnosis  -  walk-alpha.mov  (32 frames @ 24 fps)',
                 color='#f2f2f4', fontsize=15, y=0.975)

    for b in ax:
        b.set_facecolor('#1c1c24')
        b.tick_params(colors='#9a9aa6', labelsize=9)
        for s in b.spines.values():
            s.set_color('#3a3a46')
        b.grid(color='#2a2a34', lw=.7)
        b.set_xlim(0, T * 1.02)

    # ---- panel 1: feet -------------------------------------------------
    b = ax[0]
    b.plot(x, m['sep'], color='#e8b04b', lw=2.2, label='foot separation (px)')
    for i, e in enumerate(a['events']):
        b.axvline(e, color='#4fd1a5', ls='--', lw=1.4)
        b.text(e, b.get_ylim()[1] * .06, f'  legs pass\n  f{e:.1f}',
               color='#4fd1a5', fontsize=8.5, va='bottom')
    b.axvspan(N, T, color='#e0466b', alpha=.16)
    b.text((N + T) / 2, 60, 'NEVER RENDERED\nsecond step', color='#ff7d99',
           ha='center', va='center', fontsize=11, fontweight='bold')
    b.axvline(N, color='#e0466b', lw=2)
    b.set_ylabel('legs', color='#d8d8de')
    b.set_title('1.  Legs complete only ONE step (passing -> passing), not two',
                color='#f2f2f4', fontsize=11, loc='left')
    b.legend(facecolor='#24242e', edgecolor='#3a3a46', labelcolor='#d8d8de', fontsize=9)

    # ---- panel 2: hand -------------------------------------------------
    b = ax[1]
    hip = a['hip_x']
    b.plot(x, m['hand_x'], color='#6aa6ff', lw=2.4, label='near hand x (px)')
    b.axhline(hip, color='#f2f2f4', ls=':', lw=1.6)
    b.text(T * .995, hip + 2, 'hip line', color='#f2f2f4', fontsize=9, ha='right')
    b.fill_between(x, hip, m['hand_x'], where=m['hand_x'] >= hip,
                   color='#6aa6ff', alpha=.18)
    b.axvspan(N, T, color='#e0466b', alpha=.16)
    b.text((N + T) / 2, hip - 26, 'the entire BACKWARD\narm swing is missing',
           color='#ff7d99', ha='center', va='center', fontsize=11, fontweight='bold')
    b.axvline(N, color='#e0466b', lw=2)
    b.annotate('', xy=(0, m['hand_x'][0]), xytext=(N - 1, m['hand_x'][-1]),
               arrowprops=dict(arrowstyle='->', color='#ff4d6d', lw=2.2,
                               connectionstyle='arc3,rad=-0.32'))
    b.text(N * .5, m['hand_x'][-1] - 16,
           f'LOOP SNAP: hand teleports {a["hand_seam_jump"]:.0f} px '
           f'({a["hand_seam_ratio"]:.0f}x a normal frame)',
           color='#ff4d6d', fontsize=10, fontweight='bold', ha='center')
    b.set_ylabel('arm', color='#d8d8de')
    b.set_title('2.  Audience-side arm swings forward only, then snaps back',
                color='#f2f2f4', fontsize=11, loc='left')
    b.legend(facecolor='#24242e', edgecolor='#3a3a46', labelcolor='#d8d8de', fontsize=9)

    # ---- panel 3: coverage bar ----------------------------------------
    b = ax[2]
    b.barh([0], [T], color='#2f6f5a', height=.5)
    b.barh([0], [N], color='#4fd1a5', height=.5)
    b.text(N / 2, 0, f'RENDERED\n{N} frames', ha='center', va='center',
           color='#0d2f26', fontsize=11, fontweight='bold')
    b.text((N + T) / 2, 0, f'MISSING\n{T-N:.0f} frames', ha='center', va='center',
           color='#c9f5e6', fontsize=11, fontweight='bold')
    b.set_yticks([])
    b.set_xlabel('frames', color='#9a9aa6')
    b.set_title(f'3.  Coverage: {N} of {T:.0f} frames = '
                f'{a["coverage"]*100:.0f}% of one gait cycle  '
                f'(cadence {2/(T/fps)*60:.0f} steps/min - a slow catwalk stride)',
                color='#f2f2f4', fontsize=11, loc='left')
    b.set_ylim(-.5, .5)

    fig.tight_layout(rect=[0, 0, 1, 0.955])
    fig.savefig(path, dpi=125, facecolor='#14141a')
    print('chart ->', path)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('--fps', type=float, default=24)
    ap.add_argument('--chart', default=None)
    args = ap.parse_args()

    imgs = load_frames(args.src)
    m = measure(imgs)
    a = analyse(m, args.fps)
    txt = report(m, a, args.fps, os.path.basename(args.src))
    print(txt)
    if args.chart:
        try:
            chart(m, a, args.fps, args.chart)
        except ImportError:
            print('(matplotlib not installed - chart skipped)')
