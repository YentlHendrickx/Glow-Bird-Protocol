import sys
import math
import subprocess
import configparser
import socket
import struct
import threading
import http.server
import socketserver
import urllib.parse
from collections import deque

import numpy as np

# Capture desktop audio from the PulseAudio/PipeWire monitor via parec, analyse it, and stream
# it to WLED using the audioSync v2 protocol ("00002" header, 44-byte packet). WLED's receiver
# reads two independent things:
#   - sampleRaw / sampleSmth : a 0-255 loudness value that drives the "volume" effects.
#   - fftResult[16]          : 16 GEQ bands that drive the "frequency" effects.
# So both paths are auto-gained separately below. Optional live-tuning web panel via [Web].

config = configparser.ConfigParser()
config.read("conf.txt")


def audio(key, fallback):
    """Read an [Audio] key with a default, matching the type of the fallback."""
    if isinstance(fallback, bool):
        return config.getboolean("Audio", key, fallback=fallback)
    if isinstance(fallback, int):
        return config.getint("Audio", key, fallback=fallback)
    return config.getfloat("Audio", key, fallback=fallback)


WLED_IP = config.get("WLED", "WLED_IP")
WLED_PORT = config.getint("WLED", "WLED_PORT")

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
# Fold the lowest N bands into band 0 so effects that read a single bin (e.g. PS Sonic Boom on
# bin 0) catch bass reliably, wherever a track's low fundamental sits. 0 = off. ~6 covers 43-430Hz.
BASS_FOLD = audio("BASS_FOLD", 0)

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

# Volume path AGC: nudge a gain multiplier so the *average* loudness tracks AGC_TARGET.
# Beats poke above the average, quiet passages stay dim - this is what real WLED AGC does.
AGC_ENABLED = audio("AGC_ENABLED", True)
AGC_TARGET = audio("AGC_TARGET", 90.0)    # average output level (0-255) the AGC aims for
AGC_SPEED = audio("AGC_SPEED", 0.02)      # per-frame fraction the gain moves toward its target
AGC_MAX_GAIN = audio("AGC_MAX_GAIN", 2000.0)

SQUELCH = audio("SQUELCH", 40.0)          # raw mean-abs below this = silence/noise, output 0
SMTH_ALPHA = audio("SMTH_ALPHA", 0.35)    # EMA factor for sampleSmth

# Beat/onset detection -> samplePeak (a 0/1 flag to WLED, not a loudness value)
BEAT_THRESHOLD_MULT = audio("BEAT_THRESHOLD_MULT", 1.4)
BEAT_REFRACTORY_MS = audio("BEAT_REFRACTORY_MS", 120.0)

WEB_ENABLED = config.getboolean("Web", "enabled", fallback=False)
WEB_HOST = config.get("Web", "host", fallback="127.0.0.1")
WEB_PORT = config.getint("Web", "port", fallback=8080)

if FFT_WINDOW_SAMPLES < CHUNK_SAMPLES:
    sys.exit("FFT_WINDOW_SAMPLES must be >= CHUNK_BYTES/2 (one read chunk).")

CHUNK_MSEC = CHUNK_SAMPLES / SAMPLE_RATE * 1000
FRAMES_PER_SEC = 1000.0 / CHUNK_MSEC
BEAT_HISTORY_FRAMES = max(4, int(FRAMES_PER_SEC))          # ~1s of energy history for the beat threshold
VOL_AVG_ALPHA = 1 - math.exp(-CHUNK_MSEC / 400.0)          # ~400ms time constant for the AGC control average


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
    AGC_ENABLED=AGC_ENABLED,
    AGC_TARGET=AGC_TARGET,
    AGC_SPEED=AGC_SPEED,
    BAND_AGC_DECAY=BAND_AGC_DECAY,
    BAND_GAMMA=BAND_GAMMA,
    BASS_FOLD=BASS_FOLD,
    SMTH_ALPHA=SMTH_ALPHA,
    BEAT_THRESHOLD_MULT=BEAT_THRESHOLD_MULT,
    BEAT_REFRACTORY_MS=BEAT_REFRACTORY_MS,
)

# (key, label, step, min, max) - drives both the HTML form and clamping of submitted values.
# FFT window / band edges aren't here: changing them resizes precomputed arrays, which needs a restart.
_PARAM_SPEC = [
    ("GAIN", "Gain", 0.1, 0.1, 10.0),
    ("SQUELCH", "Squelch (raw noise floor)", 1, 0, 2000),
    ("AGC_TARGET", "AGC target level (0-255)", 5, 10, 240),
    ("AGC_SPEED", "AGC speed", 0.005, 0.001, 0.5),
    ("BAND_AGC_DECAY", "Band AGC decay", 0.0005, 0.9, 0.999999),
    ("BAND_GAMMA", "Band gamma (<1 = fuller)", 0.05, 0.2, 1.5),
    ("BASS_FOLD", "Bass fold into bin 0 (0=off)", 1, 0, 16),
    ("SMTH_ALPHA", "Smoothing alpha", 0.01, 0.01, 1.0),
    ("BEAT_THRESHOLD_MULT", "Beat threshold mult", 0.05, 1.0, 5.0),
    ("BEAT_REFRACTORY_MS", "Beat refractory (ms)", 5, 0, 1000),
]

udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# Precomputed analysis state
_hann = np.hanning(FFT_WINDOW_SAMPLES)
_freqs = np.fft.rfftfreq(FFT_WINDOW_SAMPLES, d=1.0 / SAMPLE_RATE)
_band_bin_edges = np.clip(np.searchsorted(_freqs, _WLED_BAND_EDGES_HZ), 0, len(_freqs) - 1)

