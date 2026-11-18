"""Baca konfigurasi dari config.yaml dan rahasia dari .env."""

from __future__ import annotations

import dataclasses
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from .diagnosis.rules import Thresholds

log = logging.getLogger(__name__)

DEFAULT_CONFIG_NAME = "config.yaml"
SINR_UNIT_CHOICES = ("auto", "db", "deci_db")
CONFIG_ENV_VAR = "CEKSINYAL_CONFIG"
KOS_UNSET = "belum-diatur"

ENV_TELEGRAM_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_TELEGRAM_CHAT = "TELEGRAM_CHAT_ID"
ENV_GOOGLE_CREDENTIALS = "GOOGLE_CREDENTIALS_FILE"
ENV_SPREADSHEET_ID = "GOOGLE_SPREADSHEET_ID"


class ConfigError(ValueError):
    """Isi config.yaml tidak valid."""


@dataclass
class DeviceConfig:
    serial: str
    operator: str | None = None
    phone_id: int | None = None
    sinr_unit: str = "auto"  # satuan rssnr LTE: auto (dari SDK HP), db, atau deci_db
    source_ip: str | None = None  # IP laptop di interface USB tethering HP ini (untuk uji kecepatan)
    multi_sim: bool = False  # diisi saat config dibaca: serial ini terdaftar lebih dari sekali (dua slot SIM)

    @property
    def key(self) -> str:
        """Nilai kolom `device_serial`: serial ADB, ditambah `/sim<N>` kalau HP ini dibaca dua slot SIM-nya."""
        return sim_key(self.serial, self.phone_id) if self.multi_sim else self.serial

    @property
    def label(self) -> str:
        """Nama untuk log dan pesan: serial, ditambah slot SIM kalau dua slot dibaca."""
        return f"{self.serial} SIM {self.phone_id + 1}" if self.multi_sim and self.phone_id is not None else self.serial


SIM_KEY_SEP = "/sim"


def sim_key(serial: str, phone_id: int | None) -> str:
    """`device_serial` untuk satu slot SIM: "SERIAL/sim1" (phone_id 0), "SERIAL/sim2" (phone_id 1)."""
    return f"{serial}{SIM_KEY_SEP}{(phone_id or 0) + 1}"


def split_sim_key(key: str) -> tuple[str, int | None]:
    """Kebalikan `sim_key`: ("SERIAL", 1) untuk "SERIAL/sim1"; (key, None) untuk serial biasa."""
    base, sep, slot = key.rpartition(SIM_KEY_SEP)
    if sep and base and slot.isdigit():
        return base, int(slot)
    return key, None


@dataclass
class CollectorConfig:
    interval_seconds: float = 10.0
    max_devices: int = 2
    adb_timeout_seconds: float = 15.0
    raw_dump_interval_minutes: float = 60.0


@dataclass
class SpeedtestConfig:
    enabled: bool = True
    light_interval_minutes: float = 15.0
    full_interval_minutes: float = 60.0  # 0 = uji penuh dimatikan
    ping_host: str = "1.1.1.1"
    ping_port: int = 443
    ping_count: int = 3
    download_url: str = "https://speed.cloudflare.com/__down?bytes=1000000"
    timeout_seconds: float = 30.0
    full_timeout_seconds: float = 180.0


@dataclass
class SheetsConfig:
    enabled: bool = True  # hanya aktif kalau spreadsheet_id dan file kredensial juga ada
    spreadsheet_id: str = ""
    credentials_file: Path | None = None
    interval_minutes: float = 5.0
    batch_size: int = 500
    aggregate_measurements: bool = True  # kirim rata-rata per menit, bukan tiap sampel

    @property
    def active(self) -> bool:
        return bool(self.enabled and self.spreadsheet_id and self.credentials_file)


