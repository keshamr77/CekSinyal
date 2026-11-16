from datetime import datetime, timedelta, timezone

import pytest
import requests

from ceksinyal import db
from ceksinyal.__main__ import main
from ceksinyal.bot.telegram import Notifier, TelegramClient, TelegramError, make_client
from ceksinyal.config import Config, load_config
from ceksinyal.diagnosis import engine
from ceksinyal.diagnosis.rules import Thresholds

T0 = datetime(2026, 11, 15, 8, 0, 0, tzinfo=timezone.utc)  # 15:00 WIB
TOKEN = "123456:TOKEN-PALSU-UNTUK-TES"


class FakeResponse:
    def __init__(self, payload=None, status=200, bad_json=False):
        self.payload = payload if payload is not None else {"ok": True, "result": {"message_id": 1}}
        self.status_code = status
        self.bad_json = bad_json

    def json(self):
        if self.bad_json:
            raise ValueError("bukan json")
        return self.payload


class FakePost:
    def __init__(self):
        self.calls = []
        self.outcome = None  # None = sukses; Exception atau FakeResponse

    def __call__(self, url, json, timeout):
        self.calls.append((url, json, timeout))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome or FakeResponse()

    @property
    def texts(self):
        return [c[1]["text"] for c in self.calls]


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def advance(self, minutes):
        self.t += timedelta(minutes=minutes)


@pytest.fixture
def env(tmp_path):
    cfg = Config(kos="kos-a", database=tmp_path / "b.db")
    conn = db.connect(cfg.database)
    post = FakePost()
    clock = Clock()
    notifier = Notifier(cfg, conn, TelegramClient(TOKEN, "-100123", post=post), now=clock)
    yield cfg, conn, post, clock, notifier
    conn.close()


def event(conn, clock, type_, detail, operator="Telkomsel", age_minutes=0.0):
    return db.insert_event(conn, db.iso(clock.t - timedelta(minutes=age_minutes)), "kos-a", operator, type_, detail)


def notified(conn):
    return [r[0] for r in conn.execute("SELECT notified FROM events ORDER BY id")]


# ------------------------------------------------------------ klien

