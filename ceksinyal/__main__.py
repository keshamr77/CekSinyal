"""CLI: python -m ceksinyal <perintah>."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

from . import __version__, db, fix_band, fix_sinr
from .collector.adb import Adb, AdbError
from .collector.parser import SignalReading, parse_registry
from .collector.service import OPERATOR_PROP, Collector, RawStore, probe
from .config import Config, ConfigError, load_config

log = logging.getLogger("ceksinyal")


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]
    for row in [headers, *rows]:
        print("  ".join(str(x).ljust(w) for x, w in zip(row, widths)).rstrip())


def _fmt(value: object, unit: str = "") -> str:
    return "-" if value is None else f"{value}{unit}"


def _print_reading(title: str, reading: SignalReading, raw_path: Path | None = None) -> None:
    print(f"== {title} ==")
    status = "OK" if reading.ok else f"GAGAL: {reading.reason}"
    rat = _fmt(reading.rat)
    if reading.nr_nsa:
        rat += f" (+NR NSA, ssRsrp={_fmt(reading.nr_rsrp, ' dBm')}, ssSinr={_fmt(reading.nr_sinr, ' dB')})"
    lines = [
        ("status", status),
        ("rat", rat),
        ("rsrp", _fmt(reading.rsrp, " dBm")),
        ("rsrq", _fmt(reading.rsrq, " dB")),
        ("sinr", _fmt(reading.sinr, " dB")),
        ("rssi", _fmt(reading.rssi, " dBm")),
        ("pci", _fmt(reading.pci)),
        ("cell_id", _fmt(reading.cell_id)),
        ("earfcn", _fmt(reading.earfcn)),
        ("band", _fmt(reading.band)),
        ("layanan", {True: "dalam layanan", False: "tidak ada layanan", None: "tidak diketahui"}[reading.in_service]),
        ("phone id", f"{_fmt(reading.phone_id)} (dari {reading.sections} bagian)"),
        ("nama di HP", _fmt(reading.operator_alpha)),
    ]
    if raw_path:
        lines.append(("output mentah", str(raw_path)))
    for key, value in lines:
        print(f"  {key:<13}: {value}")
    print()


def cmd_devices(cfg: Config, adb: Adb, args: argparse.Namespace) -> int:
    try:
        listed = adb.devices()
    except AdbError as exc:
        log.error("%s", exc)
        return 1
    rows = []
    for d in listed:
        dev_cfg = cfg.device(d.serial)
        prop = "-"
        if d.usable:
            try:
                prop = adb.getprop(d.serial, OPERATOR_PROP) or "-"
            except AdbError as exc:
                log.debug("getprop %s gagal: %s", d.serial, exc)
        rows.append([
            d.serial, d.state, d.model or "-",
            (dev_cfg.operator if dev_cfg and dev_cfg.operator else "-"), prop,
        ])
    seen = {d.serial for d in listed}
    for dev_cfg in cfg.devices:
        if dev_cfg.serial not in seen:
            rows.append([dev_cfg.serial, "tidak terhubung", "-", dev_cfg.operator or "-", "-"])
    if not rows:
        print("Tidak ada HP terhubung. Cek kabel USB dan aktifkan USB debugging di HP.")
        return 0
    _print_table(["SERIAL", "STATUS", "MODEL", "OPERATOR (config)", "OPERATOR (HP)"], rows)
    states = {d.state for d in listed}
    if "unauthorized" in states:
        print("\nHP 'unauthorized': buka kunci HP dan izinkan USB debugging untuk laptop ini.")
    if "no permissions" in states:
        print("\nHP 'no permissions': atur udev rules (Linux) atau jalankan ulang adb server.")
    unconfigured = [d.serial for d in listed if d.usable and not cfg.device(d.serial)]
    if unconfigured and cfg.devices:
        print(f"\nBelum ada di config.yaml (tidak dibaca collector): {', '.join(unconfigured)}")
    return 0


def cmd_probe(cfg: Config, adb: Adb, args: argparse.Namespace) -> int:
    if args.file:
        path = Path(args.file)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.error("tidak bisa membaca %s: %s", path, exc)
            return 1
        reading = parse_registry(text, phone_id=args.phone_id)
        _print_reading(str(path), reading)
        return 0 if reading.ok else 2

    raw_store = RawStore(cfg.raw_dir)
    try:
        results = probe(cfg, adb, raw_store, serial=args.serial, phone_id=args.phone_id)
    except AdbError as exc:
        log.error("%s", exc)
        return 1
    if not results:
        print("Tidak ada HP terhubung." if not args.serial else f"HP {args.serial} tidak terhubung.")
        return 1
    code = 0
    for res in results:
        if res.error or res.reading is None:
            print(f"== {res.serial} ==\n  GAGAL: {res.error}\n")
            code = 1
            continue
        _print_reading(f"{res.serial} ({res.operator})", res.reading, res.raw_path)
        if not res.reading.ok:
            code = 2
    if code == 2:
        print("Parsing gagal untuk sebagian HP. Kirim file output mentah di atas untuk memperbaiki parser.")
    return code


def cmd_collect(cfg: Config, adb: Adb, args: argparse.Namespace) -> int:
    if args.interval is not None:
        if args.interval < 1:
            log.error("--interval minimal 1 detik")
            return 1
        cfg.collector.interval_seconds = args.interval
    if not cfg.devices:
        log.warning("'devices' kosong di config: memakai HP apa saja yang terhubung (maks %d)",
                    cfg.collector.max_devices)
    try:
        conn = db.connect(cfg.database)
    except Exception as exc:
        log.error("tidak bisa membuka database %s: %s", cfg.database, exc)
        return 1
    try:
        Collector(cfg, conn, adb).run(iterations=args.iterations)
    except KeyboardInterrupt:
        log.info("dihentikan oleh pengguna")
    finally:
        conn.close()
    return 0


def cmd_fix_sinr(cfg: Config, adb: Adb, args: argparse.Namespace) -> int:
    path = Path(args.db) if args.db else cfg.database
    try:
        result = fix_sinr.fix_sinr(path, serial=args.serial, dry_run=args.dry_run)
    except (OSError, sqlite3.Error) as exc:
        log.error("fix-sinr gagal: %s", exc)
        return 1
    print(f"Database: {path}")
    if not result.serials:
        print("Tidak ada baris LTE lama (sinr_raw kosong) yang perlu diperiksa.")
        return 0
    rows = [
        [s.serial, str(s.checked), str(s.corrected), str(s.fractional),
         _fmt(s.median_before, " dB"), _fmt(s.median_after, " dB")]
        for s in result.serials.values()
    ]
    _print_table(["SERIAL", "DIPERIKSA", "DIKOREKSI", "PECAHAN", "MEDIAN SEBELUM", "MEDIAN SESUDAH"], rows)
    print(f"\nTotal: {result.checked} baris diperiksa, {result.corrected} dikoreksi (dibagi 10).")
    suspicious = [s.serial for s in result.serials.values() if s.corrected and not s.fractional]
    if suspicious and not args.serial:
        print(
            "Peringatan: HP berikut tidak punya satu pun nilai SINR pecahan, jadi kemungkinan Android 11+ "
            f"(dB) dan tidak perlu dikoreksi: {', '.join(suspicious)}. Pertimbangkan --serial."
        )
    if args.dry_run:
        print("--dry-run: tidak ada yang ditulis.")
    elif result.written:
        print(f"Selesai. Backup: {result.backup}")
    return 0


def cmd_fix_band(cfg: Config, adb: Adb, args: argparse.Namespace) -> int:
    path = Path(args.db) if args.db else cfg.database
    try:
        result = fix_band.fix_band(path, dry_run=args.dry_run)
    except (OSError, sqlite3.Error) as exc:
        log.error("fix-band gagal: %s", exc)
        return 1
    print(f"Database: {path}")
    print(f"{result.checked} baris LTE dengan EARFCN diperiksa, {result.corrected} band tidak cocok dengan EARFCN.")
    if result.pairs:
        rows = [
            [_fmt(old), str(new), str(count)]
            for (old, new), count in sorted(result.pairs.items(), key=lambda kv: (-kv[1], str(kv[0])))
        ]
        _print_table(["BAND LAMA", "BAND BARU", "BARIS"], rows)
    if args.dry_run:
        print("--dry-run: tidak ada yang ditulis.")
    elif result.written:
        print(f"Selesai. Backup: {result.backup}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help="path config.yaml")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="tampilkan log debug")

    parser = argparse.ArgumentParser(prog="python -m ceksinyal", description="CekSinyal")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", default=None, help="path config.yaml (bawaan: ./config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true", default=False, help="tampilkan log debug")
    sub = parser.add_subparsers(dest="command", required=True, metavar="perintah")

    p = sub.add_parser("devices", parents=[common], help="daftar HP terhubung, serial, operator")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("probe", parents=[common],
                       help="baca sekali dari tiap HP, tampilkan hasil parsing, simpan output mentah")
    p.add_argument("--serial", help="hanya HP dengan serial ini")
    p.add_argument("--phone-id", type=int, help="slot SIM (HP dual-SIM)")
    p.add_argument("--file", help="parse file output dumpsys lokal, tanpa HP")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("collect", parents=[common], help="jalankan collector")
    p.add_argument("--interval", type=float, help="ganti interval (detik)")
    p.add_argument("--iterations", type=int, help="berhenti setelah N putaran (untuk uji)")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("fix-sinr", parents=[common],
                       help="koreksi SINR LTE lama yang tercatat 10x lipat (HP Android 10)")
    p.add_argument("--db", help="path database (bawaan: database di config)")
    p.add_argument("--serial", help="hanya baris dari HP dengan serial ini")
    p.add_argument("--dry-run", action="store_true", help="tampilkan ringkasan saja, tanpa menulis")
    p.set_defaults(func=cmd_fix_sinr)

    p = sub.add_parser("fix-band", parents=[common],
                       help="koreksi band LTE lama yang tidak cocok dengan EARFCN")
    p.add_argument("--db", help="path database (bawaan: database di config)")
    p.add_argument("--dry-run", action="store_true", help="tampilkan ringkasan saja, tanpa menulis")
    p.set_defaults(func=cmd_fix_band)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        log.error("config tidak valid: %s", exc)
        return 1
    adb = Adb(cfg.adb_path, timeout=cfg.collector.adb_timeout_seconds)
    try:
        return args.func(cfg, adb, args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
