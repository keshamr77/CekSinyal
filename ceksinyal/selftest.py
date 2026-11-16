"""`python -m ceksinyal selftest`: cek config, kos, database, ADB/HP, tethering, Sheets, dan bot."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Callable

from . import db, netif
from .bot.telegram import TelegramClient, TelegramError, make_client
from .collector.adb import Adb, AdbError
from .collector.parser import parse_registry
from .collector.service import SDK_PROP, lte_sinr_unit, parse_sdk
from .config import Config
from .uploader.sheets import GspreadBackend, sheets_problem

log = logging.getLogger(__name__)

OK, FAIL, WARN, SKIP = "OK", "GAGAL", "PERINGATAN", "LEWATI"


@dataclass
class Check:
    name: str
    status: str
    detail: str


def _check_config(cfg: Config) -> list[Check]:
    out = []
    if cfg.source is None:
        out.append(Check("config", WARN, "config.yaml tidak ditemukan, memakai pengaturan bawaan (salin config.example.yaml)"))
    else:
        out.append(Check("config", OK, str(cfg.source)))
    if cfg.kos_is_set:
        out.append(Check("kos", OK, cfg.kos))
    else:
        out.append(Check("kos", FAIL, f"belum diisi (masih '{cfg.kos}'): isi 'kos' di config.yaml sebelum merekam data"))
    return out


def _check_database(cfg: Config) -> Check:
    try:
        conn = db.connect(cfg.database)
        try:
            with conn:  # uji tulis yang langsung dibatalkan
                conn.execute("CREATE TABLE IF NOT EXISTS _selftest (x INTEGER)")
                conn.execute("DROP TABLE _selftest")
            n = conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        return Check("database", FAIL, f"{cfg.database}: {exc}")
    return Check("database", OK, f"{cfg.database} ({n} sampel tersimpan)")


def _check_devices(cfg: Config, adb: Adb) -> list[Check]:
    try:
        exe = adb.executable()
        listed = {d.serial: d for d in adb.devices()}
    except AdbError as exc:
        return [Check("adb", FAIL, str(exc))]
    out = [Check("adb", OK, f"{exe}, {len(listed)} HP terdeteksi")]
    serials = [d.serial for d in cfg.devices]
    if not serials:
        if not listed:
            return out + [Check("HP", FAIL, "tidak ada HP terhubung dan 'devices' kosong di config")]
        out.append(Check("HP", WARN, "'devices' kosong di config: memakai HP apa saja yang terhubung, "
                                     "operator dari HP (isi devices supaya pasti)"))
        serials = list(listed)[: cfg.collector.max_devices]
    for serial in serials:
        name = f"HP {serial}"
        info = listed.get(serial)
        dev_cfg = cfg.device(serial)
        if info is None:
            out.append(Check(name, FAIL, "tidak terhubung (cek kabel dan USB debugging)"))
            continue
        if not info.usable:
            out.append(Check(name, FAIL, f"status adb '{info.state}' (buka kunci HP dan izinkan USB debugging)"))
            continue
        try:
            text = adb.dumpsys_registry(serial)
            sdk = None
            try:
                sdk = parse_sdk(adb.getprop(serial, SDK_PROP))
            except AdbError:
                pass
        except AdbError as exc:
            out.append(Check(name, FAIL, f"dumpsys gagal: {exc}"))
            continue
        unit = lte_sinr_unit(dev_cfg.sinr_unit if dev_cfg else "auto", sdk)
        reading = parse_registry(text, phone_id=dev_cfg.phone_id if dev_cfg else None, lte_sinr_unit=unit)
        operator = (dev_cfg.operator if dev_cfg and dev_cfg.operator else None) or reading.operator_alpha or "?"
        if not reading.ok:
            out.append(Check(name, FAIL, f"{operator}: parsing gagal ({reading.reason}); jalankan probe dan kirim output mentahnya"))
            continue
        detail = f"{operator}: {reading.rat}, RSRP {reading.rsrp}, SINR {reading.sinr}, band {reading.band}"
        missing = [k for k in ("cell_id", "earfcn") if getattr(reading, k) is None and reading.rat not in ("NONE",)]
        if reading.rat == "NONE":
            out.append(Check(name, WARN, f"{operator}: HP tidak mendapat layanan saat ini"))
        elif missing:
            out.append(Check(name, WARN, f"{detail}; {', '.join(missing)} tidak tersedia di HP ini "
                                         "(diagnosis handover terbatas)"))
        else:
            out.append(Check(name, OK, detail))
    return out


def _check_tethering(cfg: Config, local_ips: Callable[[], set[str]]) -> list[Check]:
    if not cfg.speedtest.enabled:
        return [Check("speedtest", SKIP, "speedtest.enabled: false di config")]
    targets = [d for d in cfg.devices if d.source_ip]
    out = [Check(f"tethering {d.serial}", WARN, "source_ip belum diisi: uji kecepatan HP ini dilewati "
                                                "(lihat: python -m ceksinyal interfaces)")
           for d in cfg.devices if not d.source_ip]
    if not cfg.devices:
        return [Check("speedtest", WARN, "'devices' kosong: uji kecepatan butuh devices[].source_ip")]
    if not targets:
        return out
    try:
        ips = local_ips()
    except Exception as exc:
        return out + [Check("tethering", FAIL, f"daftar interface tidak bisa dibaca: {exc}")]
    for d in targets:
        if d.source_ip in ips:
            out.append(Check(f"tethering {d.serial}", OK, f"IP sumber {d.source_ip} ada di laptop"))
        else:
            out.append(Check(f"tethering {d.serial}", FAIL, f"IP sumber {d.source_ip} tidak ada di laptop "
                                                            "(nyalakan USB tethering, cek dengan: python -m ceksinyal interfaces)"))
    return out


def _check_sheets(cfg: Config, backend_factory: Callable[[Config], GspreadBackend]) -> Check:
    s = cfg.sheets
    if not s.enabled or (not s.spreadsheet_id and not s.credentials_file):
        return Check("Google Sheets", SKIP, "belum dikonfigurasi (isi sheets.spreadsheet_id dan GOOGLE_CREDENTIALS_FILE)")
    problem = sheets_problem(cfg)
    if problem:
        return Check("Google Sheets", FAIL, problem)
    try:
        title = backend_factory(cfg).check()
    except ImportError:
        return Check("Google Sheets", FAIL, "paket gspread belum terpasang: pip install -r requirements.txt")
    except Exception as exc:
        return Check("Google Sheets", FAIL, f"tidak bisa membuka spreadsheet: {type(exc).__name__}: {exc}"[:300])
    return Check("Google Sheets", OK, f"spreadsheet \"{title}\" bisa dibuka")


def _check_telegram(cfg: Config, client: TelegramClient | None, send_test: bool) -> Check:
    t = cfg.telegram
    if not t.enabled or (not t.token and not t.chat_id):
        return Check("bot Telegram", SKIP, "belum dikonfigurasi (isi TELEGRAM_BOT_TOKEN dan TELEGRAM_CHAT_ID di .env)")
    if client is None:
        missing = "TELEGRAM_BOT_TOKEN" if not t.token else "TELEGRAM_CHAT_ID"
        return Check("bot Telegram", FAIL, f"{missing} belum diisi di .env")
    try:
        username = client.get_me()
        if send_test:
            client.send(f"✅ CekSinyal selftest dari kos {cfg.kos}: bot terhubung.")
    except TelegramError as exc:
        return Check("bot Telegram", FAIL, str(exc))
    suffix = "; pesan uji terkirim" if send_test else " (pakai --send-test untuk mengirim pesan uji)"
    return Check("bot Telegram", OK, f"@{username}{suffix}")


def run_checks(
    cfg: Config, adb: Adb, send_test: bool = False,
    local_ips: Callable[[], set[str]] = netif.local_ipv4,
    backend_factory: Callable[[Config], GspreadBackend] | None = None,
    client: TelegramClient | None = None,
) -> list[Check]:
    """Semua pemeriksaan. Satu pemeriksaan yang error tidak menghentikan yang lain."""
    backend_factory = backend_factory or (lambda c: GspreadBackend(c.sheets.spreadsheet_id, c.sheets.credentials_file))
    client = client or make_client(cfg)
    steps: list[tuple[str, Callable[[], list[Check] | Check]]] = [
        ("config", lambda: _check_config(cfg)),
        ("database", lambda: _check_database(cfg)),
        ("adb", lambda: _check_devices(cfg, adb)),
        ("speedtest", lambda: _check_tethering(cfg, local_ips)),
        ("Google Sheets", lambda: _check_sheets(cfg, backend_factory)),
        ("bot Telegram", lambda: _check_telegram(cfg, client, send_test)),
    ]
    checks: list[Check] = []
    for name, step in steps:
        try:
            result = step()
        except Exception as exc:
            log.exception("selftest %s error", name)
            result = Check(name, FAIL, f"error tak terduga: {exc!r}")
        checks += result if isinstance(result, list) else [result]
    return checks


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == FAIL for c in checks) else 0
