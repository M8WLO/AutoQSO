from dataclasses import dataclass, asdict
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"

@dataclass
class Config:
    # Station
    my_callsign: str = "M8WLO"
    my_name: str = "Operator"
    grid_square: str = "IO91"
    cq_zone: str = "14"
    itu_zone: str = "27"

    # rigctld subprocess
    rigctld_path: str = ""          # empty = auto-detect on PATH / common locations
    rigctld_host: str = "127.0.0.1"
    rigctld_port: int = 4532
    rigctld_autostart: bool = True  # launch rigctld automatically on app start

    # Radio / CAT
    rig_model: int = 1              # hamlib model number (1 = dummy/test)
    rig_port: str = "COM1"          # serial port
    rig_baud: int = 9600
    rig_data_bits: int = 8
    rig_stop_bits: str = "1"        # "1" | "2"
    rig_parity: str = "N"           # N | E | O
    rig_flow_control: str = "None"  # None | RTS/CTS | XON/XOFF
    ptt_type: str = "CAT"           # CAT | RTS | DTR | NONE

    # Audio devices  (empty = system default; partial name match OK)
    rx_device_name: str = ""
    tx_device_name: str = ""
    sample_rate: int = 16000
    vad_threshold: float = 0.015
    silence_end_sec: float = 1.8
    max_listen_sec: float = 25.0
    ptt_pre_delay_ms: int = 200
    ptt_tail_ms: int = 300

    # TTS
    tts_engine: str = "pyttsx3"
    tts_voice_index: int = 0
    tts_edge_voice: str = "en-GB-RyanNeural"
    tts_rate_wpm: int = 160
    tts_volume: float = 0.95

    # STT
    whisper_model: str = "base"
    whisper_language: str = "en"

    # Logging
    adif_log_path: str = "qso_log.adi"
    qrz_api_key: str = ""
    enable_qrz_log: bool = False
    n1mm_host: str = "127.0.0.1"
    n1mm_port: int = 2237
    enable_n1mm_udp: bool = True

    # QSO behaviour
    cq_repeat_max: int = 5
    cq_interval_sec: float = 10.0
    auto_cq_after_log: bool = False

    def save(self):
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls) -> "Config":
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text())
                return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
            except Exception:
                pass
        return cls()
