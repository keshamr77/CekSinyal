"""Uji penuh dengan speedtest-cli (`python -m speedtest --json --source <IP>`)."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from typing import Callable

from ..config import SpeedtestConfig
from . import METHOD_FULL, SpeedResult

log = logging.getLogger(__name__)


def build_command(source_ip: str | None) -> list[str]:
    # Lewat interpreter yang sama supaya tidak bergantung pada PATH (speedtest-cli memasang modul `speedtest`).
    cmd = [sys.executable, "-m", "speedtest", "--json", "--secure"]
    if source_ip:
        cmd += ["--source", source_ip]
    return cmd


def parse_output(stdout: str) -> tuple[float | None, float | None, float | None]:
    """(ping ms, unduh Mbps, unggah Mbps) dari keluaran JSON speedtest-cli (kecepatan dalam bit/detik)."""
    data = json.loads(stdout)

    def mbps(key: str) -> float | None:
        value = data.get(key)
        return round(float(value) / 1e6, 2) if value else None

    ping = data.get("ping")
    return (round(float(ping), 1) if ping is not None else None), mbps("download"), mbps("upload")


def run_full(
    cfg: SpeedtestConfig, source_ip: str | None, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
) -> SpeedResult:
    result = SpeedResult(METHOD_FULL, source_ip)
    try:
        proc = runner(
            build_command(source_ip), capture_output=True, text=True, timeout=cfg.full_timeout_seconds, check=False
        )
    except subprocess.TimeoutExpired:
        result.error = f"timeout {cfg.full_timeout_seconds:g} detik"
        return result
    except OSError as exc:
        result.error = f"speedtest-cli tidak bisa dijalankan: {exc}"
        return result
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        result.error = f"speedtest-cli gagal (kode {proc.returncode}): {detail[-1] if detail else '?'}"[:500]
        return result
    try:
        result.ping_ms, result.download_mbps, result.upload_mbps = parse_output(proc.stdout)
    except (ValueError, TypeError, AttributeError) as exc:
        result.error = f"keluaran speedtest-cli tidak bisa dibaca: {exc}"[:500]
        return result
    if result.download_mbps is None:
        result.error = "speedtest-cli tidak memberi kecepatan unduh"
        return result
    result.ok = True
    return result
