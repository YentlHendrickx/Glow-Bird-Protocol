#!/usr/bin/env python3
"""Offline replay + scoring for the beat path.

Decodes songs with ffmpeg to the exact stream parec gives the live service (mono s16le @ SAMPLE_RATE),
pushes them chunk-by-chunk through main.FramePipeline (same analysis, decimation and peak-holding as
the live capture) and the server-side PS Sonic Boom mirror, then scores the booms against reference
beat times. So a change can be judged by one number over a whole set of songs instead of by ear on one.

Reference beats come from an offline (whole-song, non-causal) beat tracker below - far more reliable
than anything causal, but not perfect. To override it for a song, put one beat time (seconds) per
line in a sidecar `<song>.beats` next to the audio file, or in tools/refs/<song stem>.beats.

  tools/replay_eval.py ~/Music/Share                         score src/conf.txt over every song
  tools/replay_eval.py ~/Music/Share -c conf -c sonicboom    compare configs side by side
  tools/replay_eval.py song.flac -c sonicboom -c sonicboom:BAND_ATTACK=3.5,BAND_GAMMA=0.8
  tools/replay_eval.py ~/Music/Share --list-refs             just print reference tempo/confidence

Configs (-c): 'conf' = src/conf.txt, a preset name from src/presets/, or a path to a .conf. Each is
overlaid on the startup values from conf.txt (same as the web panel's Load). Startup-only keys
(SAMPLE_RATE, CHUNK_BYTES, FFT_WINDOW_SAMPLES) come from conf.txt and can't vary per config.
"""

import argparse
import configparser
import hashlib
import json
import os
import random
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import main  # noqa: E402  (reads src/conf.txt; opens an unused UDP socket - harmless)

CACHE_DIR = os.path.join(ROOT, "tools", "cache")
REFS_DIR = os.path.join(ROOT, "tools", "refs")
AUDIO_EXT = (".flac", ".wav", ".mp3", ".ogg", ".opus", ".m4a", ".aac")
REF_VERSION = 1            # bump when the reference tracker changes, to invalidate the cache


# ---------------------------------------------------------------------------------------------- I/O

