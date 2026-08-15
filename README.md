# 🐧 Glow-Bird-Protocol

**Glow-Bird-Protocol** is a low-latency bridge between Linux system audio and a
WLED-enabled microcontroller. It captures your desktop audio via `parec`,
analyses it with an FFT, and streams the result over UDP using WLED's
**audioSync v2** protocol — so WLED's audio-reactive effects react to whatever
is actually playing on your machine, no microphone or WLED sound-daughterboard
required.

## ✨ What it does

* **Captures desktop audio** straight from the PipeWire/PulseAudio monitor
  (`@DEFAULT_MONITOR@`) — works out of the box on modern Linux audio stacks.
* **Feeds both WLED audio paths** from a single analysis pass:
  * `sampleRaw` / `sampleSmth` — a 0–255 loudness value that drives the
    **volume** effects, with WLED-style automatic gain control (AGC).
  * `fftResult[16]` — 16 GEQ bands that drive the **frequency** effects, using
    the same channel split and pink-noise curve as WLED's own audioreactive
    usermod, so the spectrum stays balanced instead of drowning in bass.
  * `samplePeak` — a beat/onset flag from a median-based onset detector.
* **Low latency:** small `parec` fragments (default ~5 ms) with a longer sliding
  FFT window, so latency stays low without sacrificing frequency resolution.
* **Silence-aware:** stops sending after a few seconds of silence (WLED holds
  the last, zeroed frame = off) and resumes instantly when audio returns.
* **Live web tuning panel** (optional): a spectrum monitor, every parameter as a
  live slider, presets, a WLED status readout, and a server-side **PS Sonic
  Boom** beat-detection preview — all applied in memory without a restart.

## 🚀 Quick start

### 1. Requirements

You need Python 3, `numpy`, and `parec` (from `pulseaudio-utils` / `libpulse`).

```bash
# Arch
sudo pacman -S libpulse
# Debian/Ubuntu
sudo apt install pulseaudio-utils

pip install -r requirements.txt
```

### 2. Configure

Edit `conf.txt` — at minimum, point it at your device:

```ini
[WLED]
WLED_IP = 10.16.111.2
WLED_PORT = 11988      ; WLED's audioSync UDP port (Sync settings → Audio Sync)

[Audio]
SAMPLE_RATE = 48000
CHUNK_BYTES = 512      ; 512 ≈ 5.3 ms latency; smaller = snappier
GAIN = 1.0

[Web]
enabled = true
host = 127.0.0.1
port = 8080
```

On the WLED side, enable **Sync Interfaces → Audio Sync → Receive** and set the
same UDP port. Every other key in `conf.txt` is documented inline and controls
the band balance, AGC, beat detection, and send rate.

### 3. Run

```bash
python main.py
```

With `[Web] enabled = true`, open <http://127.0.0.1:8080/> for the live monitor
and tuning panel.

## 🎛 Presets

Tune parameters live in the web panel, then save them. Presets are `conf.txt`
snapshots under `presets/`.

```bash
./preset.sh list          # list saved presets
./preset.sh save <name>   # snapshot the current conf.txt
./preset.sh use <name>    # make it the boot default and restart the service
```

The web panel can also **Save** and **Load** presets — loading applies them
instantly; `preset.sh use` sets the one that survives a restart.

## 🔧 Run as a service

`glowbird-protocol.service` is a user systemd unit. Edit `WorkingDirectory` and
`ExecStart` to your checkout path, then:

```bash
cp glowbird-protocol.service ~/.config/systemd/user/
systemctl --user enable --now glowbird-protocol.service
```

The web panel can show the service status and restart it for you.

## 🛠 How it works

1. **Capture** — spawns `parec` on `@DEFAULT_MONITOR@` and reads raw
   mono `s16le` PCM in small chunks.
2. **Analyse** — slides each chunk into a Hann-windowed FFT buffer and derives
   the 16 GEQ bands (volume path + frequency path auto-gained separately), plus
   a beat flag.
3. **Pack** — serialises everything into WLED's 44-byte audioSync v2 packet
   (`00002` header: `sampleRaw`, `sampleSmth`, `samplePeak`, `fftResult[16]`,
   `FFT_Magnitude`, `FFT_MajorPeak`).
4. **Stream** — sends over UDP to your WLED device at ~90 packets/s (throttled
   from the higher analysis rate; skipped frames are coalesced so an onset is
   never dropped).

## 📄 License

MIT — feel free to tinker, break, and despair.
