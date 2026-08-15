import sys
import math
import subprocess
import configparser
import socket
import struct
import threading
import os
import json
import time
import random
import http.server
import socketserver
import urllib.parse
import urllib.request

import numpy as np

# Capture desktop audio from the PulseAudio/PipeWire monitor via parec, analyse it, and stream
# it to WLED using the audioSync v2 protocol ("00002" header, 44-byte packet). WLED's receiver
# reads two independent things:
#   - sampleRaw / sampleSmth : a 0-255 loudness value that drives the "volume" effects.
#   - fftResult[16]          : 16 GEQ bands that drive the "frequency" effects.
# So both paths are auto-gained separately below. Optional live-tuning web panel via [Web].

# conf.txt and presets/ live next to this script (src/), so resolve them relative to it rather than
# the CWD - lets the service and the util scripts launch it from anywhere.
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config = configparser.ConfigParser()
config.read(os.path.join(_BASE_DIR, "conf.txt"))


def audio(key, fallback):
    """Read an [Audio] key with a default, matching the type of the fallback."""
    if isinstance(fallback, bool):
        return config.getboolean("Audio", key, fallback=fallback)
    if isinstance(fallback, int):
        return config.getint("Audio", key, fallback=fallback)
    return config.getfloat("Audio", key, fallback=fallback)


WLED_IP = config.get("WLED", "WLED_IP")
WLED_PORT = config.getint("WLED", "WLED_PORT")
# HTTP JSON API timeout for the (off-thread) presence check + settings sync.
HTTP_TIMEOUT = config.getfloat("WLED", "HTTP_TIMEOUT", fallback=0.6)
# CONTINUOUS_SEND: stream UDP whenever audio plays without caring whether WLED answers (good for
# one-directional / receive-only rigs). When false we still send immediately at startup (assume the
# strip is there) but pause once it's been unreachable for PRESENCE_GRACE_SEC, resuming when it returns.
CONTINUOUS_SEND = config.getboolean("WLED", "CONTINUOUS_SEND", fallback=False)
PRESENCE_GRACE_SEC = config.getfloat("WLED", "PRESENCE_GRACE_SEC", fallback=6.0)

SAMPLE_RATE = config.getint("Audio", "SAMPLE_RATE")
CHUNK_BYTES = config.getint("Audio", "CHUNK_BYTES")   # bytes read per frame; controls latency only
CHUNK_SAMPLES = CHUNK_BYTES // 2

GAIN = audio("GAIN", 1.0)                 # overall sensitivity trim (scales the AGC target)

# FFT / bands (frequency path). The 16 GEQ channels mirror WLED's own audioreactive usermod: the
# same frequency split and pink-noise compensation curve, so WLED's GEQ effects get the spectral
# balance they expect (bass doesn't drown out the highs).
FFT_WINDOW_SAMPLES = audio("FFT_WINDOW_SAMPLES", max(1024, CHUNK_SAMPLES))
NUM_BANDS = 16                            # fixed by the protocol (fftResult[16])
BAND_AGC_DECAY = audio("BAND_AGC_DECAY", 0.997)   # how fast the spectrum auto-scale ceiling falls
BAND_GAMMA = audio("BAND_GAMMA", 0.5)     # <1 lifts quieter bands for fuller bars (0.5 = sqrt, WLED-like)
# Transient/onset emphasis: additively boost the part of each band that rises above its own slow
# baseline. Makes kicks pop above sustained tones, so threshold beat-detectors in WLED effects
# (e.g. PS Sonic Boom) fire reliably even on high-sustain songs. 0 = off; ~1.5-2.5 for punchy beats.
BAND_ATTACK = audio("BAND_ATTACK", 0.0)
# Fold the lowest N bands into band 0 so effects that read a single bin (e.g. PS Sonic Boom on
# bin 0) catch energy across a range, wherever a track's content sits. 0 = off. ~6 covers 43-430Hz.
BASS_FOLD = audio("BASS_FOLD", 0)
# How BASS_FOLD combines those bands into bin 0:
#   0 = levels (max) - bin 0 shows presence; good for level effects, but fills valleys and makes
#       edge-triggered beat effects skip at high BPM.
#   1 = onsets - bin 0 fires on an attack in ANY folded band (kick, guitar strum) yet returns to
#       ~0 between hits, so beat effects re-arm cleanly. Best for PS Sonic Boom.
BASS_FOLD_ONSET = audio("BASS_FOLD_ONSET", 0)
# Adaptive whitening (Stowell & Plumbley) in the onset fold: normalise each band's onset by its own
# recent peak so a beat carried by a quiet band still registers, without retuning per song. 0/1.
FOLD_WHITEN = audio("FOLD_WHITEN", 0)

# WLED's 16-channel frequency edges (Hz), pink-noise compensation and top-end damping, straight
# from its audioreactive usermod. The weight boosts higher bands (which naturally carry far less
# energy) so the spectrum looks balanced, exactly as WLED does before it fills fftResult.
_WLED_BAND_EDGES_HZ = [43, 86, 129, 216, 301, 430, 560, 818, 1120, 1421,
                       1895, 2412, 3015, 3704, 4479, 7106, 9259]
_WLED_PINK = np.array([1.70, 1.71, 1.73, 1.78, 1.68, 1.56, 1.55, 1.63,
                       1.79, 1.62, 1.80, 2.06, 2.47, 3.35, 6.83, 9.55])
_BAND_WEIGHT = _WLED_PINK.copy()
_BAND_WEIGHT[14] *= 0.88
_BAND_WEIGHT[15] *= 0.70
# Weighting for the onset fold: undoing the pink boost makes bands fold by their real transient
# energy, which is naturally bass-first - so a kick wins when present, and treble (hi-hats) only
# fires when the low end is empty, instead of the pink-inflated highs dominating.
_FOLD_TILT = _WLED_PINK[0] / _WLED_PINK
# Approx centre frequency of each band, for the web monitor's labels.
_BAND_CENTERS_HZ = [int(round((_WLED_BAND_EDGES_HZ[i] * _WLED_BAND_EDGES_HZ[i + 1]) ** 0.5))
                    for i in range(NUM_BANDS)]

# Volume path AGC: nudge a gain multiplier so the *average* loudness tracks AGC_TARGET.
# Beats poke above the average, quiet passages stay dim - this is what real WLED AGC does.
AGC_TARGET = audio("AGC_TARGET", 90.0)    # average output level (0-255) the AGC aims for
AGC_SPEED = audio("AGC_SPEED", 0.02)      # per-frame fraction the gain moves toward its target
AGC_MAX_GAIN = audio("AGC_MAX_GAIN", 2000.0)

SQUELCH = audio("SQUELCH", 40.0)          # raw mean-abs below this = silence/noise, output 0
SMTH_ALPHA = audio("SMTH_ALPHA", 0.35)    # EMA factor for sampleSmth

# Beat/onset detection -> samplePeak (a 0/1 flag to WLED, not a loudness value)
BEAT_THRESHOLD_MULT = audio("BEAT_THRESHOLD_MULT", 1.4)
BEAT_REFRACTORY_MS = audio("BEAT_REFRACTORY_MS", 120.0)

# Output: WLED renders ~43fps, so ~90 packets/s keeps its data fresh at roughly half the traffic.
# After SILENCE_HOLD_SEC of silence we stop sending entirely (WLED holds the last, zeroed frame = off)
# and resume the instant audio returns - the hold avoids cutting out during quiet passages.
SEND_HZ = audio("SEND_HZ", 90.0)
SILENCE_HOLD_SEC = audio("SILENCE_HOLD_SEC", 3.0)

