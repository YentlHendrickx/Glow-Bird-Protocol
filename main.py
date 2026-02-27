import sys
import subprocess
import configparser
import socket
import struct
import numpy as np

# Low latency audio capture from PulseAudio/PipeWire monitor source using parec, FFT analysis, and UDP packet sending to WLED.
# Configuration is read from conf.txt. Adjust CHUNK_BYTES for lower latency (smaller) or better frequency resolution (larger).

config = configparser.ConfigParser()
config.read("conf.txt")

WLED_IP = config.get("WLED", "WLED_IP")
WLED_PORT = config.getint("WLED", "WLED_PORT")
SAMPLE_RATE = config.getint("Audio", "SAMPLE_RATE")

# CHUNK_BYTES: bytes read per frame (s16le = 2 bytes/sample). Smaller = lower latency, less frequency resolution.
# 512 bytes = 256 samples ≈ 5.3 ms @ 48 kHz; 256 bytes = 128 samples ≈ 2.7 ms.
CHUNK_BYTES = config.getint("Audio", "CHUNK_BYTES")

try:
    GAIN = config.getfloat("Audio", "gain")
except (configparser.NoOptionError, configparser.NoSectionError):
    GAIN = 1.0

# Latency in ms for parec (request small fragments from PipeWire/Pulse)
CHUNK_MSEC = (CHUNK_BYTES // 2) / SAMPLE_RATE * 1000

udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

def calculate_fft(audio_chunk):
    try:
        audio_data = np.frombuffer(audio_chunk, dtype=np.int16)

        raw_level = np.mean(np.abs(audio_data))
        peak_level = int((np.max(np.abs(audio_data)) / 32767) * 255)
        smoothed_level = int((raw_level / 32767) * 255) # based on mean

        fft_result = np.abs(np.fft.rfft(audio_data))
        fft_normalized = np.interp(fft_result, (0, np.max(fft_result)), (0, 255))
        fft_values = fft_normalized[:16].astype(np.uint8)
        freq_index = np.argmax(fft_result)
        fft_peak_frequency = freq_index * (SAMPLE_RATE / len(audio_data))
        fft_magnitude_sum = np.sum(fft_result)

        if GAIN != 1.0:
            fft_values = np.clip(fft_values * GAIN, 0, 255).astype(np.uint8)

        return fft_values, raw_level, smoothed_level, peak_level, fft_magnitude_sum, fft_peak_frequency

    except Exception as e:
        print(f"Error at calcing FFT: {e}")
        return None, 0, 0, 0, 0, 0

def create_udp_packet(fft_values, raw_level, smoothed_level, peak_level, fft_magnitude_sum, fft_peak_frequency):
    peak_level = int(peak_level)
    fft_values = [int(v) for v in fft_values]

    udp_packet = struct.pack('<6s2B2fBB16B2B2f',
        b'00002',                   # Header (6 Bytes)
        0,0,                        # Gap (2 Bytes)
        float(raw_level),           # Raw Level (4 Bytes Float)
        float(smoothed_level),      # Smoothed Level (4 Bytes Float)
        peak_level,                 # Peak Level (1 Byte)
        0,                          # static 0 (1 Byte)
        *fft_values,                # FFT Result (16 Bytes)
        0,0,                        # Gap (2 Bytes)
        float(fft_magnitude_sum),   # FFT Magnitude (4 Bytes Float)
        float(fft_peak_frequency))  # FFT Major Peak (4 Bytes Float)
    return udp_packet

def run_loopback():
    # Request low-latency fragments from Pulse/PipeWire (same size as our read chunk)
    cmd = [
        "parec",
        "-r",
        "--device=@DEFAULT_MONITOR@",
        f"--rate={SAMPLE_RATE}",
        "--channels=1",
        "--format=s16le",
        f"--latency-msec={max(1, int(CHUNK_MSEC))}",
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=CHUNK_BYTES,
        )
        print(f"Started parec @DEFAULT_MONITOR@ (chunk={CHUNK_BYTES} bytes ≈ {CHUNK_MSEC:.1f} ms latency).")
    except FileNotFoundError:
        sys.exit("parec not found. Install pulseaudio-utils or libpulse (Arch: pacman -S libpulse).")
    while True:
        raw = proc.stdout.read(CHUNK_BYTES)
        if len(raw) < CHUNK_BYTES:
            break
        fft_result = calculate_fft(raw)
        if fft_result[0] is None:
            continue
        fft_data, raw_level, smoothed_level, peak_level, fft_magnitude_sum, fft_peak_frequency = fft_result
        udp_packet = create_udp_packet(fft_data, raw_level, smoothed_level, peak_level, fft_magnitude_sum, fft_peak_frequency)
        udp_socket.sendto(udp_packet, (WLED_IP, WLED_PORT))
    proc.wait()


def main():
    if GAIN != 1.0:
        print(f"Gain: {GAIN}x")

    print("Starting capture -> WLED ...")
    run_loopback()

if __name__ == "__main__":
    main()
