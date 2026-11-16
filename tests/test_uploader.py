import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from ceksinyal import db
from ceksinyal.__main__ import main, sheets_problem
from ceksinyal.config import Config, load_config
from ceksinyal.speedtest import SpeedResult
from ceksinyal.uploader import sheets
from ceksinyal.uploader.sheets import GspreadBackend, Uploader, make_uploader

T0 = datetime(2026, 11, 15, 8, 0, 0, tzinfo=timezone.utc)


class FakeBackend:
    def __init__(self):
        self.calls: list[tuple[str, list[str], list[list]]] = []
        self.fail: Exception | None = None
        self.fail_tabs: set[str] = set()

    def append(self, tab, headers, rows):
        if self.fail and (not self.fail_tabs or tab in self.fail_tabs):
            raise self.fail
        self.calls.append((tab, headers, [list(r) for r in rows]))

    def rows(self, tab):
        return [r for t, _, rows in self.calls if t == tab for r in rows]


@pytest.fixture
def env(tmp_path):
    cfg = Config(kos="kos-a", database=tmp_path / "u.db")
    conn = db.connect(cfg.database)
    backend = FakeBackend()
    clock = {"now": T0 + timedelta(minutes=10)}
    uploader = Uploader(cfg, conn, backend, now=lambda: clock["now"])
    yield cfg, conn, backend, uploader, clock
    conn.close()


def measure(conn, seconds, serial="HP1", operator="Telkomsel", **fields):
    row = {"rat": "LTE", "rsrp": -90, "rsrq": -10, "sinr": 8.0, "pci": 5, "cell_id": 100, "earfcn": 1850,
           "band": 3, "raw_ok": 1, **fields}
    return db.insert_measurement(conn, {"ts": db.iso(T0 + timedelta(seconds=seconds)), "kos": "kos-a",
                                        "device_serial": serial, "operator": operator, **row})


def flags(conn, table="measurements"):
    return [r[0] for r in conn.execute(f"SELECT uploaded FROM {table} ORDER BY id")]


def diagnosis(conn, i=0):
    conn.execute(
        "INSERT INTO diagnoses (ts, kos, operator, window_start, window_end, n_samples, median_rsrp, result, advice, detail) "
        "VALUES (?, 'kos-a', 'Telkomsel', ?, ?, 60, -90, 'Normal', 'ok', NULL)",
        (db.iso(T0), db.iso(T0 + timedelta(minutes=10 * i)), db.iso(T0 + timedelta(minutes=10 * i + 10))))
    conn.commit()


def test_minute_aggregation(env):
    cfg, conn, backend, uploader, clock = env
    measure(conn, 0, rsrp=-90, sinr=8.0)
    measure(conn, 10, rsrp=-100, sinr=4.0, cell_id=101)
    measure(conn, 20, rat="NONE", rsrp=None, rsrq=None, sinr=None, pci=None, cell_id=None, earfcn=None, band=None)
    measure(conn, 30, rat=None, rsrp=None, raw_ok=0)
    measure(conn, 5, serial="HP2", operator="Tri", rsrp=-80)
    measure(conn, 65, rsrp=-70)
    assert uploader.upload_pending() == {"measurements": 3, "speedtests": 0, "diagnoses": 0}
    tab, headers, rows = backend.calls[0]
    assert tab == "measurements" and headers == sheets.MINUTE_HEADERS
    assert rows[0] == ["2026-11-15T08:00:00+00:00", "kos-a", "HP1", "Telkomsel", 3, 1, 1, "LTE",
                       -95.0, -100, -10.0, 6.0, 5, 101, 1850, 3]
    assert rows[1][:5] == ["2026-11-15T08:00:00+00:00", "kos-a", "HP2", "Tri", 1]
    assert rows[2][0] == "2026-11-15T08:01:00+00:00" and rows[2][8] == -70.0
    assert flags(conn) == [1] * 6
    assert uploader.upload_pending()["measurements"] == 0 and len(backend.calls) == 1  # tidak dikirim dua kali


def test_all_no_service_minute(env):
    cfg, conn, backend, uploader, clock = env
    for s in (0, 10):
        measure(conn, s, rat="NONE", rsrp=None, rsrq=None, sinr=None, pci=None, cell_id=None, earfcn=None, band=None)
    uploader.upload_pending()
    assert backend.rows("measurements")[0][4:12] == [2, 2, 0, "NONE", "", "", "", ""]


def test_current_minute_waits(env):
    cfg, conn, backend, uploader, clock = env
    measure(conn, 0)
    measure(conn, 60)
    clock["now"] = T0 + timedelta(seconds=90)  # menit 08:01 masih berjalan
    assert uploader.upload_pending()["measurements"] == 1
    assert flags(conn) == [1, 0]
    clock["now"] = T0 + timedelta(seconds=125)
    assert uploader.upload_pending()["measurements"] == 1 and flags(conn) == [1, 1]


