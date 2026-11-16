"""`python -m ceksinyal run`: collector, speedtest, diagnosis, uploader, dan bot dalam satu proses.

Tiap bagian berjalan di thread sendiri dengan koneksi SQLite sendiri (mode WAL), jadi uji kecepatan
yang lama tidak menunda pembacaan sinyal, dan error di satu bagian tidak mematikan bagian lain:
setiap putaran dibungkus try/except, dicatat di log, lalu dicoba lagi di putaran berikutnya.
"""

from __future__ import annotations

import logging
import signal
import sqlite3
import threading
from dataclasses import dataclass
from typing import Callable

from . import db
from .bot.telegram import Notifier, make_client
from .collector.adb import Adb
from .collector.service import Collector
from .config import Config
from .diagnosis import engine
from .speedtest.runner import SpeedtestRunner
from .uploader.sheets import make_uploader, sheets_problem

log = logging.getLogger(__name__)

Job = Callable[[], object]
RESTART_DELAY = 5.0  # jeda sebelum bagian yang gagal disiapkan ulang
JOIN_TIMEOUT = 10.0


@dataclass
class Part:
    """Satu bagian `run`. `setup(conn)` mengembalikan pekerjaan yang dipanggil tiap `interval` detik.

    Kalau `blocking`, pekerjaan itu sendiri adalah loop panjang (collector) yang berhenti lewat stop event.
    """

    name: str
    interval: float
    setup: Callable[[sqlite3.Connection], Job]
    blocking: bool = False


class Supervisor:
    def __init__(self, config: Config, parts: list[Part], stop: threading.Event | None = None) -> None:
        self.config = config
        self.parts = parts
        self.stop = stop or threading.Event()
        self.threads: list[threading.Thread] = []
        self.errors: dict[str, int] = {}

    def _work(self, part: Part) -> None:
        conn: sqlite3.Connection | None = None
        job: Job | None = None
        try:
            while not self.stop.is_set():
                try:
                    if job is None:
                        conn = db.connect(self.config.database)
                        job = part.setup(conn)
                    job()
                    if part.blocking and not self.stop.is_set():
                        log.warning("bagian %s berhenti sendiri, dijalankan ulang", part.name)
                except Exception:
                    self.errors[part.name] = self.errors.get(part.name, 0) + 1
                    log.exception("bagian %s error (bagian lain tetap jalan; dicoba lagi)", part.name)
                    # Siapkan ulang dari awal: koneksi/klien mungkin rusak.
                    job = None
                    if conn is not None:
                        try:
                            conn.close()
                        except sqlite3.Error:
                            pass
                        conn = None
                    self.stop.wait(RESTART_DELAY if part.blocking else min(part.interval, 60.0))
                    continue
                self.stop.wait(RESTART_DELAY if part.blocking else part.interval)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            log.debug("bagian %s berhenti", part.name)

    def start(self) -> None:
        for part in self.parts:
            thread = threading.Thread(target=self._work, args=(part,), name=part.name, daemon=True)
            thread.start()
            self.threads.append(thread)

    def shutdown(self) -> None:
        self.stop.set()
        for thread in self.threads:
            thread.join(JOIN_TIMEOUT)
            if thread.is_alive():
                log.warning("bagian %s belum selesai setelah %g detik (ditinggal)", thread.name, JOIN_TIMEOUT)

    def run_forever(self) -> None:
        """Jalan sampai Ctrl+C / SIGTERM / stop event, lalu berhenti rapi."""
        self.start()
        try:
            while not self.stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            log.info("dihentikan oleh pengguna (Ctrl+C), menunggu bagian yang sedang berjalan...")
        finally:
            self.shutdown()
            log.info("ceksinyal berhenti")


def build_parts(config: Config, adb: Adb, stop: threading.Event) -> tuple[list[Part], list[str]]:
    """Susun bagian yang aktif menurut config. Mengembalikan (bagian, catatan bagian yang tidak aktif)."""
    parts: list[Part] = []
    inactive: list[str] = []

    def collector(conn: sqlite3.Connection) -> Job:
        col = Collector(config, conn, adb, sleep=lambda seconds: stop.wait(seconds))
        return lambda: col.run(should_stop=stop.is_set)

    parts.append(Part("collector", config.collector.interval_seconds, collector, blocking=True))
    parts.append(Part("diagnosis", 60.0, lambda conn: lambda: engine.run_pending(conn, config.diagnosis)))

    if not config.speedtest.enabled:
        inactive.append("speedtest: speedtest.enabled: false di config")
    elif not any(d.source_ip for d in config.devices):
        inactive.append("speedtest: tidak ada devices[].source_ip di config (lihat: python -m ceksinyal interfaces)")
    else:
        parts.append(Part("speedtest", 30.0, lambda conn: SpeedtestRunner(config, conn).tick))

    problem = sheets_problem(config)
    if problem:
        inactive.append(f"uploader Sheets: {problem}")
    else:
        parts.append(Part("uploader", config.sheets.interval_minutes * 60,
                          lambda conn: make_uploader(config, conn).upload_pending))

    client = make_client(config)
    if client is None:
        why = "telegram.enabled: false di config" if not config.telegram.enabled else \
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID belum diisi di .env"
        inactive.append(f"bot Telegram: {why}")
    else:
        def bot(conn: sqlite3.Connection) -> Job:
            notifier = Notifier(config, conn, client)

            def job() -> None:
                notifier.process_events()
                notifier.maybe_daily_summary()
            return job

        parts.append(Part("bot", 30.0, bot))
    return parts, inactive


def run(config: Config, adb: Adb, stop: threading.Event | None = None) -> int:
    stop = stop or threading.Event()
    parts, inactive = build_parts(config, adb, stop)
    log.info("ceksinyal run: kos=%s, db=%s", config.kos, config.database)
    log.info("bagian aktif: %s", ", ".join(p.name for p in parts))
    for note in inactive:
        log.warning("TIDAK aktif - %s", note)
    if threading.current_thread() is threading.main_thread():
        try:  # berhenti rapi juga saat dimatikan sebagai layanan (Linux/Orange Pi)
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
        except (ValueError, OSError, AttributeError):
            pass
    Supervisor(config, parts, stop).run_forever()
    return 0
