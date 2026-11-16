import re
import xml.dom.minidom
from datetime import datetime, timedelta, timezone

import pytest

from ceksinyal import db
from ceksinyal.__main__ import main
from ceksinyal.diagnosis import engine
from ceksinyal.diagnosis.rules import Thresholds
from ceksinyal.report import data as report_data
from ceksinyal.report import render

T0 = datetime(2026, 11, 15, 8, 0, 0, tzinfo=timezone.utc)
WIB = timezone(timedelta(hours=7))
TH = Thresholds()


def add(conn, start, minutes, operator="Telkomsel", kos="kos-a", serial=None, **fields):
    row = {"rat": "LTE", "rsrp": -90, "rsrq": -11, "sinr": 10.0, "sinr_raw": 10, "sinr_unit": "db",
           "cell_id": 100, "pci": 5, "earfcn": 1850, "band": 3, "raw_ok": 1, **fields}
    t = start
    while t < start + timedelta(minutes=minutes):
        db.insert_measurement(conn, {"ts": db.iso(t), "kos": kos, "device_serial": serial or "HP-" + operator,
                                     "operator": operator, **row})
        t += timedelta(seconds=10)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "r.db")
    yield c
    c.close()


@pytest.fixture
def filled(conn):
    """Dua operator: Telkomsel normal lalu putus; Tri (HP ala Xiaomi) lemah, ada jeda, SINR lama salah skala."""
    add(conn, T0, 20)
    add(conn, T0 + timedelta(minutes=20), 10, rat="NONE", rsrp=None, rsrq=None, sinr=None, sinr_raw=None,
        cell_id=None, pci=None, earfcn=None, band=None)
    add(conn, T0, 10, operator="Tri", rsrp=-115, sinr=25.0, sinr_raw=None, sinr_unit=None, cell_id=None, pci=None, band=1)
    add(conn, T0 + timedelta(minutes=25), 5, operator="Tri", rsrp=-112, cell_id=None, pci=None)
    for i in range(7):
        conn.execute("INSERT INTO speedtests (ts, kos, operator, method, ping_ms, download_mbps, upload_mbps, ok, error) "
                     "VALUES (?, 'kos-a', 'Telkomsel', 'light', 40, ?, NULL, ?, ?)",
                     (db.iso(T0 + timedelta(minutes=3 * i)), 10.0 + i, 0 if i == 6 else 1, "gagal" if i == 6 else None))
    db.insert_event(conn, db.iso(T0 + timedelta(minutes=5)), "kos-a", "Tri", db.EVENT_COLLECTOR_ERROR, "HP <b>terputus</b>")
    engine.run_pending(conn, TH, now=T0 + timedelta(hours=1))
    return conn


def build(conn, kos="kos-a", **kwargs):
    return report_data.build(conn, kos, TH, WIB, "WIB", now=T0 + timedelta(hours=2), **kwargs)


def test_histogram_bins():
    assert report_data.histogram([-101, -100, -96, -95.5, -84], 5) == [
        (-105, -100, 1), (-100, -95, 3), (-95, -90, 0), (-90, -85, 0), (-85, -80, 1)]
    assert report_data.histogram([], 5) == []


def test_operator_summary(filled):
    data = build(filled)
    assert [op.operator for op in data.operators] == ["Telkomsel", "Tri"]
    tsel, tri = data.operators
    assert (tsel.n_samples, tsel.n_lost, round(tsel.pct_lost)) == (180, 60, 33)
    assert tsel.stats["rsrp"].median == -90 and tsel.stats["rsrp"].n == 120
    assert tsel.diag_counts == {"Normal": 2, "Sinyal putus": 1} and tsel.dominant == "Normal"
    assert (tsel.speed.n_ok, tsel.speed.n_failed, tsel.speed.median_download) == (6, 1, 12.5)
    assert tsel.pct_weak_rsrp == 0 and tri.pct_weak_rsrp == 100
    assert tri.dominant == "Cakupan lemah" and tri.band_counts == {"LTE B1": 60, "LTE B3": 30}
    assert len(data.lost_events) == 1 and len(data.lost_windows) == 1 and len(data.collector_events) == 1
    assert data.recommendation.best == "Telkomsel"
    assert data.first == T0 and len(data.windows) == 6