def test_failure_keeps_rows_pending_then_forwards(env):
    cfg, conn, backend, uploader, clock = env
    measure(conn, 0)
    db.insert_speedtest(conn, db.iso(T0), "kos-a", "Telkomsel", SpeedResult("light", "1.2.3.4", 30.0, 10.0, None, True))
    diagnosis(conn)
    backend.fail = ConnectionError("internet putus")
    sent = uploader.upload_pending()  # tidak melempar
    assert sent == {"measurements": -1} and backend.calls == []
    assert flags(conn) == [0] and flags(conn, "speedtests") == [0] and flags(conn, "diagnoses") == [0]
    assert uploader.pending() == {"measurements": 1, "speedtests": 1, "diagnoses": 1}

    backend.fail = None  # internet kembali
    assert uploader.upload_pending() == {"measurements": 1, "speedtests": 1, "diagnoses": 1}
    assert flags(conn) == [1] and flags(conn, "speedtests") == [1] and flags(conn, "diagnoses") == [1]
    assert uploader.pending() == {"measurements": 0, "speedtests": 0, "diagnoses": 0}


def test_failure_mid_way_marks_only_successful_batches(env):
    cfg, conn, backend, uploader, clock = env
    cfg.sheets.batch_size = 2
    for i in range(5):
        diagnosis(conn, i)
    original = backend.append
    count = {"n": 0}

    def flaky(tab, headers, rows):
        count["n"] += 1
        if count["n"] == 2:
            raise TimeoutError("timeout")
        original(tab, headers, rows)

    backend.append = flaky
    assert uploader.upload_pending()["diagnoses"] == -1
    assert flags(conn, "diagnoses") == [1, 1, 0, 0, 0]  # hanya batch pertama yang ditandai
    backend.append = original
    assert uploader.upload_pending()["diagnoses"] == 3
    assert flags(conn, "diagnoses") == [1] * 5
    ids = [r[0] for r in backend.rows("diagnoses")]
    assert ids == [1, 2, 3, 4, 5]  # tidak ada yang hilang atau ganda


def test_speedtest_and_diagnosis_rows(env):
    cfg, conn, backend, uploader, clock = env
    db.insert_speedtest(conn, db.iso(T0), "kos-a", "Telkomsel", SpeedResult("full", "1.2.3.4", 30.0, 10.5, 2.5, True))
    db.insert_speedtest(conn, db.iso(T0), "kos-a", "Telkomsel", SpeedResult("light", "1.2.3.4", error="putus"))
    diagnosis(conn)
    uploader.upload_pending()
    assert backend.rows("speedtests") == [
        [1, "2026-11-15T08:00:00+00:00", "kos-a", "Telkomsel", "full", "1.2.3.4", 30.0, 10.5, 2.5, 1, ""],
        [2, "2026-11-15T08:00:00+00:00", "kos-a", "Telkomsel", "light", "1.2.3.4", "", "", "", 0, "putus"],
    ]
    row = backend.rows("diagnoses")[0]
    assert len(row) == len(sheets.DIAGNOSIS_HEADERS) and row[11] == "Normal" and row[13] == ""
    assert all(v is not None for v in row)


def test_raw_measurements_mode_and_batches(env):
    cfg, conn, backend, uploader, clock = env
    cfg.sheets.aggregate_measurements = False
    cfg.sheets.batch_size = 2
    for i in range(5):
        measure(conn, i * 10)
    assert uploader.upload_pending()["measurements"] == 5
    assert [len(rows) for _, _, rows in backend.calls] == [2, 2, 1]
    assert backend.calls[0][1] == sheets.RAW_MEASUREMENT_HEADERS and flags(conn) == [1] * 5


def test_many_minutes_split_into_batches(env):
    cfg, conn, backend, uploader, clock = env
    cfg.sheets.batch_size = 3
    for m in range(8):
        measure(conn, m * 60)
        measure(conn, m * 60 + 30)
    clock["now"] = T0 + timedelta(hours=1)
    assert uploader.upload_pending()["measurements"] == 8
    assert [len(rows) for _, _, rows in backend.calls] == [3, 3, 2]
    assert flags(conn) == [1] * 16


# ------------------------------------------------------------ backend gspread (modul palsu)

class FakeWorksheet:
    def __init__(self, first_row=None):
        self.rows = [first_row] if first_row else []

    def row_values(self, n):
        return self.rows[n - 1] if len(self.rows) >= n else []

    def append_row(self, row, value_input_option):
        self.rows.append(list(row))

    def append_rows(self, rows, value_input_option):
        assert value_input_option == "RAW"
        self.rows.extend(list(r) for r in rows)


