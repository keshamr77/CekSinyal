"""Jadwal uji kecepatan per operator: ringan tiap 15 menit, penuh tiap 60 menit, simpan ke tabel speedtests."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from .. import db, netif
from ..config import Config
from ..diagnosis.engine import parse_ts
from . import METHOD_FULL, METHOD_LIGHT, SpeedResult
from .full import run_full
from .light import run_light

log = logging.getLogger(__name__)


@dataclass
class Target:
    serial: str
    operator: str
    source_ip: str


class SpeedtestRunner:
    def __init__(
        self,
        config: Config,
        conn: sqlite3.Connection,
        now: Callable[[], datetime] = db.utc_now,
        light: Callable[..., SpeedResult] = run_light,
        full: Callable[..., SpeedResult] = run_full,
        local_ips: Callable[[], set[str]] = netif.local_ipv4,
    ) -> None:
        self.config = config
        self.conn = conn
        self._now = now
        self._light = light
        self._full = full
        self._local_ips = local_ips
        self._warned: set[str] = set()

    # ------------------------------------------------------------ target

    def targets(self) -> list[Target]:
        """HP di config yang punya `source_ip`. Tanpa IP sumber, uji akan lewat koneksi lain (Wi-Fi laptop)
        dan hasilnya menyesatkan, jadi HP itu dilewati dengan satu peringatan."""
        out = []
        for dev in self.config.devices:
            if not dev.source_ip:
                if dev.multi_sim and any(d.source_ip for d in self.config.device_entries(dev.serial)):
                    # HP dual-SIM: tethering selalu lewat SIM data; slot lain hanya diukur sinyalnya.
                    self._warn_once(dev.key, f"HP {dev.label} ({dev.operator or '?'}): bukan SIM data, "
                                             "hanya sinyal yang diukur (uji kecepatan lewat slot lain)")
                    continue
                self._warn_once(dev.key, f"HP {dev.label}: source_ip belum diisi di config, uji kecepatan dilewati "
                                         "(lihat: python -m ceksinyal interfaces)")
                continue
            operator = dev.operator or self._operator_from_db(dev.key)
            if not operator:
                self._warn_once(dev.key, f"HP {dev.label}: operator belum diketahui, uji kecepatan dilewati")
                continue
            out.append(Target(dev.key, operator, dev.source_ip))
        if not self.config.devices:
            self._warn_once("", "'devices' kosong di config: uji kecepatan butuh devices[].source_ip")
        return out

    def _warn_once(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning("%s", message)

    def _operator_from_db(self, serial: str) -> str | None:
        row = self.conn.execute(
            "SELECT operator FROM measurements WHERE device_serial = ? AND operator IS NOT NULL "
            "ORDER BY id DESC LIMIT 1", (serial,),
        ).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------ jadwal

    def _ts(self, operator: str, method: str | None, func: str = "MAX") -> datetime | None:
        sql = f"SELECT {func}(ts) FROM speedtests WHERE kos = ? AND operator = ?"
        params = [self.config.kos, operator]
        if method:
            sql += " AND method = ?"
            params.append(method)
        row = self.conn.execute(sql, params).fetchone()
        return parse_ts(row[0]) if row and row[0] else None

    def due(self, target: Target, now: datetime) -> str | None:
        """Metode yang jatuh tempo untuk operator ini (dari waktu uji di database), atau None.

        Jadwal dihitung dari database, jadi restart tidak mengulang uji yang baru saja dijalankan.
        """
        st = self.config.speedtest
        light_gap = timedelta(minutes=st.light_interval_minutes)
        latest = self._ts(target.operator, None)
        if latest is None:
            return METHOD_LIGHT  # uji pertama selalu ringan, supaya `run` tidak langsung memakan kuota
        if st.full_interval_minutes > 0:
            last_full = self._ts(target.operator, METHOD_FULL)
            if last_full is None:
                # Uji penuh pertama: satu selang uji ringan setelah uji paling awal.
                if now - self._ts(target.operator, None, "MIN") >= light_gap:
                    return METHOD_FULL
            elif now - last_full >= timedelta(minutes=st.full_interval_minutes):
                return METHOD_FULL
        return METHOD_LIGHT if now - latest >= light_gap else None

    def tick(self) -> list[SpeedResult]:
        """Jalankan uji yang jatuh tempo. Dipanggil berkala (mis. tiap menit) oleh `run`."""
        if not self.config.speedtest.enabled:
            return []
        results = []
        for target in self.targets():
            method = self.due(target, self._now())
            if method:
                results.append(self.run_one(target, method))
        return results

    # ------------------------------------------------------------ eksekusi

    def run_one(self, target: Target, method: str, save: bool = True) -> SpeedResult:
        """Satu uji untuk satu operator. Tidak pernah melempar: kegagalan menjadi baris ok=0 + error."""
        now = self._now()
        try:
            if target.source_ip not in self._local_ips():
                result = SpeedResult(method, target.source_ip, error=(
                    f"IP sumber {target.source_ip} tidak ada di laptop (USB tethering mati atau IP berubah)"))
            else:
                run = self._full if method == METHOD_FULL else self._light
                result = run(self.config.speedtest, target.source_ip)
        except Exception as exc:
            log.exception("uji kecepatan %s gagal tak terduga", target.operator)
            result = SpeedResult(method, target.source_ip, error=f"error tak terduga: {exc!r}"[:500])
        if result.ok:
            log.info("speedtest %s %s: ping=%s ms unduh=%s Mbps unggah=%s Mbps", target.operator, method,
                     result.ping_ms, result.download_mbps, result.upload_mbps)
        else:
            log.warning("speedtest %s %s gagal: %s", target.operator, method, result.error)
        if save:
            try:
                db.insert_speedtest(self.conn, db.iso(now), self.config.kos, target.operator, result)
            except sqlite3.Error:
                log.exception("gagal menyimpan hasil speedtest %s", target.operator)
        return result