def decode(path, rate):
    """Any audio file -> mono int16 at `rate`, like `parec --channels=1 --format=s16le`."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
        capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.int16)


def find_songs(paths):
    songs = []
    for p in paths:
        if os.path.isdir(p):
            for d, _dirs, files in os.walk(p):
                songs += [os.path.join(d, f) for f in files if f.lower().endswith(AUDIO_EXT)]
        elif os.path.isfile(p):
            songs.append(p)
        else:
            sys.exit(f"not found: {p}")
    return sorted(songs)


def load_config(spec):
    """'name[:KEY=VAL,...]' -> (label, params). Overlays a .conf's [Audio] keys on the conf.txt startup
    values, then the inline overrides - e.g. 'conf:BEAT_MODE=1,ONSET_THRESH=2.5'."""
    name, _, overrides = spec.partition(":")
    if name == "conf":
        path = os.path.join(main._BASE_DIR, "conf.txt")
    elif os.path.isfile(name):
        path = name
    else:
        path = os.path.join(main.PRESETS_DIR, name + ".conf")
    cp = configparser.ConfigParser()
    if not cp.read(path):
        sys.exit(f"config not found: {name} ({path})")
    params = main.live.snapshot()
    for key, _label, _step, lo, hi, _desc in main._PARAM_SPEC:
        if cp.has_option("Audio", key):
            params[key] = min(max(cp.getfloat("Audio", key), lo), hi)
    for kv in filter(None, overrides.split(",")):
        k, _, v = kv.partition("=")
        if k not in main._SPEC_BY_KEY:
            sys.exit(f"unknown param {k}; one of: {', '.join(main._SPEC_BY_KEY)}")
        params[k] = float(v)
    label = os.path.splitext(os.path.basename(name))[0]
    return (label + ":" + overrides if overrides else label), params


# ---------------------------------------------------------------------------- reference beat tracker
# Ellis (2007) dynamic-programming beat tracker, the algorithm behind librosa.beat.beat_track:
# a log-mel spectral-flux onset envelope, a global tempo from its autocorrelation (with a log-normal
# prior around 120 BPM against octave errors), then the beat sequence that best balances "beats land
# on onsets" against "beats are one period apart". It sees the whole song, so it's a fair yardstick.

REF_FPS = 100
REF_NFFT = 2048
REF_MELS = 80


def _mel_filterbank(sr, n_fft, n_mels, fmin=30.0, fmax=16000.0):
    mel = lambda f: 2595.0 * np.log10(1.0 + f / 700.0)
    hz = lambda m: 700.0 * (10 ** (m / 2595.0) - 1.0)
    pts = hz(np.linspace(mel(fmin), mel(min(fmax, sr / 2)), n_mels + 2))
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    fb = np.zeros((n_mels, len(freqs)))
    for i in range(n_mels):
        lo, c, hi = pts[i], pts[i + 1], pts[i + 2]
        fb[i] = np.maximum(0, np.minimum((freqs - lo) / (c - lo), (hi - freqs) / (hi - c)))
    return fb


def onset_envelope(pcm, sr):
    hop = sr // REF_FPS
    x = np.pad(pcm.astype(np.float32) / 32768.0, REF_NFFT // 2)      # centred frames
    n_frames = 1 + (len(x) - REF_NFFT) // hop
    win = np.hanning(REF_NFFT).astype(np.float32)
    fb = _mel_filterbank(sr, REF_NFFT, REF_MELS).astype(np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(x, REF_NFFT)[::hop][:n_frames]
    mel = np.empty((n_frames, REF_MELS), dtype=np.float32)
    for i in range(0, n_frames, 2048):                                # blocks keep memory bounded
        mel[i:i + 2048] = np.abs(np.fft.rfft(frames[i:i + 2048] * win, axis=1)) @ fb.T
    db = 20 * np.log10(np.maximum(mel, 1e-6))
    db = np.maximum(db, db.max() - 80.0)
    # SuperFlux-style: compare against the max of neighbouring bands 2 frames back (vibrato-robust)
    ref = np.maximum(np.maximum(db, np.roll(db, 1, axis=1)), np.roll(db, -1, axis=1))
    flux = np.zeros(n_frames)
    flux[2:] = np.maximum(db[2:] - ref[:-2], 0).mean(axis=1)
    return flux


def estimate_tempo(env, fps, lo_bpm=40, hi_bpm=240):
    x = env - env.mean()
    n = len(x)
    ac = np.fft.irfft(np.abs(np.fft.rfft(x, 2 * n)) ** 2)[:n]
    lags = np.arange(int(fps * 60 / hi_bpm), int(fps * 60 / lo_bpm) + 1)
    bpms = 60.0 * fps / lags
    score = ac[lags] * np.exp(-0.5 * np.log2(bpms / 120.0) ** 2)
    k = int(np.argmax(score))
    lag = float(lags[k])
    if 0 < k < len(lags) - 1:                                         # parabolic refinement
        a, b, c = score[k - 1], score[k], score[k + 1]
        if a - 2 * b + c < 0:
            lag += 0.5 * (a - c) / (a - 2 * b + c)
    return 60.0 * fps / lag


def track_beats(env, fps, bpm, tightness=100.0):
    period = fps * 60.0 / bpm
    std = env.std()
    if std <= 0:
        return np.array([]), 0.0
    w = np.arange(-int(period), int(period) + 1)
    local = np.convolve(env / std, np.exp(-0.5 * (w * 32.0 / period) ** 2), "same")

    offs = np.arange(-int(round(2 * period)), -int(round(period / 2)) + 1)
    txwt = -tightness * np.log(-offs / period) ** 2
    n = len(local)
    cum = np.zeros(n)
    back = np.full(n, -1)
    for i in range(n):
        idx = i + offs
        ok = idx >= 0
        if not ok.any():
            cum[i] = local[i]
            continue
        cand = cum[idx[ok]] + txwt[ok]
        k = int(np.argmax(cand))
        if cand[k] > 0:
            cum[i] = local[i] + cand[k]
            back[i] = idx[ok][k]
        else:
            cum[i] = local[i]

    # last beat: the latest local max of cum that's reasonably strong (as librosa does)
    peaks = np.flatnonzero((cum[1:-1] > cum[:-2]) & (cum[1:-1] >= cum[2:])) + 1
    if len(peaks) == 0:
        return np.array([]), 0.0
    strong = peaks[cum[peaks] >= 0.5 * np.median(cum[peaks])]
    beats = [int(strong[-1])]
    while back[beats[-1]] >= 0:
        beats.append(int(back[beats[-1]]))
    beats = np.array(beats[::-1])

    # trim weak leading/trailing beats (intros/outros/silence)
    thr = 0.5 * np.sqrt(np.mean(local[beats] ** 2))
    strong_b = np.flatnonzero(local[beats] >= thr)
    if len(strong_b):
        beats = beats[strong_b[0]:strong_b[-1] + 1]
    # confidence: how much onset energy sits on the beats vs. anywhere (~1 = no pulse to speak of)
    conf = float(local[beats].mean() / max(local.mean(), 1e-9)) if len(beats) else 0.0
    return beats / fps, conf


def sidecar_beats(path):
    stem = os.path.splitext(path)[0]
    for cand in (stem + ".beats", os.path.join(REFS_DIR, os.path.basename(stem) + ".beats")):
        if os.path.isfile(cand):
            with open(cand) as f:
                t = [float(l.split()[0]) for l in f if l.strip() and not l.startswith("#")]
            return np.array(sorted(t)), cand
    return None, None


def reference(path, pcm, sr):
    """-> dict(beats, bpm, conf, source), cached on disk by file identity."""
    beats, src = sidecar_beats(path)
    if beats is not None:
        bpm = 60.0 / np.median(np.diff(beats)) if len(beats) > 1 else 0.0
        return {"beats": beats, "bpm": bpm, "conf": float("nan"), "source": os.path.basename(src)}

    st = os.stat(path)
    key = hashlib.sha1(f"{REF_VERSION}|{os.path.abspath(path)}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()
    cpath = os.path.join(CACHE_DIR, key + ".json")
    if os.path.isfile(cpath):
        with open(cpath) as f:
            d = json.load(f)
        d["beats"] = np.array(d["beats"])
        return d
    env = onset_envelope(pcm, sr)
    bpm = estimate_tempo(env, REF_FPS)
    beats, conf = track_beats(env, REF_FPS, bpm)
    d = {"beats": beats, "bpm": bpm, "conf": conf, "source": "tracker"}
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cpath, "w") as f:
        json.dump({**d, "beats": beats.tolist()}, f)
    return d


# ----------------------------------------------------------------------------------- replay + score

def replay(pcm, params, sb, level=1.0):
    """Run the live pipeline over a whole song. -> (boom times s, boom spans)."""
    random.seed(0)
    if level != 1.0:
        pcm = np.clip(pcm.astype(np.float64) * level, -32768, 32767).astype(np.int16)
    sonic = main.SonicBoom()
    sonic.set_params(**sb)
    pipe = main.FramePipeline(main.Analyzer(), sonic)
    raw = pcm.tobytes()
    step = main.CHUNK_BYTES
    times, spans = [], []
    for i in range(0, len(raw) - step + 1, step):
        t = (i + step) / 2 / main.SAMPLE_RATE           # chunk end = when the live loop would have it
        pipe.process(raw[i:i + step], t, params)
        if pipe.last_boom:
            times.append(t)
            spans.append(pipe.last_boom)
    return np.array(times), np.array(spans)


def score(ref, booms, spans, tol):
    """Beat-level match within +-tol. Recall = ref beats with a boom; precision = booms on a beat;
    p8 = booms on a beat or the half-beat between (offbeat hits aren't really wrong);
    vis = expected fraction of booms that emit >=1 particle (hw_random16(span) > 0)."""
    nb, nr = len(booms), len(ref)
    out = {"n_ref": nr, "n_boom": nb}
    if nr == 0 or nb == 0:
        return {**out, "R": 0.0, "P": 0.0, "P8": 0.0, "F": 0.0, "F8": 0.0, "vis": 0.0, "Rvis": 0.0,
                "lag_ms": 0.0}
    vis = np.where(spans > 0, (spans - 1) / np.maximum(spans, 1), 0.0)

    j = np.clip(np.searchsorted(booms, ref), 1, nb - 1) if nb > 1 else np.zeros(nr, dtype=int)
    near = np.where(np.abs(booms[j - 1] - ref) < np.abs(booms[j] - ref), j - 1, j) if nb > 1 else j
    dt = booms[near] - ref
    hit = np.abs(dt) <= tol
    recall = hit.mean()
    rvis = (hit * vis[near]).mean()

    # only judge booms inside the referenced span (intros/outros have no reference beats)
    inb = booms[(booms >= ref[0] - tol) & (booms <= ref[-1] + tol)]
    if len(inb) and nr > 1:
        prec = _frac(ref, inb, tol)
        half = np.sort(np.concatenate([ref, (ref[:-1] + ref[1:]) / 2]))
        p8 = _frac(half, inb, tol)
    else:
        prec = p8 = 0.0
    f = 2 * prec * recall / (prec + recall) if prec + recall else 0.0
    f8 = 2 * p8 * recall / (p8 + recall) if p8 + recall else 0.0
    return {**out, "R": float(recall), "P": float(prec), "P8": float(p8), "F": float(f), "F8": float(f8),
            "vis": float(vis.mean()), "Rvis": float(rvis),
            "lag_ms": float(np.median(dt[hit]) * 1000) if hit.any() else 0.0}


def score_levels(ref, booms, spans, tol):
    """Score at the reference's metrical level and at half tempo (both phases), keeping the best F:
    the tracker sometimes picks the double-time level (e.g. 186 for a 93 BPM track), and booming on
    every quarter note there shouldn't count as missing half the beats."""
    best = None
    for name, r in (("1x", ref), ("1/2", ref[0::2]), ("1/2'", ref[1::2])):
        m = score(r, booms, spans, tol)
        if best is None or m["F8"] > best["F8"] + 1e-9:
            best = {**m, "level": name}
    return best


def _frac(grid, pts, tol):
    k = np.clip(np.searchsorted(grid, pts), 1, len(grid) - 1)
    d = np.minimum(np.abs(grid[k - 1] - pts), np.abs(grid[k] - pts))
    return float((d <= tol).mean())


def run_song(args):
    path, configs, sb, level, tol, refs_only = args
    try:
        pcm = decode(path, main.SAMPLE_RATE)
        ref = reference(path, pcm, main.SAMPLE_RATE)
        res = {"path": path, "dur": len(pcm) / main.SAMPLE_RATE, "bpm": ref["bpm"], "conf": ref["conf"],
               "ref_source": ref["source"], "ref": ref["beats"].tolist(), "configs": {}}
        if not refs_only:
            for label, params in configs:
                booms, spans = replay(pcm, params, sb, level)
                res["configs"][label] = {**score_levels(ref["beats"], booms, spans, tol), "booms": booms.tolist()}
        return res
    except Exception as e:
        return {"path": path, "error": str(e)}


# ---------------------------------------------------------------------------------------------- CLI

def short(path, width):
    name = os.path.splitext(os.path.basename(path))[0]
    return name if len(name) <= width else name[:width - 1] + "~"


def main_cli():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="audio files and/or directories (searched recursively)")
    ap.add_argument("-c", "--config", action="append", help="config to score (repeatable); default: conf")
    ap.add_argument("-s", "--set", action="append", default=[], metavar="KEY=VAL",
                    help="override a param in every config (repeatable)")
    ap.add_argument("--strip-len", type=int, default=110, help="LEDs in the Sonic Boom segment")
    ap.add_argument("--intensity", type=int, default=128, help="Sonic Boom intensity slider")
    ap.add_argument("--custom3", type=int, default=0, help="Sonic Boom bin slider (0-31, bin = c3>>1)")
    ap.add_argument("--no-filter", action="store_true", help="Sonic Boom 'filter' checkbox off")
    ap.add_argument("--level", type=float, default=1.0, help="scale the decoded audio (system volume)")
    ap.add_argument("--tol", type=float, default=0.07, help="beat match tolerance in seconds")
    ap.add_argument("--min-conf", type=float, default=1.3,
                    help="songs whose reference pulse confidence is below this are shown but left out "
                         "of the averages (ambient / beatless)")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count())
    ap.add_argument("--list-refs", action="store_true", help="only compute + print the references")
    ap.add_argument("--json", metavar="FILE", help="write full per-song results (incl. boom/ref times)")
    args = ap.parse_args()

    configs = [load_config(c) for c in (args.config or ["conf"])]
    for kv in args.set:
        k, _, v = kv.partition("=")
        if k not in main._SPEC_BY_KEY:
            sys.exit(f"unknown param {k}; one of: {', '.join(main._SPEC_BY_KEY)}")
        for _label, p in configs:
            p[k] = float(v)
    sb = {"custom3": args.custom3, "intensity": args.intensity, "filter": not args.no_filter,
          "strip_len": args.strip_len}

    songs = find_songs(args.paths)
    if not songs:
        sys.exit("no audio files found")
    tasks = [(s, configs, sb, args.level, args.tol, args.list_refs) for s in songs]
    with ProcessPoolExecutor(max_workers=args.jobs) as ex:
        results = list(ex.map(run_song, tasks))

    errors = [r for r in results if "error" in r]
    results = [r for r in results if "error" not in r]
    for r in errors:
        print(f"! {r['path']}: {r['error']}", file=sys.stderr)

    def ok(r):
        return r["ref_source"] != "tracker" or r["conf"] >= args.min_conf

    labels = [c[0] for c in configs]
    W = 34
    if args.list_refs:
        print(f"{'song':<{W}} {'dur':>5} {'BPM':>6} {'conf':>5} {'beats':>6}  source")
        for r in sorted(results, key=lambda r: r["conf"]):
            print(f"{short(r['path'], W):<{W}} {r['dur']:5.0f} {r['bpm']:6.1f} {r['conf']:5.2f} "
                  f"{len(r['ref']):6d}  {r['ref_source']}")
        return

    first = labels[0]
    results.sort(key=lambda r: (not ok(r), r["configs"][first]["F8"]))
    if len(labels) == 1:
        print(f"{'song':<{W}} {'BPM':>5} {'conf':>4}   {'R':>4} {'P':>4} {'P8':>4} {'F':>4} {'F8':>4}  "
              f"{'vis':>4} {'Rvis':>4} {'lag':>5}  booms/ref")
        for r in results:
            m = r["configs"][first]
            flag = " " if ok(r) else "~"
            print(f"{short(r['path'], W):<{W}} {r['bpm']:5.0f} {r['conf']:4.1f} {flag} "
                  f"{m['R']:4.2f} {m['P']:4.2f} {m['P8']:4.2f} {m['F']:4.2f} {m['F8']:4.2f}  {m['vis']:4.2f} "
                  f"{m["Rvis"]:4.2f} {m["lag_ms"]:4.0f}ms  {m["n_boom"]}/{m["n_ref"]} {m["level"]}")
    else:
        cw = max(9, max(len(l) for l in labels) + 1)
        print(f"{'song':<{W}} {'BPM':>5}   " + "".join(f"{l[:cw - 1]:>{cw}}" for l in labels) + "   (F8)")
        for r in results:
            flag = " " if ok(r) else "~"
            print(f"{short(r['path'], W):<{W}} {r['bpm']:5.0f} {flag} "
                  + "".join(f"{r['configs'][l]['F8']:>{cw}.2f}" for l in labels))

    scored = [r for r in results if ok(r)]
    print(f"\n{len(scored)} songs scored ({len(results) - len(scored)} low-confidence '~' excluded), "
          f"tol +-{args.tol * 1000:.0f} ms, strip {args.strip_len}, intensity {args.intensity}, "
          f"bin slider {args.custom3}, filter {'off' if args.no_filter else 'on'}")
    print(f"{'config':<14} {'R':>5} {'P':>5} {'P8':>5} {'F':>5} {'F8':>5} {'vis':>5} {'Rvis':>5} {'lag':>6} {'worstF8':>8} {'boom/beat':>9}")
    for l in labels:
        ms = [r["configs"][l] for r in scored]
        if not ms:
            continue
        avg = lambda k: float(np.mean([m[k] for m in ms]))
        worst = float(np.percentile([m["F8"] for m in ms], 10))
        print(f"{l:<14} {avg('R'):5.3f} {avg('P'):5.3f} {avg('P8'):5.3f} {avg('F'):5.3f} {avg('F8'):5.3f} {avg('vis'):5.3f} "
              f"{avg('Rvis'):5.3f} {np.median([m['lag_ms'] for m in ms]):5.0f}ms {worst:8.3f} "
              f"{np.median([m['n_boom'] / max(1, len(r['ref'])) for m, r in zip(ms, scored)]):9.2f}")
    print("R = ref beats caught, P = booms on a beat, P8 = booms on a beat or half-beat, F/F8 = R combined\n"
          "with P/P8, vis = booms that emit particles, Rvis = beats caught AND visible,\n"
          "worstF8 = 10th percentile across songs (how bad the bad songs are), boom/beat = median booms\n"
          "per reference beat (>1 = flashing on more than the beat)")

    if args.json:
        with open(args.json, "w") as f:
            json.dump({"configs": dict(configs), "sb": sb, "tol": args.tol, "songs": results}, f)


if __name__ == "__main__":
    main_cli()
