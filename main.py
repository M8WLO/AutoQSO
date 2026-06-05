"""
AutoQSO — AI-free automated ham radio QSO assistant.
Calls CQ, listens and decodes callsigns/RSTs via Whisper STT,
responds with TTS, and logs to ADIF / N1MM+ UDP / QRZ.
"""
import sys
import logging
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QLabel, QPushButton, QLineEdit, QComboBox, QTextEdit,
    QTableWidget, QTableWidgetItem, QSpinBox, QDoubleSpinBox,
    QCheckBox, QTabWidget, QFormLayout, QMessageBox, QFileDialog,
    QScrollArea, QSizePolicy,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot, QObject, QTimer
from PyQt6.QtGui import QFont, QColor

from config import Config
from audio_engine import list_devices, find_device_index
from radio_interface import (
    RigctldManager, RadioInterface,
    HAMLIB_RIGS, find_rigctld, list_serial_ports,
)
from qso_controller import QSOController
from audio_monitor import AudioLevelMonitor, LevelMeterWidget

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────── worker threads

class InitWorker(QObject):
    finished = pyqtSignal()
    def __init__(self, controller): super().__init__(); self._ctrl = controller
    def run(self): self._ctrl.initialise(); self.finished.emit()


# ──────────────────────────────────────────────────────────────── Main window

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = Config.load()
        self.rigctld_mgr = RigctldManager()
        self.controller  = QSOController(self.cfg)

        # Audio level monitor (RX device → meter)
        self._rx_monitor = AudioLevelMonitor()
        self._connect_signals()
        self._build_ui()
        self._apply_style()
        self.setWindowTitle("AutoQSO")
        self.resize(980, 740)

        # Poll radio status every 2 s
        self._radio_poll = QTimer(self)
        self._radio_poll.timeout.connect(self._poll_radio_status)
        self._radio_poll.start(2000)

        self._start_init()
        self._start_rx_monitor()

        # Auto-start rigctld if configured
        if self.cfg.rigctld_autostart:
            QTimer.singleShot(500, self._start_rigctld)

    # ──────────────────────────────────────────── signal wiring
    def _connect_signals(self):
        c = self.controller
        c.state_changed.connect(self._on_state)
        c.status_message.connect(self._on_status)
        c.qso_started.connect(self._on_qso_started)
        c.qso_logged.connect(self._on_qso_logged)
        c.log_line.connect(self._on_log_line)
        c.error_signal.connect(self._on_error)
        c.ptt_changed.connect(self._on_ptt_changed)
        self._rx_monitor.rx_level.connect(self._on_rx_level)

    def _start_rx_monitor(self):
        idx = find_device_index(self.cfg.rx_device_name, "input")
        self._rx_monitor.set_device(idx)
        self._rx_monitor.start()

    # ──────────────────────────────────────────── UI build
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(4)
        root.setContentsMargins(6, 6, 6, 6)

        tabs = QTabWidget()
        tabs.addTab(self._build_operate_tab(), "Operate")
        tabs.addTab(self._build_radio_tab(),   "Radio / CAT")
        tabs.addTab(self._build_audio_tab(),   "Audio / STT / TTS")
        tabs.addTab(self._build_logging_tab(), "Logging")
        tabs.addTab(self._build_cq_tab(),      "CQ Behaviour")
        root.addWidget(tabs)

        self.status_bar = self.statusBar()
        self.status_bar.showMessage("Initialising…")

    # ════════════════════════════════════════════ OPERATE TAB
    def _build_operate_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(8)

        # ── Top row: state chip + current QSO info + level meters
        top = QHBoxLayout()

        self.state_label = QLabel("IDLE")
        self.state_label.setFont(QFont("Consolas", 16, QFont.Weight.Bold))
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_label.setMinimumWidth(160)
        top.addWidget(self._grp("State", self.state_label))

        qso_form = QFormLayout()
        self.call_label      = QLabel("—")
        self.rst_sent_label  = QLabel("—")
        self.rst_rcvd_label  = QLabel("—")
        self.call_label.setFont(QFont("Consolas", 14, QFont.Weight.Bold))
        qso_form.addRow("Their callsign:", self.call_label)
        qso_form.addRow("RST sent:",       self.rst_sent_label)
        qso_form.addRow("RST rcvd:",       self.rst_rcvd_label)
        qso_w = QWidget(); qso_w.setLayout(qso_form)
        top.addWidget(self._grp("Current QSO", qso_w), 1)

        # Level meters — RX and TX side by side in a group
        self._rx_meter = LevelMeterWidget("RX")
        self._tx_meter = LevelMeterWidget("TX")
        self._rx_meter.set_threshold(self.cfg.vad_threshold)
        meters_row = QHBoxLayout()
        meters_row.setSpacing(4)
        meters_row.addWidget(self._rx_meter)
        meters_row.addWidget(self._tx_meter)
        meters_w = QWidget(); meters_w.setLayout(meters_row)
        top.addWidget(self._grp("Levels", meters_w))

        lay.addLayout(top)

        # ── CQ controls
        btn_row = QHBoxLayout()
        self.cq_btn = QPushButton("▶  Start CQ")
        self.cq_btn.setMinimumHeight(46)
        self.cq_btn.setEnabled(False)
        self.cq_btn.clicked.connect(self._start_cq)
        self.stop_btn = QPushButton("■  Stop")
        self.stop_btn.setMinimumHeight(46)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_cq)
        btn_row.addWidget(self.cq_btn)
        btn_row.addWidget(self.stop_btn)
        lay.addLayout(btn_row)

        # ── Manual log entry
        mg = QGroupBox("Manual Log Entry")
        mf = QHBoxLayout(mg)
        self.manual_call = QLineEdit(); self.manual_call.setPlaceholderText("Callsign")
        self.manual_sent = QLineEdit("59"); self.manual_sent.setMaximumWidth(55)
        self.manual_rcvd = QLineEdit("59"); self.manual_rcvd.setMaximumWidth(55)
        log_btn = QPushButton("Log"); log_btn.clicked.connect(self._manual_log)
        mf.addWidget(QLabel("Call:"));  mf.addWidget(self.manual_call)
        mf.addWidget(QLabel("Sent:"));  mf.addWidget(self.manual_sent)
        mf.addWidget(QLabel("Rcvd:"));  mf.addWidget(self.manual_rcvd)
        mf.addWidget(log_btn)
        lay.addWidget(mg)

        # ── Transcript
        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setFont(QFont("Consolas", 9))
        lay.addWidget(self._grp("Transcript (TX / RX)", self.transcript), 1)

        # ── QSO log table
        self.qso_table = QTableWidget(0, 6)
        self.qso_table.setHorizontalHeaderLabels(
            ["Callsign", "Band", "Mode", "RST Sent", "RST Rcvd", "UTC"]
        )
        self.qso_table.horizontalHeader().setStretchLastSection(True)
        self.qso_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.qso_table.setAlternatingRowColors(True)
        lay.addWidget(self._grp("Session Log", self.qso_table), 1)

        return w

    # ════════════════════════════════════════════ RADIO / CAT TAB
    def _build_radio_tab(self) -> QWidget:
        outer = QScrollArea()
        outer.setWidgetResizable(True)
        w = QWidget()
        outer.setWidget(w)
        lay = QVBoxLayout(w)
        lay.setSpacing(10)

        # ── rigctld executable
        g = self._form_group("rigctld Executable", lay)
        self.r_rigctld_path = QLineEdit(self.cfg.rigctld_path)
        self.r_rigctld_path.setPlaceholderText("Leave empty to auto-detect")
        browse_btn = QPushButton("Browse…")
        browse_btn.setMaximumWidth(90)
        browse_btn.clicked.connect(self._browse_rigctld)
        path_row = QHBoxLayout()
        path_row.addWidget(self.r_rigctld_path)
        path_row.addWidget(browse_btn)
        path_w = QWidget(); path_w.setLayout(path_row)
        g.addRow("rigctld path:", path_w)

        detected = find_rigctld()
        detect_lbl = QLabel(f"Auto-detected: {detected or 'not found — install hamlib'}")
        detect_lbl.setStyleSheet("color:#6c7086; font-size:10px;")
        g.addRow("", detect_lbl)

        self.r_autostart = QCheckBox("Auto-start rigctld when AutoQSO launches")
        self.r_autostart.setChecked(self.cfg.rigctld_autostart)
        g.addRow("", self.r_autostart)

        # ── Rig model
        g = self._form_group("Radio Model", lay)
        self.r_model_combo = QComboBox()
        self.r_model_combo.setEditable(True)
        self.r_model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for num, name in HAMLIB_RIGS:
            self.r_model_combo.addItem(f"{num}  —  {name}", userData=num)
        # Set current
        self._set_model_combo(self.cfg.rig_model)
        self.r_model_combo.currentIndexChanged.connect(self._on_model_changed)
        g.addRow("Rig model:", self.r_model_combo)

        self.r_model_num = QSpinBox()
        self.r_model_num.setRange(1, 99999)
        self.r_model_num.setValue(self.cfg.rig_model)
        self.r_model_num.setToolTip(
            "Hamlib model number. Run  rigctl -l  to see all models."
        )
        self.r_model_num.valueChanged.connect(self._on_model_num_changed)
        g.addRow("Or enter number directly:", self.r_model_num)

        model_note = QLabel(
            "Run  rigctl -l  in a terminal to see all supported models.\n"
            "Use model 1 (Dummy) for testing without a radio connected."
        )
        model_note.setWordWrap(True)
        model_note.setStyleSheet("color:#6c7086; font-size:10px;")
        g.addRow("", model_note)

        # ── Serial port
        g = self._form_group("Serial / CAT Port", lay)
        port_row = QHBoxLayout()
        self.r_port = QComboBox()
        self.r_port.setEditable(True)
        self._refresh_ports()
        port_row.addWidget(self.r_port)
        refresh_btn = QPushButton("↻ Refresh")
        refresh_btn.setMaximumWidth(90)
        refresh_btn.clicked.connect(self._refresh_ports)
        port_row.addWidget(refresh_btn)
        port_w = QWidget(); port_w.setLayout(port_row)
        g.addRow("COM port:", port_w)

        self.r_baud = QComboBox()
        self.r_baud.addItems(["1200","2400","4800","9600","19200","38400","57600","115200"])
        self.r_baud.setCurrentText(str(self.cfg.rig_baud))
        g.addRow("Baud rate:", self.r_baud)

        self.r_data_bits = QComboBox()
        self.r_data_bits.addItems(["7", "8"])
        self.r_data_bits.setCurrentText(str(self.cfg.rig_data_bits))
        g.addRow("Data bits:", self.r_data_bits)

        self.r_stop_bits = QComboBox()
        self.r_stop_bits.addItems(["1", "2"])
        self.r_stop_bits.setCurrentText(self.cfg.rig_stop_bits)
        g.addRow("Stop bits:", self.r_stop_bits)

        self.r_parity = QComboBox()
        self.r_parity.addItems(["N — None", "E — Even", "O — Odd"])
        parity_map = {"N": 0, "E": 1, "O": 2}
        self.r_parity.setCurrentIndex(parity_map.get(self.cfg.rig_parity, 0))
        g.addRow("Parity:", self.r_parity)

        self.r_flow = QComboBox()
        self.r_flow.addItems(["None", "RTS/CTS", "XON/XOFF"])
        flow_map = {"None": 0, "RTS/CTS": 1, "XON/XOFF": 2}
        self.r_flow.setCurrentIndex(flow_map.get(self.cfg.rig_flow_control, 0))
        g.addRow("Flow control:", self.r_flow)

        # ── PTT
        g = self._form_group("PTT Control", lay)
        self.r_ptt = QComboBox()
        self.r_ptt.addItems(["CAT", "RTS", "DTR", "NONE"])
        self.r_ptt.setCurrentText(self.cfg.ptt_type)
        g.addRow("PTT type:", self.r_ptt)

        self.r_ptt_pre = QSpinBox()
        self.r_ptt_pre.setRange(0, 2000); self.r_ptt_pre.setSuffix(" ms")
        self.r_ptt_pre.setValue(self.cfg.ptt_pre_delay_ms)
        g.addRow("Pre-PTT delay:", self.r_ptt_pre)

        self.r_ptt_tail = QSpinBox()
        self.r_ptt_tail.setRange(0, 2000); self.r_ptt_tail.setSuffix(" ms")
        self.r_ptt_tail.setValue(self.cfg.ptt_tail_ms)
        g.addRow("PTT tail delay:", self.r_ptt_tail)

        ptt_note = QLabel(
            "CAT = PTT via hamlib CAT command (most modern radios).\n"
            "RTS / DTR = hardware PTT via serial pin (interfaces like SignaLink).\n"
            "NONE = VOX or press PTT manually — AutoQSO will not key the radio."
        )
        ptt_note.setWordWrap(True)
        ptt_note.setStyleSheet("color:#6c7086; font-size:10px;")
        g.addRow("", ptt_note)

        # ── rigctld TCP settings
        g = self._form_group("rigctld TCP (advanced)", lay)
        self.r_tcp_host = QLineEdit(self.cfg.rigctld_host)
        self.r_tcp_port = QSpinBox()
        self.r_tcp_port.setRange(1, 65535); self.r_tcp_port.setValue(self.cfg.rigctld_port)
        g.addRow("Listen host:", self.r_tcp_host)
        g.addRow("Listen port:", self.r_tcp_port)

        # ── Connection control + live status
        g = self._form_group("Connection", lay)

        # Status indicator
        status_row = QHBoxLayout()
        self.rig_status_dot = QLabel("●")
        self.rig_status_dot.setFont(QFont("Consolas", 18))
        self.rig_status_dot.setStyleSheet("color: #f38ba8;")  # red = disconnected
        self.rig_status_text = QLabel("rigctld not running")
        status_row.addWidget(self.rig_status_dot)
        status_row.addWidget(self.rig_status_text)
        status_row.addStretch()
        status_w = QWidget(); status_w.setLayout(status_row)
        g.addRow("Status:", status_w)

        # Buttons
        btn_row2 = QHBoxLayout()
        self.start_rig_btn = QPushButton("▶  Start rigctld")
        self.start_rig_btn.clicked.connect(self._start_rigctld)
        self.stop_rig_btn  = QPushButton("■  Stop rigctld")
        self.stop_rig_btn.clicked.connect(self._stop_rigctld)
        self.reconnect_btn = QPushButton("⟳  Reconnect")
        self.reconnect_btn.clicked.connect(self._reconnect_radio)
        btn_row2.addWidget(self.start_rig_btn)
        btn_row2.addWidget(self.stop_rig_btn)
        btn_row2.addWidget(self.reconnect_btn)
        btn_w2 = QWidget(); btn_w2.setLayout(btn_row2)
        g.addRow("", btn_w2)

        # Live radio readout
        live_form = QFormLayout()
        self.live_freq  = QLabel("—")
        self.live_mode  = QLabel("—")
        self.live_smet  = QLabel("—")
        self.live_freq.setFont(QFont("Consolas", 12))
        live_form.addRow("Frequency:", self.live_freq)
        live_form.addRow("Mode:",      self.live_mode)
        live_form.addRow("S-meter:",   self.live_smet)
        live_w = QWidget(); live_w.setLayout(live_form)
        g.addRow("Live status:", live_w)

        # Save
        save_btn = QPushButton("Save Radio Settings")
        save_btn.setMinimumHeight(36)
        save_btn.clicked.connect(self._save_radio)
        lay.addWidget(save_btn)
        lay.addStretch()

        return outer

    # ════════════════════════════════════════════ AUDIO / STT / TTS TAB
    def _build_audio_tab(self) -> QWidget:
        outer = QScrollArea(); outer.setWidgetResizable(True)
        w = QWidget(); outer.setWidget(w)
        lay = QVBoxLayout(w); lay.setSpacing(10)

        devices    = list_devices()
        in_names   = ["(system default)"] + [d["name"] for d in devices if d["inputs"]  > 0]
        out_names  = ["(system default)"] + [d["name"] for d in devices if d["outputs"] > 0]

        # ── Audio devices
        g = self._form_group("Audio Devices", lay)
        self.a_rx_dev = QComboBox(); self.a_rx_dev.addItems(in_names)
        self.a_tx_dev = QComboBox(); self.a_tx_dev.addItems(out_names)
        self._combo_select(self.a_rx_dev, self.cfg.rx_device_name)
        self._combo_select(self.a_tx_dev, self.cfg.tx_device_name)
        g.addRow("RX device (radio audio in):", self.a_rx_dev)
        g.addRow("TX device (TTS audio out):",  self.a_tx_dev)
        dev_note = QLabel(
            "AetherSDR: pick your DAX RX channel as RX and DAX TX as TX.\n"
            "USB radio (IC-7300 etc): pick its USB Audio CODEC for both."
        )
        dev_note.setWordWrap(True); dev_note.setStyleSheet("color:#6c7086; font-size:10px;")
        g.addRow("", dev_note)

        # ── Listening sensitivity
        g = self._form_group("Listening Sensitivity", lay)
        self.a_vad = QDoubleSpinBox()
        self.a_vad.setRange(0.001, 0.5); self.a_vad.setSingleStep(0.005)
        self.a_vad.setDecimals(3); self.a_vad.setValue(self.cfg.vad_threshold)
        self.a_silence = QDoubleSpinBox()
        self.a_silence.setRange(0.5, 10.0); self.a_silence.setSingleStep(0.1)
        self.a_silence.setDecimals(1); self.a_silence.setValue(self.cfg.silence_end_sec)
        self.a_maxlisten = QDoubleSpinBox()
        self.a_maxlisten.setRange(5.0, 120.0); self.a_maxlisten.setSingleStep(1.0)
        self.a_maxlisten.setDecimals(0); self.a_maxlisten.setValue(self.cfg.max_listen_sec)
        g.addRow("Voice threshold (RMS):",   self.a_vad)
        g.addRow("Silence before end (s):",  self.a_silence)
        g.addRow("Max listen window (s):",   self.a_maxlisten)
        vad_note = QLabel(
            "Threshold: lower = more sensitive. Try 0.005–0.01 for weak/noisy signals.\n"
            "Silence end: 1.8 s works for SSB; increase for slow talkers.\n"
            "Max listen: hard cut-off if nobody transmits."
        )
        vad_note.setWordWrap(True); vad_note.setStyleSheet("color:#6c7086; font-size:10px;")
        g.addRow("", vad_note)

        # ── Whisper STT
        g = self._form_group("Speech Recognition (Whisper — local, no internet)", lay)
        self.a_whisper = QComboBox()
        self.a_whisper.addItems(["tiny", "base", "small", "medium"])
        self.a_whisper.setCurrentText(self.cfg.whisper_model)
        g.addRow("Whisper model:", self.a_whisper)
        w_note = QLabel(
            "tiny  = fastest, weakest with noise.\n"
            "base  = good balance for clean SSB.\n"
            "small = recommended for real HF with QRM/QSB.\n"
            "medium= most accurate, ~4 s transcription on CPU.\n"
            "Model downloads automatically on first use (~100–500 MB)."
        )
        w_note.setWordWrap(True); w_note.setStyleSheet("color:#6c7086; font-size:10px;")
        g.addRow("", w_note)

        # ── TTS
        g = self._form_group("Voice Output (TTS)", lay)
        self.a_tts_engine = QComboBox()
        self.a_tts_engine.addItems(["pyttsx3", "edge"])
        self.a_tts_engine.setCurrentText(self.cfg.tts_engine)
        self.a_tts_voice = QSpinBox(); self.a_tts_voice.setRange(0, 20)
        self.a_tts_voice.setValue(self.cfg.tts_voice_index)
        self.a_tts_rate = QSpinBox(); self.a_tts_rate.setRange(80, 300)
        self.a_tts_rate.setValue(self.cfg.tts_rate_wpm)
        g.addRow("Engine:",              self.a_tts_engine)
        g.addRow("Voice index (pyttsx3):", self.a_tts_voice)
        g.addRow("Rate WPM (pyttsx3):",  self.a_tts_rate)
        tts_note = QLabel(
            "pyttsx3 = offline Windows SAPI (no internet required).\n"
            "edge    = Microsoft neural voices (internet required).\n"
            "Voice index 0 = first installed Windows voice (typically English)."
        )
        tts_note.setWordWrap(True); tts_note.setStyleSheet("color:#6c7086; font-size:10px;")
        g.addRow("", tts_note)

        save_btn = QPushButton("Save Audio / STT / TTS Settings")
        save_btn.setMinimumHeight(36)
        save_btn.clicked.connect(self._save_audio)
        lay.addWidget(save_btn)
        lay.addStretch()
        return outer

    # ════════════════════════════════════════════ LOGGING TAB
    def _build_logging_tab(self) -> QWidget:
        outer = QScrollArea(); outer.setWidgetResizable(True)
        w = QWidget(); outer.setWidget(w)
        lay = QVBoxLayout(w); lay.setSpacing(10)

        g = self._form_group("ADIF File Log", lay)
        adif_row = QHBoxLayout()
        self.l_adif = QLineEdit(self.cfg.adif_log_path)
        adif_browse = QPushButton("Browse…"); adif_browse.setMaximumWidth(90)
        adif_browse.clicked.connect(self._browse_adif)
        adif_row.addWidget(self.l_adif); adif_row.addWidget(adif_browse)
        adif_w = QWidget(); adif_w.setLayout(adif_row)
        g.addRow("ADIF file path:", adif_w)
        adif_note = QLabel(
            "Each logged QSO is appended to this file in standard ADIF format.\n"
            "Station Master, Log4OM, HRD and most loggers can import ADIF."
        )
        adif_note.setWordWrap(True); adif_note.setStyleSheet("color:#6c7086; font-size:10px;")
        g.addRow("", adif_note)

        g = self._form_group("N1MM+ / Station Master UDP Broadcast", lay)
        self.l_n1mm_enable = QCheckBox("Broadcast each QSO via UDP (port 2237)")
        self.l_n1mm_enable.setChecked(self.cfg.enable_n1mm_udp)
        self.l_n1mm_host = QLineEdit(self.cfg.n1mm_host)
        self.l_n1mm_port = QSpinBox(); self.l_n1mm_port.setRange(1, 65535)
        self.l_n1mm_port.setValue(self.cfg.n1mm_port)
        g.addRow("", self.l_n1mm_enable)
        g.addRow("Broadcast host:", self.l_n1mm_host)
        g.addRow("Broadcast port:", self.l_n1mm_port)
        n1mm_note = QLabel(
            "Station Master and N1MM+ listen on UDP 2237 by default.\n"
            "Use 127.0.0.1 if the logger is on the same PC."
        )
        n1mm_note.setWordWrap(True); n1mm_note.setStyleSheet("color:#6c7086; font-size:10px;")
        g.addRow("", n1mm_note)

        g = self._form_group("QRZ Logbook", lay)
        self.l_qrz_enable = QCheckBox("Upload to QRZ logbook after each QSO")
        self.l_qrz_enable.setChecked(self.cfg.enable_qrz_log)
        self.l_qrz_key = QLineEdit(self.cfg.qrz_api_key)
        self.l_qrz_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.l_qrz_key.setPlaceholderText("QRZ API key from qrz.com/XML/current_spec.html")
        g.addRow("", self.l_qrz_enable)
        g.addRow("QRZ API key:", self.l_qrz_key)

        save_btn = QPushButton("Save Logging Settings")
        save_btn.setMinimumHeight(36)
        save_btn.clicked.connect(self._save_logging)
        lay.addWidget(save_btn)
        lay.addStretch()
        return outer

    # ════════════════════════════════════════════ CQ BEHAVIOUR TAB
    def _build_cq_tab(self) -> QWidget:
        outer = QScrollArea(); outer.setWidgetResizable(True)
        w = QWidget(); outer.setWidget(w)
        lay = QVBoxLayout(w); lay.setSpacing(10)

        g = self._form_group("Station", lay)
        self.cq_my_call = QLineEdit(self.cfg.my_callsign)
        self.cq_grid    = QLineEdit(self.cfg.grid_square)
        g.addRow("My callsign:", self.cq_my_call)
        g.addRow("Grid square:", self.cq_grid)

        g = self._form_group("CQ Calling", lay)
        self.cq_repeats = QSpinBox(); self.cq_repeats.setRange(1, 30)
        self.cq_repeats.setValue(self.cfg.cq_repeat_max)
        self.cq_interval = QDoubleSpinBox()
        self.cq_interval.setRange(3.0, 60.0); self.cq_interval.setSingleStep(1.0)
        self.cq_interval.setDecimals(0); self.cq_interval.setValue(self.cfg.cq_interval_sec)
        self.cq_auto = QCheckBox("Automatically call CQ again after each logged QSO")
        self.cq_auto.setChecked(self.cfg.auto_cq_after_log)
        g.addRow("Max CQ calls before giving up:", self.cq_repeats)
        g.addRow("Wait between CQ calls (s):",     self.cq_interval)
        g.addRow("", self.cq_auto)
        cq_note = QLabel(
            "Standard procedure: CQ CQ CQ de [call] [call] [call] calling CQ and CQ DX,\n"
            "calling any station. Listen 10 s. Repeat up to max times, then stop."
        )
        cq_note.setWordWrap(True); cq_note.setStyleSheet("color:#6c7086; font-size:10px;")
        g.addRow("", cq_note)

        save_btn = QPushButton("Save CQ Settings")
        save_btn.setMinimumHeight(36)
        save_btn.clicked.connect(self._save_cq)
        lay.addWidget(save_btn)
        lay.addStretch()
        return outer

    # ──────────────────────────────────────────── helpers
    @staticmethod
    def _grp(title: str, widget: QWidget) -> QGroupBox:
        g = QGroupBox(title)
        lay = QVBoxLayout(g)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(widget)
        return g

    def _form_group(self, title: str, parent_lay: QVBoxLayout) -> QFormLayout:
        g = QGroupBox(title)
        f = QFormLayout(g)
        f.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        parent_lay.addWidget(g)
        return f

    @staticmethod
    def _combo_select(combo: QComboBox, hint: str):
        if not hint:
            return
        for i in range(combo.count()):
            if hint.lower() in combo.itemText(i).lower():
                combo.setCurrentIndex(i)
                return

    def _set_model_combo(self, model_num: int):
        for i in range(self.r_model_combo.count()):
            if self.r_model_combo.itemData(i) == model_num:
                self.r_model_combo.setCurrentIndex(i)
                return
        # Not in list — just show the number
        self.r_model_combo.setCurrentText(str(model_num))

    def _on_model_changed(self, idx: int):
        num = self.r_model_combo.itemData(idx)
        if num is not None:
            self.r_model_num.blockSignals(True)
            self.r_model_num.setValue(num)
            self.r_model_num.blockSignals(False)

    def _on_model_num_changed(self, val: int):
        self.r_model_combo.blockSignals(True)
        self._set_model_combo(val)
        self.r_model_combo.blockSignals(False)

    def _refresh_ports(self):
        ports = list_serial_ports()
        self.r_port.clear()
        self.r_port.addItems(ports if ports else ["COM1"])
        # Try to restore saved port
        for i in range(self.r_port.count()):
            if self.r_port.itemText(i) == self.cfg.rig_port:
                self.r_port.setCurrentIndex(i)
                return
        self.r_port.setCurrentText(self.cfg.rig_port)

    def _apply_style(self):
        self.setStyleSheet("""
            QMainWindow, QWidget  { background: #1e1e2e; color: #cdd6f4; }
            QScrollArea           { border: none; }
            QGroupBox {
                border: 1px solid #45475a; border-radius: 4px;
                margin-top: 10px; padding-top: 6px;
            }
            QGroupBox::title {
                subcontrol-origin: margin; left: 10px; top: -2px;
                color: #89b4fa; font-weight: bold;
            }
            QPushButton {
                background: #313244; border: 1px solid #45475a;
                border-radius: 4px; padding: 6px 14px;
            }
            QPushButton:hover   { background: #45475a; }
            QPushButton:pressed { background: #585b70; }
            QPushButton:disabled { color: #6c7086; }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
                background: #313244; border: 1px solid #45475a;
                border-radius: 3px; padding: 3px 6px;
            }
            QTextEdit  { background: #181825; border: 1px solid #45475a; }
            QTableWidget { background: #181825; gridline-color: #313244;
                           alternate-background-color: #1e1e2e; }
            QHeaderView::section {
                background: #313244; color: #89b4fa;
                border: none; padding: 4px;
            }
            QTabBar::tab {
                background: #313244; padding: 7px 16px;
                border-radius: 4px; margin-right: 2px;
            }
            QTabBar::tab:selected { background: #45475a; color: #cba6f7; }
            QCheckBox::indicator { width: 14px; height: 14px; }
        """)

    # ──────────────────────────────────────────── init
    def _start_init(self):
        self._init_thread = QThread()
        self._init_worker = InitWorker(self.controller)
        self._init_worker.moveToThread(self._init_thread)
        self._init_thread.started.connect(self._init_worker.run)
        self._init_worker.finished.connect(self._init_thread.quit)
        self._init_worker.finished.connect(self._on_init_done)
        self._init_thread.start()

    @pyqtSlot()
    def _on_init_done(self):
        self.cq_btn.setEnabled(True)
        self.status_bar.showMessage("Ready")

    # ──────────────────────────────────────────── rigctld control
    def _start_rigctld(self):
        self._save_radio(silent=True)
        ok, msg = self.rigctld_mgr.start(self.cfg)
        self.status_bar.showMessage(msg)
        if ok:
            # Give it a moment then try to connect the radio interface
            QTimer.singleShot(1200, self._reconnect_radio)
        else:
            QMessageBox.warning(self, "rigctld", msg)
        self._update_rig_status()

    def _stop_rigctld(self):
        self.rigctld_mgr.stop()
        self.controller._radio.disconnect()
        self._update_rig_status()
        self.status_bar.showMessage("rigctld stopped.")

    def _reconnect_radio(self):
        radio = self.controller._radio
        radio._host = self.cfg.rigctld_host
        radio._port = self.cfg.rigctld_port
        if radio.reconnect():
            self.status_bar.showMessage("Radio connected.")
        else:
            self.status_bar.showMessage("Could not connect — is rigctld running?")
        self._update_rig_status()

    def _update_rig_status(self):
        proc_ok = self.rigctld_mgr.is_running()
        radio_ok = self.controller._radio.connected
        if radio_ok:
            dot_color, text = "#a6e3a1", "Connected to radio via rigctld"
        elif proc_ok:
            dot_color, text = "#f9e2af", "rigctld running — radio not responding"
        else:
            dot_color, text = "#f38ba8", "rigctld not running"
        self.rig_status_dot.setStyleSheet(f"color: {dot_color};")
        self.rig_status_text.setText(text)

    def _poll_radio_status(self):
        self._update_rig_status()
        radio = self.controller._radio
        if radio.connected:
            try:
                info = radio.get_info()
                hz   = info["freq"]
                band = RadioInterface.freq_to_band(hz)
                self.live_freq.setText(
                    f"{hz / 1_000_000:.4f} MHz  ({band})" if hz else "—"
                )
                self.live_mode.setText(info["mode"] or "—")
                smet = info["smeter"]
                # Convert dBm-style to rough S units
                s_units = max(1, min(9, (smet + 127) // 6)) if smet < 0 else min(9, smet // 6 + 1)
                self.live_smet.setText(f"S{s_units}  ({smet} dBm)")
            except Exception:
                pass

    # ──────────────────────────────────────────── browse dialogs
    def _browse_rigctld(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select rigctld executable", "",
            "Executables (*.exe);;All files (*)"
        )
        if path:
            self.r_rigctld_path.setText(path)

    def _browse_adif(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "ADIF log file", self.cfg.adif_log_path,
            "ADIF files (*.adi *.adif);;All files (*)"
        )
        if path:
            self.l_adif.setText(path)

    # ──────────────────────────────────────────── save handlers
    def _save_radio(self, silent: bool = False):
        c = self.cfg
        c.rigctld_path     = self.r_rigctld_path.text().strip()
        c.rigctld_host     = self.r_tcp_host.text().strip()
        c.rigctld_port     = self.r_tcp_port.value()
        c.rigctld_autostart = self.r_autostart.isChecked()
        c.rig_model        = self.r_model_num.value()
        c.rig_port         = self.r_port.currentText().strip()
        c.rig_baud         = int(self.r_baud.currentText())
        c.rig_data_bits    = int(self.r_data_bits.currentText())
        c.rig_stop_bits    = self.r_stop_bits.currentText()
        c.rig_parity       = self.r_parity.currentText()[0]   # "N — None" → "N"
        c.rig_flow_control = self.r_flow.currentText()
        c.ptt_type         = self.r_ptt.currentText()
        c.ptt_pre_delay_ms = self.r_ptt_pre.value()
        c.ptt_tail_ms      = self.r_ptt_tail.value()
        c.save()
        if not silent:
            self.status_bar.showMessage("Radio settings saved.")

    def _save_audio(self):
        c = self.cfg
        rx = self.a_rx_dev.currentText()
        tx = self.a_tx_dev.currentText()
        c.rx_device_name = "" if rx.startswith("(system") else rx
        c.tx_device_name = "" if tx.startswith("(system") else tx
        c.vad_threshold   = self.a_vad.value()
        c.silence_end_sec = self.a_silence.value()
        c.max_listen_sec  = self.a_maxlisten.value()
        c.whisper_model   = self.a_whisper.currentText()
        c.tts_engine      = self.a_tts_engine.currentText()
        c.tts_voice_index = self.a_tts_voice.value()
        c.tts_rate_wpm    = self.a_tts_rate.value()
        c.save()
        self.status_bar.showMessage("Audio/STT/TTS settings saved — restart to apply model changes.")

    def _save_logging(self):
        c = self.cfg
        c.adif_log_path  = self.l_adif.text().strip()
        c.enable_n1mm_udp = self.l_n1mm_enable.isChecked()
        c.n1mm_host       = self.l_n1mm_host.text().strip()
        c.n1mm_port       = self.l_n1mm_port.value()
        c.enable_qrz_log  = self.l_qrz_enable.isChecked()
        c.qrz_api_key     = self.l_qrz_key.text().strip()
        c.save()
        self.status_bar.showMessage("Logging settings saved.")

    def _save_cq(self):
        c = self.cfg
        c.my_callsign      = self.cq_my_call.text().strip().upper()
        c.grid_square       = self.cq_grid.text().strip().upper()
        c.cq_repeat_max     = self.cq_repeats.value()
        c.cq_interval_sec   = self.cq_interval.value()
        c.auto_cq_after_log = self.cq_auto.isChecked()
        c.save()
        self.status_bar.showMessage("CQ settings saved.")

    # ──────────────────────────────────────────── operate handlers
    def _start_cq(self):
        self.cq_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.controller.start_cq()

    def _stop_cq(self):
        self.controller.stop()
        self.stop_btn.setEnabled(False)
        self.cq_btn.setEnabled(True)

    def _manual_log(self):
        call = self.manual_call.text().strip().upper()
        if not call:
            QMessageBox.warning(self, "AutoQSO", "Enter a callsign to log.")
            return
        self.controller.manual_log(
            call,
            self.manual_sent.text().strip() or "59",
            self.manual_rcvd.text().strip() or "59",
        )

    # ──────────────────────────────────────────── controller slots
    @pyqtSlot(str)
    def _on_state(self, state: str):
        self.state_label.setText(state.replace("_", " "))
        colours = {
            "IDLE":          "#6c7086",
            "CALLING_CQ":    "#a6e3a1",
            "LISTENING":     "#89b4fa",
            "SEND_REPORT":   "#fab387",
            "LISTEN_REPORT": "#89b4fa",
            "ACKNOWLEDGE":   "#f9e2af",
            "CLOSING":       "#cba6f7",
            "LOGGING":       "#94e2d5",
        }
        self.state_label.setStyleSheet(f"color: {colours.get(state, '#cdd6f4')};")
        if state == "IDLE":
            self.cq_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)

    @pyqtSlot(str)
    def _on_status(self, msg: str):
        self.status_bar.showMessage(msg)

    @pyqtSlot(str)
    def _on_qso_started(self, callsign: str):
        self.call_label.setText(callsign)
        self.rst_sent_label.setText("—")
        self.rst_rcvd_label.setText("—")

    @pyqtSlot(str, str, str)
    def _on_qso_logged(self, call: str, sent: str, rcvd: str):
        self.rst_sent_label.setText(sent)
        self.rst_rcvd_label.setText(rcvd)
        self._add_table_row(call, sent, rcvd)

    def _add_table_row(self, call: str, sent: str, rcvd: str):
        from datetime import datetime, timezone
        radio = self.controller._radio
        freq  = radio.get_frequency() if radio.connected else 0.0
        mode  = radio.get_mode()      if radio.connected else "USB"
        band  = RadioInterface.freq_to_band(freq) if freq else "—"
        utc   = datetime.now(timezone.utc).strftime("%H:%Mz")
        row   = self.qso_table.rowCount()
        self.qso_table.insertRow(row)
        for col, val in enumerate([call, band, mode, sent, rcvd, utc]):
            item = QTableWidgetItem(val)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.qso_table.setItem(row, col, item)
        self.qso_table.scrollToBottom()

    @pyqtSlot(str)
    def _on_log_line(self, line: str):
        self.transcript.append(line)
        sb = self.transcript.verticalScrollBar()
        sb.setValue(sb.maximum())

    @pyqtSlot(str)
    def _on_error(self, msg: str):
        self.status_bar.showMessage(f"ERROR: {msg}")
        self.transcript.append(f'<span style="color:#f38ba8">[ERR] {msg}</span>')

    @pyqtSlot(bool)
    def _on_ptt_changed(self, active: bool):
        self._tx_meter.set_active(active)
        if active:
            self._tx_meter.set_level(0.75)
        else:
            self._tx_meter.reset()

    @pyqtSlot(float)
    def _on_rx_level(self, level: float):
        self._rx_meter.set_level(level)

    def closeEvent(self, event):
        self.controller.stop()
        self._rx_monitor.stop()
        self.rigctld_mgr.stop()
        event.accept()


# ─────────────────────────────────────────────────────────────────────── main

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("AutoQSO")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
