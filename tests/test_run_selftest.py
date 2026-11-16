import threading
import time
from pathlib import Path

import pytest

from ceksinyal import db, runner, selftest
from ceksinyal.__main__ import main, warn_kos
from ceksinyal.bot.telegram import TelegramError
from ceksinyal.collector.adb import AdbError, AdbNotFoundError, DeviceInfo
from ceksinyal.config import Config, DeviceConfig
from ceksinyal.runner import Part, Supervisor

FIXTURES = Path(__file__).parent / "fixtures"
LTE_TEXT = (FIXTURES / "synthetic_lte_android10.txt").read_text(encoding="utf-8")
MASKED_TEXT = (FIXTURES / "real_redmi9c_android10.txt").read_text(encoding="utf-8", errors="replace")
UNKNOWN_TEXT = (FIXTURES / "synthetic_unknown_format.txt").read_text(encoding="utf-8")
IP = "192.168.42.129"


class FakeAdb:
    def __init__(self, devices=None, error=None):
        self.devs = devices or {}   # serial -> (state, teks dumpsys)
        self.error = error

    def executable(self):
        if self.error:
            raise self.error
        return "/usr/bin/adb"

    def devices(self):
        if self.error:
            raise self.error
        return [DeviceInfo(s, state) for s, (state, _) in self.devs.items()]

    def dumpsys_registry(self, serial):
        return self.devs[serial][1]

    def getprop(self, serial, key):
        return "30" if key.endswith("sdk") else "Telkomsel"


def wait_until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def cfg(tmp_path):
    c = Config(kos="kos-uji", database=tmp_path / "r.db", raw_dir=tmp_path / "raw", source=tmp_path / "config.yaml")
    c.devices = [DeviceConfig("HP1", operator="Telkomsel")]
    c.collector.interval_seconds = 0.02
    return c


# ------------------------------------------------------------ supervisor

def test_failing_part_does_not_stop_others(cfg, monkeypatch):
    monkeypatch.setattr(runner, "RESTART_DELAY", 0.01)
    counts = {"good": 0, "bad": 0, "setup_bad": 0}

    def good(conn):
        def job():
            counts["good"] += 1
            conn.execute("SELECT 1")
        return job

    def bad(conn):
        counts["setup_bad"] += 1

        def job():
            counts["bad"] += 1
            raise RuntimeError("bagian ini rusak")
        return job

    def broken_setup(conn):
        raise OSError("tidak bisa disiapkan")

    sup = Supervisor(cfg, [Part("baik", 0.01, good), Part("rusak", 0.01, bad), Part("gagal-siap", 0.01, broken_setup)])
    sup.start()
    assert wait_until(lambda: counts["good"] >= 5 and counts["bad"] >= 3 and sup.errors.get("gagal-siap", 0) >= 2)
    assert all(t.is_alive() for t in sup.threads)  # tidak ada thread yang mati karena error
    assert counts["setup_bad"] >= 3                # bagian yang error disiapkan ulang tiap kali
    sup.shutdown()
    assert not any(t.is_alive() for t in sup.threads)
    assert sup.errors["rusak"] == counts["bad"]


def test_stop_interrupts_long_interval_quickly(cfg):
    ran = threading.Event()
    sup = Supervisor(cfg, [Part("lambat", 3600, lambda conn: ran.set)])
    sup.start()
    assert ran.wait(2)
    start = time.monotonic()
    sup.shutdown()
    assert time.monotonic() - start < 2 and not sup.threads[0].is_alive()


def test_run_forever_stops_on_keyboard_interrupt(cfg, monkeypatch):
    stop = threading.Event()
    sup = Supervisor(cfg, [Part("x", 0.01, lambda conn: lambda: None)], stop)
    calls = {"n": 0}
    real_wait = stop.wait

    def wait(timeout=None):
        if threading.current_thread() is threading.main_thread():
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt
        return real_wait(0.01)

    monkeypatch.setattr(stop, "wait", wait)
    sup.run_forever()  # tidak melempar KeyboardInterrupt keluar
    assert stop.is_set() and not any(t.is_alive() for t in sup.threads)


# ------------------------------------------------------------ bagian yang aktif

def test_build_parts_minimal(cfg):
    parts, inactive = runner.build_parts(cfg, FakeAdb(), threading.Event())
    assert [p.name for p in parts] == ["collector", "diagnosis"]
    text = " | ".join(inactive)
    assert "speedtest: tidak ada devices[].source_ip" in text
    assert "uploader Sheets: spreadsheet_id belum diisi" in text
    assert "bot Telegram: TELEGRAM_BOT_TOKEN" in text


