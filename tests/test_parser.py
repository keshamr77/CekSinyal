import json
from pathlib import Path

import pytest

from ceksinyal.collector.parser import (
    MEASUREMENT_FIELDS,
    parse_registry,
    split_phone_sections,
    lte_band_from_earfcn,
)

FIXTURES = Path(__file__).parent / "fixtures"
ALL_FIXTURES = sorted(FIXTURES.glob("*.txt"))
VALID_RATS = {"LTE", "NR", "WCDMA", "GSM", "NONE"}


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _expected_path(fixture: Path) -> Path:
    return fixture.with_name(fixture.stem + ".expected.json")


def test_fixtures_exist():
    assert any(p.name.startswith("synthetic_") for p in ALL_FIXTURES)


@pytest.mark.parametrize("fixture", ALL_FIXTURES, ids=lambda p: p.name)
def test_fixture_matches_expected(fixture: Path):
    """Setiap fixture dengan file .expected.json harus cocok persis untuk kunci yang ditulis."""
    expected_file = _expected_path(fixture)
    if not expected_file.exists():
        pytest.skip("tidak ada .expected.json")
    expected = json.loads(expected_file.read_text(encoding="utf-8"))
    reading = parse_registry(fixture.read_text(encoding="utf-8", errors="replace"))
    actual = {key: getattr(reading, key) for key in expected}
    assert actual == expected, reading.reason


@pytest.mark.parametrize("fixture", ALL_FIXTURES, ids=lambda p: p.name)
def test_fixture_invariants(fixture: Path):
    """Berlaku untuk semua fixture, termasuk real_*.txt yang ditambahkan nanti."""
    reading = parse_registry(fixture.read_text(encoding="utf-8", errors="replace"))
    if not _expected_path(fixture).exists():
        # Fixture asli tanpa .expected.json minimal harus bisa di-parse.
        assert reading.ok, f"{fixture.name}: {reading.reason}"
    if reading.ok:
        assert reading.rat in VALID_RATS
    else:
        assert reading.rat is None and reading.reason
    for name in MEASUREMENT_FIELDS[1:]:
        value = getattr(reading, name)
        assert value is None or value != 2147483647, name
    if reading.rat == "NONE":
        assert all(getattr(reading, n) is None for n in MEASUREMENT_FIELDS[1:])
    if reading.rat in ("LTE", "NR"):
        assert reading.rsrp is not None


def test_local_logs_are_ignored():
    # Fixture Android 10 memuat mSignalStrength lain (rsrp=-122) di bagian local logs.
    assert parse_registry(_read("synthetic_lte_android10.txt")).rsrp == -98


def test_dualsim_select_phone_id():
    text = _read("synthetic_lte_android13_dualsim.txt")
    assert len(split_phone_sections(text)) == 2
    sim2 = parse_registry(text, phone_id=1)
    assert sim2.ok and sim2.rat == "NONE" and sim2.phone_id == 1
    missing = parse_registry(text, phone_id=5)
    assert not missing.ok and "Phone Id=5" in missing.reason


def test_dualsim_prefers_section_with_signal_even_if_listed_second():
    text = _read("synthetic_lte_android13_dualsim.txt")
    header, rest = text.split("  Phone Id=0", 1)
    sim0, sim1 = rest.split("  Phone Id=1", 1)
    swapped = header + "  Phone Id=1" + sim1.split("  mCarrierNetworkChangeState")[0] + "  Phone Id=0" + sim0
    reading = parse_registry(swapped)
    assert reading.phone_id == 0 and reading.rat == "LTE"


def test_nr_nsa_keeps_lte_values():
    reading = parse_registry(_read("synthetic_nr_nsa_android12.txt"))
    assert reading.rat == "LTE" and reading.nr_nsa
    assert reading.rsrp == -92  # nilai LTE anchor, bukan NR


def test_masked_and_unavailable_identity_become_none():
    reading = parse_registry(_read("synthetic_unavailable_values_android12.txt"))
    assert (reading.pci, reading.cell_id, reading.earfcn, reading.band) == (None, None, None, None)


