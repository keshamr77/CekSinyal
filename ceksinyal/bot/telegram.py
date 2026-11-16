"""Bot Telegram: alert dari tabel events dan ringkasan harian, lewat HTTP API langsung (requests)."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Callable

import requests

from .. import db
from ..config import Config
from ..diagnosis import engine, rules

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/{method}"
ALERT_TYPES = (db.EVENT_SIGNAL_LOST, db.EVENT_DIAGNOSIS_CHANGED, db.EVENT_COLLECTOR_ERROR)
# Event yang lebih tua dari ini tidak dikirim lagi (mis. setelah internet mati lama): alert basi hanya mengganggu.
STALE_AFTER = timedelta(hours=2)
MAX_TEXT = 3900  # batas pesan Telegram 4096 karakter


class TelegramError(RuntimeError):
    """Pesan gagal dikirim. Teks error tidak pernah memuat token."""


class TelegramClient:
    def __init__(
        self, token: str, chat_id: str, timeout: float = 15.0,
        post: Callable[..., requests.Response] = requests.post,
    ) -> None:
        self._token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self._post = post

    def _call(self, method: str, payload: dict) -> dict:
        url = API_URL.format(token=self._token, method=method)
        try:
            resp = self._post(url, json=payload, timeout=self.timeout)
            data = resp.json()
        except requests.RequestException as exc:
            # str(exc) bisa memuat URL lengkap, termasuk token: jangan diteruskan apa adanya.
            raise TelegramError(f"{method} gagal: {type(exc).__name__}: {self._scrub(str(exc))}") from None
        except ValueError:
            raise TelegramError(f"{method} gagal: jawaban bukan JSON (HTTP {resp.status_code})") from None
        if not data.get("ok"):
            raise TelegramError(
                f"{method} ditolak Telegram: {data.get('error_code', resp.status_code)} "
                f"{self._scrub(str(data.get('description', '')))}"
            )
        return data.get("result") or {}

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    def send(self, text: str) -> None:
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + "\n…"
        self._call("sendMessage", {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True})

    def get_me(self) -> str:
        """Nama pengguna bot (untuk selftest: memastikan token benar)."""
        return str(self._call("getMe", {}).get("username") or "?")


def _num(value: float | None, digits: int, unit: str) -> str:
    if value is None:
        return "–"
    return f"{value:.{digits}f}".replace("-", "−").replace(".", ",") + f" {unit}"


class Notifier:
    """Mengubah event jadi alert (dengan batas frekuensi) dan mengirim ringkasan harian."""

    def __init__(
        self, config: Config, conn: sqlite3.Connection, client: TelegramClient,
        now: Callable[[], datetime] = db.utc_now,
    ) -> None:
        self.config = config
        self.conn = conn
        self.client = client
        self._now = now
        self._last_sent: dict[tuple, datetime] = {}

    # ------------------------------------------------------------ alert

    def _local(self, dt: datetime, fmt: str = "%H:%M") -> str:
        return dt.astimezone(self.config.tz).strftime(fmt)

    def _diagnosis_lines(self, event: sqlite3.Row) -> list[str]:
        """Rincian jendela diagnosis yang memicu event (waktu event = akhir jendela)."""
        row = self.conn.execute(
            "SELECT * FROM diagnoses WHERE kos = ? AND operator = ? AND window_end = ? ORDER BY id DESC LIMIT 1",
            (event["kos"], event["operator"], event["ts"]),
        ).fetchone()
        if row is None:
            return []
        start, end = engine.parse_ts(row["window_start"]), engine.parse_ts(row["window_end"])
        lines = [
            f"Jendela {self._local(start)}–{self._local(end)} {self.config.tz_label}, {row['n_samples']} sampel",
            f"Median RSRP {_num(row['median_rsrp'], 0, 'dBm')}, SINR {_num(row['median_sinr'], 1, 'dB')}"
            + (f", unduh {_num(row['median_download'], 1, 'Mbps')}" if row["median_download"] is not None else ""),
        ]
        if row["advice"]:
            lines.append(f"Saran: {row['advice']}")
        return lines

    def _alert(self, event: sqlite3.Row) -> tuple[str, str] | None:
        """(kunci batas frekuensi, teks pesan), atau None kalau event ini tidak perlu di-alert."""
        operator = event["operator"] or "(semua HP)"
        head = f"CekSinyal · {event['kos']}"
        detail = event["detail"] or ""
        if event["type"] == db.EVENT_COLLECTOR_ERROR:
            return "collector_error", f"⚙️ {head}\n{operator}: gangguan alat ukur\n{detail}"
        old, _, new = detail.split(";")[0].partition(" -> ")
        old, new = old.strip(), new.strip()
        if event["type"] == db.EVENT_SIGNAL_LOST:
            lines = [f"🔴 {head}", f"{operator}: SINYAL PUTUS" + (f" (sebelumnya {old})" if old not in ("", "-") else "")]
            return "signal_lost", "\n".join(lines + self._diagnosis_lines(event))
        if new not in rules.PROBLEMS:
            return None  # kembali ke Normal: dicatat di events, tidak di-alert
        lines = [f"⚠️ {head}", f"{operator}: {old if old != '-' else 'awal'} → {new}"]
        return f"diagnosis:{new}", "\n".join(lines + self._diagnosis_lines(event))

    def _mark(self, event_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE events SET notified = 1 WHERE id = ?", (event_id,))

    def process_events(self) -> dict[str, int]:
        """Kirim alert untuk event notified=0. Maksimal satu alert yang sama per operator per masa jeda.

        Event ditandai notified=1 setelah terkirim, ditahan batas frekuensi, basi, atau memang bukan
        alert. Kalau pengiriman gagal, event dibiarkan 0 dan dicoba lagi di putaran berikutnya.
        """
        stats = {"sent": 0, "suppressed": 0, "skipped": 0, "failed": 0}
        now = self._now()
        cooldown = timedelta(minutes=self.config.telegram.alert_cooldown_minutes)
        marks = ", ".join("?" for _ in ALERT_TYPES)
        events = self.conn.execute(
            f"SELECT * FROM events WHERE notified = 0 AND type IN ({marks}) ORDER BY id", ALERT_TYPES
        ).fetchall()
        for event in events:
            alert = self._alert(event)
            if alert is None or now - engine.parse_ts(event["ts"]) > STALE_AFTER:
                self._mark(event["id"])
                stats["skipped"] += 1
                continue
            key = (event["kos"], event["operator"], alert[0])
            last = self._last_sent.get(key)
            if last is not None and now - last < cooldown:
                self._mark(event["id"])
                stats["suppressed"] += 1
                continue
            try:
                self.client.send(alert[1])
            except TelegramError as exc:
                log.warning("alert Telegram gagal (dicoba lagi nanti): %s", exc)
                stats["failed"] += 1
                break  # kemungkinan internet mati: sisanya menunggu putaran berikut
            self._last_sent[key] = now
            self._mark(event["id"])
            stats["sent"] += 1
        if stats["sent"] or stats["suppressed"]:
            log.info("bot: %d alert terkirim, %d ditahan batas frekuensi", stats["sent"], stats["suppressed"])
        return stats

    # ------------------------------------------------------------ ringkasan harian

    def summary_text(self, start: datetime, end: datetime) -> str:
        cfg = self.config
        weak = cfg.telegram.weak_rsrp_dbm
        lines = [f"📊 CekSinyal · {cfg.kos}",
                 f"Ringkasan {self._local(start, '%d/%m %H:%M')}–{self._local(end, '%H:%M')} {cfg.tz_label}"]
        lo, hi = db.iso(start), db.iso(end)
        summaries = {o.operator: o for o in engine.summarize_operators(self.conn, cfg.kos, start, end)}
        operators = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT operator FROM measurements WHERE kos = ? AND operator IS NOT NULL AND ts >= ? AND ts < ? "
            "ORDER BY operator", (cfg.kos, lo, hi),
        )]
        if not operators:
            lines.append("Tidak ada data pengukuran pada periode ini (collector tidak berjalan?).")
            return "\n".join(lines)
        for operator in operators:
            total, weak_n = self.conn.execute(
                "SELECT COUNT(rsrp), SUM(rsrp < ?) FROM measurements WHERE kos = ? AND operator = ? AND raw_ok = 1 "
                "AND ts >= ? AND ts < ?", (weak, cfg.kos, operator, lo, hi),
            ).fetchone()
            lost = self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE kos = ? AND operator = ? AND type = ? AND ts >= ? AND ts < ?",
                (cfg.kos, operator, db.EVENT_SIGNAL_LOST, lo, hi),
            ).fetchone()[0]
            s = summaries.get(operator)
            dominant = "belum ada diagnosis"
            if s and s.dominant:
                dominant = f"{s.dominant} ({s.counts.get(s.dominant, 0)} dari {s.windows} jendela)"
            weak_pct = f"{100 * (weak_n or 0) / total:.0f}%" if total else "–"
            lines += [
                "",
                f"{operator}",
                f"• Diagnosis dominan: {dominant}",
                f"• Waktu RSRP < {_num(weak, 0, 'dBm')}: {weak_pct}",
                f"• Kejadian sinyal putus: {lost}",
            ]
        rec = engine.recommend(self.conn, cfg.kos, start, end)
        if rec.best:
            lines += ["", f"Rekomendasi: {rec.best}. {rec.reason}"]
        return "\n".join(lines)

    def _summary_due(self, now: datetime) -> str | None:
        """Tanggal lokal (teks) yang ringkasannya harus dikirim sekarang, atau None."""
        when = self.config.telegram.daily_summary_time
        if not when:
            return None
        local = now.astimezone(self.config.tz)
        hour, minute = (int(x) for x in when.split(":"))
        if (local.hour, local.minute) < (hour, minute):
            return None
        date = local.strftime("%Y-%m-%d")
        done = self.conn.execute(
            "SELECT 1 FROM events WHERE kos = ? AND type = ? AND detail = ? LIMIT 1",
            (self.config.kos, db.EVENT_DAILY_SUMMARY, date),
        ).fetchone()
        return None if done else date

    def maybe_daily_summary(self) -> bool:
        """Kirim ringkasan hari ini (00:00 lokal sampai sekarang) sekali, setelah jam yang ditentukan."""
        now = self._now()
        date = self._summary_due(now)
        if date is None:
            return False
        local = now.astimezone(self.config.tz)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            self.client.send(self.summary_text(start, now))
        except TelegramError as exc:
            log.warning("ringkasan harian gagal dikirim (dicoba lagi nanti): %s", exc)
            return False
        # Penanda "sudah dikirim" disimpan di events supaya tidak terkirim dua kali setelah restart.
        db.insert_event(self.conn, db.iso(now), self.config.kos, None, db.EVENT_DAILY_SUMMARY, date, notified=1)
        log.info("ringkasan harian %s terkirim", date)
        return True


def make_client(config: Config) -> TelegramClient | None:
    """Klien dari token/chat ID di .env, atau None kalau bot tidak dikonfigurasi."""
    t = config.telegram
    return TelegramClient(t.token, t.chat_id, t.timeout_seconds) if t.active else None
