"""Smoke test tiap halaman dashboard dengan streamlit.testing (AppTest) dan DB sintetis."""

import shutil
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
pytest.importorskip("pandas")

from streamlit.testing.v1 import AppTest  # noqa: E402

from ceksinyal import db  # noqa: E402
from ceksinyal.dashboard import app as app_mod  # noqa: E402
from tests.dashboard_helpers import SERIAL_A, SERIAL_B, SERIAL_C, make_db  # noqa: E402

ENTRY = str(Path(__file__).resolve().parent.parent / "dashboard" / "streamlit_app.py")


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    return make_db(tmp_path_factory.mktemp("app") / "sintetis.db")


@pytest.fixture
def run_app(monkeypatch, tmp_path):
    """Jalankan app dari folder kosong (tanpa config.yaml/.env/secrets milik pengembang)."""
    monkeypatch.chdir(tmp_path)
    for key in ("CEKSINYAL_CONFIG", "GOOGLE_CREDENTIALS_FILE", "GOOGLE_SPREADSHEET_ID",
                "CEKSINYAL_DASHBOARD_MODE", "CEKSINYAL_DASHBOARD_PUBLIC", "CEKSINYAL_DASHBOARD_DB"):
        monkeypatch.delenv(key, raising=False)

    def run(path, **env):
        monkeypatch.setenv("CEKSINYAL_DASHBOARD_DB", str(path))
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        at = AppTest.from_file(ENTRY, default_timeout=60)
        at.run()
        return at

    return run


def text_of(at) -> str:
    """Semua teks yang tampil (markdown, caption, judul, kotak info, isi tabel)."""
    parts = []
    for kind in ("title", "header", "subheader", "markdown", "caption", "info", "warning", "success", "error"):
        parts += [str(el.value) for el in getattr(at, kind)]
    parts += [el.value.to_csv() for el in at.dataframe]
    parts += [f"{m.label}={m.value}" for m in at.metric]
    parts += [str(e.label) for e in at.expander]
    return "\n".join(parts)


def test_all_pages_render_without_exception(run_app, db_path):
    at = run_app(db_path)
    assert not at.exception, at.exception
    assert [t.label for t in at.tabs] == app_mod.TABS and len(at.tabs) == 7
    text = text_of(at)
    for expected in (
        "Kos 1", "Kos 2",                                   # mode publik bawaan: alias otomatis
        "Telkomsel", "Tri", "XL",
        "Alat ukur diam",                                   # data sintetis sudah lama -> peringatan collector diam
        "✅ Baik", "⛔ Buruk", "⚠️ Cukup",                    # kategori dengan ikon + label
        "Interferensi", "Cakupan lemah",                    # diagnosis
        "Rekomendasi operator per kos", "Telkomsel.",       # perbandingan
        "Kecepatan unduh (Mbps)", "Uji yang gagal",         # kecepatan
        "Sinyal putus", "→",                                # kejadian
        "HP terputus dari laptop",                          # gangguan alat ukur tanpa rincian
        "menyembunyikan identitas sel", "Data bolong 1 kali",  # kualitas data
        "Tiga angka sinyal", "Arti tiap diagnosis",         # cara membaca
    ):
        assert expected in text, expected
    # privasi: tidak ada nama kos asli, serial, atau path laptop di mana pun
    for secret in ("Garden House", "Kos Melati", SERIAL_A, SERIAL_B, SERIAL_C, "budi", "192.168."):
        assert secret not in text, secret


def test_each_tab_has_content(run_app, db_path):
    at = run_app(db_path)
    for tab in at.tabs:
        assert len(tab.children) > 0, tab.label
    charts = at.get("vega_lite_chart")
    assert len(charts) == 2 * 3 + 1 + 3  # linimasa (2 operator x 3 besaran) + perbandingan + unduh/unggah/ping


def test_private_mode_and_aliases(run_app, db_path, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "dashboard:\n  public: false\n  kos_aliases:\n    Garden House: Kos A\n", encoding="utf-8")
    at = run_app(db_path)
    assert not at.exception
    text = text_of(at)
    assert "Kos A" in text and "Kos Melati" in text and "Garden House" not in text
    assert SERIAL_A not in text  # serial tidak pernah tampil, juga di mode privat
    at2 = run_app(db_path, CEKSINYAL_DASHBOARD_PUBLIC="1")  # lingkungan menimpa config
    text2 = text_of(at2)
    assert "Kos A" in text2 and "Kos Melati" not in text2 and "Kos 1" in text2


def test_filters_change_view(run_app, db_path):
    at = run_app(db_path)
    kos = next(w for w in at.multiselect if w.key == "filter_kos")
    assert kos.options == ["Kos 1", "Kos 2"]
    kos.set_value(["Kos 2"]).run()
    assert not at.exception
    text = text_of(at)
    assert "XL" in text and "Telkomsel" not in text.split("Tiga angka sinyal")[0].split("Rekomendasi")[0]
    operator = next(w for w in at.multiselect if w.key == "filter_operator")
    operator.set_value(["XL"]).run()
    rng = next(w for w in at.selectbox if w.key == "filter_rentang")
    rng.set_value("Semua data").run()
    assert not at.exception
    metric = next(w for w in at.get("button_group") if w.key == "banding_besaran")
    metric.set_value("sinr").run()
    assert not at.exception and "SINR: Baik ≥ 10 dB" in text_of(at)


def test_empty_and_missing_database(run_app, tmp_path):
    empty = tmp_path / "kosong.db"
    db.connect(empty).close()
    at = run_app(empty)
    assert not at.exception and "Belum ada data pengukuran" in text_of(at)
    at = run_app(tmp_path / "tidak-ada.db")
    assert not at.exception and "File database tidak ditemukan" in text_of(at)


def test_database_with_measurements_only(run_app, tmp_path):
    """DB yang baru berisi pengukuran (tanpa diagnosis, speedtest, event) tetap tampil di semua halaman."""
    path = tmp_path / "minimal.db"
    conn = db.connect(path)
    for i in range(30):
        db.insert_measurement(conn, {"ts": f"2026-11-15T08:{i // 6:02d}:{i % 6 * 10:02d}+00:00", "kos": "k",
                                     "device_serial": "S1", "operator": "Tri", "rat": "LTE", "rsrp": -82,
                                     "sinr": 0.0, "raw_ok": 1})
    conn.close()
    at = run_app(path)
    assert not at.exception, at.exception
    text = text_of(at)
    assert "Belum ada diagnosis" in text and "Belum ada uji kecepatan" in text
    assert "SINR selalu persis 0" in text and "Tidak ada kejadian" in text


def test_sheets_mode_without_credentials_shows_clear_error(run_app, db_path, monkeypatch):
    monkeypatch.setenv("CEKSINYAL_DASHBOARD_MODE", "sheets")
    monkeypatch.delenv("CEKSINYAL_DASHBOARD_DB", raising=False)
    at = AppTest.from_file(ENTRY, default_timeout=60)
    at.run()
    assert not at.exception and "ID spreadsheet belum diisi" in text_of(at)
    shutil.rmtree(".streamlit", ignore_errors=True)
