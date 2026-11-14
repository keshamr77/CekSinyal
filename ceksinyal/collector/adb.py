"""Menjalankan perintah ADB secara lintas platform."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


class AdbError(RuntimeError):
    """Perintah adb gagal (HP terputus, timeout, dsb.)."""


class AdbNotFoundError(AdbError):
    """Program adb tidak ditemukan."""


@dataclass
class DeviceInfo:
    serial: str
    state: str
    props: dict[str, str] = field(default_factory=dict)

    @property
    def model(self) -> str | None:
        return self.props.get("model")

    @property
    def usable(self) -> bool:
        return self.state == "device"


def parse_devices_output(text: str) -> list[DeviceInfo]:
    """Parse output `adb devices -l`."""
    devices: list[DeviceInfo] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("*") or line.lower().startswith("list of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state, rest = parts[0], parts[1], parts[2:]
        if state == "no" and rest and rest[0].startswith("permissions"):
            devices.append(DeviceInfo(serial, "no permissions"))
            continue
        props = dict(p.split(":", 1) for p in rest if ":" in p)
        devices.append(DeviceInfo(serial, state, props))
    return devices


def find_adb(adb_path: str = "adb") -> str:
    """Cari executable adb: path dari config, PATH, lalu lokasi SDK yang umum.

    Lokasi SDK hanya dicoba kalau adb_path bawaan ("adb"); path yang diisi sendiri harus benar.
    """
    candidate = Path(adb_path).expanduser()
    if candidate.is_file():
        return str(candidate)
    found = shutil.which(adb_path)
    if found:
        return found
    if adb_path != "adb":
        raise AdbNotFoundError(f"adb tidak ditemukan di adb_path '{adb_path}' (cek config.yaml)")
    exe = "adb.exe" if os.name == "nt" else "adb"
    roots = [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT")]
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        roots.append(str(Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk"))
    else:
        roots += [str(Path.home() / "Android" / "Sdk"), str(Path.home() / "Library" / "Android" / "sdk")]
    for root in filter(None, roots):
        path = Path(root) / "platform-tools" / exe
        if path.is_file():
            return str(path)
    raise AdbNotFoundError(
        f"adb tidak ditemukan ('{adb_path}'). Pasang Android platform-tools lalu tambahkan ke PATH "
        "atau isi adb_path di config.yaml."
    )


class Adb:
    def __init__(self, adb_path: str = "adb", timeout: float = 15.0) -> None:
        self.adb_path = adb_path
        self.timeout = timeout
        self._exe: str | None = None

    def executable(self) -> str:
        if self._exe is None:
            self._exe = find_adb(self.adb_path)
        return self._exe

    def run(self, args: list[str], serial: str | None = None, timeout: float | None = None) -> str:
        cmd = [self.executable()]
        if serial:
            cmd += ["-s", serial]
        cmd += args
        timeout = timeout or self.timeout
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        except FileNotFoundError as exc:
            self._exe = None
            raise AdbNotFoundError(f"adb tidak bisa dijalankan: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"timeout {timeout:g} detik: adb {' '.join(args)}") from exc
        except OSError as exc:
            raise AdbError(f"adb gagal dijalankan: {exc}") from exc
        stdout = _decode(proc.stdout)
        if proc.returncode != 0:
            detail = (_decode(proc.stderr).strip() or stdout.strip())[:300]
            raise AdbError(f"adb {' '.join(args)} gagal (kode {proc.returncode}): {detail}")
        return stdout

    def devices(self) -> list[DeviceInfo]:
        return parse_devices_output(self.run(["devices", "-l"]))

    def dumpsys_registry(self, serial: str) -> str:
        out = self.run(["shell", "dumpsys", "telephony.registry"], serial=serial)
        if not out.strip():
            raise AdbError("output dumpsys telephony.registry kosong")
        return out

    def getprop(self, serial: str, key: str) -> str:
        return self.run(["shell", "getprop", key], serial=serial).strip()


def _decode(data: bytes | None) -> str:
    text = (data or b"").decode("utf-8", errors="replace")
    return text.replace("\r\r\n", "\n").replace("\r\n", "\n")