def test_client_send_payload():
    post = FakePost()
    TelegramClient(TOKEN, "-100123", timeout=7, post=post).send("halo")
    url, payload, timeout = post.calls[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage" and timeout == 7
    assert payload == {"chat_id": "-100123", "text": "halo", "disable_web_page_preview": True}


def test_client_truncates_long_text():
    post = FakePost()
    TelegramClient(TOKEN, "1", post=post).send("x" * 5000)
    assert len(post.texts[0]) < 4096


@pytest.mark.parametrize("outcome,expected", [
    (requests.ConnectionError(f"Max retries exceeded with url: /bot{TOKEN}/sendMessage"), "ConnectionError"),
    (requests.Timeout("timed out"), "Timeout"),
    (FakeResponse({"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked"}, 403), "403 Forbidden"),
    (FakeResponse(status=502, bad_json=True), "bukan JSON (HTTP 502)"),
])
def test_client_errors_never_leak_token(outcome, expected):
    post = FakePost()
    post.outcome = outcome
    with pytest.raises(TelegramError) as info:
        TelegramClient(TOKEN, "1", post=post).send("halo")
    assert expected in str(info.value) and TOKEN not in str(info.value)
    assert info.value.__cause__ is None  # exception asli (yang memuat URL) tidak ikut di traceback


def test_get_me():
    post = FakePost()
    post.outcome = FakeResponse({"ok": True, "result": {"username": "ceksinyal_bot"}})
    assert TelegramClient(TOKEN, "1", post=post).get_me() == "ceksinyal_bot"
    assert post.calls[0][0].endswith("/getMe")


# ------------------------------------------------------------ alert

def test_alert_on_diagnosis_change_with_window_details(env):
    cfg, conn, post, clock, notifier = env
    conn.execute(
        "INSERT INTO diagnoses (ts, kos, operator, window_start, window_end, n_samples, median_rsrp, median_sinr, result, advice) "
        "VALUES (?, 'kos-a', 'Telkomsel', ?, ?, 58, -97.0, -3.5, 'Interferensi', 'Coba operator lain.')",
        (db.iso(T0), db.iso(T0 - timedelta(minutes=10)), db.iso(T0)))
    conn.commit()
    event(conn, clock, db.EVENT_DIAGNOSIS_CHANGED, "Normal -> Interferensi; jendela ...")
    assert notifier.process_events() == {"sent": 1, "suppressed": 0, "skipped": 0, "failed": 0}
    text = post.texts[0]
    assert "kos-a" in text and "Telkomsel: Normal → Interferensi" in text
    assert "Jendela 14:50–15:00 WIB, 58 sampel" in text
    assert "Median RSRP −97 dBm, SINR −3,5 dB" in text and "Saran: Coba operator lain." in text
    assert notified(conn) == [1]
    assert notifier.process_events()["sent"] == 0 and len(post.calls) == 1  # tidak dikirim ulang


def test_signal_lost_and_collector_error_alerts(env):
    cfg, conn, post, clock, notifier = env
    event(conn, clock, db.EVENT_SIGNAL_LOST, "Normal -> Sinyal putus; jendela ...")
    event(conn, clock, db.EVENT_COLLECTOR_ERROR, "HP R58M terputus: tidak terdeteksi oleh adb", operator="Tri")
    event(conn, clock, db.EVENT_COLLECTOR_ERROR, "adb tidak bisa dipakai", operator=None)
    assert notifier.process_events()["sent"] == 3
    assert "Telkomsel: SINYAL PUTUS (sebelumnya Normal)" in post.texts[0]
    assert "Tri: gangguan alat ukur" in post.texts[1] and "HP R58M terputus" in post.texts[1]
    assert "(semua HP): gangguan alat ukur" in post.texts[2]


def test_recovery_and_daily_marker_are_not_alerted(env):
    cfg, conn, post, clock, notifier = env
    event(conn, clock, db.EVENT_DIAGNOSIS_CHANGED, "Interferensi -> Normal; jendela ...")
    db.insert_event(conn, db.iso(clock.t), "kos-a", None, db.EVENT_DAILY_SUMMARY, "2026-11-14", notified=1)
    assert notifier.process_events() == {"sent": 0, "suppressed": 0, "skipped": 1, "failed": 0}
    assert post.calls == [] and notified(conn) == [1, 1]


def test_first_problem_diagnosis_is_alerted(env):
    cfg, conn, post, clock, notifier = env
    event(conn, clock, db.EVENT_DIAGNOSIS_CHANGED, "- -> Cakupan lemah; jendela ...")
    assert notifier.process_events()["sent"] == 1 and "Telkomsel: awal → Cakupan lemah" in post.texts[0]


def test_rate_limit_same_alert_per_operator_15_minutes(env):
    cfg, conn, post, clock, notifier = env
    lost = "Normal -> Sinyal putus; jendela ..."
    event(conn, clock, db.EVENT_SIGNAL_LOST, lost)
    event(conn, clock, db.EVENT_SIGNAL_LOST, lost)                        # sama, operator sama -> ditahan
    event(conn, clock, db.EVENT_SIGNAL_LOST, lost, operator="Tri")        # operator lain -> kirim
    event(conn, clock, db.EVENT_DIAGNOSIS_CHANGED, "Normal -> Interferensi; ...")   # alert lain -> kirim
    event(conn, clock, db.EVENT_DIAGNOSIS_CHANGED, "Normal -> Cakupan lemah; ...")  # hasil lain -> kirim
    event(conn, clock, db.EVENT_DIAGNOSIS_CHANGED, "Normal -> Interferensi; ...")   # sama -> ditahan
    assert notifier.process_events() == {"sent": 4, "suppressed": 2, "skipped": 0, "failed": 0}
    assert notified(conn) == [1] * 6  # yang ditahan tidak menumpuk untuk dikirim belakangan

    clock.advance(14.9)
    event(conn, clock, db.EVENT_SIGNAL_LOST, lost)
    assert notifier.process_events()["suppressed"] == 1 and len(post.calls) == 4

    clock.advance(0.2)  # 15,1 menit sejak alert pertama
    event(conn, clock, db.EVENT_SIGNAL_LOST, lost)
    assert notifier.process_events()["sent"] == 1 and len(post.calls) == 5


def test_collector_error_rate_limited_per_operator(env):
    cfg, conn, post, clock, notifier = env
    for detail in ("HP terputus", "parsing gagal", "HP terputus lagi"):
        event(conn, clock, db.EVENT_COLLECTOR_ERROR, detail)
    event(conn, clock, db.EVENT_COLLECTOR_ERROR, "HP terputus", operator="Tri")
    assert notifier.process_events() == {"sent": 2, "suppressed": 2, "skipped": 0, "failed": 0}


def test_cooldown_configurable(env):
    cfg, conn, post, clock, notifier = env
    cfg.telegram.alert_cooldown_minutes = 0
    for _ in range(3):
        event(conn, clock, db.EVENT_SIGNAL_LOST, "Normal -> Sinyal putus")
    assert notifier.process_events()["sent"] == 3


def test_send_failure_keeps_event_for_retry(env):
    cfg, conn, post, clock, notifier = env
    event(conn, clock, db.EVENT_SIGNAL_LOST, "Normal -> Sinyal putus")
    event(conn, clock, db.EVENT_COLLECTOR_ERROR, "HP terputus")
    post.outcome = requests.ConnectionError("internet mati")
    assert notifier.process_events() == {"sent": 0, "suppressed": 0, "skipped": 0, "failed": 1}
    assert notified(conn) == [0, 0] and len(post.calls) == 1  # berhenti di kegagalan pertama
    post.outcome = None
    clock.advance(1)
    assert notifier.process_events()["sent"] == 2 and notified(conn) == [1, 1]


def test_stale_events_are_dropped(env):
    cfg, conn, post, clock, notifier = env
    event(conn, clock, db.EVENT_SIGNAL_LOST, "Normal -> Sinyal putus", age_minutes=121)
    assert notifier.process_events() == {"sent": 0, "suppressed": 0, "skipped": 1, "failed": 0}
    assert post.calls == [] and notified(conn) == [1]


def test_end_to_end_from_diagnosis_engine(env):
    cfg, conn, post, clock, notifier = env

    def add(start, minutes, **fields):
        row = {"rat": "LTE", "rsrp": -90, "sinr": 10.0, "cell_id": 1, "raw_ok": 1, **fields}
        t = start
        while t < start + timedelta(minutes=minutes):
            db.insert_measurement(conn, {"ts": db.iso(t), "kos": "kos-a", "device_serial": "HP1",
                                         "operator": "Telkomsel", **row})
            t += timedelta(seconds=10)

    add(T0 - timedelta(minutes=20), 10)
    add(T0 - timedelta(minutes=10), 10, rsrp=-118)
    engine.run_pending(conn, Thresholds(), now=clock.t)
    assert notifier.process_events()["sent"] == 1
    assert "Telkomsel: Normal → Cakupan lemah" in post.texts[0] and "Median RSRP −118 dBm" in post.texts[0]
    assert "Saran: Cakupan lemah" in post.texts[0]


# ------------------------------------------------------------ ringkasan harian

def fill_day(conn, clock):
    start = clock.t.astimezone(timezone(timedelta(hours=7))).replace(hour=0, minute=0, second=0).astimezone(timezone.utc)
    for i in range(60):
        ts = db.iso(start + timedelta(minutes=10, seconds=10 * i))
        db.insert_measurement(conn, {"ts": ts, "kos": "kos-a", "device_serial": "A", "operator": "Telkomsel",
                                     "rat": "LTE", "rsrp": -105 if i < 15 else -90, "sinr": 8.0, "cell_id": 1, "raw_ok": 1})
        db.insert_measurement(conn, {"ts": ts, "kos": "kos-a", "device_serial": "B", "operator": "Tri",
                                     "rat": "LTE", "rsrp": -95, "sinr": -4.0, "cell_id": 2, "raw_ok": 1})
        db.insert_measurement(conn, {"ts": ts, "kos": "kos-lain", "device_serial": "C", "operator": "XL",
                                     "rat": "LTE", "rsrp": -120, "raw_ok": 1})
    engine.run_pending(conn, Thresholds(), now=clock.t)
    db.insert_event(conn, db.iso(start + timedelta(hours=1)), "kos-a", "Tri", db.EVENT_SIGNAL_LOST, "x", notified=1)
    db.insert_event(conn, db.iso(start - timedelta(hours=1)), "kos-a", "Tri", db.EVENT_SIGNAL_LOST, "kemarin", notified=1)
    conn.execute("UPDATE events SET notified = 1")
    conn.commit()
    return start


def test_summary_text(env):
    cfg, conn, post, clock, notifier = env
    start = fill_day(conn, clock)
    text = notifier.summary_text(start, clock.t)
    assert "📊 CekSinyal · kos-a" in text and "Ringkasan 15/11 00:00–15:00 WIB" in text
    tsel, tri = text.split("\nTri\n")
    assert "Telkomsel" in tsel and "Diagnosis dominan: Normal (1 dari 1 jendela)" in tsel
    assert "Waktu RSRP < −100 dBm: 25%" in tsel and "Kejadian sinyal putus: 0" in tsel
    assert "Diagnosis dominan: Interferensi (1 dari 1 jendela)" in tri
    assert "Waktu RSRP < −100 dBm: 0%" in tri and "Kejadian sinyal putus: 1" in tri
    assert "Rekomendasi: Telkomsel." in text and "XL" not in text


def test_summary_without_data(env):
    cfg, conn, post, clock, notifier = env
    assert "Tidak ada data pengukuran" in notifier.summary_text(T0 - timedelta(hours=5), T0)


def test_daily_summary_sent_once_after_configured_time(env):
    cfg, conn, post, clock, notifier = env
    fill_day(conn, clock)
    cfg.telegram.daily_summary_time = "21:00"
    assert notifier.maybe_daily_summary() is False       # 15:00 WIB: belum waktunya
    clock.advance(6 * 60 - 1)
    assert notifier.maybe_daily_summary() is False       # 20:59
    clock.advance(1)
    assert notifier.maybe_daily_summary() is True        # 21:00
    assert "📊 CekSinyal · kos-a" in post.texts[0]
    clock.advance(30)
    assert notifier.maybe_daily_summary() is False and len(post.calls) == 1   # sudah dikirim hari ini
    # restart: penanda ada di database
    again = Notifier(cfg, conn, notifier.client, now=clock)
    assert again.maybe_daily_summary() is False
    marker = conn.execute("SELECT type, detail, notified FROM events ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(marker) == ("daily_summary", "2026-11-15", 1)
    clock.advance(24 * 60)
    assert notifier.maybe_daily_summary() is True and len(post.calls) == 2    # hari berikutnya


def test_daily_summary_retry_after_failure_and_disabled(env):
    cfg, conn, post, clock, notifier = env
    clock.advance(7 * 60)  # 22:00 WIB
    post.outcome = requests.Timeout("timeout")
    assert notifier.maybe_daily_summary() is False
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    post.outcome = None
    assert notifier.maybe_daily_summary() is True
    cfg.telegram.daily_summary_time = ""
    clock.advance(24 * 60)
    assert notifier.maybe_daily_summary() is False


# ------------------------------------------------------------ config & CLI

def test_config_telegram_from_env_only(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    p = tmp_path / "config.yaml"
    p.write_text("telegram:\n  token: JANGAN-DI-SINI\n  alert_cooldown_minutes: 30\n  daily_summary_time: \"20:30\"\n",
                 encoding="utf-8")
    cfg = load_config(p)
    assert cfg.telegram.token == "" and not cfg.telegram.active and make_client(cfg) is None
    assert (cfg.telegram.alert_cooldown_minutes, cfg.telegram.daily_summary_time) == (30, "20:30")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123")
    cfg = load_config(p)
    assert cfg.telegram.active and TOKEN not in repr(cfg)
    assert make_client(cfg).chat_id == "-100123"
    p.write_text("telegram:\n  daily_summary_time: jam sembilan\n", encoding="utf-8")
    from ceksinyal.config import ConfigError
    with pytest.raises(ConfigError):
        load_config(p)


def test_cli_summary(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("CEKSINYAL_CONFIG", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "config.yaml").write_text("kos: kos-a\ndatabase: s.db\n", encoding="utf-8")
    conn = db.connect(tmp_path / "s.db")
    db.insert_measurement(conn, {"ts": db.iso(db.utc_now() - timedelta(minutes=5)), "kos": "kos-a",
                                 "device_serial": "A", "operator": "Telkomsel", "rat": "LTE", "rsrp": -104, "raw_ok": 1})
    conn.close()
    assert main(["summary", "--hours", "1"]) == 0
    out = capsys.readouterr().out
    assert "Telkomsel" in out and "100%" in out and "belum ada diagnosis" in out
    assert main(["summary", "--send"]) == 1  # token belum ada