@pytest.mark.parametrize("text", ["", "   \n", "Can't find service: telephony.registry\n", "\x00\xff garbage {{{ ]]"])
def test_garbage_input_never_raises(text):
    reading = parse_registry(text)
    assert not reading.ok and reading.reason


def test_permission_denial():
    text = "Permission Denial: can't dump telephony.registry from pid=123, uid=2000\n"
    reading = parse_registry(text)
    assert not reading.ok and "mSignalStrength" in reading.reason


def test_in_service_without_valid_signal_is_failure():
    text = (
        "  Phone Id=0\n"
        "    mServiceState={mVoiceRegState=0(IN_SERVICE), mDataRegState=0(IN_SERVICE)}\n"
        "    mSignalStrength=SignalStrength:{mLte=CellSignalStrengthLte: rssi=2147483647 "
        "rsrp=2147483647 rsrq=2147483647 rssnr=2147483647 level=0,mNr=Invalid}\n"
    )
    reading = parse_registry(text)
    assert not reading.ok and reading.rat is None


def test_unknown_service_state_and_no_signal_is_none():
    text = "    mSignalStrength=SignalStrength:{mLte=CellSignalStrengthLte: rssi=2147483647 rsrp=2147483647}\n"
    reading = parse_registry(text)
    assert reading.ok and reading.rat == "NONE" and reading.phone_id is None


def test_out_of_range_values_become_none():
    text = (
        "    mServiceState={mVoiceRegState=0(IN_SERVICE), mDataRegState=0(IN_SERVICE)}\n"
        "    mSignalStrength=SignalStrength:{mLte=CellSignalStrengthLte: rssi=-200 rsrp=-95 "
        "rsrq=-90 rssnr=999 level=3}\n"
        "    mCellIdentity=CellIdentityLte:{ mCi=999999999999 mPci=600 mEarfcn=-5 mBands=[0]}\n"
    )
    r = parse_registry(text)
    assert r.rat == "LTE" and r.rsrp == -95
    assert (r.rsrq, r.sinr, r.rssi, r.pci, r.cell_id, r.earfcn, r.band) == (None,) * 7


def test_cellinfo_fallback_when_signal_strength_invalid():
    text = (
        "    mServiceState={mVoiceRegState=0(IN_SERVICE), mDataRegState=0(IN_SERVICE)}\n"
        "    mSignalStrength=SignalStrength:{mLte=Invalid,mNr=Invalid}\n"
        "    mCellInfo=[CellInfoLte:{mRegistered=YES CellIdentityLte:{ mCi=123 mPci=7 mEarfcn=38750 "
        "mAdditionalPlmns={} } CellSignalStrengthLte: rssi=-70 rsrp=-101 rsrq=-12 rssnr=5 level=2}]\n"
    )
    r = parse_registry(text)
    assert (r.rat, r.rsrp, r.sinr, r.pci, r.cell_id, r.band) == ("LTE", -101, 5.0, 7, 123, 40)


def test_wcdma():
    text = (
        "    mServiceState={mVoiceRegState=0(IN_SERVICE), mDataRegState=0(IN_SERVICE)}\n"
        "    mSignalStrength=SignalStrength:{mGsm=Invalid,mWcdma=CellSignalStrengthWcdma: ss=-81 "
        "ber=99 rscp=-89 ecno=-6 mLevel=3,mLte=Invalid,mNr=Invalid}\n"
        "    mCellIdentity=CellIdentityWcdma:{ mLac=1234 mCid=567890 mPsc=101 mUarfcn=10638}\n"
    )
    r = parse_registry(text)
    assert (r.rat, r.rssi, r.cell_id, r.rsrp) == ("WCDMA", -81, 567890, None)


def test_android9_sinr_units():
    # rssnr=45 di Android 9 berarti 4,5 dB
    assert parse_registry(_read("synthetic_lte_android9_legacy.txt")).sinr == 4.5


@pytest.mark.parametrize("earfcn,band", [(275, 1), (1850, 3), (3500, 8), (38750, 40), (None, None), (99999, None)])
def test_lte_band_from_earfcn(earfcn, band):
    assert lte_band_from_earfcn(earfcn) == band


