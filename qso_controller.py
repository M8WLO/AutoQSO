"""
QSO state machine — correct ham radio procedure:
  CQ CQ CQ de <call> <call> <call> calling CQ and CQ DX, calling any station, over
  → listen 10s → extract their callsign → give RST → extract our RST →
  acknowledge → check for repeat request → 73 → loop CQ
"""
import os
import re
import time
import logging
import threading

from PyQt6.QtCore import QObject, pyqtSignal
from enum import Enum, auto

from config import Config
from radio_interface import RadioInterface
from audio_engine import AudioRecorder, find_device_index, play_audio_file
from stt_engine import STTEngine
from tts_engine import TTSEngine
from phonetics import (callsign_to_speech, speech_to_callsign,
                       speech_to_rst, smeter_to_rst)
from qso_logger import QSOLogger, QSORecord

log = logging.getLogger(__name__)

_REPEAT_PHRASES = re.compile(
    r'\b(again|repeat|say again|once more|qsb|not read|copy)\b',
    re.IGNORECASE
)
_CLOSE_PHRASES = re.compile(
    r'\b(73|seventy.?three|goodbye|bye|closing|end|thank you|thanks|cheerio)\b',
    re.IGNORECASE
)


class QSOState(Enum):
    IDLE          = auto()
    CALLING_CQ    = auto()
    LISTENING     = auto()
    SEND_REPORT   = auto()
    LISTEN_REPORT = auto()
    ACKNOWLEDGE   = auto()
    CLOSING       = auto()
    LOGGING       = auto()