_rolling_buffer = np.zeros(FFT_WINDOW_SAMPLES, dtype=np.float64)
_band_agc_ref = 1.0
_agc_gain = 1.0        # volume-path AGC multiplier
_vol_avg = 1.0         # slow average of raw_level, the AGC control signal
_smth_level = 0.0
_energy_hist = deque(maxlen=BEAT_HISTORY_FRAMES)
_beat_refractory = 0


def compute_bands(p, gated):
    """16 GEQ bands (0-254) using WLED's own channel split + pink-noise curve, then a global
    linear auto-scale so the bars read like a spectrum analyser instead of a wall of bass."""
    global _band_agc_ref

    windowed = _rolling_buffer * _hann          # Hann window cuts spectral leakage between bins
    spectrum = np.abs(np.fft.rfft(windowed))
    n_bins = len(spectrum)

    band_energy = np.empty(NUM_BANDS)
    for i in range(NUM_BANDS):
        lo = int(_band_bin_edges[i])
        hi = int(min(max(lo + 1, _band_bin_edges[i + 1]), n_bins))
        band_energy[i] = np.mean(spectrum[lo:hi])
    band_energy *= _BAND_WEIGHT                  # pink-noise compensation (lift highs, damp top two)

    if not gated:
        _band_agc_ref = max(band_energy.max(), _band_agc_ref * p["BAND_AGC_DECAY"], 1e-6)

    norm = np.clip(band_energy / _band_agc_ref, 0, 1)
    bars = (norm ** p["BAND_GAMMA"]) * 255 * p["GAIN"]   # gamma<1 fills the bars out
    bars = np.where(gated, 0, np.clip(bars, 0, 254)).astype(np.uint8)

    fold = int(p["BASS_FOLD"])
    if fold > 1:
        bars[0] = bars[:fold].max()     # bin 0 catches bass wherever the fundamental sits

    peak_bin = int(np.argmax(spectrum))
    return bars, float(_freqs[peak_bin]), float(spectrum[peak_bin])


def compute_volume(raw_level, p, gated):
    """Volume loudness (0-255) for sampleRaw, plus its smoothed EMA for sampleSmth."""
    global _agc_gain, _vol_avg, _smth_level

    if p["AGC_ENABLED"]:
        if not gated:
            _vol_avg += (raw_level - _vol_avg) * VOL_AVG_ALPHA
            target_gain = (p["AGC_TARGET"] * p["GAIN"]) / max(_vol_avg, 1e-3)
            _agc_gain += (target_gain - _agc_gain) * p["AGC_SPEED"]
            _agc_gain = min(max(_agc_gain, 0.0), AGC_MAX_GAIN)
        level = raw_level * _agc_gain
    else:
        level = raw_level * p["GAIN"]

    raw_255 = 0.0 if gated else float(np.clip(level, 0, 255))
    _smth_level = p["SMTH_ALPHA"] * raw_255 + (1 - p["SMTH_ALPHA"]) * _smth_level
    return raw_255, _smth_level


def detect_beat(raw_level, p, gated):
    """Onset detector -> samplePeak flag: instant energy vs. its recent average, with a refractory gap."""
    global _beat_refractory

    _energy_hist.append(raw_level)
    local_avg = np.mean(_energy_hist) if _energy_hist else raw_level
    if (not gated and _beat_refractory == 0
            and raw_level > local_avg * p["BEAT_THRESHOLD_MULT"] and raw_level > p["SQUELCH"]):
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


class _ParamPanelHandler(http.server.BaseHTTPRequestHandler):
    """Bare HTML form for the live-tunable knobs. No auth - localhost or trusted LAN only."""

    def log_message(self, fmt, *args):
        pass

    def _render_page(self):
        values = live.snapshot()
        rows = "".join(
            f"<tr><td>{label}</td><td><input type='number' name='{key}' "
            f"value='{values[key]}' step='{step}' min='{lo}' max='{hi}'></td></tr>"
            for key, label, step, lo, hi in _PARAM_SPEC
        )
        return (
            "<html><head><title>WLED audio sync</title></head><body>"
            "<h3>WLED audio sync - live parameters</h3>"
            "<form method='POST' action='/update'>"
            f"<table>{rows}</table><br><button type='submit'>Apply</button></form>"
            "<p>FFT window and band edges need a restart (conf.txt) - they resize internal buffers.</p>"
            "</body></html>"
        )

    def do_GET(self):
        if self.path != "/":
            self.send_response(404)
            self.end_headers()
            return
        body = self._render_page().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/update":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        for key, _label, _step, lo, hi in _PARAM_SPEC:
            if key in form:
                try:
                    live.set(key, min(max(float(form[key][0]), lo), hi))
                except ValueError:
                    pass
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()


def start_web_panel():
    try:
        httpd = socketserver.ThreadingTCPServer((WEB_HOST, WEB_PORT), _ParamPanelHandler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        print(f"Live tuning panel: http://{WEB_HOST}:{WEB_PORT}/")
    except Exception as e:
        print(f"Could not start web panel (continuing without it): {e}")


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

    while True:
        raw = proc.stdout.read(CHUNK_BYTES)
        if len(raw) < CHUNK_BYTES:
            print(f"Parec exited early. Stderr: {proc.stderr.read().decode()}", flush=True)
            break
        result = analyse(raw)
        if result[0] is None:
            continue
        udp_socket.sendto(create_udp_packet(*result), (WLED_IP, WLED_PORT))
    proc.wait()


def main():
    if GAIN != 1.0:
        print(f"Gain: {GAIN}x")
    if WEB_ENABLED:
        start_web_panel()
    else:
        print("Live tuning panel disabled (set [Web] enabled = true in conf.txt to turn it on).")
    print("Starting capture -> WLED ...")
    run_loopback()


if __name__ == "__main__":
    main()