def _masked_lte(service_state: str) -> str:
    """HP yang menyembunyikan identitas sel (pola Xiaomi MIUI): hanya mChannelNumber yang tersisa."""
    return (
        "  Phone Id=0\n"
        f"    mServiceState={{mVoiceRegState=0(IN_SERVICE), mDataRegState=0(IN_SERVICE), {service_state}}}\n"
        "    mSignalStrength=SignalStrength:{mLte=CellSignalStrengthLte: rssi=2147483647 rsrp=-101 "
        "rsrq=-11 rssnr=55 level=3,mNr=Invalid}\n"
        "    mCellInfo=[CellInfoLte:{mRegistered=YES CellIdentityLte:{ mBandwidth=2147483647 mMcc=510 "
        "mMnc=10 mAlphaLong=Telkomsel} CellSignalStrengthLte: rssi=2147483647 rsrp=-100 rsrq=-13}]\n"
    )


def test_real_redmi9c_earfcn_from_service_state():
    r = parse_registry(_read("real_redmi9c_android10.txt"))
    assert (r.pci, r.cell_id) == (None, None)
    assert (r.earfcn, r.band) == (38750, 40)


def test_masked_identity_ignores_channel_number_in_local_logs():
    # Local logs memuat mChannelNumber=38750 dan 500; yang dipakai harus service state saat ini (1850).
    r = parse_registry(_read("synthetic_lte_masked_identity_android10.txt"))
    assert (r.earfcn, r.band) == (1850, 3)


@pytest.mark.parametrize(
    "service_state,earfcn,band",
    [
        ("mChannelNumber=38750, duplexMode()=2, getRilDataRadioTechnology=14(LTE)", 38750, 40),
        ("mChannelNumber=1850, duplexMode()=1", 1850, 3),
        ("mChannelNumber=2147483647, duplexMode()=0", None, None),
        ("mChannelNumber=-1, duplexMode()=0", None, None),
        ("mChannelNumber=999999, duplexMode()=1", None, None),
        ("mChannelNumber=10638, getRilDataRadioTechnology=3(UMTS)", None, None),
        ("duplexMode()=0", None, None),
    ],
)
def test_masked_identity_channel_number_fallback(service_state, earfcn, band):
    r = parse_registry(_masked_lte(service_state))
    assert r.rat == "LTE" and r.rsrp == -101
    assert (r.pci, r.cell_id, r.earfcn, r.band) == (None, None, earfcn, band)


def test_channel_number_only_in_local_logs_is_ignored():
    text = _masked_lte("duplexMode()=0") + (
        "local logs:\n"
        "  2026-11-14T08:00:01.123 - notifyServiceStateForSubscriber: subId=1 phoneId=0 "
        "state={mVoiceRegState=0(IN_SERVICE), mChannelNumber=38750}\n"
        "  mServiceState={mVoiceRegState=0(IN_SERVICE), mChannelNumber=1850}\n"
    )
    assert parse_registry(text).earfcn is None


def test_identity_earfcn_takes_precedence_over_channel_number():
    text = _masked_lte("mChannelNumber=1850").replace("mBandwidth=2147483647", "mEarfcn=3500 mBandwidth=2147483647")
    r = parse_registry(text)
    assert (r.earfcn, r.band) == (3500, 8)


def _lte_rssnr(rssnr: int) -> str:
    return (
        "  Phone Id=0\n"
        "    mServiceState={mVoiceRegState=0(IN_SERVICE), mDataRegState=0(IN_SERVICE)}\n"
        "    mSignalStrength=SignalStrength:{mLte=CellSignalStrengthLte: rssi=-70 rsrp=-101 "
        f"rsrq=-11 rssnr={rssnr} cqi=0 ta=0 level=3,mNr=Invalid}}\n"
    )


@pytest.mark.parametrize("rssnr,sinr", [(25, 2.5), (-15, -1.5), (55, 5.5), (300, 30.0), (-200, -20.0), (2147483647, None)])
def test_lte_sinr_deci_db(rssnr, sinr):
    # Android 10 ke bawah: rssnr selalu dalam 0,1 dB, termasuk nilai kecil yang terlihat seperti dB.
    r = parse_registry(_lte_rssnr(rssnr), lte_sinr_unit="deci_db")
    assert r.sinr == sinr
    assert (r.sinr_raw, r.sinr_unit) == ((rssnr, "deci_db") if sinr is not None else (None, None))