def test_limitations_per_device(filled):
    tsel, tri = build(filled).operators
    text = " | ".join(tri.limitations)
    assert "menyembunyikan cell_id" in text and "EARFCN" in text
    assert "60 sampel LTE direkam dengan versi lama" in text and "60 di antaranya berpola salah skala" in text
    assert "60 sampel punya band yang tidak cocok dengan EARFCN" in text
    assert "Data bolong 1 kali (total 15 menit" in text
    assert "Tidak ada uji kecepatan" in text and "Rekaman hanya 30 menit" in text
    assert not any("cell_id" in n or "versi lama" in n or "bolong" in n for n in tsel.limitations)


def test_sinr_always_zero_and_unset_kos(conn):
    add(conn, T0, 10, kos="belum-diatur", sinr=0.0, sinr_raw=0)
    data = build(conn, kos="belum-diatur")
    assert any("SINR selalu persis 0" in n for n in data.operators[0].limitations)
    assert any("belum-diatur" in n for n in data.notes)
    assert any("Belum ada diagnosis" in n for n in data.notes)


def test_series_breaks_at_gap_and_downsamples(conn):
    add(conn, T0, 5)
    add(conn, T0 + timedelta(minutes=30), 5)
    series = build(conn).operators[0].series
    assert [v for _, v in series].count(None) == 1 and len(series) == 61
    add(conn, T0 + timedelta(hours=1), 180)
    assert len(build(conn).operators[0].series) <= report_data.MAX_SERIES_POINTS + 5


def test_period_filter(filled):
    data = build(filled, since=T0 + timedelta(minutes=20))
    assert data.operators[0].n_samples == 60 and data.operators[0].n_lost == 60
    assert all(w.end > T0 + timedelta(minutes=20) for w in data.windows)


def test_markdown(filled):
    md = render.render_markdown(build(filled))
    for text in ("# Laporan Bukti Sinyal — kos-a", "## Ringkasan per operator", "## Distribusi sinyal",
                 "## Linimasa diagnosis", "## Kejadian sinyal putus", "## Keterbatasan data per HP",
                 "## Gangguan pengumpulan data", "**Rekomendasi operator:** Telkomsel", "Distribusi RSRP:",
                 "15/11 15:20–15:30", "1 kejadian putus", "█", "−115 dBm"):
        assert text in md, text
    assert "15:00 s.d." in md  # 08:00 UTC = 15:00 WIB


def test_html_is_single_file_and_well_formed(filled):
    page = render.render_html(build(filled))
    assert page.startswith("<!doctype html>") and "Laporan Bukti Sinyal — kos-a" in page
    assert not re.search(r'(src|href)\s*=\s*"https?://', page)  # tidak ada aset luar
    assert "HP &lt;b&gt;terputus&lt;/b&gt;" in page and "<b>terputus</b>" not in page  # teks dari data di-escape
    svgs = re.findall(r"<svg.*?</svg>", page, flags=re.S)
    assert len(svgs) == 1 + 3 + 3 + 2  # garis RSRP, 3 histogram x 2 operator, 2 linimasa
    for svg in svgs:
        xml.dom.minidom.parseString(svg)
    assert page.count('class="line l1"') >= 1 and 'class="line l2"' in page
    assert page.count("lane-problem") >= 3  # 1 putus + cakupan lemah (di luar CSS)
    assert '<span class="key k2"></span>Tri' in page  # legenda untuk dua operator


def test_empty_kos_renders(conn):
    data = build(conn, kos="kosong")
    assert "Tidak ada data pengukuran" in render.render_markdown(data)
    assert "Tidak ada data pengukuran" in render.render_html(data)


def test_cli_report(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CEKSINYAL_CONFIG", raising=False)
    path = tmp_path / "d.db"
    c = db.connect(path)
    add(c, T0, 20)
    c.close()
    assert main(["report", "--kos", "tidak-ada", "--db", str(path)]) == 1
    assert "Kos yang tersedia: kos-a (120 sampel)" in capsys.readouterr().out
    assert main(["report", "--kos", "kos-a", "--db", str(path), "--out", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert "1 operator, 2 jendela diagnosis" in out  # diagnosis dijalankan otomatis
    files = sorted(p.suffix for p in (tmp_path / "out").iterdir())
    assert files == [".html", ".md"]
    assert main(["report", "--kos", "kos-a", "--db", str(path), "--since", "kemarin"]) == 1
    assert main(["report", "--kos", "kos-a", "--db", str(path), "--since", "2026-11-16", "--out", str(tmp_path / "o2")]) == 0
    md = next((tmp_path / "o2").glob("*.md")).read_text(encoding="utf-8")
    assert "Tidak ada data pengukuran" in md