def test_build_parts_all(cfg, tmp_path):
    cfg.devices[0].source_ip = IP
    cred = tmp_path / "sa.json"
    cred.write_text("{}", encoding="utf-8")
    cfg.sheets.spreadsheet_id, cfg.sheets.credentials_file = "ABC", cred
    cfg.telegram.token, cfg.telegram.chat_id = "1:x", "5"
    parts, inactive = runner.build_parts(cfg, FakeAdb(), threading.Event())
    assert [p.name for p in parts] == ["collector", "diagnosis", "speedtest", "uploader", "bot"] and inactive == []
    assert parts[0].blocking and parts[3].interval == 300
    cfg.speedtest.enabled = cfg.telegram.enabled = cfg.sheets.enabled = False
    parts, inactive = runner.build_parts(cfg, FakeAdb(), threading.Event())
    assert [p.name for p in parts] == ["collector", "diagnosis"] and len(inactive) == 3


def test_run_collects_until_stopped(cfg, monkeypatch):
    """`run` utuh dengan ADB palsu: collector menyimpan data, lalu berhenti rapi saat stop."""
    monkeypatch.setattr(runner, "RESTART_DELAY", 0.01)
    adb = FakeAdb({"HP1": ("device", LTE_TEXT)})
    stop = threading.Event()
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("code", runner.run(cfg, adb, stop)))
    thread.start()

    def rows():
        conn = db.connect(cfg.database)
        try:
            return conn.execute("SELECT COUNT(*) FROM measurements WHERE operator = 'Telkomsel' AND rsrp = -98").fetchone()[0]
        finally:
            conn.close()

    assert wait_until(lambda: cfg.database.exists() and rows() >= 3)
    stop.set()
    thread.join(10)
    assert not thread.is_alive() and result["code"] == 0


def test_collector_error_does_not_kill_run(cfg, monkeypatch):
    monkeypatch.setattr(runner, "RESTART_DELAY", 0.01)
    adb = FakeAdb(error=AdbNotFoundError("adb tidak ditemukan"))
    stop = threading.Event()
    thread = threading.Thread(target=runner.run, args=(cfg, adb, stop))
    thread.start()

    def events():
        conn = db.connect(cfg.database)
        try:
            return conn.execute("SELECT COUNT(*) FROM events WHERE type = 'collector_error'").fetchone()[0]
        finally:
            conn.close()

    assert wait_until(lambda: cfg.database.exists() and events() == 1)
    assert thread.is_alive()  # masih jalan walau adb tidak ada
    stop.set()
    thread.join(10)
    assert not thread.is_alive()


# ------------------------------------------------------------ peringatan kos

def test_warn_kos(caplog):
    with caplog.at_level("WARNING"):
        warn_kos(Config(kos="kos-a"))
        assert caplog.records == []
        warn_kos(Config())
    assert any("BELUM DIISI" in r.message for r in caplog.records)