@dataclass
class TelegramConfig:
    enabled: bool = True  # hanya aktif kalau token dan chat id ada di .env
    token: str = field(default="", repr=False)
    chat_id: str = ""
    alert_cooldown_minutes: float = 15.0
    daily_summary_time: str = "21:00"  # waktu lokal (lihat timezone_offset_hours); kosong = tanpa ringkasan
    weak_rsrp_dbm: float = -100.0
    timeout_seconds: float = 15.0

    @property
    def active(self) -> bool:
        return bool(self.enabled and self.token and self.chat_id)


DASHBOARD_MODES = ("auto", "sqlite", "sheets")


@dataclass
class DashboardConfig:
    mode: str = "auto"  # auto = sheets kalau spreadsheet dikonfigurasi, selain itu sqlite
    public: bool = True  # mode publik: nama kos selalu diganti alias
    kos_aliases: dict[str, str] = field(default_factory=dict)  # nama kos asli -> alias (mis. "Kos A")
    stale_minutes: float = 15.0      # collector dianggap diam kalau tidak ada data selama ini
    cache_ttl_seconds: int = 300     # jeda baca ulang sumber data (hemat kuota baca Sheets)
    # Batas kategori "Baik". Batas "Buruk" RSRP dan SINR memakai ambang diagnosis
    # (diagnosis.rsrp_weak_dbm, diagnosis.sinr_interference_db) supaya konsisten.
    rsrp_good_dbm: float = -100.0
    rsrq_good_db: float = -10.0
    rsrq_poor_db: float = -15.0
    sinr_good_db: float = 10.0
    download_good_mbps: float = 10.0
    download_poor_mbps: float = 2.0


