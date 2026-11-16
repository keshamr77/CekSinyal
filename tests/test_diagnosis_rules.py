"""Setiap cabang pohon keputusan, dengan data sintetis."""

import pytest

from ceksinyal.diagnosis import rules
from ceksinyal.diagnosis.rules import Sample, Thresholds, count_changes, evaluate


def lte(n=30, rsrp=-90, sinr=10.0, cell_id=100, earfcn=1850):
    return [Sample("LTE", rsrp, sinr, cell_id, earfcn) for _ in range(n)]


def none(n):
    return [Sample("NONE") for _ in range(n)]


def test_normal():
    r = evaluate(lte(60), [20.0], [20.0] * 6)
    assert r.result == rules.NORMAL
    assert (r.n_samples, r.median_rsrp, r.median_sinr, r.median_download, r.cell_changes) == (60, -90, 10.0, 20.0, 0)
    assert r.notes == [] and r.advice


def test_insufficient_samples():
    r = evaluate(lte(19, rsrp=-130))
    assert r.result == rules.INSUFFICIENT and r.n_samples == 19
    assert r.median_rsrp is None and "19 sampel" in r.detail


def test_empty_window_is_insufficient():
    assert evaluate([]).result == rules.INSUFFICIENT


def test_exactly_min_samples_is_diagnosed():
    assert evaluate(lte(20)).result == rules.NORMAL


def test_signal_lost_at_half():
    r = evaluate(lte(15, rsrp=-120) + none(15))
    assert r.result == rules.LOST and "15 dari 30" in r.notes[0]
    assert r.median_rsrp == -120  # nilai dari sampel yang masih ada tetap dicatat


def test_signal_lost_just_below_half_falls_through():
    r = evaluate(lte(16) + none(14))
    assert r.result == rules.NORMAL


def test_all_samples_lost():
    r = evaluate(none(40))
    assert r.result == rules.LOST and r.median_rsrp is None and r.cell_changes is None


def test_weak_coverage_before_interference():
    r = evaluate(lte(30, rsrp=-115, sinr=-5.0))
    assert r.result == rules.WEAK


def test_rsrp_boundary_not_weak():
    assert evaluate(lte(30, rsrp=-110)).result == rules.NORMAL


def test_interference():
    r = evaluate(lte(30, rsrp=-95, sinr=-2.5))
    assert r.result == rules.INTERFERENCE and r.median_sinr == -2.5


def test_sinr_boundary_not_interference():
    assert evaluate(lte(30, sinr=0.5)).result == rules.NORMAL


def test_congested():
    r = evaluate(lte(30), [4.0], [10.0] * 6)
    assert r.result == rules.CONGESTED and r.median_download == 4.0


def test_congestion_boundary_and_skips():
    assert evaluate(lte(30), [5.0], [10.0] * 6).result == rules.NORMAL  # tepat 50% bukan sel padat
    few = evaluate(lte(30), [1.0], [10.0] * 5)
    assert few.result == rules.NORMAL and "pembanding harian tidak tersedia (5 data" in few.detail
    no_speed = evaluate(lte(30), [], [10.0] * 6)
    assert no_speed.result == rules.NORMAL and "kecepatan: tidak tersedia" in no_speed.detail


def test_handover_by_cell_id():
    samples = lte(30)
    for i, s in enumerate(samples):
        s.cell_id = 100 + (i // 6) % 2  # 100,101,100,101,100 -> 4 pergantian
    r = evaluate(samples, [20.0], [20.0] * 6)
    assert r.result == rules.HANDOVER and r.cell_changes == 4
    assert "earfcn" not in r.detail


def test_three_changes_is_not_handover():
    samples = lte(30)
    for i, s in enumerate(samples):
        s.cell_id = 100 + min(i // 8, 3)
    r = evaluate(samples)
    assert r.result == rules.NORMAL and r.cell_changes == 3


def test_handover_by_earfcn_when_cell_id_hidden():
    samples = lte(30, cell_id=None)
    for i, s in enumerate(samples):
        s.earfcn = (1850, 38750)[(i // 5) % 2]
    r = evaluate(samples)
    assert r.result == rules.HANDOVER and r.cell_changes == 5
    assert "dihitung dari earfcn, cell_id tidak tersedia" in r.detail


def test_cell_id_preferred_over_earfcn():
    samples = lte(30)
    for i, s in enumerate(samples):
        s.earfcn = (1850, 38750)[i % 2]  # earfcn bolak-balik, cell_id tetap
    r = evaluate(samples)
    assert r.result == rules.NORMAL and r.cell_changes == 0


def test_handover_unavailable():
    r = evaluate(lte(30, cell_id=None, earfcn=None))
    assert r.result == rules.NORMAL and r.cell_changes is None
    assert "handover: tidak tersedia" in r.detail


def test_sparse_earfcn_counts_lower_bound():
    # Pola Xiaomi: earfcn hanya di sebagian kecil sampel. Nilai kosong bukan pergantian.
    samples = lte(40, cell_id=None, earfcn=None)
    for i, e in zip((0, 7, 15, 22, 30, 38), (1850, 38750, 1850, 38750, 1850, 38750)):
        samples[i].earfcn = e
    r = evaluate(samples)
    assert r.result == rules.HANDOVER and r.cell_changes == 5
    assert "batas bawah" in r.detail and "6 dari 40" in r.detail


def test_rsrp_unavailable_skips_rule():
    r = evaluate(lte(30, rsrp=None, sinr=-5.0))
    assert r.result == rules.INTERFERENCE and "rsrp: tidak tersedia" in r.detail


def test_sinr_unavailable_skips_rule():
    r = evaluate(lte(30, sinr=None))
    assert r.result == rules.NORMAL and r.median_sinr is None and "sinr: tidak tersedia (0 nilai)" in r.detail


def test_sinr_always_zero_is_unavailable_not_zero_db():
    # Infinix SIM 2: rssnr=0 di semua sampel, kemungkinan nilai kosong.
    r = evaluate(lte(30, sinr=0.0), th=Thresholds(sinr_interference_db=3))
    assert r.result == rules.NORMAL and r.median_sinr is None
    assert "sinr: tidak tersedia (selalu 0" in r.detail


def test_sinr_sometimes_zero_is_real():
    samples = lte(30, sinr=0.0)
    samples[3].sinr = -1.0
    r = evaluate(samples, th=Thresholds(sinr_interference_db=3))
    assert r.result == rules.INTERFERENCE and r.median_sinr == 0.0


def test_thresholds_are_configurable():
    th = Thresholds(min_samples=5, rsrp_weak_dbm=-85, handover_changes=1)
    assert evaluate(lte(5), th=th).result == rules.WEAK
    assert evaluate(lte(5, rsrp=-80), th=th).result == rules.NORMAL


@pytest.mark.parametrize("values,expected", [
    ([], 0), ([1], 0), ([1, 1, 1], 0), ([1, 2, 1], 2), ([None, 1, None, 1, None, 2], 1), ([None, None], 0),
])
def test_count_changes(values, expected):
    assert count_changes(values) == expected


def test_every_result_has_advice():
    for name in (*rules.SEVERITY, rules.INSUFFICIENT):
        assert rules.ADVICE[name]
