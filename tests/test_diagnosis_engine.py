from datetime import datetime, timedelta, timezone

import pytest

from ceksinyal import db
from ceksinyal.__main__ import main
from ceksinyal.diagnosis import engine, rules
from ceksinyal.diagnosis.rules import Thresholds

T0 = datetime(2026, 11, 15, 8, 0, 0, tzinfo=timezone.utc)
TH = Thresholds()


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def add(conn, start, minutes, operator="Telkomsel", kos="kos-a", step=10, **fields):
    """Sampel tiap `step` detik selama `minutes` menit mulai dari `start`."""
    row = {"rat": "LTE", "rsrp": -90, "sinr": 10.0, "cell_id": 100, "earfcn": 1850, "raw_ok": 1, **fields}
    t = start
    while t < start + timedelta(minutes=minutes):
        db.insert_measurement(conn, {"ts": db.iso(t), "kos": kos, "device_serial": "HP-" + operator,
                                     "operator": operator, **row})
        t += timedelta(seconds=step)


def speed(conn, ts, mbps, operator="Telkomsel", kos="kos-a", ok=1):
    conn.execute(
        "INSERT INTO speedtests (ts, kos, operator, method, download_mbps, ok) VALUES (?, ?, ?, 'light', ?, ?)",
        (db.iso(ts), kos, operator, mbps, ok),
    )
    conn.commit()


def results(conn, operator="Telkomsel"):
    return [r[0] for r in conn.execute(
        "SELECT result FROM diagnoses WHERE operator = ? ORDER BY window_start", (operator,))]


def events(conn):
    return [tuple(r) for r in conn.execute("SELECT type, operator, detail, notified, ts FROM events ORDER BY id")]


def test_floor_window():
    assert engine.floor_window(T0 + timedelta(minutes=17, seconds=59), 10) == T0 + timedelta(minutes=10)
    assert engine.floor_window(T0, 10) == T0


def test_windows_on_fixed_grid_and_values_stored(conn):
    add(conn, T0 + timedelta(minutes=3), 20)  # 08:03-08:23 -> jendela 08:00, 08:10, 08:20
    rows = engine.run_pending(conn, TH, now=T0 + timedelta(hours=1))
    assert [(r.window_start, r.window.n_samples) for r in rows] == [
        (T0, 42), (T0 + timedelta(minutes=10), 60), (T0 + timedelta(minutes=20), 18)]
    assert results(conn) == [rules.NORMAL, rules.NORMAL, rules.INSUFFICIENT]
    d = dict(conn.execute("SELECT * FROM diagnoses ORDER BY id").fetchone())
    assert (d["kos"], d["operator"], d["n_samples"], d["median_rsrp"], d["median_sinr"], d["cell_changes"]) == (
        "kos-a", "Telkomsel", 42, -90, 10.0, 0)
    assert d["window_start"] == "2026-11-15T08:00:00+00:00" and d["window_end"] == "2026-11-15T08:10:00+00:00"
    assert d["advice"] and "kecepatan: tidak tersedia" in d["detail"] and d["uploaded"] == 0


def test_only_completed_windows_and_no_duplicates(conn):
    add(conn, T0, 25)
    assert len(engine.run_pending(conn, TH, now=T0 + timedelta(minutes=15))) == 1  # 08:10-08:20 belum selesai
    assert len(engine.run_pending(conn, TH, now=T0 + timedelta(minutes=15))) == 0
    assert len(engine.run_pending(conn, TH, now=T0 + timedelta(minutes=31))) == 2
    assert len(results(conn)) == 3


def test_gap_counts_real_samples(conn):
    # Collector berhenti 15 menit: jendela di tengah harus "Data kurang", bukan diagnosis dari asumsi.
    add(conn, T0, 10)
    add(conn, T0 + timedelta(minutes=27), 13)
    engine.run_pending(conn, TH, now=T0 + timedelta(hours=2))
    assert results(conn) == [rules.NORMAL, rules.INSUFFICIENT, rules.INSUFFICIENT, rules.NORMAL]
    n = [r[0] for r in conn.execute("SELECT n_samples FROM diagnoses ORDER BY window_start")]
    assert n == [60, 0, 18, 60]
    assert events(conn) == []  # "Data kurang" bukan perubahan diagnosis


def test_no_windows_after_last_sample(conn):
    add(conn, T0, 10)
    engine.run_pending(conn, TH, now=T0 + timedelta(days=3))
    assert len(results(conn)) == 1


