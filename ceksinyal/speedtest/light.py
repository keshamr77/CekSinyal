"""Uji ringan: ping (waktu jabat tangan TCP) + unduhan kecil, keduanya dari IP sumber tethering.

"Ping" memakai waktu koneksi TCP, bukan ICMP: program `ping` berbeda opsi dan bahasa keluarannya di
tiap OS, sedangkan socket yang di-bind ke IP sumber bekerja sama di Windows, macOS, dan Linux.
"""

from __future__ import annotations

import logging
import socket
import statistics
import time
from typing import Callable

import requests
from requests.adapters import HTTPAdapter

from ..config import SpeedtestConfig
from . import METHOD_LIGHT, SpeedResult

log = logging.getLogger(__name__)

MIN_BYTES = 50_000  # unduhan lebih kecil dari ini tidak dipakai untuk menghitung kecepatan
CHUNK = 16_384


class SourceAddressAdapter(HTTPAdapter):
    """Adapter requests yang mengikat koneksi keluar ke satu IP lokal (interface tethering)."""

    def __init__(self, source_ip: str, **kwargs) -> None:
        self.source_ip = source_ip
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["source_address"] = (self.source_ip, 0)
        return super().init_poolmanager(*args, **kwargs)


def make_session(source_ip: str | None) -> requests.Session:
    session = requests.Session()
    session.trust_env = False  # jangan lewat proxy sistem: yang diukur harus jalur tethering
    if source_ip:
        adapter = SourceAddressAdapter(source_ip, max_retries=0)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
    return session


def tcp_ping(
    host: str, port: int, source_ip: str | None, count: int, timeout: float,
    connect: Callable[..., socket.socket] = socket.create_connection,
    clock: Callable[[], float] = time.perf_counter,
) -> float:
    """Median waktu koneksi TCP (ms) dari `count` percobaan. OSError kalau semuanya gagal."""
    times = []
    last_error: OSError | None = None
    for _ in range(count):
        start = clock()
        try:
            sock = connect((host, port), timeout=timeout, source_address=(source_ip, 0) if source_ip else None)
        except OSError as exc:
            last_error = exc
            continue
        times.append((clock() - start) * 1000)
        sock.close()
    if not times:
        raise last_error or OSError("ping gagal")
    return round(statistics.median(times), 1)


def download(
    url: str, source_ip: str | None, timeout: float,
    session_factory: Callable[[str | None], requests.Session] = make_session,
    clock: Callable[[], float] = time.perf_counter,
) -> float:
    """Kecepatan unduh (Mbps) dari satu file kecil. Waktu dihitung sejak header respons diterima.

    Kalau batas waktu tercapai sebelum file selesai (sinyal buruk), kecepatan dihitung dari byte
    yang sudah diterima; itu tetap pengukuran yang sah.
    """
    session = session_factory(source_ip)
    try:
        with session.get(url, stream=True, timeout=timeout) as resp:
            resp.raise_for_status()
            start = clock()
            total = 0
            for chunk in resp.iter_content(CHUNK):
                total += len(chunk)
                if clock() - start > timeout:
                    break
            elapsed = clock() - start
    finally:
        session.close()
    if total < MIN_BYTES:
        raise ValueError(f"unduhan terlalu kecil ({total} byte)")
    if elapsed <= 0:
        raise ValueError("waktu unduh tidak terukur")
    return round(total * 8 / elapsed / 1e6, 2)


def run_light(
    cfg: SpeedtestConfig, source_ip: str | None,
    ping: Callable[..., float] = tcp_ping, fetch: Callable[..., float] = download,
) -> SpeedResult:
    """Uji ringan. ok=True kalau unduhan berhasil; ping yang gagal dicatat di error tanpa menggagalkan."""
    result = SpeedResult(METHOD_LIGHT, source_ip)
    errors = []
    try:
        result.ping_ms = ping(cfg.ping_host, cfg.ping_port, source_ip, cfg.ping_count, min(cfg.timeout_seconds, 5))
    except Exception as exc:
        errors.append(f"ping gagal: {exc}")
    try:
        result.download_mbps = fetch(cfg.download_url, source_ip, cfg.timeout_seconds)
        result.ok = True
    except Exception as exc:
        errors.append(f"unduh gagal: {exc}")
    result.error = "; ".join(errors)[:500] or None
    return result
