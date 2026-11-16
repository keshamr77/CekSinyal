"""Pengaturan dashboard (env > secrets > config) dan mode sheets dengan gspread palsu."""

import sys
import types
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("pandas")

from ceksinyal import db  # noqa: E402
from ceksinyal.config import Config  # noqa: E402
from ceksinyal.dashboard import data, settings  # noqa: E402
from ceksinyal.uploader.sheets import Uploader  # noqa: E402
from tests.dashboard_helpers import END, make_db  # noqa: E402

SERVICE_ACCOUNT = {"type": "service_account", "client_email": "baca@contoh.iam.gserviceaccount.com",
                   "private_key": "PLACEHOLDER"}


@pytest.fixture
def clean(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("CEKSINYAL_CONFIG", "GOOGLE_CREDENTIALS_FILE", "GOOGLE_SPREADSHEET_ID", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_defaults_are_sqlite_and_public(clean):
    s = settings.resolve({}, {})
    assert (s.mode, s.public, s.aliases) == ("sqlite", True, {})
    assert s.db_path == clean / "data" / "ceksinyal.db" and "tidak ditemukan" in s.problem()


def test_env_db_forces_sqlite_and_overrides_everything(clean):
    path = make_db(clean / "x.db")
    secrets = {"dashboard": {"mode": "sheets", "spreadsheet_id": "ABC"}, "gcp_service_account": SERVICE_ACCOUNT}
    s = settings.resolve(secrets, {settings.ENV_DB: str(path), settings.ENV_PUBLIC: "0"})
    assert (s.mode, s.db_path, s.public) == ("sqlite", path, False) and s.problem() is None
    assert s.cache_key[:2] == ("sqlite", str(path)) and s.cache_key[2] > 0
    assert settings.resolve(secrets, {settings.ENV_MODE: "sqlite"}).mode == "sqlite"


def test_secrets_configure_cloud_deployment(clean):
    secrets = {
        "dashboard": {"spreadsheet_id": "SHEET123", "stale_minutes": 30, "timezone_offset_hours": 8,
                      "sinr_good_db": 13},
        "kos_aliases": {"Garden House": "Kos A"},
        "diagnosis": {"rsrp_weak_dbm": -105},
        "gcp_service_account": SERVICE_ACCOUNT,
    }
    s = settings.resolve(secrets, {})
    assert (s.mode, s.spreadsheet_id, s.public) == ("sheets", "SHEET123", True)  # auto -> sheets; publik bawaan
    assert s.aliases == {"Garden House": "Kos A"} and s.credentials_info["client_email"].startswith("baca@")
    assert s.config.dashboard.stale_minutes == 30 and s.config.tz_label == "WITA"
    cats = data.Categories.from_config(s.config)
    assert (cats.rsrp_poor, cats.sinr_good) == (-105, 13)  # ambang kategori ikut ambang diagnosis
    assert s.problem() is None and s.cache_key == ("sheets", "SHEET123")
    assert "PLACEHOLDER" not in repr(s.cache_key)


def test_local_config_yaml_and_env_credentials(clean, monkeypatch):
    (clean / "sa.json").write_text("{}", encoding="utf-8")
    (clean / "config.yaml").write_text(
        "sheets:\n  spreadsheet_id: LOKAL\ndashboard:\n  public: false\n  kos_aliases:\n    Garden House: Kos A\n",
        encoding="utf-8")
    monkeypatch.setenv("GOOGLE_CREDENTIALS_FILE", "sa.json")
    s = settings.resolve({}, {})
    assert (s.mode, s.spreadsheet_id, s.public, s.aliases) == ("sheets", "LOKAL", False, {"Garden House": "Kos A"})
    assert s.credentials_file == clean / "sa.json" and s.problem() is None
    assert settings.resolve({}, {settings.ENV_PUBLIC: "1"}).public is True


def test_sheets_mode_problems(clean):
    assert "ID spreadsheet belum diisi" in settings.resolve({}, {settings.ENV_MODE: "sheets"}).problem()
    s = settings.resolve({"dashboard": {"mode": "sheets", "spreadsheet_id": "ABC"}}, {})
    assert "Kredensial service account belum ada" in s.problem()


def test_broken_config_falls_back_to_defaults(clean):
    (clean / "config.yaml").write_text("dashboard: [bukan, mapping]\n", encoding="utf-8")
    s = settings.resolve({}, {})
    assert s.mode == "sqlite" and s.public is True


# ------------------------------------------------------------ mode sheets dengan gspread palsu

@pytest.fixture
def fake_gspread(monkeypatch, clean):
    """Spreadsheet palsu yang diisi lewat uploader sungguhan, lalu dibaca dashboard lewat modul gspread palsu."""
    path = make_db(clean / "src.db")
    tabs: dict[str, list[list[str]]] = {}

    class Store:
        def append(self, tab, headers, rows):
            tabs.setdefault(tab, [list(headers)]).extend([["" if v is None else str(v) for v in r] for r in rows])

    conn = db.connect(path)
    cfg = Config(kos="x")
    cfg.sheets.batch_size = 100000
    Uploader(cfg, conn, Store(), now=lambda: END + timedelta(hours=1)).upload_pending()
    conn.close()
    tabs.pop("events")  # spreadsheet lama: tab events belum pernah dikirim

    module = types.ModuleType("gspread")
    calls = {"auth": [], "opened": [], "reads": []}

    class WorksheetNotFound(Exception):
        pass

    class Worksheet:
        def __init__(self, name):
            self.name = name

        def get_all_values(self):
            calls["reads"].append(self.name)
            return tabs[self.name]

    class Spreadsheet:
        def worksheet(self, name):
            if name not in tabs:
                raise WorksheetNotFound(name)
            return Worksheet(name)

    class Client:
        def open_by_key(self, key):
            calls["opened"].append(key)
            return Spreadsheet()

    def from_dict(info, scopes):
        calls["auth"].append(("dict", info["client_email"], tuple(scopes)))
        return Client()

    def from_file(filename, scopes):
        calls["auth"].append(("file", Path(filename).name, tuple(scopes)))
        return Client()

    module.WorksheetNotFound = WorksheetNotFound
    module.service_account_from_dict = from_dict
    module.service_account = from_file
    monkeypatch.setitem(sys.modules, "gspread", module)
    return calls


def test_load_dataset_from_sheets_with_secrets(fake_gspread, clean):
    s = settings.resolve({"dashboard": {"spreadsheet_id": "SHEET123"}, "gcp_service_account": SERVICE_ACCOUNT}, {})
    ds = settings.load_dataset(s)
    assert fake_gspread["auth"] == [("dict", "baca@contoh.iam.gserviceaccount.com", tuple(settings.READONLY_SCOPES))]
    assert settings.READONLY_SCOPES == ["https://www.googleapis.com/auth/spreadsheets.readonly"]  # hanya-baca
    assert fake_gspread["opened"] == ["SHEET123"]
    assert sorted(fake_gspread["reads"]) == ["diagnoses", "measurements", "speedtests"]  # satu baca per tab
    assert len(ds.measurements) == 340 and ds.events.empty  # tab events belum ada: tidak error
    assert ds.kos_names() == ["Garden House", "Kos Melati"] and "Google Sheets" in ds.source
    cards = data.summary_cards(data.apply_privacy(ds, s.aliases, s.public), data.Categories(), now=END)
    assert {(c.kos, c.operator) for c in cards} == {("Kos 1", "Telkomsel"), ("Kos 1", "Tri"), ("Kos 2", "XL")}


def test_load_dataset_from_sheets_with_local_credentials_file(fake_gspread, clean, monkeypatch):
    (clean / "sa.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_CREDENTIALS_FILE", "sa.json")
    monkeypatch.setenv("GOOGLE_SPREADSHEET_ID", "LOKAL")
    s = settings.resolve({}, {})
    assert s.mode == "sheets"
    settings.load_dataset(s)
    assert fake_gspread["auth"] == [("file", "sa.json", tuple(settings.READONLY_SCOPES))]


def test_app_renders_in_sheets_mode(fake_gspread, clean, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    import streamlit as st

    st.cache_data.clear()
    for key in ("CEKSINYAL_DASHBOARD_DB", "CEKSINYAL_DASHBOARD_MODE", "CEKSINYAL_DASHBOARD_PUBLIC"):
        monkeypatch.delenv(key, raising=False)
    entry = str(Path(__file__).resolve().parent.parent / "dashboard" / "streamlit_app.py")
    at = AppTest.from_file(entry, default_timeout=60)
    # secrets lewat AppTest (bukan file .streamlit/secrets.toml), supaya tidak terbawa ke tes lain
    at.secrets["dashboard"] = {"spreadsheet_id": "SHEET123"}
    at.secrets["kos_aliases"] = {"Garden House": "Kos A"}
    at.secrets["gcp_service_account"] = dict(SERVICE_ACCOUNT)
    at.run()
    assert not at.exception, at.exception
    text = "\n".join(str(el.value) for kind in ("subheader", "markdown", "caption") for el in getattr(at, kind))
    assert "Kos A" in text and "Garden House" not in text and "Google Sheets" in text
    assert len(at.tabs) == 7
    at.run()  # muat kedua dalam TTL: dari cache, tanpa membaca Sheets lagi
    assert len(fake_gspread["reads"]) == 3
    st.cache_data.clear()
