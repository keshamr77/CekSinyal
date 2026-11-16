"""Identitas sel: semua kolom dari satu blok, band konsisten dengan EARFCN."""

from pathlib import Path

import pytest

from ceksinyal.collector.parser import LTE_BANDS, lte_band_from_earfcn, parse_registry

FIXTURES = Path(__file__).parent / "fixtures"
INFINIX = (FIXTURES / "real_infinix_android14_dualsim.txt").read_text(encoding="utf-8", errors="replace")

HEAD = (
    "  Phone Id=0\n"
    "    mServiceState={{mVoiceRegState=0(IN_SERVICE), mDataRegState=0(IN_SERVICE){extra}}}\n"
    "    mSignalStrength=SignalStrength:{{mLte=CellSignalStrengthLte: rssi=-70 rsrp=-95 rsrq=-10 "
    "rssnr=8 level=3,mNr=Invalid}}\n"
)


def _ident(ci="*****", pci="*****", earfcn="*****", bands=None) -> str:
    b = f" mBands=[{bands}]" if bands is not None else ""
    return f"CellIdentityLte:{{ mCi={ci} mPci={pci} mTac=1 mEarfcn={earfcn}{b} mBandwidth=20000 mMcc=510 mMnc=10}}"


def _text(*lines: str, extra: str = "") -> str:
    return HEAD.format(extra=extra) + "".join(f"    {line}\n" for line in lines)


def _nri(domain: str, ident: str) -> str:
    return f", mNetworkRegistrationInfos=[NetworkRegistrationInfo{{ domain={domain} transportType=WWAN cellIdentity={ident} x=1}}]"


def _cellinfo(ident: str) -> str:
    return (
        f"mCellInfo=[CellInfoLte:{{mRegistered=YES mTimeStamp=1ns {ident} "
        "CellSignalStrengthLte: rssi=-61 rsrp=-72 rsrq=-11 rssnr=0 level=4}]"
    )


def _fields(r):
    return (r.pci, r.cell_id, r.earfcn, r.band)


# ------------------------------------------------------------ fixture asli Infinix

def test_infinix_sim1():
    r = parse_registry(INFINIX, phone_id=0)
    assert (r.rat, r.rsrp, r.rsrq, r.sinr, r.sinr_unit) == ("LTE", -66, -10, 10.0, "db")
    assert _fields(r) == (50, 110079775, 1850, 3)


def test_infinix_sim2():
    r = parse_registry(INFINIX, phone_id=1)
    assert (r.rat, r.rsrp, r.rsrq) == ("LTE", -82, -12)
    assert _fields(r) == (329, 102782025, 1625, 3)
    # TODO: rssnr=0 di SIM 2 (juga di semua CellInfo) kemungkinan nilai kosong, bukan 0 dB sungguhan.
    assert (r.sinr, r.sinr_raw) == (0.0, 0)


def test_infinix_slot3_off():
    r = parse_registry(INFINIX, phone_id=2)
    assert r.ok and r.rat == "NONE"


# ------------------------------------------------------------ satu blok

def test_band_follows_earfcn_when_mbands_disagrees():
    r = parse_registry(_text("mCellIdentity=" + _ident(ci=123, pci=7, earfcn=1850, bands=1)))
    assert _fields(r) == (7, 123, 1850, 3)


def test_mbands_used_when_earfcn_not_in_table():
    r = parse_registry(_text("mCellIdentity=" + _ident(ci=123, pci=7, earfcn=66500, bands=66)))
    assert _fields(r) == (7, 123, 66500, 66)


def test_mbands_used_when_no_earfcn_at_all():
    r = parse_registry(_text("mCellIdentity=" + _ident(ci=123, bands=8)))
    assert _fields(r) == (None, 123, None, 8)


def test_masked_block_band_corrected_by_channel_number():
    # Pola mCellIdentity Infinix sendirian: EARFCN disembunyikan, mBands salah, mChannelNumber benar.
    r = parse_registry(_text("mCellIdentity=" + _ident(ci=123, bands=1), extra=", mChannelNumber=1850"))
    assert _fields(r) == (None, 123, 1850, 3)


def test_columns_not_mixed_between_blocks_with_different_cell_id():
    # Blok A lengkap sebagian (cell 111, tanpa pci), blok B sel lain yang lengkap tapi dari CellInfo.
    r = parse_registry(_text(
        "mCellIdentity=" + _ident(ci=111, earfcn=1850, bands=3),
        _cellinfo(_ident(ci=222, pci=9, earfcn=500, bands=1)),
    ))
    # CellInfo punya skor lebih tinggi (3 > 2), jadi SEMUA kolom dari CellInfo.
    assert _fields(r) == (9, 222, 500, 1)