@pytest.mark.parametrize("rssnr,sinr", [(5, 5.0), (25, 25.0), (-15, -15.0), (55, None), (2147483647, None)])
def test_lte_sinr_db(rssnr, sinr):
    r = parse_registry(_lte_rssnr(rssnr), lte_sinr_unit="db")
    assert r.sinr == sinr
    if rssnr != 2147483647:
        assert (r.sinr_raw, r.sinr_unit) == (rssnr, "db")  # nilai mentah tetap disimpan


@pytest.mark.parametrize("rssnr,sinr,unit", [(25, 25.0, "db"), (55, 5.5, "deci_db"), (999, None, "db")])
def test_lte_sinr_without_unit_guesses_per_sample(rssnr, sinr, unit):
    r = parse_registry(_lte_rssnr(rssnr))
    assert (r.sinr, r.sinr_unit) == (sinr, unit)


def test_real_redmi9c_deci_db():
    r = parse_registry(_read("real_redmi9c_android10.txt"), lte_sinr_unit="deci_db")
    assert (r.sinr, r.sinr_raw, r.sinr_unit) == (5.5, 55, "deci_db")


def test_cellinfo_fallback_uses_unit():
    text = (
        "    mServiceState={mVoiceRegState=0(IN_SERVICE), mDataRegState=0(IN_SERVICE)}\n"
        "    mSignalStrength=SignalStrength:{mLte=Invalid,mNr=Invalid}\n"
        "    mCellInfo=[CellInfoLte:{mRegistered=YES CellIdentityLte:{ mCi=123 } "
        "CellSignalStrengthLte: rssi=-70 rsrp=-101 rsrq=-12 rssnr=25 level=2}]\n"
    )
    assert parse_registry(text, lte_sinr_unit="deci_db").sinr == 2.5


@pytest.mark.parametrize("unit", [None, "db", "deci_db"])
def test_android9_legacy_always_deci_db(unit):
    r = parse_registry(_read("synthetic_lte_android9_legacy.txt"), lte_sinr_unit=unit)
    assert (r.sinr, r.sinr_raw, r.sinr_unit) == (4.5, 45, "deci_db")


@pytest.mark.parametrize("unit", [None, "db", "deci_db"])
def test_nr_sinr_not_affected_by_lte_unit(unit):
    sa = parse_registry(_read("synthetic_nr_sa_android14.txt"), lte_sinr_unit=unit)
    assert (sa.rat, sa.sinr, sa.sinr_unit) == ("NR", 18.0, "db")
    nsa = parse_registry(_read("synthetic_nr_nsa_android12.txt"), lte_sinr_unit=unit)
    assert nsa.nr_sinr == 8.0


def test_invalid_unit_raises():
    with pytest.raises(ValueError):
        parse_registry(_lte_rssnr(5), lte_sinr_unit="dB?")


def test_partially_masked_identity_is_none():
    # Samsung Android 15 menyamarkan sebagian: mCi=1******19 tidak boleh terbaca sebagai cell_id 1.
    text = (
        "    mServiceState={mVoiceRegState=0(IN_SERVICE), mDataRegState=0(IN_SERVICE)}\n"
        "    mSignalStrength=SignalStrength:{mLte=CellSignalStrengthLte: rssi=-51 rsrp=-76 rsrq=-10 rssnr=14}\n"
        "    mCellIdentity=CellIdentityLte:{ mCi=1******19 mPci=474 mTac=2**5 mEarfcn=1850 mBands=[3] }\n"
    )
    r = parse_registry(text)
    assert (r.cell_id, r.pci, r.earfcn, r.band) == (None, 474, 1850, 3)


def test_real_samsung_s22_sim2():
    r = parse_registry(_read("real_samsung_s22_android15_dualsim.txt"), phone_id=1)
    assert r.ok and r.rat == "LTE" and r.operator_alpha == "INDOSATOOREDOO"
    assert (r.rsrp, r.pci, r.cell_id, r.earfcn, r.band) == (-83, 115, None, 1475, 3)
