"""
Audio I/O: device enumeration, recording with VAD, and playback.
Uses pyaudio for capture and sounddevice/soundfile for playback.
"""
import io
import time
import logging
import threading
import numpy as np
import pyaudio
import sounddevice as sd
import soundfile as sf

log = logging.getLogger(__name__)

_CHUNK = 1024
_FORMAT = pyaudio.paInt16
_CHANNELS = 1


def list_devices() -> list[dict]:
    """Return list of audio device dicts with index, name, inputs, outputs."""
    pa = pyaudio.PyAudio()
    devices = []
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        devices.append({
            "index": i,
            "name": info["name"],
            "inputs": int(info["maxInputChannels"]),
            "outputs": int(info["maxOutputChannels"]),
        })
    pa.terminate()
    return devices


def find_device_index(name_hint: str, direction: str = "input") -> int | None:
    """Find device index by partial name match. direction = 'input' or 'output'."""
    if not name_hint:
        return None
    key = "inputs" if direction == "input" else "outputs"
    for d in list_devices():
        if name_hint.lower() in d["name"].lower() and d[key] > 0:
            return d["index"]
    return None


class AudioRecorder:
    """Records audio from an input device with basic energy-based VAD."""

    def __init__(self, device_index: int | None = None,
                 sample_rate: int = 16000,
                 vad_threshold: float = 0.015,
                 silence_end_sec: float = 1.8,
                 max_listen_sec: float = 25.0):
        self._device = device_index
        self._rate = sample_rate
        self._threshold = vad_threshold
        self._silence_end = silence_end_sec
        self._max_sec = max_listen_sec
        self._pa = pyaudio.PyAudio()

    def record_until_silence(self, status_callback=None) -> np.ndarray | None:
        """
        Record audio until silence is detected or max_listen_sec elapsed.
        Returns numpy float32 array at self._rate, or None if nothing heard.
        """
        stream = self._pa.open(
            format=_FORMAT,
            channels=_CHANNELS,
            rate=self._rate,
            input=True,
            input_device_index=self._device,
            frames_per_buffer=_CHUNK,
        )

        frames = []
        heard_voice = False
        silence_chunks = 0
        silence_chunks_needed = int(self._silence_end * self._rate / _CHUNK)
        max_chunks = int(self._max_sec * self._rate / _CHUNK)
        chunks_recorded = 0

        if status_callback:
            status_callback("Listening...")

        try:
            while chunks_recorded < max_chunks:
                data = stream.read(_CHUNK, exception_on_overflow=False)
                chunks_recorded += 1
                samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                rms = float(np.sqrt(np.mean(samples ** 2)))

                if rms > self._threshold:
                    heard_voice = True
                    silence_chunks = 0
                    frames.append(data)
                elif heard_voice:
                    silence_chunks += 1
                    frames.append(data)
                    if silence_chunks >= silence_chunks_needed:
                        break
        finally:
            stream.stop_stream()
            stream.close()

        if not heard_voice or not frames:
            return None

        raw = b"".join(frames)
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return audio

    def close(self):
        self._pa.terminate()


def play_audio_bytes(audio_bytes: bytes, device_name_hint: str = "", blocking: bool = True):
    """Play audio from bytes (WAV/MP3/etc.) via sounddevice."""
    buf = io.BytesIO(audio_bytes)
    data, samplerate = sf.read(buf, dtype="float32")
    device = None
    if device_name_hint:
        for d in sd.query_devices():
            if device_name_hint.lower() in d["name"].lower() and d["max_output_channels"] > 0:
                device = d["name"]
                break
    sd.play(data, samplerate=samplerate, device=device)
    if blocking:
        sd.wait()


def play_audio_file(path: str, device_name_hint: str = "", blocking: bool = True):
    data, samplerate = sf.read(path, dtype="float32")
    device = None
    if device_name_hint:
        for d in sd.query_devices():
            if device_name_hint.lower() in d["name"].lower() and d["max_output_channels"] > 0:
                device = d["name"]
                break
    sd.play(data, samplerate=samplerate, device=device)
    if blocking:
        sd.wait()