def test_tie_prefers_source_order_and_does_not_borrow_from_other_cell():
    r = parse_registry(_text(
        "mCellIdentity=" + _ident(ci=111, earfcn=1850),
        _cellinfo(_ident(ci=222, pci=9, bands=1)),
    ))
    # Seri (2 vs 2): mCellIdentity menang; pci TIDAK dipinjam dari sel 222.
    assert _fields(r) == (None, 111, 1850, 3)


def test_missing_column_filled_from_block_with_same_cell_id():
    r = parse_registry(_text(
        "mCellIdentity=" + _ident(ci=111, earfcn=3500),
        _cellinfo(_ident(ci=111, pci=9)),
    ))
    assert _fields(r) == (9, 111, 3500, 8)


def test_no_fill_without_cell_id():
    r = parse_registry(_text(
        "mCellIdentity=" + _ident(earfcn=3500),
        _cellinfo(_ident(pci=9)),
    ))
    assert _fields(r) == (None, None, 3500, 8)


def test_source_order_nri_ps_cs_identity_barring_cellinfo():
    blocks = {
        "ps": _nri("PS", _ident(ci=1, pci=1, earfcn=100)),
        "cs": _nri("CS", _ident(ci=2, pci=2, earfcn=700)),
    }
    ident = "mCellIdentity=" + _ident(ci=3, pci=3, earfcn=1300)
    barring = "mBarringInfo=BarringInfo {mCellIdentity=" + _ident(ci=4, pci=4, earfcn=2500) + ", mBarringServiceInfos={}}"
    info = _cellinfo(_ident(ci=5, pci=5, earfcn=3500))

    def cell(*lines, extra=""):
        return parse_registry(_text(*lines, extra=extra)).cell_id

    # urutan baris di dump tidak berpengaruh
    assert cell(info, barring, ident, extra=blocks["cs"] + blocks["ps"]) == 1
    assert cell(info, barring, ident, extra=blocks["cs"]) == 2
    assert cell(info, barring, ident) == 3
    assert cell(info, barring) == 4
    assert cell(info) == 5


def test_barring_block_not_treated_as_top_level_identity():
    # Skor sama: mCellIdentity tingkat atas harus menang atas mBarringInfo walau muncul belakangan.
    barring = "mBarringInfo=BarringInfo {mCellIdentity=" + _ident(ci=4, pci=4, earfcn=2500) + ", mBarringServiceInfos={}}"
    r = parse_registry(_text(barring, "mCellIdentity=" + _ident(ci=3, pci=3, earfcn=1300)))
    assert _fields(r) == (3, 3, 1300, 3)


def test_nr_single_block():
    text = (
        "    mServiceState={mVoiceRegState=0(IN_SERVICE), mDataRegState=0(IN_SERVICE), "
        "getRilDataRadioTechnology=20(NR)}\n"
        "    mSignalStrength=SignalStrength:{mLte=Invalid,mNr=CellSignalStrengthNr:{ csiRsrp = 2147483647 "
        "ssRsrp = -90 ssRsrq = -11 ssSinr = 15 level = 3 }}\n"
        "    mCellIdentity=CellIdentityNr:{ mPci = 2147483647 mNrArfcn = 2147483647 mBands = [1] mNci = 777 }\n"
        "    mCellInfo=[CellInfoNr:{mRegistered=YES CellIdentityNr:{ mPci = 5 mNrArfcn = 630000 "
        "mBands = [78] mNci = 888 } CellSignalStrengthNr:{ ssRsrp = -91 }}]\n"
    )
    r = parse_registry(text)
    assert r.rat == "NR"
    assert _fields(r) == (5, 888, 630000, 78)  # semua dari CellInfo (skor 3), tidak dicampur dengan sel 777


# ------------------------------------------------------------ tabel band

@pytest.mark.parametrize("band,lo,hi", [
    # 3GPP TS 36.101 Tabel 5.7.3-1 (rentang N_DL) untuk band yang dipakai di Indonesia
    (1, 0, 599), (3, 1200, 1949), (5, 2400, 2649), (8, 3450, 3799), (40, 38650, 39649),
])
def test_lte_band_table_indonesia(band, lo, hi):
    assert (band, lo, hi) in LTE_BANDS
    assert lte_band_from_earfcn(lo) == band and lte_band_from_earfcn(hi) == band
    assert lte_band_from_earfcn(lo - 1) != band and lte_band_from_earfcn(hi + 1) != band
