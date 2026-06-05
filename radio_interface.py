"""
Radio CAT interface and rigctld subprocess manager.

RigctldManager  — launches / monitors / stops rigctld as a child process.
RadioInterface  — sends hamlib commands over the rigctld TCP socket.
"""
import os
import re
import shutil
import socket
import logging
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

_SOCKET_TIMEOUT = 2.0

# ─────────────────────────────────────────────────────────────── Hamlib models
# (model_number, display_name)
HAMLIB_RIGS: list[tuple[int, str]] = [
    (1,    "Hamlib Dummy (no radio — for testing)"),
    (2,    "Hamlib NET rigctl (network)"),
    # ── Icom
    (3001, "Icom IC-706"),
    (3007, "Icom IC-706MkII"),
    (3008, "Icom IC-706MkIIG"),
    (3011, "Icom IC-7000"),
    (3021, "Icom IC-7100"),
    (3060, "Icom IC-7200"),
    (3073, "Icom IC-7300"),
    (3076, "Icom IC-7410"),
    (3078, "Icom IC-7700"),
    (3080, "Icom IC-7610"),
    (3085, "Icom IC-9700"),
    (3086, "Icom IC-705"),
    (3090, "Icom IC-7851"),
    (3061, "Icom IC-7100"),
    (3003, "Icom IC-735"),
    (3005, "Icom IC-746"),
    (3009, "Icom IC-751A"),
    (3013, "Icom IC-756"),
    (3014, "Icom IC-756PRO"),
    (3015, "Icom IC-756PROII"),
    (3016, "Icom IC-756PROIII"),
    (3043, "Icom IC-910"),
    # ── Yaesu
    (122,  "Yaesu FT-100"),
    (128,  "Yaesu FT-450"),
    (129,  "Yaesu FT-450D"),
    (116,  "Yaesu FT-736R"),
    (224,  "Yaesu FT-817"),
    (224,  "Yaesu FT-818ND"),
    (228,  "Yaesu FT-857D"),
    (229,  "Yaesu FT-991A"),
    (227,  "Yaesu FT-897"),
    (106,  "Yaesu FT-920"),
    (920,  "Yaesu FTDX10"),
    (921,  "Yaesu FTDX101D"),
    (922,  "Yaesu FTDX101MP"),
    (923,  "Yaesu FTDX5000"),
    # ── Kenwood
    (1013, "Kenwood TS-2000"),
    (1014, "Kenwood TS-480"),
    (1020, "Kenwood TS-590S"),
    (1021, "Kenwood TS-590SG"),
    (1022, "Kenwood TS-890S"),
    (1009, "Kenwood TS-50"),
    (1011, "Kenwood TS-850"),
    (1012, "Kenwood TS-950"),
    (1015, "Kenwood TS-570"),
    # ── Elecraft
    (2051, "Elecraft K3"),
    (2052, "Elecraft KX3"),
    (2053, "Elecraft K3S"),
    (2057, "Elecraft K4"),
    (2061, "Elecraft KX2"),
    # ── FlexRadio
    (6001, "FlexRadio FLEX-6000 series"),
    # ── SDRplay / other
    (9900, "SDRplay RSP (via SDRuno)"),
    # ── TenTec
    (1701, "TenTec Orion"),
    (1702, "TenTec Orion II"),
    # ── Alinco
    (2101, "Alinco DX-SR8"),
]


