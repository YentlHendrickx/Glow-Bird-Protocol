# 🐧 Glow-Bird-Protocol

**Glow-Bird-Protocol** is a low-latency bridge between Linux system audio and WLED-enabled devices. It sniffs audio output via `parec`, chops it into frequency bands using Fast Fourier Transform (FFT), and prepares/sends UDP packets compatible with WLED. 
## ✨ Features
* **Near-Zero Latency:** Uses `parec` fragment requests to keep audio-to-light delay minimal. (network latency can be a factor)
* **PipeWire/PulseAudio Native:** Works out of the box with modern Linux audio stacks.
* **FFT Analysis:** Extracts 16 frequency bands, peak frequencies, and smoothed volume levels.
* **Configurable:** Adjust gain, sample rates, and chunk sizes via a simple `conf.txt`.

## 🚀 Quick Start

### 1. Requirements
Ensure you have `pulseaudio-utils` (for `parec`) and Python 3 installed.

```bash
pip install -r requirements.txt

```

### 2. Configuration

Create (or modify) `conf.txt` in the root directory:

```ini
[WLED]
WLED_IP = 192.168.1.50
WLED_PORT = 4048

[Audio]
SAMPLE_RATE = 48000
CHUNK_BYTES = 512
gain = 1.5

```

### 3. Run the Protocol

```bash
python main.py

```

## 🛠 How it Works

1. **Capture:** The script spawns a `parec` subprocess to record the `@DEFAULT_MONITOR@` source.
2. **Analyze:** It uses `numpy` to perform an `rfft` on the raw PCM data.
3. **Pack:** Data is serialized into a specific 30-byte UDP packet format compatible with WLED's realtime DDP/DRGB protocols.
4. **Broadcast:** Packets are fired off to your WLED IP.

## 📄 License

This project is licensed under the **MIT License**. Feel free to tinker, break and despair.

