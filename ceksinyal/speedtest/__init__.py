"""Uji kecepatan lewat USB tethering: ringan (ping + unduhan kecil) dan penuh (speedtest-cli)."""

from __future__ import annotations

from dataclasses import dataclass

METHOD_LIGHT = "light"
METHOD_FULL = "full"


@dataclass
class SpeedResult:
    method: str
    source_ip: str | None = None
    ping_ms: float | None = None
    download_mbps: float | None = None
    upload_mbps: float | None = None
    ok: bool = False
    error: str | None = None
