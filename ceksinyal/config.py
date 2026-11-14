"""Baca konfigurasi dari config.yaml dan rahasia dari .env."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

DEFAULT_CONFIG_NAME = "config.yaml"
CONFIG_ENV_VAR = "CEKSINYAL_CONFIG"


class ConfigError(ValueError):
    """Isi config.yaml tidak valid."""


@dataclass
class DeviceConfig:
    serial: str
    operator: str | None = None
    phone_id: int | None = None


@dataclass
class CollectorConfig:
    interval_seconds: float = 10.0
    max_devices: int = 2
    adb_timeout_seconds: float = 15.0
    raw_dump_interval_minutes: float = 60.0


@dataclass
class Config:
    kos: str = "belum-diatur"
    database: Path = Path("data/ceksinyal.db")
    raw_dir: Path = Path("data/raw")
    adb_path: str = "adb"
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    devices: list[DeviceConfig] = field(default_factory=list)
    source: Path | None = None

    def device(self, serial: str) -> DeviceConfig | None:
        for dev in self.devices:
            if dev.serial == serial:
                return dev
        return None


def load_dotenv(path: Path) -> None:
    """Muat KEY=VALUE dari file .env ke os.environ tanpa menimpa variabel yang sudah ada."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def load_config(path: str | Path | None = None) -> Config:
    """Baca config.yaml (atau path dari argumen / CEKSINYAL_CONFIG).

    Kalau file tidak ada dan path tidak diberikan secara eksplisit, pakai nilai bawaan.
    """
    explicit = path is not None or bool(os.environ.get(CONFIG_ENV_VAR))
    cfg_path = Path(path or os.environ.get(CONFIG_ENV_VAR) or Path.cwd() / DEFAULT_CONFIG_NAME)
    cfg_path = cfg_path.expanduser()

    if not cfg_path.is_file():
        if explicit:
            raise ConfigError(f"file config tidak ditemukan: {cfg_path}")
        log.warning("%s tidak ditemukan, memakai pengaturan bawaan (salin config.example.yaml)", cfg_path)
        base = Path.cwd()
        load_dotenv(base / ".env")
        return _build({}, base, None)

    base = cfg_path.resolve().parent
    load_dotenv(base / ".env")
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{cfg_path}: YAML tidak valid: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{cfg_path}: isi config harus berupa mapping")
    return _build(data, base, cfg_path)


def _build(data: dict[str, Any], base: Path, source: Path | None) -> Config:
    cfg = Config(source=source)
    cfg.kos = str(data.get("kos") or cfg.kos)
    cfg.database = _resolve(base, data.get("database", cfg.database))
    cfg.raw_dir = _resolve(base, data.get("raw_dir", cfg.raw_dir))
    cfg.adb_path = str(data.get("adb_path") or cfg.adb_path)

    col = data.get("collector") or {}
    if not isinstance(col, dict):
        raise ConfigError("collector harus berupa mapping")
    cc = cfg.collector
    cc.interval_seconds = _number(col, "interval_seconds", cc.interval_seconds, minimum=1)
    cc.max_devices = int(_number(col, "max_devices", cc.max_devices, minimum=1))
    cc.adb_timeout_seconds = _number(col, "adb_timeout_seconds", cc.adb_timeout_seconds, minimum=1)
    cc.raw_dump_interval_minutes = _number(
        col, "raw_dump_interval_minutes", cc.raw_dump_interval_minutes, minimum=0
    )

    devices = data.get("devices") or []
    if not isinstance(devices, list):
        raise ConfigError("devices harus berupa daftar (- serial: ...)")
    for i, item in enumerate(devices):
        if not isinstance(item, dict) or not item.get("serial"):
            raise ConfigError(f"devices[{i}] harus punya 'serial'")
        phone_id = item.get("phone_id")
        if phone_id is not None and not isinstance(phone_id, int):
            raise ConfigError(f"devices[{i}].phone_id harus bilangan bulat")
        operator = item.get("operator")
        cfg.devices.append(
            DeviceConfig(
                serial=str(item["serial"]),
                operator=str(operator) if operator else None,
                phone_id=phone_id,
            )
        )
    if cfg.kos == "belum-diatur":
        log.warning("'kos' belum diisi di config; data disimpan dengan kos='%s'", cfg.kos)
    return cfg


def _resolve(base: Path, value: Any) -> Path:
    p = Path(str(value)).expanduser()
    return p if p.is_absolute() else base / p


def _number(section: dict[str, Any], key: str, default: float, minimum: float) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"collector.{key} harus angka")
    if value < minimum:
        raise ConfigError(f"collector.{key} minimal {minimum}")
    return float(value)
