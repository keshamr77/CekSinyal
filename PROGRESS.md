# PROGRESS — CekSinyal

Untuk melanjutkan di sesi baru: "baca PROGRESS.md dan lanjutkan". Spesifikasi utama tetap `CLAUDE.md`.
Branch kerja: `pengembangan-m1-m7` (dibuat dari `main`; gabungkan dengan `git checkout main && git merge --ff-only pengembangan-m1-m7`).

## Status milestone

| # | Isi | Status |
|---|---|---|
| M1 | Identitas sel satu blok, band dari EARFCN, `fix-band` | selesai |
| M2 | Diagnosis (`diagnosis/rules.py`, `engine.py`, `diagnose [--rerun]`) | selesai |
| M3 | `interfaces` + speedtest ringan/penuh | belum |
| M4 | `report --kos` (Markdown + HTML) | belum |
| M5 | Uploader Google Sheets (store-and-forward) | belum |
| M6 | Bot Telegram (alert + ringkasan harian) | belum |
| M7 | `run`, `selftest`, README lengkap | belum |

## Rencana per milestone

### M1 — identitas sel / band
- Ubah: `collector/parser.py` (`_identity_blocks`, `_pick_identity`, `_identity`), `__main__.py` (`fix-band`).
- Baru: `fix_band.py`, `tests/test_identity.py`, `tests/test_fix_band.py`, fixture `real_infinix_android14_dualsim.*`.
- Dependensi baru: tidak ada.

### M2 — diagnosis
- Baru: `diagnosis/rules.py` (fungsi murni: sampel satu jendela → hasil), `diagnosis/engine.py` (jendela, baca/tulis DB, event,
  rekomendasi operator), `tests/test_diagnosis_rules.py`, `tests/test_diagnosis_engine.py`.
- Ubah: `config.py` (bagian `diagnosis:` = semua ambang), `db.py` (kolom `diagnoses.detail`), `__main__.py` (`diagnose`).
- Desain: jendela 10 menit pada kisi tetap (kelipatan 10 menit UTC), dihitung dari sampel nyata; jendela kosong di tengah data
  menjadi "Data kurang" (n=0). Setiap aturan punya status "tidak tersedia". `diagnose` = kejar jendela yang belum didiagnosis;
  `--rerun` = hapus hasil lama lalu hitung ulang semuanya.
- Dependensi baru: tidak ada.

### M3 — interfaces + speedtest
- Baru: `speedtest/light.py` (ping TCP + unduh ±1 MB), `speedtest/full.py` (`python -m speedtest --json --source`),
  `speedtest/runner.py` (jadwal 15/60 menit), `netif.py` (`interfaces`), test dengan mock.
- Ubah: `config.py` (`speedtest:`, `devices[].source_ip`), `__main__.py` (`interfaces`, `speedtest`).
- Dependensi baru: `psutil` (daftar interface; diminta CLAUDE.md), `requests` (unduhan + nanti Telegram),
  `speedtest-cli` (uji penuh; diminta CLAUDE.md).

### M4 — report
- Baru: `report/data.py` (kumpulkan statistik), `report/render.py` (Markdown + HTML satu file, grafik SVG inline tanpa
  pustaka), test.
- Ubah: `__main__.py` (`report --kos`). Dependensi baru: tidak ada.

### M5 — uploader
- Baru: `uploader/sheets.py`, test dengan klien palsu. Dependensi baru: `gspread` (diminta CLAUDE.md; diimpor hanya saat dipakai).

### M6 — bot
- Baru: `bot/telegram.py` (klien `sendMessage`, pemroses event dengan batas 15 menit, ringkasan harian), test dengan mock.

### M7 — run + selftest
- Baru: `runner.py` (thread per bagian, berhenti rapi), `selftest.py`. README lengkap.

## Keputusan desain (default yang diambil tanpa bertanya)