class QSOController(QObject):
    state_changed   = pyqtSignal(str)
    status_message  = pyqtSignal(str)
    qso_started     = pyqtSignal(str)
    qso_logged      = pyqtSignal(str, str, str)
    log_line        = pyqtSignal(str)
    error_signal    = pyqtSignal(str)
    ptt_changed     = pyqtSignal(bool)   # True = PTT on (transmitting)

    def __init__(self, cfg: Config, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._state = QSOState.IDLE
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._their_call = ""
        self._rst_sent   = "59"
        self._rst_rcvd   = ""

        self._radio    = RadioInterface(cfg.rigctld_host, cfg.rigctld_port)
        self._recorder: AudioRecorder | None = None
        self._stt:      STTEngine     | None = None
        self._tts:      TTSEngine     | None = None
        self._logger:   QSOLogger     | None = None

    # ─────────────────────────────────────────────────────── init

    def initialise(self):
        self.status_message.emit("Loading Whisper model…")
        self._stt = STTEngine(self.cfg.whisper_model, self.cfg.whisper_language)
        if not self._stt.ready:
            self.error_signal.emit("Whisper model failed to load.")

        self.status_message.emit("Initialising TTS…")
        self._tts = TTSEngine(
            engine=self.cfg.tts_engine,
            voice_index=self.cfg.tts_voice_index,
            edge_voice=self.cfg.tts_edge_voice,
            rate_wpm=self.cfg.tts_rate_wpm,
            volume=self.cfg.tts_volume,
        )

        self.status_message.emit("Connecting to radio…")
        if self._radio.connect():
            self.status_message.emit("Radio connected via rigctld.")
        else:
            self.status_message.emit("rigctld not found — CAT/S-meter disabled.")

        rx_idx = find_device_index(self.cfg.rx_device_name, "input")
        self._recorder = AudioRecorder(
            device_index=rx_idx,
            sample_rate=self.cfg.sample_rate,
            vad_threshold=self.cfg.vad_threshold,
            silence_end_sec=self.cfg.silence_end_sec,
            max_listen_sec=self.cfg.max_listen_sec,
        )

        self._logger = QSOLogger(
            adif_path=self.cfg.adif_log_path,
            qrz_api_key=self.cfg.qrz_api_key,
            enable_qrz=self.cfg.enable_qrz_log,
            n1mm_host=self.cfg.n1mm_host,
            n1mm_port=self.cfg.n1mm_port,
            enable_n1mm=self.cfg.enable_n1mm_udp,
        )
        self.status_message.emit("Ready.")

    # ─────────────────────────────────────────────────────── control

    def start_cq(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._cq_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self.status_message.emit("Stopping after current operation…")

    def manual_log(self, callsign: str, rst_sent: str, rst_rcvd: str):
        self._do_log(callsign, rst_sent, rst_rcvd)

    # ─────────────────────────────────────────────────────── TX helper

    def _say(self, text: str):
        """Speak text over PTT. Callsigns embedded as phonetics should already be converted."""
        log.info("TX: %s", text)
        self.log_line.emit(f"[TX] {text}")

        ptt_on = self.cfg.ptt_type != "NONE" and self._radio.connected
        if ptt_on:
            self._radio.set_ptt(True)
            time.sleep(self.cfg.ptt_pre_delay_ms / 1000)
        self.ptt_changed.emit(True)

        path = self._tts.speak_to_file(text)
        if path:
            play_audio_file(path, device_name_hint=self.cfg.tx_device_name)
            try:
                os.unlink(path)
            except OSError:
                pass
        else:
            self._tts.speak_blocking(text)

        self.ptt_changed.emit(False)
        if ptt_on:
            time.sleep(self.cfg.ptt_tail_ms / 1000)
            self._radio.set_ptt(False)

    # ─────────────────────────────────────────────────────── RX helper

    def _listen(self, hint: str = "Listening…") -> str:
        self.status_message.emit(hint)
        audio = self._recorder.record_until_silence(
            status_callback=lambda m: self.status_message.emit(m)
        )
        if audio is None:
            return ""
        self.status_message.emit("Transcribing…")
        text = self._stt.transcribe(audio)
        if text:
            self.log_line.emit(f"[RX] {text}")
            log.info("RX: %s", text)
        return text

    # ─────────────────────────────────────────────────────── state helper

    def _set_state(self, s: QSOState):
        self._state = s
        self.state_changed.emit(s.name)

    # ─────────────────────────────────────────────────────── CQ loop

    def _cq_loop(self):
        my = self.cfg.my_callsign
        ph = callsign_to_speech(my)

        # Standard CQ call — three times, CQ DX variant, calling any station
        cq_text = (
            f"C Q, C Q, C Q, this is {ph}, {ph}, {ph}, "
            f"calling C Q and C Q D X, calling any station, "
            f"this is {ph} over"
        )

        attempt = 0
        while not self._stop_event.is_set():
            self._set_state(QSOState.CALLING_CQ)
            self._say(cq_text)

            # Listen for reply — 10 seconds per the user's requirement
            self._set_state(QSOState.LISTENING)
            self.status_message.emit(
                f"Listening for reply… (attempt {attempt + 1}/{self.cfg.cq_repeat_max})"
            )
            transcript = self._listen(hint="Listening for reply…")

            if self._stop_event.is_set():
                break

            if not transcript:
                attempt += 1
                if attempt >= self.cfg.cq_repeat_max:
                    self.status_message.emit("No reply after max attempts — stopping.")
                    break
                # Wait 10 seconds (minus time already spent) before next CQ
                self._wait(self.cfg.cq_interval_sec)
                continue

            # Someone replied — try to extract their callsign
            their_call = speech_to_callsign(transcript)
            if not their_call:
                # Ask them to confirm
                self._say(
                    f"Station calling {ph}, please say your callsign again, over"
                )
                transcript2 = self._listen(hint="Waiting for callsign repeat…")
                their_call = speech_to_callsign(transcript2) if transcript2 else None

            if not their_call:
                self.status_message.emit("Still couldn't parse callsign — calling CQ again.")
                attempt += 1
                self._wait(3)
                continue

            # We have their callsign — run the QSO
            attempt = 0
            self._their_call = their_call
            self._run_qso()

            if self._stop_event.is_set():
                break

            # Back to CQ
            if self.cfg.auto_cq_after_log:
                self._wait(3)
            else:
                break   # single QSO mode: stop after one contact

        self._set_state(QSOState.IDLE)

    # ─────────────────────────────────────────────────────── QSO exchange

    def _run_qso(self):
        my  = self.cfg.my_callsign
        ph_my    = callsign_to_speech(my)
        ph_their = callsign_to_speech(self._their_call)

        self.qso_started.emit(self._their_call)
        self._set_state(QSOState.SEND_REPORT)

        # Read S-meter for their signal report
        smeter = self._radio.get_smeter() if self._radio.connected else 36
        rst = smeter_to_rst(smeter)
        self._rst_sent = rst

        # Give them their signal report (repeat RST twice for clarity over noise)
        report_text = (
            f"{ph_their} {ph_their}, de {ph_my}, "
            f"your signal report is {rst[0]} {rst[1]}, "
            f"I say again {rst[0]} {rst[1]}, "
            f"over"
        )
        self._say(report_text)

        # Listen for their response — they should give us our RST
        self._set_state(QSOState.LISTEN_REPORT)
        self._rst_rcvd = ""

        for attempt in range(3):
            if self._stop_event.is_set():
                return
            transcript = self._listen(hint="Listening for their report…")
            if not transcript:
                if attempt < 2:
                    self._say(f"{ph_their} please go ahead, over")
                continue

            rst_rcvd = speech_to_rst(transcript)
            if rst_rcvd:
                self._rst_rcvd = rst_rcvd
                break

            # Couldn't parse RST — ask to repeat
            if attempt < 2:
                self._say(
                    f"{ph_their} say again your signal report please, over"
                )

        if not self._rst_rcvd:
            self._rst_rcvd = "59"
            self.status_message.emit("Could not parse their report — assuming 59.")

        # Acknowledge the report
        self._set_state(QSOState.ACKNOWLEDGE)
        ack_text = (
            f"Roger {ph_their}, received {self._rst_rcvd}, thank you, over"
        )

        # Check if the last transcript asked for a repeated report
        last_text = transcript if transcript else ""
        if _REPEAT_PHRASES.search(last_text):
            ack_text = (
                f"Roger {ph_their}, I confirm your signal report is "
                f"{rst[0]} {rst[1]}, {rst[0]} {rst[1]}, over"
            )

        self._say(ack_text)

        # Listen once more — they may want another report or will say 73
        self._set_state(QSOState.LISTEN_REPORT)
        follow_up = self._listen(hint="Listening for follow-up…")

        if follow_up:
            if _REPEAT_PHRASES.search(follow_up):
                # They asked for another signal report
                self._say(
                    f"{ph_their} your signal report is {rst[0]} {rst[1]}, "
                    f"{rst[0]} {rst[1]}, over"
                )
                self._listen(hint="Listening for final acknowledgement…")

        # Close the QSO
        self._set_state(QSOState.CLOSING)
        close_text = (
            f"Thank you for the contact {ph_their}, "
            f"seven three, this is {ph_my}, seven three"
        )
        self._say(close_text)

        # Log
        self._set_state(QSOState.LOGGING)
        self._do_log(self._their_call, self._rst_sent, self._rst_rcvd)
        self._their_call = ""

    # ─────────────────────────────────────────────────────── logging

    def _do_log(self, callsign: str, rst_sent: str, rst_rcvd: str):
        freq = self._radio.get_frequency() if self._radio.connected else 14_200_000.0
        mode = self._radio.get_mode()      if self._radio.connected else "USB"
        band = self._radio.freq_to_band(freq)

        qso = QSORecord(
            call=callsign, rst_sent=rst_sent, rst_rcvd=rst_rcvd,
            freq_hz=freq, mode=mode, band=band,
            my_call=self.cfg.my_callsign, grid=self.cfg.grid_square,
        )
        if self._logger.log(qso):
            self.qso_logged.emit(callsign, rst_sent, rst_rcvd)
            self.status_message.emit(f"Logged: {callsign} {rst_sent}/{rst_rcvd} {band} {mode}")
        else:
            self.error_signal.emit(f"Logging failed for {callsign}")

    # ─────────────────────────────────────────────────────── util

    def _wait(self, seconds: float):
        """Interruptible sleep."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self._stop_event.is_set():
            time.sleep(0.1)
