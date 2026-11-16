"""CLI: python -m ceksinyal <perintah>."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

from . import __version__, db, fix_band, fix_sinr, netif
from .speedtest import METHOD_FULL, METHOD_LIGHT
from .speedtest.runner import SpeedtestRunner
from .collector.adb import Adb, AdbError
from .collector.parser import SignalReading, parse_registry
from .collector.service import OPERATOR_PROP, Collector, RawStore, probe, safe_name
from .config import Config, ConfigError, load_config
from .diagnosis import engine

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
    warn_kos(cfg)
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


def warn_kos(cfg: Config) -> None:
    """Peringatan jelas kalau nama kos belum diisi: datanya tidak bisa dibandingkan antar-kos."""
    if not cfg.kos_is_set:
        log.warning("=" * 72)
        log.warning("'kos' BELUM DIISI di config.yaml: data akan tersimpan dengan kos='%s'.", cfg.kos)
        log.warning("Isi dulu, mis.  kos: kos-garden-house-a  lalu jalankan ulang.")
        log.warning("=" * 72)


def _db_path(cfg: Config, args: argparse.Namespace) -> Path:
    return Path(args.db) if getattr(args, "db", None) else cfg.database


def _open_existing_db(cfg: Config, args: argparse.Namespace) -> sqlite3.Connection | None:
    path = _db_path(cfg, args)
    if not path.is_file():
        log.error("database tidak ditemukan: %s", path)
        return None
    try:
        return db.connect(path)
    except sqlite3.Error as exc:
        log.error("tidak bisa membuka database %s: %s", path, exc)
        return None


def _local(cfg: Config, dt) -> str:
    return dt.astimezone(cfg.tz).strftime("%Y-%m-%d %H:%M")


def cmd_diagnose(cfg: Config, adb: Adb, args: argparse.Namespace) -> int:
    conn = _open_existing_db(cfg, args)
    if conn is None:
        return 1
    try:
        run = engine.rerun if args.rerun else engine.run_pending
        rows = run(conn, cfg.diagnosis, kos=args.kos)
        print(f"Database: {_db_path(cfg, args)}")
        if not rows:
            print("Tidak ada jendela baru untuk didiagnosis." + ("" if args.rerun else " (pakai --rerun untuk menghitung ulang)"))
        else:
            table = [
                [r.kos, r.operator, _local(cfg, r.window_start), str(r.window.n_samples),
                 _fmt(r.window.median_rsrp), _fmt(r.window.median_sinr), _fmt(r.window.median_download),
                 _fmt(r.window.cell_changes), r.window.result, r.window.detail]
                for r in rows
            ]
            _print_table(
                ["KOS", "OPERATOR", f"JENDELA ({cfg.tz_label})", "N", "RSRP", "SINR", "UNDUH", "GANTI SEL", "HASIL", "CATATAN"],
                table,
            )
            counts: dict[tuple[str, str], dict[str, int]] = {}
            for r in rows:
                c = counts.setdefault((r.kos, r.operator), {})
                c[r.window.result] = c.get(r.window.result, 0) + 1
            print()
            for (kos, operator), c in counts.items():
                parts = ", ".join(f"{name} {n}" for name, n in sorted(c.items(), key=lambda kv: -kv[1]))
                print(f"{kos} / {operator}: {sum(c.values())} jendela ({parts})")
        for kos in sorted({r[0] for r in conn.execute("SELECT DISTINCT kos FROM diagnoses")}):
            if args.kos and kos != args.kos:
                continue
            rec = engine.recommend(conn, kos)
            if rec.best:
                print(f"Rekomendasi operator di {kos}: {rec.best}. {rec.reason}")
        return 0
    finally:
        conn.close()


def cmd_interfaces(cfg: Config, adb: Adb, args: argparse.Namespace) -> int:
    try:
        interfaces = netif.list_interfaces()
    except ImportError:
        log.error("psutil belum terpasang: pip install -r requirements.txt")
        return 1
    if not interfaces:
        print("Tidak ada interface dengan alamat IPv4.")
        return 0
    used = {d.source_ip: d for d in cfg.devices if d.source_ip}
    rows = []
    for i in interfaces:
        dev = used.get(i.ipv4)
        note = i.hint
        if dev:
            note = f"dipakai di config: {dev.operator or dev.serial}" + (f" ({note})" if note else "")
        rows.append([i.name, i.ipv4, i.netmask or "-", {True: "aktif", False: "mati", None: "?"}[i.is_up], note])
    _print_table(["INTERFACE", "IPv4", "NETMASK", "STATUS", "KETERANGAN"], rows)
    missing = [f"{d.source_ip} ({d.operator or d.serial})" for d in cfg.devices
               if d.source_ip and d.source_ip not in {i.ipv4 for i in interfaces}]
    if missing:
        print(f"\nsource_ip di config yang TIDAK ada di laptop saat ini: {', '.join(missing)}")
    print(
        "\nCara mengisi: nyalakan USB tethering di HP (dan matikan Wi-Fi HP), jalankan perintah ini lagi, lalu salin\n"
        "IPv4 interface yang baru muncul ke devices[].source_ip di config.yaml. IP bisa berubah setelah HP dicabut."
    )
    return 0


def cmd_speedtest(cfg: Config, adb: Adb, args: argparse.Namespace) -> int:
    try:
        conn = db.connect(cfg.database)
    except Exception as exc:
        log.error("tidak bisa membuka database %s: %s", cfg.database, exc)
        return 1
    try:
        runner = SpeedtestRunner(cfg, conn)
        targets = runner.targets()
        if not targets:
            print("Tidak ada HP dengan source_ip di config. Lihat: python -m ceksinyal interfaces")
            return 1
        method = METHOD_FULL if args.full else METHOD_LIGHT
        rows, code = [], 0
        for target in targets:
            r = runner.run_one(target, method, save=not args.no_save)
            rows.append([target.operator, r.method, r.source_ip or "-", _fmt(r.ping_ms, " ms"),
                         _fmt(r.download_mbps, " Mbps"), _fmt(r.upload_mbps, " Mbps"),
                         "OK" if r.ok else "GAGAL", r.error or ""])
            code = code if r.ok else 2
        _print_table(["OPERATOR", "METODE", "IP SUMBER", "PING", "UNDUH", "UNGGAH", "STATUS", "CATATAN"], rows)
        return code
    finally:
        conn.close()


def _parse_day(cfg: Config, text: str | None, end: bool = False):
    """Tanggal lokal YYYY-MM-DD (atau 'YYYY-MM-DD HH:MM') -> datetime; `end` = akhir hari itu."""
    if not text:
        return None
    from datetime import datetime, timedelta
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=cfg.tz)
        except ValueError:
            continue
        return dt + timedelta(days=1) if end and fmt == "%Y-%m-%d" else dt
    raise ValueError(f"tanggal tidak dikenali: {text!r} (pakai YYYY-MM-DD)")


def cmd_report(cfg: Config, adb: Adb, args: argparse.Namespace) -> int:
    from .report import data as report_data, render

    try:
        since, until = _parse_day(cfg, args.since), _parse_day(cfg, args.until, end=True)
    except ValueError as exc:
        log.error("%s", exc)
        return 1
    conn = _open_existing_db(cfg, args)
    if conn is None:
        return 1
    try:
        known = report_data.list_kos(conn)
        if args.kos not in {k for k, _ in known}:
            log.error("kos '%s' tidak ada di database %s", args.kos, _db_path(cfg, args))
            if known:
                print("Kos yang tersedia: " + ", ".join(f"{k} ({n} sampel)" for k, n in known))
            return 1
        if not args.no_diagnose:
            engine.run_pending(conn, cfg.diagnosis, kos=args.kos)
        data = report_data.build(conn, args.kos, cfg.diagnosis, cfg.tz, cfg.tz_label, since, until)
    finally:
        conn.close()
    out_dir = Path(args.out) if args.out else cfg.report_dir
    stamp = data.generated.astimezone(cfg.tz).strftime("%Y%m%d-%H%M")
    base = f"laporan_{safe_name(args.kos)}_{stamp}"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path, html_path = out_dir / f"{base}.md", out_dir / f"{base}.html"
        md_path.write_text(render.render_markdown(data), encoding="utf-8")
        html_path.write_text(render.render_html(data), encoding="utf-8")
    except OSError as exc:
        log.error("gagal menulis laporan: %s", exc)
        return 1
    print(f"Laporan kos {args.kos}: {len(data.operators)} operator, {len(data.windows)} jendela diagnosis.")
    print(f"  Markdown: {md_path}")
    print(f"  HTML    : {html_path}")
    return 0


def sheets_problem(cfg: Config) -> str | None:
    """Alasan uploader Sheets tidak aktif, atau None kalau siap."""
    s = cfg.sheets
    if not s.enabled:
        return "sheets.enabled: false di config"
    if not s.spreadsheet_id:
        return "spreadsheet_id belum diisi (config sheets.spreadsheet_id atau GOOGLE_SPREADSHEET_ID di .env)"
    if not s.credentials_file:
        return "file kredensial belum diisi (GOOGLE_CREDENTIALS_FILE di .env)"
    if not s.credentials_file.is_file():
        return f"file kredensial tidak ditemukan: {s.credentials_file}"
    return None


def cmd_upload(cfg: Config, adb: Adb, args: argparse.Namespace) -> int:
    from .uploader.sheets import make_uploader

    problem = sheets_problem(cfg)
    if problem:
        log.error("uploader Sheets belum siap: %s", problem)
        return 1
    try:
        conn = db.connect(cfg.database)
    except Exception as exc:
        log.error("tidak bisa membuka database %s: %s", cfg.database, exc)
        return 1
    try:
        uploader = make_uploader(cfg, conn)
        sent = uploader.upload_pending()
        left = uploader.pending()
        _print_table(["TAB", "TERKIRIM (baris sheet)", "MASIH TERTUNDA (baris lokal)"],
                     [[t, "GAGAL" if n < 0 else str(n), str(left[t])] for t, n in sent.items()])
        return 1 if any(n < 0 for n in sent.values()) else 0
    finally:
        conn.close()


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

    p = sub.add_parser("diagnose", parents=[common], help="diagnosis jendela 10 menit yang belum didiagnosis")
    p.add_argument("--rerun", action="store_true",
                   help="hapus diagnosis lama dan hitung ulang semua data (setelah ambang diubah)")
    p.add_argument("--db", help="path database (bawaan: database di config)")
    p.add_argument("--kos", help="hanya kos ini (bawaan: semua kos di database)")
    p.set_defaults(func=cmd_diagnose)

    p = sub.add_parser("interfaces", parents=[common],
                       help="daftar interface jaringan dan IP (untuk mengisi source_ip tethering)")
    p.set_defaults(func=cmd_interfaces)

    p = sub.add_parser("speedtest", parents=[common], help="jalankan uji kecepatan sekali untuk tiap operator")
    p.add_argument("--full", action="store_true", help="uji penuh (speedtest-cli) alih-alih uji ringan")
    p.add_argument("--no-save", action="store_true", help="jangan simpan hasil ke database")
    p.set_defaults(func=cmd_speedtest)

    p = sub.add_parser("report", parents=[common], help="laporan bukti per kos (Markdown + HTML)")
    p.add_argument("--kos", required=True, help="nama kos (persis seperti di data)")
    p.add_argument("--db", help="path database (bawaan: database di config)")
    p.add_argument("--out", help="folder keluaran (bawaan: report_dir di config, data/reports)")
    p.add_argument("--since", help="mulai tanggal lokal YYYY-MM-DD (bawaan: semua data)")
    p.add_argument("--until", help="sampai tanggal lokal YYYY-MM-DD, termasuk hari itu")
    p.add_argument("--no-diagnose", action="store_true",
                   help="jangan mendiagnosis jendela yang belum didiagnosis sebelum membuat laporan")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("upload", parents=[common], help="kirim baris yang belum ter-upload ke Google Sheets sekarang")
    p.set_defaults(func=cmd_upload)
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