### Umum
- Fixture Infinix di-rename dari `real_infinix_android14_dualsim.txt.txt` ke `.txt` (ekstensi ganda tidak sengaja).
- Commit di branch `pengembangan-m1-m7`, bukan langsung di `main`.

### M1
- Identitas sel diambil dari satu blok: skor kelengkapan (cell id, pci, arfcn yang valid) tertinggi; seri → urutan sumber
  NetworkRegistrationInfo PS > NRI CS > `mCellIdentity` > `mBarringInfo` > CellInfo registered.
- Kolom kosong di blok terpilih hanya diisi dari blok lain yang cell id-nya sama (dan valid). Kalau blok terpilih tidak punya
  cell id, tidak ada pengisian silang sama sekali (lebih baik kosong daripada tercampur).
- Band LTE: kalau EARFCN (dari blok terpilih atau cadangan `mChannelNumber`) ada di tabel `LTE_BANDS`, band = hasil hitung dari
  EARFCN, apa pun isi `mBands`. `mBands` hanya dipakai kalau EARFCN kosong atau di luar tabel.
- Logika satu-blok yang sama dipakai untuk NR, WCDMA, GSM (tanpa cek band untuk NR; tidak ada tabel NR-ARFCN).
- Tabel `LTE_BANDS` dicek terhadap 3GPP TS 36.101 Tabel 5.7.3-1: band 1 (0–599), 3 (1200–1949), 5 (2400–2649),
  8 (3450–3799), 40 (38650–39649) sudah benar; tidak ada yang diubah.
- `fix-band` juga mengisi band yang kosong kalau EARFCN-nya dikenal (dilaporkan sebagai "- → N"). Baris yang sudah
  di-upload tidak ditandai ulang.
- Tidak ada `expected.json` lama yang berubah.

### M2
- Jendela di kisi tetap 10 menit (UTC), bukan dihitung mundur dari "sekarang": hasil `--rerun` sama dengan mode langsung.
- "Data kurang" diperiksa paling dulu (n < 20), termasuk jendela kosong di tengah data (n=0). Setelah sampel terakhir
  tidak dibuat jendela lagi, jadi HP yang dicabut tidak menghasilkan baris "Data kurang" tanpa akhir.
- Hanya baris `raw_ok=1` yang dihitung; jumlah baris `raw_ok=0` dicatat di `detail`.
- Median RSRP/SINR butuh ≥ 5 nilai (`min_metric_samples`), kalau kurang → "tidak tersedia".
- SINR yang persis 0 di SEMUA sampel jendela → "tidak tersedia" (pelajaran Infinix SIM 2), bukan 0 dB.
- Aturan sel padat: kecepatan jendela = uji di jendela itu, atau uji terakhir dalam 20 menit (`speed_lookback_minutes`)
  karena uji ringan tiap 15 menit sedangkan jendela 10 menit. "Median harian" = 24 jam sebelum akhir jendela (bukan hari
  kalender), supaya rerun tidak memakai data masa depan.
- Handover: pakai `cell_id` kalau ada ≥ 2 nilai di jendela; kalau tidak, `earfcn`; kalau tidak ada → tidak tersedia.
  Nilai kosong dilewati (bukan pergantian). Kalau identitas terisi < 50% sampel, dicatat "batas bawah".
- Kolom baru `diagnoses.detail` (lewat `db.migrate()`), berisi catatan "tidak tersedia" dsb.
- Event: `signal_lost` saat hasil berubah menjadi "Sinyal putus", `diagnosis_changed` untuk perubahan lain (termasuk
  kembali ke Normal, dan diagnosis pertama kalau langsung bermasalah). Pembanding = hasil konklusif terakhir.
  Event dari jendela berumur > 30 menit langsung `notified=1`.
- Rekomendasi dua operator: porsi jendela Normal tertinggi, lalu median RSRP.
- Peringatan `kos` belum diisi dipindah dari saat config dibaca (muncul di semua perintah) ke `collect` (nanti juga
  `run`/`selftest`), dengan kotak peringatan yang mencolok.