WEB_ENABLED = config.getboolean("Web", "enabled", fallback=False)
WEB_HOST = config.get("Web", "host", fallback="127.0.0.1")
WEB_PORT = config.getint("Web", "port", fallback=8080)
PRESETS_DIR = os.path.join(_BASE_DIR, "presets")

if FFT_WINDOW_SAMPLES < CHUNK_SAMPLES:
    sys.exit("FFT_WINDOW_SAMPLES must be >= CHUNK_BYTES/2 (one read chunk).")

CHUNK_MSEC = CHUNK_SAMPLES / SAMPLE_RATE * 1000
SEND_DECIMATE = max(1, round((1000.0 / CHUNK_MSEC) / SEND_HZ))   # send every Nth analysed frame
VOL_AVG_ALPHA = 1 - math.exp(-CHUNK_MSEC / 400.0)          # ~400ms time constant for the AGC control average
BASELINE_ALPHA = 1 - math.exp(-CHUNK_MSEC / 250.0)         # ~250ms per-band baseline for transient emphasis
_WHITEN_DECAY = math.exp(-CHUNK_MSEC / 2000.0)             # ~2s per-bin peak memory for adaptive whitening
WHITEN_FLOOR = audio("WHITEN_FLOOR", 0.08)                 # keep silent bins from whitening up into noise
MEDIAN_WIN = max(8, int(700.0 / CHUNK_MSEC))               # ~0.7s window for the median beat threshold


class LiveParams:
    """Thread-safe store for the knobs the web panel may change while running. The audio thread
    reads one snapshot() per frame; the web thread writes via set()."""

    def __init__(self, **initial):
        self._lock = threading.Lock()
        self._values = dict(initial)

    def snapshot(self):
        with self._lock:
            return dict(self._values)

    def set(self, name, value):
        with self._lock:
            if name in self._values:
                self._values[name] = value


live = LiveParams(
    GAIN=GAIN,
    SQUELCH=SQUELCH,
    AGC_TARGET=AGC_TARGET,
    AGC_SPEED=AGC_SPEED,
    BAND_AGC_DECAY=BAND_AGC_DECAY,
    BAND_GAMMA=BAND_GAMMA,
    BAND_ATTACK=BAND_ATTACK,
    BASS_FOLD=BASS_FOLD,
    BASS_FOLD_ONSET=BASS_FOLD_ONSET,
    FOLD_WHITEN=FOLD_WHITEN,
    SMTH_ALPHA=SMTH_ALPHA,
    BEAT_THRESHOLD_MULT=BEAT_THRESHOLD_MULT,
    BEAT_REFRACTORY_MS=BEAT_REFRACTORY_MS,
    AGC_MAX_GAIN=AGC_MAX_GAIN,
    WHITEN_FLOOR=WHITEN_FLOOR,
)

# (key, label, step, min, max) - drives both the HTML form and clamping of submitted values.
# FFT window / band edges aren't here: changing them resizes precomputed arrays, which needs a restart.
_PARAM_SPEC = [
    ("GAIN", "Gain", 0.1, 0.1, 10.0),
    ("SQUELCH", "Squelch (raw noise floor)", 1, 0, 2000),
    ("AGC_TARGET", "AGC target level (0-255)", 5, 10, 240),
    ("AGC_SPEED", "AGC speed", 0.001, 0.001, 0.5),
    ("AGC_MAX_GAIN", "AGC max gain", 50, 100, 5000),
    ("BAND_AGC_DECAY", "Band AGC decay", 0.0005, 0.9, 0.999999),
    ("BAND_GAMMA", "Band gamma (<1 = fuller)", 0.05, 0.2, 1.5),
    ("BAND_ATTACK", "Beat attack (transient boost)", 0.1, 0.0, 4.0),
    ("BASS_FOLD", "Fold N low bands into bin 0 (0=off)", 1, 0, 16),
    ("BASS_FOLD_ONSET", "Fold onsets not levels (0/1)", 1, 0, 1),
    ("FOLD_WHITEN", "Adaptive whitening in fold (0/1)", 1, 0, 1),
    ("WHITEN_FLOOR", "Whitening floor", 0.01, 0.01, 0.5),
    ("SMTH_ALPHA", "Smoothing alpha", 0.01, 0.01, 1.0),
    ("BEAT_THRESHOLD_MULT", "Beat threshold mult", 0.05, 1.0, 5.0),
    ("BEAT_REFRACTORY_MS", "Beat refractory (ms)", 5, 0, 1000),
]

# Grouping for the web panel so the knobs aren't one long list.
_PARAM_GROUPS = [
    ("General", ["GAIN", "SQUELCH"]),
    ("Frequency bands", ["BAND_GAMMA", "BAND_ATTACK", "BAND_AGC_DECAY",
                         "BASS_FOLD", "BASS_FOLD_ONSET", "FOLD_WHITEN", "WHITEN_FLOOR"]),
    ("Volume", ["AGC_TARGET", "AGC_SPEED", "AGC_MAX_GAIN", "SMTH_ALPHA"]),
    ("Beat (samplePeak)", ["BEAT_THRESHOLD_MULT", "BEAT_REFRACTORY_MS"]),
]
_SPEC_BY_KEY = {k: (k, label, step, lo, hi) for k, label, step, lo, hi in _PARAM_SPEC}

class Viz:
    """Latest analysis frame, published by the audio thread for the web monitor. No lock needed:
    each field is replaced wholesale and only ever read for display."""

    def __init__(self):
        self.bins = [0] * NUM_BANDS
        self.peak = np.zeros(NUM_BANDS)   # decaying peak-hold, so fast transients aren't missed
        self.raw = 0.0
        self.smth = 0.0
        self.peakhz = 0.0
        self.beats = 0                    # monotonic samplePeak counter; the UI diffs it to flash

    def update(self, result):
        bars, raw_255, smth_255, beat, _peak_mag, peak_freq = result
        self.peak = np.maximum(bars, self.peak * 0.92)
        self.bins = bars.tolist()
        self.raw = float(raw_255)
        self.smth = float(smth_255)
        self.peakhz = float(peak_freq)
        if beat:
            self.beats += 1

    def data(self):
        return {
            "bins": self.bins,
            "peak": [int(x) for x in self.peak],
            "raw": round(self.raw, 1),
            "smth": round(self.smth, 1),
            "peakHz": round(self.peakhz, 1),
            "beats": self.beats,
        }


viz = Viz()

# WLED renders each effect once per frame (~42fps) and reads whatever fftResult last arrived over
# UDP. We stream at ~SEND_HZ, so the Sonic Boom sim ticks on the exact bins we send (the peak-held
# acc_bins), decimated down to WLED's render cadence - see run_loopback().
WLED_RENDER_FPS = 42.0
SEND_FPS = (1000.0 / CHUNK_MSEC) / SEND_DECIMATE
SB_SEND_DECIMATE = max(1, round(SEND_FPS / WLED_RENDER_FPS))