def test_raw_ok_zero_excluded(conn):
    add(conn, T0, 10, step=20)             # 30 sampel baik
    add(conn, T0, 10, step=20, raw_ok=0, rat=None, rsrp=None)
    rows = engine.run_pending(conn, TH, now=T0 + timedelta(hours=1))
    assert rows[0].window.n_samples == 30 and "30 pembacaan gagal di-parse" in rows[0].window.detail


def test_events_on_change(conn):
    add(conn, T0, 10)
    add(conn, T0 + timedelta(minutes=10), 10, sinr=-4.0)
    add(conn, T0 + timedelta(minutes=20), 10, sinr=-4.0)
    add(conn, T0 + timedelta(minutes=30), 10, rat="NONE", rsrp=None, sinr=None, cell_id=None, earfcn=None)
    add(conn, T0 + timedelta(minutes=40), 10)
    engine.run_pending(conn, TH, now=T0 + timedelta(minutes=51))
    assert results(conn) == [rules.NORMAL, rules.INTERFERENCE, rules.INTERFERENCE, rules.LOST, rules.NORMAL]
    ev = events(conn)
    assert [(e[0], e[2].split(";")[0]) for e in ev] == [
        ("diagnosis_changed", "Normal -> Interferensi"),
        ("signal_lost", "Interferensi -> Sinyal putus"),
        ("diagnosis_changed", "Sinyal putus -> Normal"),
    ]
    assert ev[0][4] == "2026-11-15T08:20:00+00:00"  # waktu event = akhir jendela
    # jendela lebih tua dari 30 menit tidak di-alert; yang baru di-alert
    assert [e[3] for e in ev] == [1, 0, 0]


def test_first_diagnosis_problem_emits_event_but_first_normal_does_not(conn):
    add(conn, T0, 10, rsrp=-120)
    add(conn, T0, 10, operator="Tri")
    engine.run_pending(conn, TH, now=T0 + timedelta(minutes=11))
    assert [(e[0], e[1], e[2].split(";")[0]) for e in events(conn)] == [
        ("diagnosis_changed", "Telkomsel", "- -> Cakupan lemah")]


def test_change_compares_across_insufficient_windows(conn):
    add(conn, T0, 10, sinr=-4.0)
    add(conn, T0 + timedelta(minutes=10), 2)     # Data kurang
    add(conn, T0 + timedelta(minutes=20), 10, sinr=-4.0)
    engine.run_pending(conn, TH, now=T0 + timedelta(minutes=31))
    assert results(conn) == [rules.INTERFERENCE, rules.INSUFFICIENT, rules.INTERFERENCE]
    assert len(events(conn)) == 1  # hanya "- -> Interferensi" di awal


def test_congestion_uses_speedtests(conn):
    add(conn, T0, 10)
    for i in range(6):
        speed(conn, T0 - timedelta(hours=1, minutes=15 * i), 20.0)
    speed(conn, T0 + timedelta(minutes=4), 3.0)
    speed(conn, T0 + timedelta(minutes=5), 99.0, ok=0)       # gagal: diabaikan
    speed(conn, T0 + timedelta(minutes=5), 0.1, operator="Tri")  # operator lain: diabaikan
    rows = engine.run_pending(conn, TH, now=T0 + timedelta(minutes=11))
    assert rows[0].window.result == rules.CONGESTED and rows[0].window.median_download == 3.0


def test_speed_lookback_when_window_has_no_test(conn):
    add(conn, T0, 10)
    for i in range(6):
        speed(conn, T0 - timedelta(hours=2, minutes=15 * i), 20.0)
    speed(conn, T0 - timedelta(minutes=5), 2.0)   # 5 menit sebelum jendela, masih dalam 20 menit
    rows = engine.run_pending(conn, TH, now=T0 + timedelta(minutes=11))
    assert rows[0].window.result == rules.CONGESTED


def test_operators_and_kos_are_separate(conn):
    add(conn, T0, 10)
    add(conn, T0, 10, operator="Tri", rsrp=-118)
    add(conn, T0, 10, kos="kos-b", sinr=-3.0)
    engine.run_pending(conn, TH, now=T0 + timedelta(minutes=11))
    got = {(r[0], r[1]): r[2] for r in conn.execute("SELECT kos, operator, result FROM diagnoses")}
    assert got == {("kos-a", "Telkomsel"): rules.NORMAL, ("kos-a", "Tri"): rules.WEAK,
                   ("kos-b", "Telkomsel"): rules.INTERFERENCE}