- Config baru: `timezone_offset_hours` (bawaan 7 = WIB) untuk tampilan; data tetap UTC.

## Hasil verifikasi dengan data asli

### M1
- `parse_registry(real_infinix..., phone_id=0)` → LTE, rsrp −66, rsrq −10, sinr 10,0 (db), pci 50, cell_id 110079775,
  earfcn 1850, band 3. `phone_id=1` → LTE, rsrp −82, rsrq −12, sinr 0,0, pci 329, cell_id 102782025, earfcn 1625, band 3.
  `phone_id=2` → NONE (slot mati). Sama dengan angka yang diharapkan.
- `fix-band --dry-run`: Infinix 44 baris band 1 → 3; Xiaomi 158 baris ber-EARFCN, tidak ada yang salah.

### M2 (dijalankan pada SALINAN DB di folder sementara; `data/` tidak diubah)
- Xiaomi: `fix-sinr` mengoreksi 263 dari 750 baris (median 6,0 → 4,5 dB); `fix-band` 0 baris. `diagnose --rerun`:
  15 jendela = 13 Normal + 2 Data kurang. TIDAK ada Interferensi (perkiraan awal: mungkin Interferensi 15:30–15:50 WIB).
  Penjelasan: jeda collector ternyata 15:34:14–15:49:27 WIB, persis di rentang itu. Jendela 15:30 hanya punya 26 sampel
  (15:30–15:34) dengan median SINR +1,1 dB (> 0), dan jendela 15:40 hanya 4 sampel (median −2,25 dB) sehingga "Data
  kurang". SINR memang turun negatif tepat sebelum dan sesudah jeda (−0,8 … −8,3 dB pada ±10 sampel), tetapi tidak cukup
  lama untuk menggeser median satu jendela. Median SINR terendah per jendela: 1,1 dB (15:30) dan 1,2 dB (16:50).
  Handover: jendela 15:50–16:20 dihitung dari EARFCN (0 pergantian), jendela lain "tidak tersedia". Kecepatan: tidak
  tersedia (belum ada speedtest). Catatan: 76 baris SINR bernilai 5/7/−3 ambigu dan tidak dikoreksi `fix-sinr`.
- Infinix: 2 jendela. 23:10 WIB = Data kurang (hanya 18 sampel karena rekaman mulai 23:16:41), 23:20 = Normal
  (26 sampel, median RSRP −65 dBm, SINR 12 dB, 0 pergantian cell_id). Jadi "Normal" sesuai perkiraan untuk jendela yang
  cukup datanya.

## Perlu uji manual (oleh tim, dengan perangkat asli)

- [ ] M1: `probe` di Infinix sekarang menampilkan band 3; jalankan `fix-band` pada DB Infinix yang asli (ada backup otomatis).
- [ ] M2: jalankan `diagnose` pada data ≥ 1 jam dari dua operator di kos yang sama dan cek rekomendasi operatornya masuk akal.
- [ ] M2: kalibrasi ambang (`diagnosis.*`) dengan lembar validasi; setelah diubah jalankan `diagnose --rerun`.
- [ ] M1: `probe` di HP lain (Samsung/Oppo/Vivo, dan HP 5G) lalu tambahkan fixture `real_*.txt`.

## TODO / di luar cakupan

- Infinix SIM 2 (Tri): `rssnr=0` di `mSignalStrength` dan di semua CellInfo, kemungkinan nilai kosong, bukan 0 dB sungguhan.
  Parser masih menyimpannya sebagai SINR 0,0. Perlu dicek (bandingkan dengan aplikasi Network Cell Info) kalau SIM 2 ikut dipantau.
- Xiaomi Android 10 (Redmi 9C): `pci`/`cell_id` kosong di semua sampel, `earfcn`/`band` hanya ±21% sampel. Perlu sumber lain.