class SonicBoom:
    """Server-side mirror of WLED's PS Sonic Boom beat logic, run on the real bin stream at WLED's
    frame cadence. It reproduces both the threshold crossing AND the particle-emit randomness, so
    the monitor can show which 'booms' actually put particles on the strip vs. which emit nothing."""

    def __init__(self):
        self.custom3 = 0         # WLED "Bin" slider (0-31); the effect uses custom3>>1 as the GEQ bin
        self.intensity = 128
        self.filter = True
        self.strip_len = 60
        self.step = 0.0          # the effect's SEGMENT.step low-pass state
        self.armed = True
        self.booms = 0           # threshold crossings (beats the effect detects)
        self.visible = 0         # crossings that actually emitted >=1 particle (seen on strip)
        self.particles = 0       # particle count of the last boom
        self.loud = 0.0
        self.thr = 0.0
        self._times = []         # timestamps of recent booms, for BPM

    @property
    def bin(self):
        return self.custom3 >> 1     # two slider positions per real GEQ bin, exactly like WLED

    def set_params(self, custom3=None, intensity=None, filter=None, strip_len=None):
        if custom3 is not None:
            self.custom3 = max(0, min(31, custom3))
        if intensity is not None:
            self.intensity = max(0, min(255, intensity))
        if filter is not None:
            self.filter = bool(filter)
        if strip_len is not None:
            self.strip_len = max(1, min(4000, strip_len))

    def tick(self, bars):
        b = self.bin
        loud = int(bars[b])
        if b > 12:
            loud <<= 2                                   # effect boosts high bins for detection
        thr = 150 - (self.intensity >> 1)
        if self.filter:
            self.step = (self.step * 31500 + loud * (32768 - 31500)) / 32768
            thr = 20 + (thr >> 1) + self.step
        self.loud, self.thr = float(loud), float(thr)

        if loud > thr:
            if self.armed:
                self.armed = False
                self.booms += 1
                self._times.append(time.monotonic())
                self._times = self._times[-16:]
                base = 4 + ((self.strip_len - 1) >> 2)   # explosionsize before randomisation
                span = (base * loud) >> 10
                self.particles = random.randrange(span) if span > 0 else 0  # hw_random16(span)
                if self.particles > 0:
                    self.visible += 1
        else:
            self.armed = True

    def bpm(self):
        now = time.monotonic()
        recent = [t for t in self._times if now - t <= 4.0]
        if len(recent) < 3:
            return 0
        gaps = sorted(recent[i + 1] - recent[i] for i in range(len(recent) - 1))
        med = gaps[len(gaps) // 2]
        bpm = 60.0 / med if med > 0 else 0
        return round(bpm) if 30 <= bpm <= 300 else 0

    def state(self):
        return {
            "custom3": self.custom3, "bin": self.bin, "intensity": self.intensity,
            "filter": self.filter, "stripLen": self.strip_len,
            "loud": round(self.loud, 1), "thr": round(self.thr, 1),
            "booms": self.booms, "visible": self.visible, "particles": self.particles,
            "bpm": self.bpm(),
        }


sonic = SonicBoom()

# Frames/sec of the decimated (WLED-cadence) onset stream that feeds the tempo tracker.
SB_FPS = SEND_FPS / SB_SEND_DECIMATE


class TempoTracker:
    """Estimates BPM by autocorrelating the recent onset envelope and picking the strongest lag in
    a musical range - more robust than inter-beat timing since it ignores missed/extra hits."""

    def __init__(self, fps, seconds=6.0, bpm_lo=60, bpm_hi=200):
        self.n = max(64, int(fps * seconds))
        self.buf = np.zeros(self.n)
        self.i = 0
        self.bpm = 0
        self.fps = fps
        self._since = 0
        self._every = max(1, int(fps // 2))                 # recompute ~2x/sec
        lo = max(2, int(round(fps * 60.0 / bpm_hi)))        # smallest lag = fastest tempo
        hi = min(self.n - 1, int(round(fps * 60.0 / bpm_lo)))
        self.lags = np.arange(lo, hi + 1)
        bpms = 60.0 * self.fps / self.lags
        self.weights = np.exp(-0.5 * (np.log2(bpms / 125.0) / 0.9) ** 2)  # prefer mid-tempo, avoids octave errors

    def push(self, onset):
        self.buf[self.i] = onset
        self.i = (self.i + 1) % self.n
        self._since += 1
        if self._since >= self._every:
            self._since = 0
            self._estimate()

    def _estimate(self):
        x = np.concatenate([self.buf[self.i:], self.buf[:self.i]])   # oldest -> newest
        x = x - x.mean()
        if not np.any(x) or len(self.lags) == 0:
            self.bpm = 0
            return
        best_lag, best = 0, 0.0
        for k, lag in enumerate(self.lags):
            r = float(np.dot(x[lag:], x[:-lag])) * self.weights[k]
            if r > best:
                best, best_lag = r, int(lag)
        self.bpm = round(60.0 * self.fps / best_lag) if best_lag else 0


tempo = TempoTracker(SB_FPS)


class TxStats:
    """Rolling UDP send rate to WLED (packets/s and KB/s), refreshed once per second."""

    def __init__(self):
        self._t0 = time.monotonic()
        self._last_add = self._t0
        self._pkts = 0
        self._bytes = 0
        self.pps = 0
        self.kbps = 0.0

    def add(self, nbytes):
        now = time.monotonic()
        if now - self._last_add > 1.2:          # resumed after a gap: start a fresh window
            self._t0 = now
            self._pkts = self._bytes = 0
        self._last_add = now
        self._pkts += 1
        self._bytes += nbytes
        dt = now - self._t0
        if dt >= 1.0:
            self.pps = round(self._pkts / dt)
            self.kbps = round(self._bytes / dt / 1024, 1)
            self._t0 = now
            self._pkts = self._bytes = 0

    def current(self):
        # report 0 when we haven't sent for over a second (e.g. silence-stopped), not a stale rate
        if time.monotonic() - self._last_add > 1.2:
            return 0, 0.0
        return self.pps, self.kbps


tx = TxStats()


class WledClient:
    """Polls WLED's JSON API so the panel can show what the strip is doing and, crucially, pull the
    live effect/intensity/bin/length back to drive the Sonic Boom preview from the real settings."""

    def __init__(self, ip, timeout):
        self.ip = ip
        self.timeout = timeout
        self.online = False
        self.polled = False        # has at least one poll attempt finished?
        self.last_seen = None      # monotonic time of the last successful poll
        self.error = ""
        self.info = {}
        self.effects = []
        self.state = {}

    def reachable(self, grace):
        """Cheap in-memory presence check for the send loop (no I/O - polling runs off-thread).
        Assumes the strip is present until the first poll finishes, so startup never blocks on WLED
        and the first packets go straight out; after that it must have answered within `grace`."""
        if not self.polled:
            return True
        if self.online:
            return True
        if self.last_seen is None:      # polled but never reachable -> strip not found
            return False
        return (time.monotonic() - self.last_seen) < grace

    def _get(self, path):
        with urllib.request.urlopen(f"http://{self.ip}{path}", timeout=self.timeout) as r:
            return json.loads(r.read().decode())

    def poll(self):
        try:
            if not self.effects:
                full = self._get("/json")
                self.effects = full.get("effects", [])
                self.info = full.get("info", {})
                self.state = full.get("state", {})
            else:
                self.state = self._get("/json/state")
            self.online = True
            self.last_seen = time.monotonic()
            self.error = ""
        except Exception as e:
            self.online = False
            self.error = str(e)
        finally:
            self.polled = True

    def active_seg(self):
        segs = self.state.get("seg", [])
        for s in segs:
            if s.get("sel"):
                return s
        main = self.state.get("mainseg", 0)
        if 0 <= main < len(segs):
            return segs[main]
        return segs[0] if segs else {}

    def summary(self):
        seg = self.active_seg()
        fx = seg.get("fx")
        name = self.effects[fx] if isinstance(fx, int) and fx < len(self.effects) else None
        leds = self.info.get("leds", {})
        seg_len = seg.get("len") or (seg.get("stop", 0) - seg.get("start", 0)) or leds.get("count")
        return {
            "online": self.online, "error": self.error,
            "name": self.info.get("name"), "ver": self.info.get("ver"),
            "leds": leds.get("count"), "fps": leds.get("fps"),
            "on": self.state.get("on"), "bri": self.state.get("bri"),
            "fx": fx, "effect": (name.split("@")[0] if name else None),
            "isSonicBoom": bool(name and name.startswith("PS Sonic Boom")),
            # full segment slider set - sync_sonic() uses ix/c3/o2/len today; the rest (Color c1,
            # Position c2, ...) are kept so a per-effect monitor can read them later.
            "seg": {"ix": seg.get("ix"), "c1": seg.get("c1"), "c2": seg.get("c2"),
                    "c3": seg.get("c3"), "o2": seg.get("o2"), "len": seg_len},
        }

    def sync_sonic(self):
        """Push the live segment's Sonic Boom settings into the local sim."""
        s = self.summary()["seg"]
        sonic.set_params(
            custom3=s.get("c3"),          # raw 0-31 slider; SonicBoom derives the bin (>>1) itself
            intensity=s.get("ix"),
            filter=bool(s["o2"]) if s.get("o2") is not None else None,
            strip_len=s.get("len"),
        )


wled = WledClient(WLED_IP, HTTP_TIMEOUT)

SERVICE_UNIT = config.get("Web", "service", fallback="glowbird-protocol.service")


def service_status():
    try:
        active = subprocess.run(["systemctl", "--user", "is-active", SERVICE_UNIT],
                                capture_output=True, text=True, timeout=3).stdout.strip()
        text = subprocess.run(["systemctl", "--user", "status", SERVICE_UNIT, "--no-pager", "-n", "12"],
                              capture_output=True, text=True, timeout=3).stdout
        return {"active": active or "unknown", "text": text[:4000]}
    except Exception as e:
        return {"active": "unknown", "text": f"status failed: {e}"}


def service_restart():
    # --no-block so restarting our own unit returns before systemd stops this process
    subprocess.run(["systemctl", "--user", "restart", "--no-block", SERVICE_UNIT],
                   capture_output=True, text=True, timeout=5)


udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# Precomputed analysis state
_hann = np.hanning(FFT_WINDOW_SAMPLES)
_freqs = np.fft.rfftfreq(FFT_WINDOW_SAMPLES, d=1.0 / SAMPLE_RATE)
_band_bin_edges = np.clip(np.searchsorted(_freqs, _WLED_BAND_EDGES_HZ), 0, len(_freqs) - 1)

_rolling_buffer = np.zeros(FFT_WINDOW_SAMPLES, dtype=np.float64)
_band_baseline = np.zeros(NUM_BANDS)   # slow per-band level, subtracted for transient emphasis
_fold_peak = np.zeros(NUM_BANDS)       # per-band recent onset peak, for adaptive whitening
_energy_ring = np.zeros(MEDIAN_WIN)    # recent raw levels, for the median beat threshold
_ring_i = 0
_band_agc_ref = 1.0
_agc_gain = 1.0        # volume-path AGC multiplier
_vol_avg = 1.0         # slow average of raw_level, the AGC control signal
_smth_level = 0.0
_beat_refractory = 0


def compute_bands(p, gated):
    """16 GEQ bands (0-254) using WLED's own channel split + pink-noise curve, then a global
    linear auto-scale so the bars read like a spectrum analyser instead of a wall of bass."""
    global _band_agc_ref, _band_baseline, _fold_peak

    windowed = _rolling_buffer * _hann          # Hann window cuts spectral leakage between bins
    spectrum = np.abs(np.fft.rfft(windowed))
    n_bins = len(spectrum)

    band_energy = np.empty(NUM_BANDS)
    for i in range(NUM_BANDS):
        lo = int(_band_bin_edges[i])
        hi = int(min(max(lo + 1, _band_bin_edges[i + 1]), n_bins))
        band_energy[i] = np.mean(spectrum[lo:hi])
    band_energy *= _BAND_WEIGHT                  # pink-noise compensation (lift highs, damp top two)

    _band_baseline += (band_energy - _band_baseline) * BASELINE_ALPHA
    if p["BAND_ATTACK"] > 0:                     # push transients above the sustained floor
        band_energy += p["BAND_ATTACK"] * np.maximum(band_energy - _band_baseline, 0)

    if not gated:
        _band_agc_ref = max(band_energy.max(), _band_agc_ref * p["BAND_AGC_DECAY"], 1e-6)

    norm = np.clip(band_energy / _band_agc_ref, 0, 1)
    bars = (norm ** p["BAND_GAMMA"]) * 255 * p["GAIN"]   # gamma<1 fills the bars out
    bars = np.where(gated, 0, np.clip(bars, 0, 254)).astype(np.uint8)

    fold = int(p["BASS_FOLD"])
    if fold > 1:
        if p["BASS_FOLD_ONSET"] > 0:
            # Fold ONSETS (energy above each band's slow baseline), not levels: bin 0 fires on an
            # attack in any folded band but falls back to ~0 between hits, so edge-triggered beat
            # effects re-arm cleanly and don't skip at high BPM.
            onset = np.clip(np.maximum(band_energy - _band_baseline, 0.0) / _band_agc_ref, 0, 1)
            if p["FOLD_WHITEN"] > 0:
                # normalise each band's onset by its own recent peak so a beat carried by a quiet
                # band still registers (adaptive whitening); the floor keeps silent bins down.
                _fold_peak = np.maximum(onset, np.maximum(p["WHITEN_FLOOR"], _WHITEN_DECAY * _fold_peak))
                onset = onset / _fold_peak
            ob = (onset[:fold] ** p["BAND_GAMMA"]) * 255 * p["GAIN"] * _FOLD_TILT[:fold]
            bars[0] = 0 if gated else np.uint8(np.clip(ob.max(), 0, 254))
        else:
            bars[0] = bars[:fold].max()     # fold levels: bin 0 shows bass presence

    peak_bin = int(np.argmax(spectrum[1:])) + 1   # skip DC so FFT_MajorPeak is never 0 Hz
    return bars, float(_freqs[peak_bin]), float(spectrum[peak_bin])


def compute_volume(raw_level, p, gated):
    """Volume loudness (0-255) for sampleRaw, plus its smoothed EMA for sampleSmth."""
    global _agc_gain, _vol_avg, _smth_level

    if not gated:
        _vol_avg += (raw_level - _vol_avg) * VOL_AVG_ALPHA
        target_gain = (p["AGC_TARGET"] * p["GAIN"]) / max(_vol_avg, 1e-3)
        _agc_gain += (target_gain - _agc_gain) * p["AGC_SPEED"]
        _agc_gain = min(max(_agc_gain, 0.0), p["AGC_MAX_GAIN"])

    raw_255 = 0.0 if gated else float(np.clip(raw_level * _agc_gain, 0, 255))
    _smth_level = p["SMTH_ALPHA"] * raw_255 + (1 - p["SMTH_ALPHA"]) * _smth_level
    return raw_255, _smth_level


def detect_beat(raw_level, p, gated):
    """Onset detector -> samplePeak flag: instant level vs. a MEDIAN of recent levels (robust to the
    beat's own spike, unlike a mean), with a refractory gap so one hit doesn't retrigger."""
    global _beat_refractory, _ring_i

    _energy_ring[_ring_i] = raw_level
    _ring_i = (_ring_i + 1) % MEDIAN_WIN
    threshold = np.median(_energy_ring) * p["BEAT_THRESHOLD_MULT"]

    if not gated and _beat_refractory == 0 and raw_level > threshold and raw_level > p["SQUELCH"]:
        _beat_refractory = max(1, int(p["BEAT_REFRACTORY_MS"] / CHUNK_MSEC))
        return 1
    _beat_refractory = max(0, _beat_refractory - 1)
    return 0


def analyse(audio_chunk):
    try:
        p = live.snapshot()
        new_samples = np.frombuffer(audio_chunk, dtype=np.int16).astype(np.float64)
        n = len(new_samples)

        # slide the analysis window and append the newest chunk (more FFT resolution, no extra latency)
        _rolling_buffer[:-n] = _rolling_buffer[n:]
        _rolling_buffer[-n:] = new_samples

        raw_level = float(np.mean(np.abs(new_samples)))
        gated = raw_level < p["SQUELCH"]

        bars, peak_freq, peak_mag = compute_bands(p, gated)
        raw_255, smth_255 = compute_volume(raw_level, p, gated)
        beat = detect_beat(raw_level, p, gated)
        return bars, raw_255, smth_255, beat, peak_mag, peak_freq
    except Exception as e:
        print(f"Error analysing audio: {e}")
        return None, 0, 0, 0, 0, 0


def create_udp_packet(fft_values, raw_level, smoothed_level, peak_flag, fft_magnitude, fft_peak_frequency):
    return struct.pack('<6s2B2fBB16B2B2f',
        b'00002',                   # header
        0, 0,                       # reserved
        float(raw_level),           # sampleRaw
        float(smoothed_level),      # sampleSmth
        int(peak_flag),             # samplePeak (0 / 1)
        0,                          # reserved
        *[int(v) for v in fft_values],  # fftResult - 16 GEQ channels
        0, 0,                       # reserved
        float(fft_magnitude),       # FFT_Magnitude (of the major peak)
        float(fft_peak_frequency))  # FFT_MajorPeak (Hz)


def safe_preset_name(name):
    """Keep only filename-safe characters so a preset name can't escape PRESETS_DIR."""
    return "".join(c for c in name if c.isalnum() or c in "-_")


def list_presets():
    if not os.path.isdir(PRESETS_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(PRESETS_DIR) if f.endswith(".conf"))


def build_conf_text():
    """Serialise the current settings (live tunables + fixed startup values) to a full conf.txt,
    so a saved preset can be dropped straight back in as conf.txt."""
    p = live.snapshot()
    g = lambda x: f"{x:g}"
    return (
        f"[WLED]\nWLED_IP = {WLED_IP}\nWLED_PORT = {WLED_PORT}\n"
        f"HTTP_TIMEOUT = {g(HTTP_TIMEOUT)}\nCONTINUOUS_SEND = {str(CONTINUOUS_SEND).lower()}\n"
        f"PRESENCE_GRACE_SEC = {g(PRESENCE_GRACE_SEC)}\n\n"
        f"[Audio]\nSAMPLE_RATE = {SAMPLE_RATE}\nCHUNK_BYTES = {CHUNK_BYTES}\n"
        f"GAIN = {g(p['GAIN'])}\n\n"
        f"FFT_WINDOW_SAMPLES = {FFT_WINDOW_SAMPLES}\n"
        f"BAND_AGC_DECAY = {g(p['BAND_AGC_DECAY'])}\nBAND_GAMMA = {g(p['BAND_GAMMA'])}\n"
        f"BAND_ATTACK = {g(p['BAND_ATTACK'])}\nBASS_FOLD = {int(p['BASS_FOLD'])}\n"
        f"BASS_FOLD_ONSET = {int(p['BASS_FOLD_ONSET'])}\nFOLD_WHITEN = {int(p['FOLD_WHITEN'])}\n"
        f"WHITEN_FLOOR = {g(p['WHITEN_FLOOR'])}\n\n"
        f"AGC_TARGET = {g(p['AGC_TARGET'])}\n"
        f"AGC_SPEED = {g(p['AGC_SPEED'])}\nAGC_MAX_GAIN = {g(p['AGC_MAX_GAIN'])}\n"
        f"SQUELCH = {g(p['SQUELCH'])}\nSMTH_ALPHA = {g(p['SMTH_ALPHA'])}\n\n"
        f"BEAT_THRESHOLD_MULT = {g(p['BEAT_THRESHOLD_MULT'])}\n"
        f"BEAT_REFRACTORY_MS = {g(p['BEAT_REFRACTORY_MS'])}\n\n"
        f"SEND_HZ = {g(SEND_HZ)}\nSILENCE_HOLD_SEC = {g(SILENCE_HOLD_SEC)}\n\n"
        f"[Web]\nenabled = {str(WEB_ENABLED).lower()}\nhost = {WEB_HOST}\nport = {WEB_PORT}\n"
    )


def save_preset(name):
    os.makedirs(PRESETS_DIR, exist_ok=True)
    with open(os.path.join(PRESETS_DIR, name + ".conf"), "w") as f:
        f.write(build_conf_text())


def load_preset(name):
    """Apply a preset's live-tunable values without a restart (fixed startup keys are ignored)."""
    cp = configparser.ConfigParser()
    if not cp.read(os.path.join(PRESETS_DIR, name + ".conf")):
        return
    for key, _label, _step, lo, hi in _PARAM_SPEC:
        if cp.has_option("Audio", key):
            try:
                live.set(key, min(max(cp.getfloat("Audio", key), lo), hi))
            except ValueError:
                pass


_PAGE_CSS = """
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;background:#0f1115;color:#e6e8ec;font:14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:640px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:18px;font-weight:600;margin:0 0 2px}
.sub{color:#8b93a1;font-size:12px;margin:0 0 20px}
.card{background:#171a21;border:1px solid #232833;border-radius:10px;padding:16px 18px;margin-bottom:16px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:#8b93a1;margin:0 0 12px;font-weight:600}
.row{display:grid;grid-template-columns:1fr 1.6fr 76px;gap:12px;align-items:center;margin:9px 0}
.row label{color:#c3c9d4}
input[type=range]{width:100%;accent-color:#5b8cff}
input[type=number],.save input{background:#0f1115;border:1px solid #2a2f3a;color:#e6e8ec;border-radius:6px;padding:6px 8px;font:inherit}
input[type=number]{width:100%;text-align:right}
button{background:#5b8cff;color:#fff;border:0;border-radius:6px;padding:8px 16px;font:inherit;font-weight:600;cursor:pointer}
button:hover{background:#6f9bff}
button.mini{padding:4px 12px;font-size:12px;background:#2a2f3a}
button.mini:hover{background:#353c4a}
.actions{margin-top:14px}
.presets{list-style:none;margin:0;padding:0}
.presets li{display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-top:1px solid #232833}
.presets li:first-child{border-top:0}.presets .empty{color:#8b93a1}
.presets form{margin:0}
.save{display:flex;gap:8px;margin-top:14px}.save input{flex:1}
.note{color:#8b93a1;font-size:12px;margin:12px 0 0}
code{background:#0f1115;border:1px solid #2a2f3a;border-radius:4px;padding:1px 5px}
#spec{width:100%;height:auto;display:block;background:#0f1115;border:1px solid #232833;border-radius:8px}
.mrow{display:flex;align-items:center;gap:10px;margin:9px 0}
.ml{color:#c3c9d4;font-size:12px;min-width:74px}
.mv{color:#8b93a1;font-size:12px;min-width:52px;text-align:right;font-variant-numeric:tabular-nums}
.track{flex:1;height:10px;background:#0f1115;border:1px solid #2a2f3a;border-radius:5px;overflow:hidden}
.track i{display:block;height:100%;width:0;background:linear-gradient(90deg,#5b8cff,#8b5bff)}
.dot{width:13px;height:13px;border-radius:50%;background:#ff5b7a;opacity:.15}
.sb{margin-top:16px;border-top:1px solid #232833;padding-top:14px}
.sb h3{font-size:12px;margin:0 0 10px;color:#c3c9d4;font-weight:600}
.sbctl{display:flex;flex-direction:column;gap:8px;margin-bottom:12px;font-size:12px;color:#c3c9d4}
.sbctl label{display:flex;align-items:center;gap:8px}
.sbctl input[type=range]{flex:1;max-width:220px;accent-color:#ff8f5b}
.sbctl b{color:#e6e8ec;min-width:70px}
.sbtrack{position:relative;height:16px;background:#0f1115;border:1px solid #2a2f3a;border-radius:5px;overflow:hidden;margin-bottom:8px}
.sbtrack i{display:block;height:100%;width:0;background:linear-gradient(90deg,#ffb15b,#ff5b5b)}
.sbtrack .thr{position:absolute;top:0;bottom:0;width:2px;background:#fff;left:0}
.sbnums{font-size:12px;color:#8b93a1;font-variant-numeric:tabular-nums}
.sbnums b{color:#e6e8ec}
.boom{color:#ff8f5b;font-weight:700}
.sbstat{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 16px;margin-top:10px;font-size:12px;color:#8b93a1;font-variant-numeric:tabular-nums}
.sbstat b{color:#e6e8ec}
.bpm{font-size:22px;font-weight:700;color:#8b5bff;margin-right:6px}
.pill{font-size:11px;padding:2px 9px;border-radius:20px;background:#2a2f3a;color:#c3c9d4}
.pill.on{background:#1e3a2a;color:#6ee7a0}.pill.off{background:#3a1e24;color:#ff8fa3}
.groups{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:6px 22px}
.group{padding:4px 0}
.group h3{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#8b93a1;margin:8px 0 6px;font-weight:600}
.wled{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}
.wled .kv{background:#0f1115;border:1px solid #232833;border-radius:8px;padding:8px 10px}
.wled .k{font-size:11px;color:#8b93a1}.wled .v{font-size:14px;color:#e6e8ec;font-weight:600;margin-top:2px}
.bar-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:12px}
pre.svc{background:#0f1115;border:1px solid #232833;border-radius:8px;padding:10px;font-size:11px;
  color:#c3c9d4;overflow-x:auto;white-space:pre;max-height:240px;margin:12px 0 0}
"""

_VIZ_JS = """
const spec=document.getElementById('spec'),sx=spec.getContext('2d');
const $=id=>document.getElementById(id);
let lastBeats=0,beatFlash=0,boomFlash=0;

function drawSpectrum(bins,peak){
  const W=spec.width,H=spec.height,n=bins.length,bw=W/n,base=H-14;
  sx.clearRect(0,0,W,H);
  for(let i=0;i<n;i++){
    const x=i*bw,h=bins[i]/254*base,ph=peak[i]/254*base,hue=210-i/n*210;
    sx.fillStyle='hsl('+hue+',70%,55%)';sx.fillRect(x+1,base-h,bw-2,h);
    sx.fillStyle='rgba(255,255,255,.75)';sx.fillRect(x+1,base-ph-2,bw-2,2);
    if(i%2===0){sx.fillStyle='#8b93a1';sx.font='9px system-ui';sx.textAlign='center';sx.fillText(i,x+bw/2,H-3);}
  }
}
let lastBooms=0;
function pushSB(){
  const q='c3='+(+$('sbbin').value)+'&intensity='+(+$('sbint').value)
    +'&filter='+($('sbfilter').checked?1:0)+'&len='+(+$('sblen').value);
  fetch('/sbconfig?'+q);
}
function renderWled(w){
  const box=$('wledbox');
  if(!w||!w.online){box.innerHTML="<div class='kv'><div class='k'>WLED</div><div class='v'>offline</div></div>";
    $('wsync').disabled=true;return;}
  $('wsync').disabled=false;
  const cells=[['Name',w.name||'-'],['Effect',w.effect||('fx '+w.fx)],
    ['LEDs',w.leds!=null?w.leds:'-'],['Strip FPS',w.fps!=null?w.fps:'-'],
    ['Brightness',w.bri!=null?w.bri:'-'],['Power',w.on?'on':'off']];
  box.innerHTML=cells.map(c=>"<div class='kv'><div class='k'>"+c[0]+"</div><div class='v'>"+c[1]+"</div></div>").join('');
}
async function tick(){
  let d;try{d=await(await fetch('/data')).json();}catch(e){return;}
  drawSpectrum(d.bins,d.peak);
  $('mraw').style.width=d.raw/255*100+'%';$('rawv').textContent=d.raw.toFixed(0);
  $('msmth').style.width=d.smth/255*100+'%';$('smthv').textContent=d.smth.toFixed(0);
  $('peakhz').textContent=d.peakHz.toFixed(0)+' Hz';
  if(d.beats>lastBeats)beatFlash=5;lastBeats=d.beats;
  $('beatdot').style.opacity=beatFlash>0?1:.15;if(beatFlash>0)beatFlash--;

  // PS Sonic Boom - values come from the server sim (real bins, WLED cadence, particle randomness)
  const sb=d.sb;
  $('sbbinv').textContent=sb.custom3+' → bin '+sb.bin+' (~'+BAND_HZ[sb.bin]+' Hz)';
  $('sbintv').textContent=sb.intensity;
  $('sbloud').style.width=Math.min(100,sb.loud/255*100)+'%';
  $('sbthr').style.left=Math.min(100,sb.thr/255*100)+'%';
  $('sbloudv').textContent=Math.round(sb.loud);$('sbthrv').textContent=Math.round(sb.thr);
  $('bpm').textContent=((d.tempo>0)?d.tempo:(sb.bpm>0?sb.bpm:'--'))+' BPM';
  $('sbbooms').textContent=sb.booms;$('sbvis').textContent=sb.visible;$('sbpart').textContent=sb.particles;
  if(document.activeElement!==$('sblen'))$('sblen').value=sb.stripLen;  // auto-followed from WLED
  if(sb.booms>lastBooms){boomFlash=8;}lastBooms=sb.booms;
  const dot=$('boomdot');
  dot.style.opacity=boomFlash>0?1:.2;dot.style.background=sb.particles>0?'#6ee7a0':'#ff5b7a';
  if(boomFlash>0)boomFlash--;
  renderWled(d.wled);
  if(d.tx){$('txpps').textContent=d.tx.pps;$('txkb').textContent=d.tx.kbps;}
}
async function svcStatus(){
  $('svcout').textContent='...';
  try{const s=await(await fetch('/service')).json();
    $('svcpill').textContent=s.active;$('svcpill').className='pill '+(s.active==='active'?'on':'off');
    $('svcout').textContent=s.text;}catch(e){$('svcout').textContent='failed: '+e;}
}
async function svcRestart(){
  if(!confirm('Restart glowbird-protocol.service?'))return;
  $('svcout').textContent='restarting...';
  try{await fetch('/service/restart',{method:'POST'});
    setTimeout(svcStatus,1500);}catch(e){$('svcout').textContent='failed: '+e;}
}
function syncFields(sb){$('sbbin').value=sb.custom3;$('sbint').value=sb.intensity;
  $('sbfilter').checked=sb.filter;$('sblen').value=sb.stripLen;}
['sbbin','sbint','sbfilter','sblen'].forEach(id=>$(id).addEventListener('input',pushSB));
$('wsync').addEventListener('click',async()=>{await fetch('/wled/sync',{method:'POST'});
  const d=await(await fetch('/data')).json();syncFields(d.sb);});
$('svcbtn').addEventListener('click',svcStatus);
$('svcrestart').addEventListener('click',svcRestart);
(async()=>{try{const d=await(await fetch('/data')).json();syncFields(d.sb);}catch(e){}
  setInterval(tick,50);})();
"""


class _ParamPanelHandler(http.server.BaseHTTPRequestHandler):
    """Styled panel for the live-tunable knobs + presets. No auth - localhost or trusted LAN only."""

    def log_message(self, fmt, *args):
        pass

    def _render_page(self):
        values = live.snapshot()

        def row(key):
            k, label, step, lo, hi = _SPEC_BY_KEY[key]
            v = f"{values[k]:g}"
            return (f'<div class="row"><label>{label}</label>'
                    f'<input type="range" min="{lo}" max="{hi}" step="{step}" value="{v}" '
                    f'oninput="this.nextElementSibling.value=this.value">'
                    f'<input type="number" name="{k}" min="{lo}" max="{hi}" step="{step}" value="{v}" '
                    f'oninput="this.previousElementSibling.value=this.value"></div>')

        groups = "".join(
            f"<div class='group'><h3>{gname}</h3>" + "".join(row(k) for k in keys) + "</div>"
            for gname, keys in _PARAM_GROUPS
        )
        presets = "".join(
            f'<li><span>{n}</span>'
            f'<form method="POST" action="/load"><input type="hidden" name="name" value="{n}">'
            f'<button class="mini">Load</button></form></li>'
            for n in list_presets()
        ) or '<li class="empty">No presets saved yet</li>'
        return (
            '<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>WLED audio sync</title><style>{_PAGE_CSS}</style></head><body><div class='wrap'>"
            "<h1>WLED audio sync</h1>"
            "<p class='sub'>Live monitor + tuning. Parameter changes apply instantly in memory; "
            "save a preset to keep them.</p>"
            # ---- Live monitor ----
            "<div class='card'><h2>Live monitor</h2>"
            "<canvas id='spec' width='480' height='150'></canvas>"
            "<div class='mrow'><span class='ml'>Raw vol</span><div class='track'><i id='mraw'></i></div>"
            "<span id='rawv' class='mv'>0</span></div>"
            "<div class='mrow'><span class='ml'>Smooth vol</span><div class='track'><i id='msmth'></i></div>"
            "<span id='smthv' class='mv'>0</span></div>"
            "<div class='mrow'><span class='ml'>samplePeak</span><span id='beatdot' class='dot'></span>"
            "<span class='ml' style='margin-left:auto;min-width:0'>Major peak</span>"
            "<span id='peakhz' class='mv'>-- Hz</span></div></div>"
            # ---- Sonic Boom preview ----
            "<div class='card'><h2>PS Sonic Boom preview</h2>"
            "<div class='sbctl'>"
            "<label>Bin <input type='range' id='sbbin' min='0' max='31' step='1' value='0'><b id='sbbinv'>0</b></label>"
            "<label>Intensity <input type='range' id='sbint' min='0' max='255' step='1' value='128'><b id='sbintv'>128</b></label>"
            "<label>Strip length <input type='number' id='sblen' min='1' max='4000' value='60' style='width:80px'> px</label>"
            "<label><input type='checkbox' id='sbfilter' checked> Filter (dynamic threshold)</label>"
            "<div class='bar-actions'><button type='button' id='wsync' class='mini'>Sync from WLED</button>"
            "<span class='dot' id='boomdot'></span><span class='ml' style='min-width:0'>boom</span></div></div>"
            "<div class='sbtrack'><i id='sbloud'></i><span id='sbthr' class='thr'></span></div>"
            "<div class='sbnums'>loudness <b id='sbloudv'>0</b> &middot; threshold <b id='sbthrv'>0</b></div>"
            "<div class='sbstat'><span class='bpm' id='bpm'>-- BPM</span>"
            "<span>booms <b id='sbbooms'>0</b></span><span>on strip <b id='sbvis'>0</b></span>"
            "<span>last particles <b id='sbpart'>0</b></span></div>"
            "<p class='note'>Green boom = particles hit the strip; red = fired but emitted 0 particles "
            "(too quiet, so nothing shows). The gap between <b>booms</b> and <b>on strip</b> is why some "
            "beats don't light up. Runs on the real bins at WLED's frame rate.</p></div>"
            # ---- WLED status ----
            "<div class='card'><h2>WLED</h2><div id='wledbox' class='wled'>"
            "<div class='kv'><div class='k'>WLED</div><div class='v'>connecting...</div></div></div>"
            f"<p class='note'>Sending <b id='txpps'>0</b> pkt/s &middot; <b id='txkb'>0</b> KB/s "
            f"&rarr; {WLED_IP}:{WLED_PORT}</p></div>"
            # ---- Parameters (grouped) ----
            "<form method='POST' action='/update'><div class='card'><h2>Parameters</h2>"
            f"<div class='groups'>{groups}</div>"
            "<div class='actions'><button type='submit'>Apply</button></div>"
            f"<p class='note'>Startup only (edit conf.txt + restart): sample rate {SAMPLE_RATE} Hz &middot; "
            f"chunk {CHUNK_BYTES} B (~{CHUNK_MSEC:.1f} ms) &middot; FFT window {FFT_WINDOW_SAMPLES} samples &middot; "
            f"send {SEND_HZ:g}/s &middot; silence stop {SILENCE_HOLD_SEC:g} s</p>"
            "</div></form>"
            # ---- Presets ----
            "<div class='card'><h2>Presets</h2>"
            f"<ul class='presets'>{presets}</ul>"
            "<form method='POST' action='/save' class='save'>"
            "<input type='text' name='name' placeholder='preset name' pattern='[A-Za-z0-9_-]+' required>"
            "<button type='submit'>Save current</button></form>"
            "<p class='note'>Load applies a preset live. To set the boot default, run "
            "<code>utils/preset.sh use NAME</code> then restart the service.</p></div>"
            # ---- Service ----
            "<div class='card'><h2>Service <span class='pill' id='svcpill'>?</span></h2>"
            "<div class='bar-actions'><button type='button' id='svcbtn' class='mini'>Get status</button>"
            "<button type='button' id='svcrestart' class='mini'>Restart</button></div>"
            "<pre class='svc' id='svcout'>Click &ldquo;Get status&rdquo;.</pre></div>"
            f"<script>const BAND_HZ={_BAND_CENTERS_HZ};</script><script>{_VIZ_JS}</script>"
            "</div></body></html>"
        )

    def _read_form(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        return urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))

    def _redirect_home(self):
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def _send(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = urllib.parse.urlparse(self.path)
        if parts.path == "/":
            self._send(self._render_page().encode("utf-8"), "text/html; charset=utf-8")
        elif parts.path == "/data":
            tx_pps, tx_kbps = tx.current()
            payload = {**viz.data(), "sb": sonic.state(), "wled": wled.summary(), "tempo": tempo.bpm,
                       "tx": {"pps": tx_pps, "kbps": tx_kbps}}
            self._send(json.dumps(payload).encode("utf-8"), "application/json")
        elif parts.path == "/sbconfig":
            f = urllib.parse.parse_qs(parts.query)
            gi = lambda k: int(float(f[k][0])) if k in f else None
            sonic.set_params(custom3=gi("c3"), intensity=gi("intensity"),
                             filter=(f["filter"][0] == "1") if "filter" in f else None,
                             strip_len=gi("len"))
            self._send(b'{"ok":1}', "application/json")
        elif parts.path == "/service":
            self._send(json.dumps(service_status()).encode("utf-8"), "application/json")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/update":
            form = self._read_form()
            for key, _label, _step, lo, hi in _PARAM_SPEC:
                if key in form:
                    try:
                        live.set(key, min(max(float(form[key][0]), lo), hi))
                    except ValueError:
                        pass
            return self._redirect_home()

        if self.path in ("/save", "/load"):
            name = safe_preset_name(self._read_form().get("name", [""])[0])
            if name:
                try:
                    (save_preset if self.path == "/save" else load_preset)(name)
                except Exception as e:
                    print(f"Preset {self.path} failed: {e}")
            return self._redirect_home()

        if self.path == "/service/restart":
            threading.Timer(0.4, service_restart).start()   # respond first, then restart
            return self._send(b'{"ok":1}', "application/json")

        if self.path == "/wled/sync":
            try:
                wled.sync_sonic()
            except Exception as e:
                print(f"WLED sync failed: {e}")
            return self._send(b'{"ok":1}', "application/json")

        self.send_response(404)
        self.end_headers()


def _wled_poll_loop():
    last_leds = None
    synced = False
    while True:
        wled.poll()
        if wled.online and not synced:      # adopt the strip's live effect settings on first contact
            try:
                wled.sync_sonic()
                synced = True
            except Exception as e:
                print(f"Startup WLED sync failed: {e}", flush=True)
        leds = wled.summary().get("leds") if wled.online else None
        if leds and leds != last_leds:      # strip length is physical - keep the sim honest
            sonic.set_params(strip_len=leds)
            last_leds = leds
        time.sleep(2.0)


class _ReuseTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True   # rebind during the old socket's TIME_WAIT (systemd fast restarts)
    daemon_threads = True


def start_web_panel():
    try:
        httpd = _ReuseTCPServer((WEB_HOST, WEB_PORT), _ParamPanelHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        print(f"Live tuning panel: http://{WEB_HOST}:{WEB_PORT}/", flush=True)
    except Exception as e:
        print(f"Could not start web panel (continuing without it): {e}", flush=True)


def run_loopback():
    cmd = [
        "parec", "-r",
        "--device=@DEFAULT_MONITOR@",
        f"--rate={SAMPLE_RATE}",
        "--channels=1",
        "--format=s16le",
        f"--latency-msec={max(1, int(CHUNK_MSEC))}",
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=CHUNK_BYTES)
        print(f"Started parec @DEFAULT_MONITOR@ (chunk={CHUNK_BYTES}B ~ {CHUNK_MSEC:.1f}ms latency, "
              f"FFT window={FFT_WINDOW_SAMPLES} samples).")
    except FileNotFoundError:
        sys.exit("parec not found. Install pulseaudio-utils or libpulse (Arch: pacman -S libpulse).")
    except Exception as e:
        sys.exit(f"Error starting parec: {e}")

    frame = 0
    sends = 0
    last_active = time.monotonic()
    acc_bins = np.zeros(NUM_BANDS, dtype=np.uint8)   # coalesce skipped frames so throttling never
    acc_raw, acc_beat = 0.0, 0                        # drops an onset (peak-hold bins, OR the beat)
    while True:
        raw = proc.stdout.read(CHUNK_BYTES)
        if len(raw) < CHUNK_BYTES:
            print(f"Parec exited early. Stderr: {proc.stderr.read().decode()}", flush=True)
            break
        result = analyse(raw)
        if result[0] is None:
            continue
        bars, raw_255, smth_255, beat, mag, freq = result
        viz.update(result)
        frame += 1

        np.maximum(acc_bins, bars, out=acc_bins)
        acc_raw = max(acc_raw, raw_255)
        acc_beat |= int(beat)
        now = time.monotonic()
        if bars.any() or raw_255 > 0:
            last_active = now

        if frame % SEND_DECIMATE == 0:
            live_audio = now - last_active < SILENCE_HOLD_SEC   # else: stay silent, WLED holds last (off)
            # Presence gate is a cheap flag read (HTTP polling is off-thread) that assumes the strip
            # is there until proven otherwise, so this never adds startup latency to the send path.
            strip_ok = CONTINUOUS_SEND or wled.reachable(PRESENCE_GRACE_SEC)
            if live_audio and strip_ok:
                packet = create_udp_packet(acc_bins, acc_raw, smth_255, acc_beat, mag, freq)
                udp_socket.sendto(packet, (WLED_IP, WLED_PORT))
                tx.add(len(packet))
            # Mirror Sonic Boom + tempo on the exact bins WLED receives (peak-held acc_bins),
            # decimated to its render cadence - so 'booms'/'on strip' match the real strip.
            sends += 1
            if sends % SB_SEND_DECIMATE == 0:
                sonic.tick(acc_bins)
                tempo.push(int(acc_bins[0]))
            acc_bins[:] = 0
            acc_raw, acc_beat = 0.0, 0
    proc.wait()


def main():
    try:                                   # so prints reach the journal (systemd pipes block-buffer)
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    if GAIN != 1.0:
        print(f"Gain: {GAIN}x")
    if WEB_ENABLED:
        start_web_panel()
    else:
        print("Live tuning panel disabled (set [Web] enabled = true in conf.txt to turn it on).")
    # Poll WLED off-thread when the panel needs it, or when presence-gating drives the send loop.
    if WEB_ENABLED or not CONTINUOUS_SEND:
        threading.Thread(target=_wled_poll_loop, daemon=True).start()
    if CONTINUOUS_SEND:
        print("Send mode: continuous (streaming whenever audio plays; strip presence ignored).")
    else:
        print(f"Send mode: presence-gated (assume strip present at startup, pause after "
              f"{PRESENCE_GRACE_SEC:g}s unreachable; HTTP timeout {HTTP_TIMEOUT:g}s).")
    print("Starting capture -> WLED ...")
    run_loopback()


if __name__ == "__main__":
    main()
