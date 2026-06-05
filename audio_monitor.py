"""
Real-time audio level monitoring.
AudioLevelMonitor  — background thread that continuously reads from an input device
                     and emits RMS levels as Qt signals.
LevelMeterWidget   — vertical bar meter with peak hold and threshold marker.
"""
import threading
import numpy as np
import pyaudio

from PyQt6.QtCore import QObject, pyqtSignal, QTimer, Qt
from PyQt6.QtGui import QPainter, QColor, QFont, QPen
from PyQt6.QtWidgets import QWidget

_CHUNK      = 512
_SAMPLE_RATE = 16000
_DECAY_MS   = 40    # timer interval for meter decay
_DECAY_STEP = 0.04  # level units lost per tick
_PEAK_HOLD  = 25    # ticks before peak starts falling


class AudioLevelMonitor(QObject):
    """Reads audio from one input device continuously and emits normalised RMS."""
    rx_level = pyqtSignal(float)   # 0.0 – 1.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._device_index: int | None = None
        self._running = False
        self._thread: threading.Thread | None = None

    def set_device(self, index: int | None):
        """Call before start(), or stop → set → start to change device."""
        self._device_index = index

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _run(self):
        pa = pyaudio.PyAudio()
        stream = None
        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=_SAMPLE_RATE,
                input=True,
                input_device_index=self._device_index,
                frames_per_buffer=_CHUNK,
            )
            while self._running:
                try:
                    data = stream.read(_CHUNK, exception_on_overflow=False)
                except OSError:
                    break
                samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                rms = float(np.sqrt(np.mean(samples ** 2)))
                # Scale for display: 0.015 RMS ≈ mid-meter; 0.15 ≈ full
                normalised = min(1.0, rms / 0.15)
                self.rx_level.emit(normalised)
        except Exception:
            pass
        finally:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            pa.terminate()


class LevelMeterWidget(QWidget):
    """
    Vertical bar meter.
      • Colour zones: green → yellow → red
      • White peak-hold line
      • Optional dashed red threshold marker (set_threshold)
      • Label underneath
    """
    _GREEN  = QColor("#a6e3a1")
    _YELLOW = QColor("#f9e2af")
    _RED    = QColor("#f38ba8")
    _BG     = QColor("#181825")
    _TRACK  = QColor("#313244")
    _PEAK   = QColor("#cdd6f4")
    _THRESH = QColor("#f38ba8")
    _LABEL  = QColor("#6c7086")

    def __init__(self, label: str = "", parent=None):
        super().__init__(parent)
        self._label = label
        self._level  = 0.0
        self._peak   = 0.0
        self._thresh = 0.0
        self._peak_hold_count = 0
        self._active = False   # True while TX/RX is in progress (tints border)

        self.setMinimumSize(28, 100)
        self.setMaximumWidth(52)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(_DECAY_MS)

    # ── public API ──────────────────────────────────────────────

    def set_level(self, normalised: float):
        self._level = max(0.0, min(1.0, normalised))
        if self._level > self._peak:
            self._peak = self._level
            self._peak_hold_count = _PEAK_HOLD
        self.update()

    def set_threshold(self, rms_raw: float):
        """Set threshold in raw RMS units (same scale as vad_threshold config)."""
        self._thresh = min(1.0, rms_raw / 0.15)
        self.update()

    def set_active(self, active: bool):
        self._active = active
        self.update()

    def reset(self):
        self._level = 0.0
        self._peak  = 0.0
        self.update()

    # ── internal ────────────────────────────────────────────────

    def _tick(self):
        changed = False
        if self._level > 0:
            self._level = max(0.0, self._level - _DECAY_STEP)
            changed = True
        if self._peak_hold_count > 0:
            self._peak_hold_count -= 1
        elif self._peak > 0:
            self._peak = max(0.0, self._peak - _DECAY_STEP * 0.4)
            changed = True
        if changed:
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        W, H   = self.width(), self.height()
        mg     = 4
        lh     = 14   # label height
        mh     = H - mg * 2 - lh  # meter height
        my     = mg               # meter top y
        mx     = mg               # meter left x
        mw     = W - mg * 2       # meter width

        # Background
        p.fillRect(0, 0, W, H, self._BG)

        # Border tint when active
        if self._active:
            border_pen = QPen(QColor("#89b4fa"), 1)
            p.setPen(border_pen)
            p.drawRect(0, 0, W - 1, H - 1)

        # Track
        p.fillRect(mx, my, mw, mh, self._TRACK)

        # Level bar (bottom-up)
        bar_h = int(self._level * mh)
        if bar_h > 0:
            bar_y = my + mh - bar_h
            if self._level < 0.6:
                colour = self._GREEN
            elif self._level < 0.85:
                colour = self._YELLOW
            else:
                colour = self._RED
            p.fillRect(mx, bar_y, mw, bar_h, colour)

        # Threshold dashed line
        if self._thresh > 0:
            ty = my + int((1.0 - self._thresh) * mh)
            pen = QPen(self._THRESH, 1, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(mx, ty, mx + mw, ty)

        # Peak hold line
        if self._peak > 0.01:
            py = my + int((1.0 - self._peak) * mh)
            p.setPen(QPen(self._PEAK, 2))
            p.drawLine(mx, py, mx + mw, py)

        # Label
        p.setPen(self._LABEL)
        p.setFont(QFont("Consolas", 8))
        p.drawText(0, H - lh, W, lh, Qt.AlignmentFlag.AlignCenter, self._label)