def test_collect_warns_when_kos_unset(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CEKSINYAL_CONFIG", raising=False)
    (tmp_path / "config.yaml").write_text("adb_path: /tidak/ada/adb\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert main(["collect", "--iterations", "1"]) == 0
    assert any("BELUM DIISI" in r.message for r in caplog.records)


# ------------------------------------------------------------ selftest

class FakeBackend:
    def __init__(self, error=None):
        self.error = error

    def check(self):
        if self.error:
            raise self.error
        return "CekSinyal Bersama"


class FakeClient:
    def __init__(self, error=None):
        self.error, self.sent = error, []

    def get_me(self):
        if self.error:
            raise self.error
        return "ceksinyal_bot"

    def send(self, text):
        self.sent.append(text)


def status(checks):
    return {c.name: c.status for c in checks}


def run_checks(cfg, adb, **kwargs):
    kwargs.setdefault("local_ips", lambda: {IP})
    return selftest.run_checks(cfg, adb, **kwargs)


def test_selftest_all_ok(cfg, tmp_path):
    cfg.devices[0].source_ip = IP
    cred = tmp_path / "sa.json"
    cred.write_text("{}", encoding="utf-8")
    cfg.sheets.spreadsheet_id, cfg.sheets.credentials_file = "ABC", cred
    cfg.telegram.token, cfg.telegram.chat_id = "1:x", "5"
    client = FakeClient()
    checks = run_checks(cfg, FakeAdb({"HP1": ("device", LTE_TEXT)}), backend_factory=lambda c: FakeBackend(),
                        client=client, send_test=True)
    assert set(status(checks).values()) == {"OK"}, checks
    assert selftest.exit_code(checks) == 0
    by_name = {c.name: c.detail for c in checks}
    assert "Telkomsel: LTE, RSRP -98" in by_name["HP HP1"]
    assert "CekSinyal Bersama" in by_name["Google Sheets"] and "@ceksinyal_bot" in by_name["bot Telegram"]
    assert len(client.sent) == 1 and "kos-uji" in client.sent[0]


def test_selftest_kos_unset_fails(cfg):
    cfg.kos = "belum-diatur"
    checks = run_checks(cfg, FakeAdb({"HP1": ("device", LTE_TEXT)}))
    assert status(checks)["kos"] == "GAGAL" and selftest.exit_code(checks) == 1


def test_selftest_optional_parts_skipped_not_failed(cfg):
    checks = run_checks(cfg, FakeAdb({"HP1": ("device", LTE_TEXT)}))
    s = status(checks)
    assert s["Google Sheets"] == "LEWATI" and s["bot Telegram"] == "LEWATI"
    assert s["tethering HP1"] == "PERINGATAN"
    assert selftest.exit_code(checks) == 0


def test_selftest_adb_missing(cfg):
    checks = run_checks(cfg, FakeAdb(error=AdbNotFoundError("adb tidak ditemukan")))
    assert status(checks)["adb"] == "GAGAL" and status(checks)["database"] == "OK"
    assert selftest.exit_code(checks) == 1


@pytest.mark.parametrize("devices,expected,text", [
    ({}, "GAGAL", "tidak terhubung"),
    ({"HP1": ("unauthorized", "")}, "GAGAL", "unauthorized"),
    ({"HP1": ("device", UNKNOWN_TEXT)}, "GAGAL", "parsing gagal"),
    ({"HP1": ("device", MASKED_TEXT)}, "PERINGATAN", "cell_id tidak tersedia"),
])
def test_selftest_device_states(cfg, devices, expected, text):
    checks = run_checks(cfg, FakeAdb(devices))
    check = next(c for c in checks if c.name == "HP HP1")
    assert check.status == expected and text in check.detail


def test_selftest_no_devices_in_config(cfg):
    cfg.devices = []
    checks = run_checks(cfg, FakeAdb({"X9": ("device", LTE_TEXT)}))
    s = status(checks)
    assert s["HP"] == "PERINGATAN" and s["HP X9"] == "OK"
    assert status(run_checks(cfg, FakeAdb()))["HP"] == "GAGAL"


def test_selftest_tethering_ip_missing(cfg):
    cfg.devices[0].source_ip = "10.1.1.1"
    checks = run_checks(cfg, FakeAdb({"HP1": ("device", LTE_TEXT)}))
    check = next(c for c in checks if c.name == "tethering HP1")
    assert check.status == "GAGAL" and "tidak ada di laptop" in check.detail


def test_selftest_sheets_and_bot_failures(cfg, tmp_path):
    cfg.sheets.spreadsheet_id = "ABC"  # kredensial belum ada -> setengah dikonfigurasi
    cfg.telegram.token = "1:x"         # chat id belum ada
    s = status(run_checks(cfg, FakeAdb({"HP1": ("device", LTE_TEXT)})))
    assert s["Google Sheets"] == "GAGAL" and s["bot Telegram"] == "GAGAL"

    cred = tmp_path / "sa.json"
    cred.write_text("{}", encoding="utf-8")
    cfg.sheets.credentials_file = cred
    cfg.telegram.chat_id = "5"
    checks = run_checks(cfg, FakeAdb({"HP1": ("device", LTE_TEXT)}),
                        backend_factory=lambda c: FakeBackend(PermissionError("403 spreadsheet belum dibagikan")),
                        client=FakeClient(TelegramError("getMe ditolak Telegram: 401 Unauthorized")))
    by_name = {c.name: c for c in checks}
    assert by_name["Google Sheets"].status == "GAGAL" and "403" in by_name["Google Sheets"].detail
    assert by_name["bot Telegram"].status == "GAGAL" and "401" in by_name["bot Telegram"].detail


def test_selftest_unexpected_error_in_one_check_keeps_others(cfg):
    def boom():
        raise RuntimeError("psutil rusak")

    cfg.devices[0].source_ip = IP
    checks = run_checks(cfg, FakeAdb({"HP1": ("device", LTE_TEXT)}), local_ips=boom)
    s = status(checks)
    assert s["tethering"] == "GAGAL" and s["HP HP1"] == "OK" and "bot Telegram" in s


def test_cli_selftest(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("CEKSINYAL_CONFIG", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GOOGLE_CREDENTIALS_FILE",
                "GOOGLE_SPREADSHEET_ID"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "config.yaml").write_text("adb_path: /tidak/ada/adb\ndatabase: s.db\n", encoding="utf-8")
    assert main(["selftest"]) == 1
    out = capsys.readouterr().out
    assert "kos" in out and "GAGAL" in out and "belum diisi" in out and "LEWATI" in out
    assert "pemeriksaan GAGAL" in out
