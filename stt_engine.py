"""
Speech-to-text using faster-whisper (local, offline).
Falls back to openai-whisper if faster-whisper is not installed.
"""
import logging
import numpy as np

log = logging.getLogger(__name__)


class STTEngine:
    def __init__(self, model_size: str = "base", language: str = "en"):
        self._model_size = model_size
        self._language = language
        self._model = None
        self._backend = None
        self._load_model()

    def _load_model(self):
        # Try faster-whisper first
        try:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self._model_size, device="cpu",
                                       compute_type="int8")
            self._backend = "faster_whisper"
            log.info("faster-whisper loaded (model=%s)", self._model_size)
            return
        except ImportError:
            pass

        # Fall back to openai-whisper
        try:
            import whisper
            self._model = whisper.load_model(self._model_size)
            self._backend = "openai_whisper"
            log.info("openai-whisper loaded (model=%s)", self._model_size)
            return
        except ImportError:
            pass

        log.error("No Whisper backend found. Install faster-whisper or openai-whisper.")

    def transcribe(self, audio: np.ndarray) -> str:
        """
        Transcribe float32 numpy audio array (mono, 16 kHz).
        Returns the recognised text string.
        """
        if self._model is None or audio is None or len(audio) == 0:
            return ""

        try:
            if self._backend == "faster_whisper":
                segments, _ = self._model.transcribe(
                    audio,
                    language=self._language,
                    vad_filter=True,
                    beam_size=5,
                )
                return " ".join(s.text for s in segments).strip()

            elif self._backend == "openai_whisper":
                result = self._model.transcribe(audio, language=self._language)
                return result.get("text", "").strip()

        except Exception as e:
            log.error("Transcription error: %s", e)

        return ""

    @property
    def ready(self) -> bool:
        return self._model is not None
