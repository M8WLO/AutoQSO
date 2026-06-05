"""
Text-to-speech engine.
Primary: pyttsx3 (Windows SAPI, fully offline).
Optional: edge-tts (Microsoft neural voices, requires internet).
Both return WAV/audio bytes or write to a temp file for playback.
"""
import io
import os
import tempfile
import logging
import asyncio
import threading

log = logging.getLogger(__name__)


class TTSEngine:
    def __init__(self, engine: str = "pyttsx3", voice_index: int = 0,
                 edge_voice: str = "en-GB-RyanNeural",
                 rate_wpm: int = 160, volume: float = 0.95):
        self._engine_name = engine
        self._voice_index = voice_index
        self._edge_voice = edge_voice
        self._rate = rate_wpm
        self._volume = volume
        self._pyttsx_engine = None
        self._lock = threading.Lock()

        if engine == "pyttsx3":
            self._init_pyttsx3()

    def _init_pyttsx3(self):
        try:
            import pyttsx3
            self._pyttsx_engine = pyttsx3.init()
            voices = self._pyttsx_engine.getProperty("voices")
            if voices and self._voice_index < len(voices):
                self._pyttsx_engine.setProperty("voice", voices[self._voice_index].id)
            self._pyttsx_engine.setProperty("rate", self._rate)
            self._pyttsx_engine.setProperty("volume", self._volume)
            log.info("pyttsx3 TTS ready, voice index %d", self._voice_index)
        except Exception as e:
            log.error("pyttsx3 init failed: %s", e)
            self._pyttsx_engine = None

    def get_voices(self) -> list[str]:
        """Return list of available voice names (pyttsx3 only)."""
        if self._pyttsx_engine:
            return [v.name for v in self._pyttsx_engine.getProperty("voices")]
        return []

    def speak_to_file(self, text: str) -> str | None:
        """
        Synthesise text and save to a temp WAV file.
        Returns the file path or None on failure.
        """
        if self._engine_name == "pyttsx3":
            return self._pyttsx_to_file(text)
        elif self._engine_name == "edge":
            return self._edge_to_file(text)
        return None

    def speak_blocking(self, text: str):
        """Synthesise and play immediately (blocking)."""
        if self._engine_name == "pyttsx3" and self._pyttsx_engine:
            with self._lock:
                try:
                    self._pyttsx_engine.say(text)
                    self._pyttsx_engine.runAndWait()
                except Exception as e:
                    log.error("pyttsx3 speak error: %s", e)
        else:
            path = self.speak_to_file(text)
            if path:
                from audio_engine import play_audio_file
                play_audio_file(path)
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def _pyttsx_to_file(self, text: str) -> str | None:
        if not self._pyttsx_engine:
            return None
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        with self._lock:
            try:
                self._pyttsx_engine.save_to_file(text, path)
                self._pyttsx_engine.runAndWait()
                return path
            except Exception as e:
                log.error("pyttsx3 save_to_file error: %s", e)
                return None

    def _edge_to_file(self, text: str) -> str | None:
        try:
            import edge_tts
            fd, path = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)

            async def _run():
                comm = edge_tts.Communicate(text, voice=self._edge_voice)
                await comm.save(path)

            asyncio.run(_run())
            return path
        except Exception as e:
            log.error("edge-tts error: %s", e)
            return None
