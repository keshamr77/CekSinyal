"""Loop collector: baca 1-2 HP secara berkala dan simpan ke tabel measurements."""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import db
from ..config import Config, DeviceConfig
from .adb import Adb, AdbError, AdbNotFoundError, DeviceInfo
from .parser import SINR_UNIT_DB, SINR_UNIT_DECI_DB, SINR_UNITS, SignalReading, parse_registry

log = logging.getLogger(__name__)

OPERATOR_PROP = "gsm.operator.alpha"
SDK_PROP = "ro.build.version.sdk"
# AOSP mengonversi rssnr LTE dari 0,1 dB ke dB mulai Android 11 (SDK 30).
LAST_DECI_DB_SDK = 29
# Di mode dB, |rssnr| di atas ini mustahil (rentang dB -20..30); di atas 300 juga mustahil dalam 0,1 dB.
SINR_DB_GUARD = (30, 300)
_TS_FORMAT = "%Y%m%dT%H%M%SZ"


def safe_name(serial: str) -> str:
    """Serial yang aman dipakai sebagai nama file (serial ADB lewat Wi-Fi berisi ':')."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", serial)


class RawStore:
    """Menyimpan output dumpsys mentah ke data/raw/ untuk memperbaiki parser."""

    def __init__(self, raw_dir: Path, min_interval_seconds: float = 3600) -> None:
        self.raw_dir = Path(raw_dir)
        self.min_interval_seconds = min_interval_seconds
        self._last: dict[str, datetime] = {}

    def save(self, serial: str, text: str, now: datetime, prefix: str = "") -> Path:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        stamp = now.astimezone(timezone.utc).strftime(_TS_FORMAT)
        path = self.raw_dir / f"{prefix}{safe_name(serial)}_{stamp}.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def maybe_save(self, serial: str, text: str, now: datetime) -> Path | None:
        """Simpan kalau belum ada file untuk HP ini dalam periode min_interval (juga lintas restart)."""
        last = self._last.get(serial) or self._latest_on_disk(serial)
        if last is not None and (now - last).total_seconds() < self.min_interval_seconds:
            return None
        path = self.save(serial, text, now)
        self._last[serial] = now
        return path

    def _latest_on_disk(self, serial: str) -> datetime | None:
        if not self.raw_dir.is_dir():
            return None
        pattern = re.compile(rf"^{re.escape(safe_name(serial))}_(\d{{8}}T\d{{6}}Z)\.txt$")
        latest = None
        for path in self.raw_dir.iterdir():
            m = pattern.match(path.name)
            if m:
                ts = datetime.strptime(m.group(1), _TS_FORMAT).replace(tzinfo=timezone.utc)
                latest = ts if latest is None or ts > latest else latest
        return latest


def pick_operator_alpha(prop: str | None, phone_id: int | None) -> str | None:
    """`gsm.operator.alpha` di HP dual-SIM berisi 'OpA,OpB'; ambil sesuai slot."""
    if not prop:
        return None
    parts = [p.strip() for p in prop.split(",")]
    idx = phone_id if phone_id is not None and phone_id < len(parts) else 0
    value = parts[idx] or next((p for p in parts if p), "")
    return value or None


def resolve_operator(
    serial: str, dev_cfg: DeviceConfig | None, prop: str | None, reading: SignalReading | None
) -> str:
    """Nama operator: config -> getprop -> nama dari dumpsys -> 'unknown:<serial>'."""
    if dev_cfg and dev_cfg.operator:
        return dev_cfg.operator
    phone_id = reading.phone_id if reading else (dev_cfg.phone_id if dev_cfg else None)
    return (
        pick_operator_alpha(prop, phone_id)
        or (reading.operator_alpha if reading else None)
        or f"unknown:{serial}"
    )


def parse_sdk(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def lte_sinr_unit(configured: str | None, sdk: int | None) -> str | None:
    """Satuan rssnr LTE: override config, lalu dari SDK. None kalau SDK tidak diketahui."""
    if configured in SINR_UNITS:
        return configured
    if sdk is None:
        return None
    return SINR_UNIT_DECI_DB if sdk <= LAST_DECI_DB_SDK else SINR_UNIT_DB


def _configured_unit(dev_cfg: DeviceConfig | None) -> str:
    return dev_cfg.sinr_unit if dev_cfg else "auto"


@dataclass
class _DeviceState:
    connected: bool | None = None
    prop_alpha: str | None = None
    operator: str | None = None
    sdk: int | None = None
    sdk_checked: bool = False
    sinr_unit: str | None = None  # satuan yang terakhir dipakai (untuk log perubahan)
    sinr_forced_deci: bool = False  # pengaman: HP "dB" ternyata mengirim 0,1 dB


@dataclass
class _ReadResult:
    text: str
    prop_alpha: str | None
    sdk: int | None = None
    sdk_checked: bool = False


class Collector:
    def __init__(
        self,
        config: Config,
        conn: sqlite3.Connection,
        adb: Adb,
        raw_store: RawStore | None = None,
        now: Callable[[], datetime] = db.utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.conn = conn
        self.adb = adb
        self.raw_store = raw_store or RawStore(
            config.raw_dir, config.collector.raw_dump_interval_minutes * 60
        )
        self._now = now
        self._monotonic = monotonic
        self._sleep = sleep
        self._states: dict[str, _DeviceState] = {}
        self._adb_ok: bool | None = None
        self._warned_limit = False
        self._pool: ThreadPoolExecutor | None = None

    # ------------------------------------------------------------ loop

    def run(self, iterations: int | None = None) -> None:
        interval = self.config.collector.interval_seconds
        log.info(
            "collector mulai: kos=%s, interval=%gs, maks %d HP, db=%s",
            self.config.kos, interval, self.config.collector.max_devices, self.config.database,
        )
        self._pool = ThreadPoolExecutor(
            max_workers=self.config.collector.max_devices, thread_name_prefix="adb"
        )
        count = 0
        next_tick = self._monotonic()
        try:
            while iterations is None or count < iterations:
                try:
                    self.tick()
                except Exception as exc:  # satu putaran gagal tidak boleh mematikan collector
                    log.exception("error tak terduga di collector")
                    self._event(None, f"error tak terduga di collector: {exc!r}")
                count += 1
                if iterations is not None and count >= iterations:
                    break
                next_tick += interval
                delay = next_tick - self._monotonic()
                if delay < 0:
                    log.warning("pembacaan tertinggal %.1f detik dari jadwal", -delay)
                    next_tick = self._monotonic()
                    delay = 0
                self._sleep(delay)
        finally:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
            log.info("collector berhenti")

    def tick(self) -> int:
        """Satu putaran pembacaan. Mengembalikan jumlah baris measurements yang tersimpan."""
        try:
            listed = self.adb.devices()
        except AdbError as exc:
            self._set_adb_ok(False, str(exc))
            return 0
        self._set_adb_ok(True)

        by_serial = {d.serial: d for d in listed}
        ready: list[str] = []
        for serial in self._targets(listed):
            info = by_serial.get(serial)
            if info is None:
                self._mark_disconnected(serial, "tidak terdeteksi oleh adb")
            elif not info.usable:
                self._mark_disconnected(serial, f"status adb '{info.state}'")
            else:
                ready.append(serial)
        ready = ready[: self.config.collector.max_devices]

        if self._pool is not None:
            futures = {s: self._pool.submit(self._read, s) for s in ready}
            results = {}
            for serial, fut in futures.items():
                try:
                    results[serial] = fut.result()
                except Exception as exc:
                    results[serial] = exc
        else:
            results = {}
            for serial in ready:
                try:
                    results[serial] = self._read(serial)
                except Exception as exc:
                    results[serial] = exc

        saved = 0
        for serial, result in results.items():
            if isinstance(result, Exception):
                self._mark_disconnected(serial, f"gagal membaca: {result}")
                continue
            self._mark_connected(serial)
            if self._store(serial, result):
                saved += 1
        return saved

    # ------------------------------------------------------------ pembacaan

    def _targets(self, listed: list[DeviceInfo]) -> list[str]:
        limit = self.config.collector.max_devices
        if self.config.devices:
            serials = [d.serial for d in self.config.devices]
            if len(serials) > limit and not self._warned_limit:
                log.warning("config berisi %d HP, hanya %d pertama yang dibaca", len(serials), limit)
                self._warned_limit = True
            return serials[:limit]
        # Mode otomatis: HP yang pernah terlihat tetap dipantau supaya putusnya tercatat.
        known = list(self._states)
        new = [d.serial for d in listed if d.usable and d.serial not in self._states]
        active = sum(1 for s in self._states.values() if s.connected)
        slots = max(0, limit - active)
        if len(new) > slots and not self._warned_limit:
            log.warning(
                "%d HP terhubung tetapi maks %d; isi 'devices' di config untuk memilih",
                len(new) + active, limit,
            )
            self._warned_limit = True
        return known + new[:slots]

    def _read(self, serial: str) -> _ReadResult:
        """Dijalankan di thread: hanya memanggil adb, tidak menyentuh database."""
        text = self.adb.dumpsys_registry(serial)
        state = self._states.get(serial)
        prop = state.prop_alpha if state else None
        dev_cfg = self.config.device(serial)
        if prop is None and not (dev_cfg and dev_cfg.operator):
            try:
                prop = self.adb.getprop(serial, OPERATOR_PROP) or None
            except AdbError as exc:
                log.debug("%s: getprop gagal: %s", serial, exc)
        result = _ReadResult(text, prop)
        if _configured_unit(dev_cfg) == "auto" and not (state and state.sdk_checked):
            # Kalau gagal, dicoba lagi di putaran berikutnya.
            try:
                result.sdk = parse_sdk(self.adb.getprop(serial, SDK_PROP))
                result.sdk_checked = True
            except AdbError as exc:
                log.debug("%s: getprop %s gagal: %s", serial, SDK_PROP, exc)
        return result

    def _sinr_unit(self, serial: str, dev_cfg: DeviceConfig | None, state: _DeviceState) -> str:
        configured = _configured_unit(dev_cfg)
        if configured == "auto" and state.sinr_forced_deci:
            unit, why = SINR_UNIT_DECI_DB, "pengaman: rssnr di luar rentang dB"
        else:
            unit = lte_sinr_unit(configured, state.sdk)
            if configured != "auto":
                why = "dari config"
            elif unit is None:
                # SDK belum diketahui: anggap dB; pengaman di _store memindahkan ke 0,1 dB bila perlu.
                unit, why = SINR_UNIT_DB, "SDK tidak diketahui, sementara"
            else:
                why = f"SDK {state.sdk}"
        if unit != state.sinr_unit:
            log.info("%s: satuan SINR LTE = %s (%s)", serial, unit, why)
            state.sinr_unit = unit
        return unit

    def _store(self, serial: str, result: _ReadResult) -> bool:
        now = self._now()
        dev_cfg = self.config.device(serial)
        state = self._states.setdefault(serial, _DeviceState())
        state.prop_alpha = result.prop_alpha
        if result.sdk_checked:
            state.sdk, state.sdk_checked = result.sdk, True
        phone_id = dev_cfg.phone_id if dev_cfg else None
        unit = self._sinr_unit(serial, dev_cfg, state)
        try:
            reading = parse_registry(result.text, phone_id=phone_id, lte_sinr_unit=unit)
        except Exception as exc:  # parse_registry seharusnya tidak melempar, tapi tetap dijaga
            log.exception("%s: parser error", serial)
            reading = SignalReading(reason=f"error parser: {exc!r}")
        operator = resolve_operator(serial, dev_cfg, result.prop_alpha, reading)
        state.operator = operator

        raw = reading.sinr_raw
        if (
            _configured_unit(dev_cfg) == "auto" and unit == SINR_UNIT_DB
            and reading.rat == "LTE" and reading.sinr_unit == SINR_UNIT_DB
            and raw is not None and SINR_DB_GUARD[0] < abs(raw) <= SINR_DB_GUARD[1]
        ):
            state.sinr_forced_deci = True
            detail = (
                f"HP {serial} (SDK {state.sdk if state.sdk is not None else '?'}) mengirim rssnr={raw}, "
                "di luar rentang dB; satuan SINR LTE dipindah ke 0,1 dB untuk sesi ini. "
                "Kalau ini benar, isi sinr_unit: deci_db di config."
            )
            log.warning("%s", detail)
            self._event(operator, detail)
            unit = self._sinr_unit(serial, dev_cfg, state)
            reading = parse_registry(result.text, phone_id=phone_id, lte_sinr_unit=unit)

        row = {
            "ts": db.iso(now),
            "kos": self.config.kos,
            "device_serial": serial,
            "operator": operator,
            "raw_ok": 1 if reading.ok else 0,
            **reading.measurement_fields(),
        }
        saved = True
        try:
            db.insert_measurement(self.conn, row)
        except sqlite3.Error:
            log.exception("%s: gagal menyimpan measurement", serial)
            saved = False

        if reading.ok:
            log.info(
                "%s %s: %s rsrp=%s rsrq=%s sinr=%s pci=%s cell=%s band=%s",
                serial, operator, reading.rat, reading.rsrp, reading.rsrq, reading.sinr,
                reading.pci, reading.cell_id, reading.band,
            )
        else:
            log.warning("%s %s: parsing gagal: %s", serial, operator, reading.reason)
            self._save_raw(serial, operator, result.text, reading.reason or "?", now)
        return saved

    def _save_raw(self, serial: str, operator: str, text: str, reason: str, now: datetime) -> None:
        try:
            path = self.raw_store.maybe_save(serial, text, now)
        except OSError:
            log.exception("%s: gagal menyimpan output mentah", serial)
            return
        if path is not None:
            log.warning("%s: output mentah disimpan ke %s", serial, path)
            self._event(operator, f"parsing gagal untuk HP {serial}: {reason}; output mentah: {path}")

    # ------------------------------------------------------------ status & event

    def _operator_for(self, serial: str) -> str | None:
        dev_cfg = self.config.device(serial)
        if dev_cfg and dev_cfg.operator:
            return dev_cfg.operator
        state = self._states.get(serial)
        return state.operator if state else None

    def _mark_disconnected(self, serial: str, why: str) -> None:
        state = self._states.setdefault(serial, _DeviceState())
        if state.connected is not False:
            verb = "terputus" if state.connected else "belum terhubung"
            log.warning("HP %s (%s) %s: %s", serial, self._operator_for(serial) or "?", verb, why)
            self._event(self._operator_for(serial), f"HP {serial} {verb}: {why}")
        state.connected = False
        state.prop_alpha = None  # bisa saja HP/SIM diganti; baca ulang saat tersambung lagi

    def _mark_connected(self, serial: str) -> None:
        state = self._states.setdefault(serial, _DeviceState())
        if state.connected is False:
            log.info("HP %s terhubung kembali", serial)
        elif state.connected is None:
            log.info("HP %s terhubung", serial)
        state.connected = True

    def _set_adb_ok(self, ok: bool, detail: str = "") -> None:
        if not ok and self._adb_ok is not False:
            log.error("adb tidak bisa dipakai: %s (dicoba lagi tiap putaran)", detail)
            self._event(None, f"adb tidak bisa dipakai: {detail}")
        elif ok and self._adb_ok is False:
            log.info("adb kembali normal")
        self._adb_ok = ok

    def _event(self, operator: str | None, detail: str) -> None:
        try:
            db.insert_event(
                self.conn, db.iso(self._now()), self.config.kos, operator,
                db.EVENT_COLLECTOR_ERROR, detail,
            )
        except sqlite3.Error:
            log.exception("gagal mencatat event: %s", detail)


# ---------------------------------------------------------------- probe

@dataclass
class ProbeResult:
    serial: str
    operator: str | None = None
    reading: SignalReading | None = None
    raw_path: Path | None = None
    error: str | None = None


def probe(config: Config, adb: Adb, raw_store: RawStore, serial: str | None = None,
          phone_id: int | None = None) -> list[ProbeResult]:
    """Baca sekali dari tiap HP yang terhubung, simpan output mentah, kembalikan hasil parsing.

    Melempar AdbError kalau daftar HP tidak bisa dibaca sama sekali.
    """
    results = []
    for info in adb.devices():
        if serial and info.serial != serial:
            continue
        if not info.usable:
            results.append(ProbeResult(info.serial, error=f"status adb '{info.state}'"))
            continue
        res = ProbeResult(info.serial)
        try:
            text = adb.dumpsys_registry(info.serial)
        except AdbError as exc:
            res.error = str(exc)
            results.append(res)
            continue
        try:
            res.raw_path = raw_store.save(info.serial, text, db.utc_now(), prefix="probe_")
        except OSError as exc:
            log.error("%s: gagal menyimpan output mentah: %s", info.serial, exc)
        dev_cfg = config.device(info.serial)
        pid = phone_id if phone_id is not None else (dev_cfg.phone_id if dev_cfg else None)
        sdk = None
        if _configured_unit(dev_cfg) == "auto":
            try:
                sdk = parse_sdk(adb.getprop(info.serial, SDK_PROP))
            except AdbError:
                pass
        # SDK tidak diketahui -> None: parser menebak per sampel (cukup untuk sekali baca).
        res.reading = parse_registry(
            text, phone_id=pid, lte_sinr_unit=lte_sinr_unit(_configured_unit(dev_cfg), sdk)
        )
        prop = None
        if not (dev_cfg and dev_cfg.operator):
            try:
                prop = adb.getprop(info.serial, OPERATOR_PROP)
            except AdbError:
                pass
        res.operator = resolve_operator(info.serial, dev_cfg, prop, res.reading)
        results.append(res)
    return results