@dataclass
class Config:
    kos: str = KOS_UNSET
    database: Path = Path("data/ceksinyal.db")
    raw_dir: Path = Path("data/raw")
    report_dir: Path = Path("data/reports")
    adb_path: str = "adb"
    timezone_offset_hours: float = 7.0  # WIB; dipakai untuk tampilan waktu, laporan, dan ringkasan harian
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    diagnosis: Thresholds = field(default_factory=Thresholds)
    speedtest: SpeedtestConfig = field(default_factory=SpeedtestConfig)
    sheets: SheetsConfig = field(default_factory=SheetsConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    devices: list[DeviceConfig] = field(default_factory=list)
    source: Path | None = None

    def device(self, serial: str) -> DeviceConfig | None:
        """Entri pertama untuk serial ini (HP dual-SIM yang dibaca dua slot punya dua entri: lihat device_entries)."""
        for dev in self.devices:
            if dev.serial == serial:
                return dev
        return None

    def device_entries(self, serial: str) -> list[DeviceConfig]:
        """Semua entri config untuk satu HP: satu per slot SIM yang dibaca."""
        return [dev for dev in self.devices if dev.serial == serial]

    def device_serials(self) -> list[str]:
        """Serial HP fisik di config, urut dan tanpa ganda."""
        return list(dict.fromkeys(dev.serial for dev in self.devices))

    @property
    def kos_is_set(self) -> bool:
        return bool(self.kos) and self.kos != KOS_UNSET

    @property
    def tz(self) -> timezone:
        return timezone(timedelta(hours=self.timezone_offset_hours))

    @property
    def tz_label(self) -> str:
        names = {7.0: "WIB", 8.0: "WITA", 9.0: "WIT", 0.0: "UTC"}
        return names.get(float(self.timezone_offset_hours), f"UTC{self.timezone_offset_hours:+g}")


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
    cfg.kos = str(data.get("kos") or cfg.kos).strip()
    cfg.database = _resolve(base, data.get("database", cfg.database))
    cfg.raw_dir = _resolve(base, data.get("raw_dir", cfg.raw_dir))
    cfg.report_dir = _resolve(base, data.get("report_dir", cfg.report_dir))
    cfg.adb_path = str(data.get("adb_path") or cfg.adb_path)
    cfg.timezone_offset_hours = _number(data, "timezone_offset_hours", cfg.timezone_offset_hours, -12, "", 14)

    col = _section(data, "collector")
    cc = cfg.collector
    cc.interval_seconds = _number(col, "interval_seconds", cc.interval_seconds, 1, "collector")
    cc.max_devices = int(_number(col, "max_devices", cc.max_devices, 1, "collector"))
    cc.adb_timeout_seconds = _number(col, "adb_timeout_seconds", cc.adb_timeout_seconds, 1, "collector")
    cc.raw_dump_interval_minutes = _number(
        col, "raw_dump_interval_minutes", cc.raw_dump_interval_minutes, 0, "collector"
    )

    _fill(cfg.diagnosis, _section(data, "diagnosis"), "diagnosis", minimums={
        "window_minutes": 1, "min_samples": 1, "signal_lost_fraction": 0.01, "congestion_ratio": 0.01,
        "min_speed_samples": 1, "speed_lookback_minutes": 0, "handover_changes": 1, "min_metric_samples": 1,
    })
    if cfg.diagnosis.signal_lost_fraction > 1:
        raise ConfigError("diagnosis.signal_lost_fraction maksimal 1")

    _fill(cfg.speedtest, _section(data, "speedtest"), "speedtest", minimums={
        "light_interval_minutes": 1, "full_interval_minutes": 0, "ping_port": 1, "ping_count": 1,
        "timeout_seconds": 1, "full_timeout_seconds": 10,
    })

    sheets = _section(data, "sheets")
    _fill(cfg.sheets, sheets, "sheets", skip=("credentials_file",), minimums={
        "interval_minutes": 0.1, "batch_size": 1,
    })
    cfg.sheets.spreadsheet_id = (cfg.sheets.spreadsheet_id or os.environ.get(ENV_SPREADSHEET_ID, "")).strip()
    cred = sheets.get("credentials_file") or os.environ.get(ENV_GOOGLE_CREDENTIALS)
    cfg.sheets.credentials_file = _resolve(base, cred) if cred else None

    _fill(cfg.telegram, _section(data, "telegram"), "telegram", skip=("token", "chat_id"), minimums={
        "alert_cooldown_minutes": 0, "timeout_seconds": 1,
    })
    # Rahasia hanya dari lingkungan/.env, tidak pernah dari config.yaml.
    cfg.telegram.token = os.environ.get(ENV_TELEGRAM_TOKEN, "").strip()
    cfg.telegram.chat_id = os.environ.get(ENV_TELEGRAM_CHAT, "").strip()
    if cfg.telegram.daily_summary_time and not re.fullmatch(
        r"([01]?\d|2[0-3]):[0-5]\d", cfg.telegram.daily_summary_time
    ):
        raise ConfigError("telegram.daily_summary_time harus berformat JJ:MM (mis. \"21:00\")")

    dash = _section(data, "dashboard")
    _fill(cfg.dashboard, dash, "dashboard", skip=("kos_aliases",), minimums={
        "stale_minutes": 1, "cache_ttl_seconds": 0, "download_good_mbps": 0, "download_poor_mbps": 0,
    })
    cfg.dashboard.mode = cfg.dashboard.mode.lower() or "auto"
    if cfg.dashboard.mode not in DASHBOARD_MODES:
        raise ConfigError(f"dashboard.mode harus salah satu dari {', '.join(DASHBOARD_MODES)}")
    aliases = dash.get("kos_aliases") or {}
    if not isinstance(aliases, dict):
        raise ConfigError("dashboard.kos_aliases harus berupa mapping nama kos -> alias")
    cfg.dashboard.kos_aliases = {str(k): str(v) for k, v in aliases.items() if v}

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
        sinr_unit = str(item.get("sinr_unit") or "auto").lower()
        if sinr_unit not in SINR_UNIT_CHOICES:
            raise ConfigError(f"devices[{i}].sinr_unit harus salah satu dari {', '.join(SINR_UNIT_CHOICES)}")
        source_ip = item.get("source_ip")
        cfg.devices.append(
            DeviceConfig(
                serial=str(item["serial"]),
                operator=str(operator) if operator else None,
                phone_id=phone_id,
                sinr_unit=sinr_unit,
                source_ip=str(source_ip).strip() if source_ip else None,
            )
        )
    _check_multi_sim(cfg.devices)
    return cfg


def _check_multi_sim(devices: list[DeviceConfig]) -> None:
    """HP dual-SIM boleh ditulis dua kali (satu entri per slot), asal tiap entri punya phone_id berbeda.

    Uji kecepatan lewat tethering selalu memakai SIM data, jadi hanya satu slot per HP yang boleh punya source_ip,
    dan satu source_ip tidak boleh dipakai dua entri (hasilnya akan dicatat untuk operator yang salah).
    """
    by_serial: dict[str, list[DeviceConfig]] = {}
    for dev in devices:
        by_serial.setdefault(dev.serial, []).append(dev)
    for serial, entries in by_serial.items():
        if len(entries) == 1:
            continue
        ids = [d.phone_id for d in entries]
        if any(i is None for i in ids):
            raise ConfigError(f"HP {serial} ditulis {len(entries)} kali di devices: tiap entri wajib punya phone_id "
                              "(0 = SIM 1, 1 = SIM 2)")
        if len(set(ids)) != len(ids):
            raise ConfigError(f"HP {serial}: phone_id ganda di devices; tiap slot SIM hanya boleh ditulis sekali")
        if sum(1 for d in entries if d.source_ip) > 1:
            raise ConfigError(f"HP {serial}: source_ip hanya boleh diisi di satu slot (SIM yang dipakai untuk data "
                              "seluler); tethering selalu lewat SIM data")
        for d in entries:
            d.multi_sim = True
    seen_ip: dict[str, str] = {}
    for dev in devices:
        if dev.source_ip:
            if dev.source_ip in seen_ip:
                raise ConfigError(f"source_ip {dev.source_ip} dipakai dua entri devices ({seen_ip[dev.source_ip]} dan "
                                  f"{dev.label}); tiap IP sumber hanya mewakili satu operator")
            seen_ip[dev.source_ip] = dev.label


def _resolve(base: Path, value: Any) -> Path:
    p = Path(str(value)).expanduser()
    return p if p.is_absolute() else base / p


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    section = data.get(name) or {}
    if not isinstance(section, dict):
        raise ConfigError(f"{name} harus berupa mapping")
    return section


def _number(
    section: dict[str, Any], key: str, default: float, minimum: float, prefix: str = "collector",
    maximum: float | None = None,
) -> float:
    name = f"{prefix}.{key}" if prefix else key
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} harus angka")
    if value < minimum:
        raise ConfigError(f"{name} minimal {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} maksimal {maximum}")
    return float(value)


def _fill(
    target: Any, section: dict[str, Any], prefix: str,
    minimums: dict[str, float] | None = None, skip: tuple[str, ...] = (),
) -> None:
    """Isi dataclass dari satu bagian config, dengan tipe mengikuti nilai bawaannya."""
    minimums = minimums or {}
    names = {f.name for f in dataclasses.fields(target)} - set(skip)
    unknown = sorted(set(section) - names - set(skip))
    if unknown:
        log.warning("config: kunci tidak dikenal di %s: %s", prefix, ", ".join(unknown))
    for name in sorted(names & set(section)):
        current = getattr(target, name)
        value = section[name]
        if isinstance(current, bool):
            if not isinstance(value, bool):
                raise ConfigError(f"{prefix}.{name} harus true/false")
        elif isinstance(current, (int, float)):
            number = _number(section, name, current, minimums.get(name, float("-inf")), prefix)
            value = int(number) if isinstance(current, int) else number
            if isinstance(current, int) and number != int(number):
                raise ConfigError(f"{prefix}.{name} harus bilangan bulat")
        else:
            value = "" if value is None else str(value).strip()
        setattr(target, name, value)