def test_rerun_after_threshold_change(conn):
    add(conn, T0, 30, rsrp=-105)
    now = T0 + timedelta(hours=5)
    engine.run_pending(conn, TH, now=now)
    conn.execute("UPDATE diagnoses SET uploaded = 1")
    db.insert_event(conn, "t", "kos-a", "Telkomsel", db.EVENT_COLLECTOR_ERROR, "HP terputus")
    assert results(conn) == [rules.NORMAL] * 3

    rows = engine.rerun(conn, Thresholds(rsrp_weak_dbm=-100), now=now)
    assert len(rows) == 3 and results(conn) == [rules.WEAK] * 3
    ev = events(conn)
    assert [e[0] for e in ev] == ["collector_error", "diagnosis_changed"]  # event collector tidak dihapus
    assert ev[1][3] == 1  # data lama: tidak di-alert
    assert conn.execute("SELECT SUM(uploaded) FROM diagnoses").fetchone()[0] == 0
    # rerun dua kali tidak menggandakan
    engine.rerun(conn, Thresholds(rsrp_weak_dbm=-100), now=now)
    assert len(results(conn)) == 3 and len(events(conn)) == 2


def test_rerun_single_kos(conn):
    add(conn, T0, 10)
    add(conn, T0, 10, kos="kos-b")
    now = T0 + timedelta(hours=1)
    engine.run_pending(conn, TH, now=now)
    engine.rerun(conn, Thresholds(rsrp_weak_dbm=-80), now=now, kos="kos-b")
    got = {r[0]: r[1] for r in conn.execute("SELECT kos, result FROM diagnoses")}
    assert got == {"kos-a": rules.NORMAL, "kos-b": rules.WEAK}


def test_recommend_by_diagnosis_then_rsrp(conn):
    add(conn, T0, 20)
    add(conn, T0, 10, operator="Tri")
    add(conn, T0 + timedelta(minutes=10), 10, operator="Tri", sinr=-5.0)
    engine.run_pending(conn, TH, now=T0 + timedelta(hours=1))
    rec = engine.recommend(conn, "kos-a")
    assert rec.best == "Telkomsel" and "100%" in rec.reason and "50%" in rec.reason
    tri = next(o for o in rec.operators if o.operator == "Tri")
    assert tri.dominant == rules.INTERFERENCE  # seri 1-1 -> yang lebih parah


def test_recommend_tie_uses_median_rsrp(conn):
    add(conn, T0, 10, rsrp=-95)
    add(conn, T0, 10, operator="Tri", rsrp=-85)
    engine.run_pending(conn, TH, now=T0 + timedelta(hours=1))
    rec = engine.recommend(conn, "kos-a")
    assert rec.best == "Tri" and "median RSRP" in rec.reason


def test_recommend_needs_two_operators(conn):
    add(conn, T0, 10)
    engine.run_pending(conn, TH, now=T0 + timedelta(hours=1))
    rec = engine.recommend(conn, "kos-a")
    assert rec.best is None and "minimal dua operator" in rec.reason


def test_cli_diagnose_and_rerun(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CEKSINYAL_CONFIG", raising=False)
    path = tmp_path / "d.db"
    c = db.connect(path)
    add(c, T0, 20)
    add(c, T0, 20, operator="Tri", rsrp=-115)
    c.close()
    assert main(["diagnose", "--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Normal" in out and "Cakupan lemah" in out and "15:00" in out  # 08:00 UTC = 15:00 WIB
    assert "Rekomendasi operator di kos-a: Telkomsel" in out
    assert main(["diagnose", "--db", str(path)]) == 0
    assert "Tidak ada jendela baru" in capsys.readouterr().out
    (tmp_path / "config.yaml").write_text("diagnosis:\n  rsrp_weak_dbm: -80\n", encoding="utf-8")
    assert main(["diagnose", "--db", str(path), "--rerun"]) == 0
    out = capsys.readouterr().out
    assert "Normal" not in out.split("Rekomendasi")[0]
    assert main(["diagnose", "--db", str(tmp_path / "tidak-ada.db")]) == 1


def test_config_thresholds(tmp_path):
    from ceksinyal.config import ConfigError, load_config
    p = tmp_path / "c.yaml"
    p.write_text("diagnosis:\n  min_samples: 12\n  rsrp_weak_dbm: -105.5\n", encoding="utf-8")
    cfg = load_config(p)
    assert (cfg.diagnosis.min_samples, cfg.diagnosis.rsrp_weak_dbm, cfg.diagnosis.handover_changes) == (12, -105.5, 4)
    p.write_text("diagnosis:\n  min_samples: banyak\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)
    p.write_text("diagnosis:\n  signal_lost_fraction: 2\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)
