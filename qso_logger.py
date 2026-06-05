"""
QSO logging: ADIF file, N1MM+ UDP broadcast, QRZ XML logbook API.
"""
import socket
import logging
import datetime
import requests
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class QSORecord:
    call: str
    rst_sent: str
    rst_rcvd: str
    freq_hz: float
    mode: str
    band: str
    my_call: str
    grid: str = ""
    name: str = ""
    dt: datetime.datetime = None

    def __post_init__(self):
        if self.dt is None:
            self.dt = datetime.datetime.utcnow()

    def to_adif(self) -> str:
        def f(tag, value):
            s = str(value)
            return f"<{tag}:{len(s)}>{s}"

        date_str = self.dt.strftime("%Y%m%d")
        time_str = self.dt.strftime("%H%M%S")
        freq_mhz = f"{self.freq_hz / 1_000_000:.4f}"

        parts = [
            f(  "CALL",     self.call),
            f(  "QSO_DATE", date_str),
            f(  "TIME_ON",  time_str),
            f(  "FREQ",     freq_mhz),
            f(  "MODE",     self.mode),
            f(  "BAND",     self.band),
            f(  "RST_SENT", self.rst_sent),
            f(  "RST_RCVD", self.rst_rcvd),
            f(  "STATION_CALLSIGN", self.my_call),
        ]
        if self.grid:
            parts.append(f("GRIDSQUARE", self.grid))
        if self.name:
            parts.append(f("NAME", self.name))
        parts.append("<EOR>")
        return " ".join(parts) + "\n"

    def to_n1mm_xml(self) -> str:
        dt = self.dt.strftime("%Y-%m-%d %H:%M:%S")
        freq_khz = int(self.freq_hz / 1000)
        return (
            f"<contactinfo>"
            f"<timestamp>{dt}</timestamp>"
            f"<mycall>{self.my_call}</mycall>"
            f"<call>{self.call}</call>"
            f"<band>{self.band}</band>"
            f"<mode>{self.mode}</mode>"
            f"<rxfreq>{freq_khz}</rxfreq>"
            f"<txfreq>{freq_khz}</txfreq>"
            f"<snt>{self.rst_sent}</snt>"
            f"<rcv>{self.rst_rcvd}</rcv>"
            f"<gridsquare>{self.grid}</gridsquare>"
            f"</contactinfo>"
        )


class QSOLogger:
    def __init__(self, adif_path: str, qrz_api_key: str = "",
                 enable_qrz: bool = False,
                 n1mm_host: str = "127.0.0.1", n1mm_port: int = 2237,
                 enable_n1mm: bool = True):
        self._adif_path = Path(adif_path)
        self._qrz_key = qrz_api_key
        self._enable_qrz = enable_qrz and bool(qrz_api_key)
        self._n1mm_host = n1mm_host
        self._n1mm_port = n1mm_port
        self._enable_n1mm = enable_n1mm
        self._ensure_adif_header()

    def _ensure_adif_header(self):
        if not self._adif_path.exists():
            self._adif_path.write_text(
                "AutoQSO Log\n"
                f"<PROGRAMID:7>AutoQSO\n"
                "<EOH>\n"
            )

    def log(self, qso: QSORecord) -> bool:
        success = True
        try:
            with self._adif_path.open("a") as f:
                f.write(qso.to_adif())
            log.info("ADIF logged: %s %s %s", qso.call, qso.band, qso.mode)
        except OSError as e:
            log.error("ADIF write failed: %s", e)
            success = False

        if self._enable_n1mm:
            self._send_n1mm(qso)

        if self._enable_qrz:
            self._send_qrz(qso)

        return success

    def _send_n1mm(self, qso: QSORecord):
        try:
            msg = qso.to_n1mm_xml().encode()
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.sendto(msg, (self._n1mm_host, self._n1mm_port))
            log.debug("N1MM UDP sent")
        except OSError as e:
            log.warning("N1MM UDP send failed: %s", e)

    def _send_qrz(self, qso: QSORecord):
        try:
            adif = qso.to_adif().strip()
            resp = requests.post(
                "https://logbook.qrz.com/api",
                data={"KEY": self._qrz_key, "ACTION": "INSERT", "ADIF": adif},
                timeout=10,
            )
            if "RESULT=OK" in resp.text or "STATUS=OK" in resp.text:
                log.info("QRZ logbook: OK")
            else:
                log.warning("QRZ logbook response: %s", resp.text[:200])
        except Exception as e:
            log.warning("QRZ logbook error: %s", e)