def find_rigctld() -> str:
    """Find rigctld executable. Returns path or empty string if not found."""
    # 1. In PATH
    found = shutil.which("rigctld")
    if found:
        return found
    # 2. Common install locations on Windows
    candidates = [
        Path("C:/hamlib/bin/rigctld.exe"),
        Path("C:/Program Files/Hamlib/bin/rigctld.exe"),
        Path("C:/Program Files (x86)/Hamlib/bin/rigctld.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "hamlib/bin/rigctld.exe",
        # Same folder as this script (bundled)
        Path(__file__).parent / "rigctld.exe",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return ""


def list_serial_ports() -> list[str]:
    """Return available serial port names."""
    try:
        import serial.tools.list_ports
        return [p.device for p in serial.tools.list_ports.comports()]
    except ImportError:
        pass
    # Fallback: probe COM1–COM32
    ports = []
    for i in range(1, 33):
        name = f"COM{i}"
        try:
            s = socket.socket()  # dummy check — real check below
            import serial
            ser = serial.Serial(name)
            ser.close()
            ports.append(name)
        except Exception:
            pass
    return ports or [f"COM{i}" for i in range(1, 9)]


# ─────────────────────────────────────────────────────── rigctld subprocess

class RigctldManager:
    """Launches and manages a rigctld child process."""

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def build_command(self, cfg) -> list[str]:
        path = cfg.rigctld_path or find_rigctld()
        if not path:
            raise FileNotFoundError(
                "rigctld not found. Install hamlib and set the path in Settings → Radio."
            )
        cmd = [
            path,
            "-m", str(cfg.rig_model),
            "-r", cfg.rig_port,
            "-s", str(cfg.rig_baud),
            "-t", str(cfg.rigctld_port),
            "-T", cfg.rigctld_host,
        ]
        # Data bits
        cmd += ["--set-conf", f"data_bits={cfg.rig_data_bits}"]
        # Stop bits
        cmd += ["--set-conf", f"stop_bits={cfg.rig_stop_bits}"]
        # Parity
        parity_map = {"N": "None", "E": "Even", "O": "Odd"}
        cmd += ["--set-conf", f"serial_parity={parity_map.get(cfg.rig_parity, 'None')}"]
        # PTT
        if cfg.ptt_type != "NONE":
            cmd += ["-P", cfg.ptt_type]

        return cmd

    def start(self, cfg) -> tuple[bool, str]:
        """Start rigctld. Returns (success, message)."""
        with self._lock:
            if self.is_running():
                return True, "rigctld already running."
            try:
                cmd = self.build_command(cfg)
                log.info("Starting rigctld: %s", " ".join(cmd))
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                time.sleep(1.0)  # give it a moment to bind the port
                if self._proc.poll() is not None:
                    stderr = self._proc.stderr.read().decode(errors="replace")
                    return False, f"rigctld exited immediately: {stderr[:300]}"
                return True, f"rigctld started (PID {self._proc.pid})"
            except FileNotFoundError as e:
                return False, str(e)
            except Exception as e:
                return False, f"Failed to start rigctld: {e}"

    def stop(self):
        with self._lock:
            if self._proc:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=3)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
                self._proc = None

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def get_stderr_lines(self, n: int = 20) -> list[str]:
        """Read last n lines of rigctld stderr (non-blocking)."""
        if not self._proc or not self._proc.stderr:
            return []
        lines = []
        try:
            import select
            while True:
                r, _, _ = select.select([self._proc.stderr], [], [], 0)
                if not r:
                    break
                line = self._proc.stderr.readline()
                if not line:
                    break
                lines.append(line.decode(errors="replace").rstrip())
        except Exception:
            pass
        return lines[-n:]


# ─────────────────────────────────────────────────────── hamlib TCP client

class RadioInterface:
    def __init__(self, host: str = "127.0.0.1", port: int = 4532):
        self._host = host
        self._port = port
        self._sock: socket.socket | None = None
        self.connected = False

    def connect(self) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(_SOCKET_TIMEOUT)
            s.connect((self._host, self._port))
            self._sock = s
            self.connected = True
            log.info("Connected to rigctld at %s:%d", self._host, self._port)
            return True
        except OSError as e:
            log.warning("rigctld not available: %s", e)
            self.connected = False
            return False

    def disconnect(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self.connected = False

    def reconnect(self) -> bool:
        self.disconnect()
        return self.connect()

    def _cmd(self, command: str) -> str:
        if not self.connected:
            return ""
        try:
            self._sock.sendall((command + "\n").encode())
            data = b""
            while True:
                chunk = self._sock.recv(512)
                if not chunk:
                    break
                data += chunk
                if b"RPRT" in data or data.endswith(b"\n"):
                    break
            return data.decode(errors="replace").strip()
        except OSError as e:
            log.warning("rigctld cmd failed: %s", e)
            self.connected = False
            return ""

    def get_frequency(self) -> float:
        resp = self._cmd("f")
        try:
            return float(resp.splitlines()[0])
        except (ValueError, IndexError):
            return 0.0

    def get_mode(self) -> str:
        resp = self._cmd("m")
        lines = resp.splitlines()
        return lines[0].strip() if lines else "USB"

    def get_smeter(self) -> int:
        resp = self._cmd("l STRENGTH")
        for line in resp.splitlines():
            line = line.strip()
            if line.lstrip("-").isdigit():
                return int(line)
        return 0

    def get_info(self) -> dict:
        """Return dict of freq, mode, smeter in one round trip where possible."""
        return {
            "freq":   self.get_frequency(),
            "mode":   self.get_mode(),
            "smeter": self.get_smeter(),
        }

    def set_ptt(self, on: bool):
        self._cmd(f"T {'1' if on else '0'}")

    @staticmethod
    def freq_to_band(hz: float) -> str:
        bands = [
            (1_800_000,   2_000_000,   "160M"),
            (3_500_000,   4_000_000,   "80M"),
            (5_250_000,   5_450_000,   "60M"),
            (7_000_000,   7_300_000,   "40M"),
            (10_100_000,  10_150_000,  "30M"),
            (14_000_000,  14_350_000,  "20M"),
            (18_068_000,  18_168_000,  "17M"),
            (21_000_000,  21_450_000,  "15M"),
            (24_890_000,  24_990_000,  "12M"),
            (28_000_000,  29_700_000,  "10M"),
            (50_000_000,  54_000_000,  "6M"),
            (144_000_000, 148_000_000, "2M"),
            (430_000_000, 440_000_000, "70cm"),
        ]
        for lo, hi, name in bands:
            if lo <= hz <= hi:
                return name
        return "GEN"