@pytest.fixture
def fake_gspread(monkeypatch):
    module = types.ModuleType("gspread")

    class WorksheetNotFound(Exception):
        pass

    state = {"sheets": {"diagnoses": FakeWorksheet(["sudah", "ada", "judul"])}, "opened": [], "auth": []}

    class Spreadsheet:
        title = "CekSinyal Bersama"

        def worksheet(self, name):
            if name not in state["sheets"]:
                raise WorksheetNotFound(name)
            return state["sheets"][name]

        def add_worksheet(self, title, rows, cols):
            state["sheets"][title] = FakeWorksheet()
            return state["sheets"][title]

    class Client:
        def open_by_key(self, key):
            state["opened"].append(key)
            return Spreadsheet()

    def service_account(filename, scopes):
        state["auth"].append((filename, scopes))
        return Client()

    module.WorksheetNotFound = WorksheetNotFound
    module.service_account = service_account
    monkeypatch.setitem(sys.modules, "gspread", module)
    return state


def test_gspread_backend_creates_tab_and_header(fake_gspread, tmp_path):
    backend = GspreadBackend("SHEET123", tmp_path / "kredensial.json")
    assert backend.check() == "CekSinyal Bersama"
    backend.append("speedtests", ["a", "b"], [[1, 2], [3, 4]])
    backend.append("speedtests", ["a", "b"], [[5, 6]])
    backend.append("diagnoses", ["x"], [[9]])
    assert fake_gspread["sheets"]["speedtests"].rows == [["a", "b"], [1, 2], [3, 4], [5, 6]]  # judul sekali saja
    assert fake_gspread["sheets"]["diagnoses"].rows == [["sudah", "ada", "judul"], [9]]  # judul lama tidak ditimpa
    assert fake_gspread["opened"] == ["SHEET123"]
    assert fake_gspread["auth"] == [(str(tmp_path / "kredensial.json"), sheets.SCOPES)]


def test_gspread_backend_reconnects_after_error(fake_gspread, tmp_path):
    backend = GspreadBackend("SHEET123", tmp_path / "k.json")
    backend.append("speedtests", ["a"], [[1]])

    def boom(rows, value_input_option):
        raise ConnectionError("putus")

    ws = fake_gspread["sheets"]["speedtests"]
    good = ws.append_rows
    ws.append_rows = boom
    with pytest.raises(ConnectionError):
        backend.append("speedtests", ["a"], [[2]])
    ws.append_rows = good
    backend.append("speedtests", ["a"], [[2]])
    assert ws.rows == [["a"], [1], [2]] and len(fake_gspread["opened"]) == 2


# ------------------------------------------------------------ config & CLI

def test_config_sheets(tmp_path, monkeypatch):
    for key in ("GOOGLE_CREDENTIALS_FILE", "GOOGLE_SPREADSHEET_ID"):
        monkeypatch.delenv(key, raising=False)
    p = tmp_path / "config.yaml"
    p.write_text("kos: k\n", encoding="utf-8")
    cfg = load_config(p)
    assert not cfg.sheets.active and make_uploader(cfg, None) is None
    assert "spreadsheet_id belum diisi" in sheets_problem(cfg)

    p.write_text("sheets:\n  spreadsheet_id: ABC\n  batch_size: 100\n", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_CREDENTIALS_FILE", "rahasia/sa.json")
    cfg = load_config(p)
    assert cfg.sheets.active and cfg.sheets.batch_size == 100
    assert cfg.sheets.credentials_file == tmp_path / "rahasia" / "sa.json"
    assert "tidak ditemukan" in sheets_problem(cfg)
    (tmp_path / "rahasia").mkdir()
    (tmp_path / "rahasia" / "sa.json").write_text("{}", encoding="utf-8")
    assert sheets_problem(cfg) is None
    assert isinstance(make_uploader(cfg, None).backend, GspreadBackend)

    p.write_text("sheets:\n  spreadsheet_id: ABC\n  enabled: false\n", encoding="utf-8")
    assert not load_config(p).sheets.active


def test_cli_upload(tmp_path, capsys, monkeypatch, fake_gspread):
    monkeypatch.chdir(tmp_path)
    for key in ("CEKSINYAL_CONFIG", "GOOGLE_CREDENTIALS_FILE", "GOOGLE_SPREADSHEET_ID"):
        monkeypatch.delenv(key, raising=False)
    assert main(["upload"]) == 1  # belum dikonfigurasi
    (tmp_path / "sa.json").write_text("{}", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "kos: k\ndatabase: u.db\nsheets:\n  spreadsheet_id: ABC\n  credentials_file: sa.json\n", encoding="utf-8")
    conn = db.connect(tmp_path / "u.db")
    measure(conn, 0)
    diagnosis(conn)
    conn.close()
    assert main(["upload"]) == 0
    out = capsys.readouterr().out
    assert "measurements" in out and "GAGAL" not in out
    assert len(fake_gspread["sheets"]["measurements"].rows) == 2  # judul + 1 menit
    assert fake_gspread["sheets"]["diagnoses"].rows[-1][11] == "Normal"
