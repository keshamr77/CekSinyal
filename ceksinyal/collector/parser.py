"""Parser toleran untuk output `dumpsys telephony.registry`.

Format output berbeda antar-versi Android (9-14) dan merek HP, jadi semua nilai diambil
dengan regex per kunci, bukan berdasarkan posisi kolom. Nilai yang tidak ada, bernilai
"unavailable" (2147483647, dsb.), disamarkan (`mCi=*****`, atau sebagian seperti Samsung
`mCi=1******19`), atau di luar rentang fisik
menjadi None.

Aturan RAT:
- Service state jelas tidak dalam layanan  -> rat NONE, semua nilai None.
- NR valid dan (LTE tidak valid atau teknologi data = NR) -> NR (SA).
- LTE valid -> LTE (kalau NR juga ada: nr_nsa=True, nilai NR hanya informasi).
- Lalu WCDMA, lalu GSM.
- Tidak ada nilai valid: NONE, kecuali HP mengaku dalam layanan -> ok=False
  (kemungkinan parser tidak cocok; collector menyimpan output mentah).

Satuan SINR LTE (rssnr): modem selalu melapor dalam 0,1 dB. AOSP baru mengonversinya ke dB
mulai Android 11 (SDK 30, convertRssnrUnitFromTenDbToDB). Jadi Android 10 ke bawah = 0,1 dB,
Android 11+ = dB. Satuannya tidak bisa ditebak dengan andal dari satu sampel (nilai mentah 25
bisa 25 dB atau 2,5 dB), sehingga collector menentukannya per HP dari SDK dan mengirimnya lewat
`lte_sinr_unit`. Format Android 9 (kunci `ss=` atau kolom posisi) selalu 0,1 dB. SINR NR (dB)
tidak terpengaruh.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

UNAVAILABLE = frozenset({2**31 - 1, -(2**31), 2**63 - 1, -(2**63)})

# Rentang valid (inklusif), mengikuti dokumentasi android.telephony.
RSRP_LTE = (-140, -43)
RSRQ_LTE = (-34, 3)
SINR_LTE = (-20.0, 30.0)
RSSI = (-113, -51)
RSRP_NR = (-156, -31)
RSRQ_NR = (-43, 20)
SINR_NR = (-23.0, 40.0)
RSCP_WCDMA = (-120, -24)
PCI_LTE = (0, 503)
PCI_NR = (0, 1007)
CI_LTE = (0, 268_435_455)
NCI_NR = (0, 68_719_476_735)
CID_WCDMA = (0, 268_435_455)
CID_GSM = (0, 65_535)
EARFCN = (0, 262_143)
NRARFCN = (0, 3_279_165)
BAND = (1, 1024)

# Band LTE berdasarkan EARFCN downlink (3GPP TS 36.101 Tabel 5.7.3-1, N_Offs-DL sampai batas atas
# rentang N_DL), untuk band yang umum dipakai. Indonesia: 1 (2100), 3 (1800), 5 (850), 8 (900),
# 40 (2300 TDD). Dipakai untuk HP yang tidak menampilkan mBands dan untuk mengoreksi mBands yang
# tidak cocok dengan EARFCN.
LTE_BANDS = (
    (1, 0, 599), (2, 600, 1199), (3, 1200, 1949), (4, 1950, 2399), (5, 2400, 2649),
    (7, 2750, 3449), (8, 3450, 3799), (20, 6150, 6449), (28, 9210, 9659),
    (38, 37750, 38249), (39, 38250, 38649), (40, 38650, 39649), (41, 39650, 41589),
)

SINR_UNIT_DB = "db"
SINR_UNIT_DECI_DB = "deci_db"
SINR_UNITS = (SINR_UNIT_DB, SINR_UNIT_DECI_DB)

MEASUREMENT_FIELDS = (
    "rat", "rsrp", "rsrq", "sinr", "rssi", "pci", "cell_id", "earfcn", "band", "sinr_raw", "sinr_unit",
)


@dataclass
class SignalReading:
    rat: str | None = None
    rsrp: int | None = None
    rsrq: int | None = None
    sinr: float | None = None
    rssi: int | None = None
    pci: int | None = None
    cell_id: int | None = None
    earfcn: int | None = None
    band: int | None = None
    sinr_raw: int | None = None
    sinr_unit: str | None = None
    ok: bool = False
    reason: str | None = None
    in_service: bool | None = None
    phone_id: int | None = None
    sections: int = 1
    operator_alpha: str | None = None
    data_connected: bool = False
    nr_nsa: bool = False
    nr_rsrp: int | None = None
    nr_sinr: float | None = None

    def measurement_fields(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in MEASUREMENT_FIELDS}


@dataclass
class _Sig:
    found: bool = False
    rsrp: int | None = None
    rsrq: int | None = None
    sinr: float | None = None
    rssi: int | None = None
    rscp: int | None = None
    sinr_raw: int | None = None
    sinr_unit: str | None = None


# ---------------------------------------------------------------- helper umum

def _key_re(key: str) -> re.Pattern[str]:
    # `(?!\*)`: nilai yang disamarkan sebagian (Samsung Android 15: `mCi=1******19`, `mTac=2**5`)
    # tidak boleh terbaca sebagai digit pertamanya saja; kunci itu dianggap tidak ada.
    return re.compile(rf"\b(?:{key})\s*[=:]\s*(-?\d+)\b(?!\*)", re.IGNORECASE)


def _int(key: str, text: str) -> int | None:
    m = _key_re(key).search(text)
    return int(m.group(1)) if m else None


def _has_key(key: str, text: str) -> bool:
    return re.search(rf"\b(?:{key})\s*[=:]", text, re.IGNORECASE) is not None


def _valid(value: int | float | None, rng: tuple[float, float]) -> int | float | None:
    if value is None or value in UNAVAILABLE:
        return None
    return value if rng[0] <= value <= rng[1] else None


def _rssi(value: int | None) -> int | None:
    """RSSI dalam dBm; nilai 0..31 dianggap ASU (format lama) dan dikonversi."""
    if value is None or value in UNAVAILABLE:
        return None
    if RSSI[0] <= value <= RSSI[1]:
        return value
    if 0 <= value <= 31:
        return -113 + 2 * value
    return None


def _lte_sinr(value: int | None, unit: str | None) -> tuple[float | None, str | None]:
    """RSSNR LTE -> (SINR dalam dB, satuan yang dipakai).

    unit "deci_db" (Android 10 ke bawah): selalu dibagi 10. unit "db" (Android 11+): apa adanya.
    unit None (info HP tidak ada, mis. test/`probe --file`): tebakan per sampel, dibagi 10 hanya
    kalau nilai mentah di luar rentang dB. Tebakan ini salah untuk SINR Android 10 antara -2 dan
    3 dB, jadi collector selalu memberikan unit.
    """
    if value is None or value in UNAVAILABLE:
        return None, None
    v = float(value)
    if unit is None:
        in_db_range = SINR_LTE[0] <= v <= SINR_LTE[1]
        unit = SINR_UNIT_DB if in_db_range or not -200 <= v <= 300 else SINR_UNIT_DECI_DB
    if unit == SINR_UNIT_DECI_DB:
        v = round(v / 10, 1)
    return (v if SINR_LTE[0] <= v <= SINR_LTE[1] else None), unit


def _first(*values):
    for v in values:
        if v is not None:
            return v
    return None


def _field_line(section: str, name: str) -> str | None:
    m = re.search(rf"(?m)^[ \t]*{name}\s*=(.*)$", section)
    return m.group(1) if m else None


def _brace_block(text: str, open_idx: int) -> str:
    """Isi kurung kurawal yang dibuka di text[open_idx] (sadar kurung bersarang)."""
    depth = 0
    for i in range(open_idx, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i]
    return text[open_idx + 1:open_idx + 2000]


def _segment(text: str, marker: str) -> str | None:
    """Teks setelah `CellSignalStrengthXxx:` sampai penanda CellSignalStrength berikutnya."""
    m = re.search(rf"{marker}\s*:", text)
    if not m:
        return None
    start = m.end()
    nxt = re.search(r"CellSignalStrength\w+\s*:|,\s*primary\s*=", text[start:])
    return text[start:start + nxt.start()] if nxt else text[start:]


# ---------------------------------------------------------------- sinyal

def _lte_signal(seg: str | None, sinr_unit: str | None) -> _Sig:
    if seg is None:
        return _Sig()
    # Android 9: kunci `ss=` (ASU) dan rssnr selalu dalam 0,1 dB.
    if _has_key("m?ss", seg) and not _has_key("m?rssi", seg):
        sinr_unit = SINR_UNIT_DECI_DB
    raw = _int("m?rssnr|m?sinr|m?snr", seg)
    sinr, unit = _lte_sinr(raw, sinr_unit)
    return _Sig(
        found=_has_key("m?rsrp", seg),
        rsrp=_valid(_int("m?rsrp", seg), RSRP_LTE),
        rsrq=_valid(_int("m?rsrq", seg), RSRQ_LTE),
        sinr=sinr,
        rssi=_first(_rssi(_int("m?rssi", seg)), _rssi(_int("m?ss", seg))),
        sinr_raw=raw if unit else None,
        sinr_unit=unit,
    )


def _nr_signal(seg: str | None) -> _Sig:
    if seg is None:
        return _Sig()
    sinr = _first(_valid(_int("m?ssSinr", seg), SINR_NR), _valid(_int("m?csiSinr", seg), SINR_NR))
    return _Sig(
        sinr_raw=int(sinr) if sinr is not None else None,
        sinr_unit=SINR_UNIT_DB if sinr is not None else None,
        found=_has_key("m?ssRsrp|m?csiRsrp", seg),
        rsrp=_first(_valid(_int("m?ssRsrp", seg), RSRP_NR), _valid(_int("m?csiRsrp", seg), RSRP_NR)),
        rsrq=_first(_valid(_int("m?ssRsrq", seg), RSRQ_NR), _valid(_int("m?csiRsrq", seg), RSRQ_NR)),
        sinr=float(sinr) if sinr is not None else None,
    )


def _wcdma_signal(seg: str | None) -> _Sig:
    if seg is None:
        return _Sig()
    return _Sig(
        found=_has_key("m?rscp|m?rssi|m?ss", seg),
        rscp=_valid(_int("m?rscp", seg), RSCP_WCDMA),
        rssi=_first(_rssi(_int("m?rssi", seg)), _rssi(_int("m?ss", seg))),
    )


def _gsm_signal(seg: str | None) -> _Sig:
    if seg is None:
        return _Sig()
    return _Sig(
        found=_has_key("m?rssi|m?ss", seg),
        rssi=_first(_rssi(_int("m?rssi", seg)), _rssi(_int("m?ss", seg))),
    )


def _legacy_positional(sig_line: str) -> tuple[_Sig, _Sig] | None:
    """Format lama: `SignalStrength: 99 0 -120 ... ` (kolom berdasarkan posisi).

    Kolom: 0 gsmSignalStrength(ASU), 7 lteSignalStrength(ASU), 8 lteRsrp, 9 lteRsrq,
    10 lteRssnr (0,1 dB).
    """
    m = re.match(r"\s*SignalStrength\s*:\s+(-?\d+(?:\s+-?\d+){10,})", sig_line)
    if not m:
        return None
    v = [int(x) for x in m.group(1).split()]
    sinr, unit = _lte_sinr(v[10], SINR_UNIT_DECI_DB)
    lte = _Sig(
        found=True,
        rsrp=_valid(v[8], RSRP_LTE),
        rsrq=_valid(v[9], RSRQ_LTE),
        sinr=sinr,
        rssi=_rssi(v[7]),
        sinr_raw=v[10] if unit else None,
        sinr_unit=unit,
    )
    gsm = _Sig(found=True, rssi=_rssi(v[0]))
    return lte, gsm


# ---------------------------------------------------------------- identitas sel

def _registered_infos(section: str, kind: str) -> list[str]:
    """Isi blok CellInfo<kind> dengan mRegistered=YES (sel yang sedang melayani)."""
    blocks = []
    pattern = rf"CellInfo{kind}\s*:?\s*\{{\s*mRegistered\s*=\s*(?:YES|true)\b"
    for m in re.finditer(pattern, section, re.IGNORECASE):
        blocks.append(_brace_block(section, section.index("{", m.start())))
    return blocks


# Urutan kepercayaan sumber identitas sel (kecil = lebih dipercaya). CellInfo paling akhir karena
# bisa basi: di Infinix Android 14 CellInfo "registered" menunjuk sel lain (EARFCN 500) padahal
# mServiceState dan mSignalStrength sudah di sel Band 3 / EARFCN 1850.
SRC_NRI_PS, SRC_NRI_CS, SRC_CELL_IDENTITY, SRC_BARRING, SRC_CELLINFO = range(5)

# kind -> (kunci cell id, rentang, kunci pci, rentang, kunci arfcn, rentang)
_IDENTITY_KEYS = {
    "Lte": ("m?ci", CI_LTE, "m?pci", PCI_LTE, "m?earfcn", EARFCN),
    "Nr": ("m?nci", NCI_NR, "m?pci", PCI_NR, "m?nrArfcn", NRARFCN),
    "Wcdma": ("m?cid", CID_WCDMA, None, None, None, None),
    "Gsm": ("m?cid", CID_GSM, None, None, None, None),
}


@dataclass
class _Ident:
    source: int = SRC_CELLINFO
    cell_id: int | None = None
    pci: int | None = None
    arfcn: int | None = None
    band: int | None = None

    @property
    def score(self) -> int:
        """Kelengkapan: berapa dari cell id, pci, arfcn yang valid."""
        return sum(v is not None for v in (self.cell_id, self.pci, self.arfcn))


def _identity_blocks(section: str, kind: str) -> list[tuple[int, str]]:
    """Semua blok CellIdentity<kind> milik sel yang melayani, sebagai (sumber, isi blok)."""
    ident = rf"CellIdentity{kind}\s*:?\s*\{{"
    blocks: list[tuple[int, str]] = []
    for m in re.finditer(r"NetworkRegistrationInfo\s*\{", section):
        nri = _brace_block(section, m.end() - 1)
        im = re.search(rf"cellIdentity\s*=\s*{ident}", nri)
        if im:
            source = SRC_NRI_PS if re.search(r"domain\s*=\s*PS", nri) else SRC_NRI_CS
            blocks.append((source, _brace_block(nri, im.end() - 1)))
    # mBarringInfo juga berisi "mCellIdentity=": dibedakan dari mCellIdentity tingkat atas lewat posisinya.
    barring = [
        (m.start(), m.end() + len(_brace_block(section, m.end() - 1)))
        for m in re.finditer(r"\bmBarringInfo\s*=\s*BarringInfo\s*\{", section)
    ]
    for m in re.finditer(rf"\bmCellIdentity\s*=\s*{ident}", section):
        in_barring = any(lo <= m.start() <= hi for lo, hi in barring)
        blocks.append((SRC_BARRING if in_barring else SRC_CELL_IDENTITY, _brace_block(section, m.end() - 1)))
    for info in _registered_infos(section, kind):
        m = re.search(ident, info)
        if m:
            blocks.append((SRC_CELLINFO, _brace_block(info, m.end() - 1)))
    return blocks


def _block_band(block: str) -> int | None:
    m = re.search(r"\bm?Bands\s*[=:]\s*\[\s*(\d+)", block, re.IGNORECASE)
    if m and _valid(int(m.group(1)), BAND) is not None:
        return int(m.group(1))
    return None


def _valid_int(key: str | None, rng: tuple[float, float] | None, block: str) -> int | None:
    if key is None or rng is None:
        return None
    value = _valid(_int(key, block), rng)
    return int(value) if value is not None else None


def _pick_identity(section: str, kind: str) -> _Ident:
    """Identitas sel dari SATU blok: skor kelengkapan tertinggi, seri -> urutan sumber.

    Kolom tidak dicampur antar-blok: HP bisa menulis sel yang berbeda di blok yang berbeda (CellInfo
    basi) atau nilai salah di blok yang sebagian disembunyikan (Infinix: mCellIdentity mBands=[1]
    padahal selnya Band 3). Kolom yang kosong di blok terpilih hanya diisi dari blok lain yang
    cell id-nya sama persis.
    """
    ci_key, ci_rng, pci_key, pci_rng, arfcn_key, arfcn_rng = _IDENTITY_KEYS[kind]
    cands = [
        _Ident(
            source=source,
            cell_id=_valid_int(ci_key, ci_rng, block),
            pci=_valid_int(pci_key, pci_rng, block),
            arfcn=_valid_int(arfcn_key, arfcn_rng, block),
            band=_block_band(block),
        )
        for source, block in _identity_blocks(section, kind)
    ]
    if not cands:
        return _Ident()
    cands.sort(key=lambda c: c.source)  # stabil: urutan kemunculan dipertahankan dalam satu sumber
    best = min(cands, key=lambda c: (-c.score, c.source))
    if best.cell_id is not None:
        for other in cands:
            if other is best or other.cell_id != best.cell_id:
                continue
            for name in ("pci", "arfcn", "band"):
                if getattr(best, name) is None:
                    setattr(best, name, getattr(other, name))
    return best


def lte_band_from_earfcn(earfcn: int | None) -> int | None:
    if earfcn is None:
        return None
    for band, lo, hi in LTE_BANDS:
        if lo <= earfcn <= hi:
            return band
    return None


def _service_state_earfcn(section: str) -> int | None:
    """EARFCN dari `mChannelNumber` di baris mServiceState saat ini.

    Cadangan untuk HP yang menyembunyikan identitas sel (mis. Xiaomi MIUI: CellIdentityLte tanpa
    mCi/mPci/mEarfcn). Hanya baris `mServiceState=` yang dibaca, bukan riwayat `state={...}` di
    local logs. Kalau service state menyebut teknologi data selain LTE, nilainya bukan EARFCN.
    """
    line = _field_line(section, "mServiceState")
    if line is None:
        return None
    tech = re.search(r"RilDataRadioTechnology\s*=\s*\d+\s*\(\s*([\w-]+)", line)
    if tech and not tech.group(1).upper().startswith("LTE"):
        return None
    value = _valid(_int("mChannelNumber", line), EARFCN)
    return int(value) if value is not None else None


def _identity(section: str, rat: str) -> dict[str, int | None]:
    if rat == "LTE":
        best = _pick_identity(section, "Lte")
        earfcn = _first(best.arfcn, _service_state_earfcn(section))
        # EARFCN menentukan band secara pasti; mBands hanya dipakai kalau EARFCN tidak ada/tidak dikenal.
        band = _first(lte_band_from_earfcn(earfcn), best.band)
        return {"pci": best.pci, "cell_id": best.cell_id, "earfcn": earfcn, "band": band}
    if rat == "NR":
        best = _pick_identity(section, "Nr")
        return {"pci": best.pci, "cell_id": best.cell_id, "earfcn": best.arfcn, "band": best.band}
    return {"cell_id": _pick_identity(section, "Wcdma" if rat == "WCDMA" else "Gsm").cell_id}


# ---------------------------------------------------------------- service state

def _service_state(section: str) -> bool | None:
    """True kalau suara atau data IN_SERVICE (0), False kalau keduanya tidak, None kalau tak diketahui."""
    voice = _int("mVoiceRegState", section)
    data = _int("mDataRegState", section)
    if voice is None and data is None:
        # Format lama: mServiceState=<voice> <data> voice home data home ...
        m = re.search(r"(?m)^\s*mServiceState\s*=\s*(\d)\s+(\d)\s", section)
        if not m:
            return None
        voice, data = int(m.group(1)), int(m.group(2))
    return 0 in (voice, data)


def _operator_alpha(section: str) -> str | None:
    for key in ("mDataOperatorAlphaLong", "mOperatorAlphaLong", "mVoiceOperatorAlphaLong"):
        m = re.search(rf"\b{key}\s*=\s*([^,}}\n]*)", section)
        if m:
            value = m.group(1).strip()
            if value and value.lower() != "null":
                return value
    return None


_SA_HINT = re.compile(
    r"RilDataRadioTechnology\s*=\s*\d+\s*\(\s*NR\s*\)|accessNetworkTechnology\s*=\s*NR\b"
)
_NSA_HINT = re.compile(r"overrideNetwork\s*=\s*NR_(?:NSA|ADVANCED)|nrState\s*=\s*CONNECTED")


# ---------------------------------------------------------------- parse utama

def split_phone_sections(text: str) -> list[tuple[int | None, str]]:
    """Pecah output per `Phone Id=N` (HP dual-SIM). Bagian `local logs` dibuang."""
    text = re.split(r"(?im)^\s*local logs\s*:", text)[0]
    matches = list(re.finditer(r"(?m)^\s*Phone Id\s*=\s*(\d+)", text))
    if not matches:
        return [(None, text)]
    sections = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((int(m.group(1)), text[m.start():end]))
    return sections


def _parse_section(section: str, phone_id: int | None, sinr_unit: str | None) -> SignalReading:
    base = {
        "phone_id": phone_id,
        "in_service": _service_state(section),
        "operator_alpha": _operator_alpha(section),
        "data_connected": _int("mDataConnectionState", section) == 2,
    }

    lte = nr = wcdma = gsm = _Sig()
    recognized = False
    sig_line = _field_line(section, "mSignalStrength")
    if sig_line is not None:
        legacy = _legacy_positional(sig_line)
        if legacy is not None:
            lte, gsm = legacy
            recognized = True
        else:
            lte = _lte_signal(_segment(sig_line, "CellSignalStrengthLte"), sinr_unit)
            nr = _nr_signal(_segment(sig_line, "CellSignalStrengthNr"))
            wcdma = _wcdma_signal(_segment(sig_line, "CellSignalStrengthWcdma"))
            gsm = _gsm_signal(_segment(sig_line, "CellSignalStrengthGsm"))
            recognized = any(s.found for s in (lte, nr, wcdma, gsm)) or bool(
                re.search(r"\bm(?:Lte|Nr)\s*=\s*Invalid", sig_line)
            )

    # Cadangan: nilai sinyal dari CellInfo sel yang terdaftar.
    if lte.rsrp is None:
        for info in _registered_infos(section, "Lte"):
            cand = _lte_signal(_segment(info, "CellSignalStrengthLte"), sinr_unit)
            if cand.rsrp is not None:
                lte, recognized = cand, True
                break
    if nr.rsrp is None:
        for info in _registered_infos(section, "Nr"):
            cand = _nr_signal(_segment(info, "CellSignalStrengthNr"))
            if cand.rsrp is not None:
                nr, recognized = cand, True
                break

    if sig_line is None and not recognized:
        return SignalReading(reason="mSignalStrength tidak ditemukan", **base)
    if not recognized:
        return SignalReading(reason="format mSignalStrength tidak dikenali", **base)

    if base["in_service"] is False:
        return SignalReading(rat="NONE", ok=True, **base)

    if nr.rsrp is not None and (lte.rsrp is None or _SA_HINT.search(section)):
        return SignalReading(
            rat="NR", rsrp=nr.rsrp, rsrq=nr.rsrq, sinr=nr.sinr, ok=True,
            sinr_raw=nr.sinr_raw, sinr_unit=nr.sinr_unit,
            **_identity(section, "NR"), **base,
        )
    if lte.rsrp is not None:
        return SignalReading(
            rat="LTE", rsrp=lte.rsrp, rsrq=lte.rsrq, sinr=lte.sinr, rssi=lte.rssi, ok=True,
            sinr_raw=lte.sinr_raw, sinr_unit=lte.sinr_unit,
            nr_nsa=nr.rsrp is not None or bool(_NSA_HINT.search(section)),
            nr_rsrp=nr.rsrp, nr_sinr=nr.sinr,
            **_identity(section, "LTE"), **base,
        )
    if wcdma.rscp is not None or wcdma.rssi is not None:
        return SignalReading(rat="WCDMA", rssi=wcdma.rssi, ok=True, **_identity(section, "WCDMA"), **base)
    if gsm.rssi is not None:
        return SignalReading(rat="GSM", rssi=gsm.rssi, ok=True, **_identity(section, "GSM"), **base)

    if base["in_service"] is True:
        return SignalReading(reason="HP dalam layanan tetapi tidak ada nilai sinyal yang valid", **base)
    return SignalReading(rat="NONE", ok=True, **base)


def parse_registry(
    text: str, phone_id: int | None = None, lte_sinr_unit: str | None = None
) -> SignalReading:
    """Parse output `dumpsys telephony.registry` menjadi satu SignalReading.

    Untuk HP dual-SIM, `phone_id` memilih slot. Tanpa `phone_id`, dipilih slot yang
    berhasil di-parse dan punya sinyal, diutamakan yang koneksi datanya aktif.
    `lte_sinr_unit` ("db"/"deci_db") adalah satuan rssnr LTE HP ini; None = tebakan per sampel
    (lihat _lte_sinr). Fungsi ini tidak melempar exception untuk teks apa pun.
    """
    if lte_sinr_unit not in (None, *SINR_UNITS):
        raise ValueError(f"lte_sinr_unit tidak dikenal: {lte_sinr_unit!r}")
    if not text or not text.strip():
        return SignalReading(reason="output kosong")
    sections = split_phone_sections(text)
    readings = []
    for pid, section in sections:
        try:
            readings.append(_parse_section(section, pid, lte_sinr_unit))
        except Exception as exc:  # parser harus toleran terhadap format aneh
            readings.append(SignalReading(phone_id=pid, reason=f"error parser: {exc!r}"))

    if phone_id is not None:
        chosen = next((r for r in readings if r.phone_id == phone_id), None)
        if chosen is None:
            found = [r.phone_id for r in readings]
            return SignalReading(reason=f"Phone Id={phone_id} tidak ada (ditemukan: {found})", sections=len(readings))
    else:
        chosen = max(readings, key=lambda r: (r.ok, r.rat not in (None, "NONE"), r.data_connected))
    chosen.sections = len(readings)
    return chosen
