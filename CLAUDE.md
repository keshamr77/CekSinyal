# CekSinyal — konteks proyek untuk Claude Code

## Apa ini
CekSinyal adalah sistem pemantau sinyal seluler untuk tugas besar ET4132 Kapita Selekta Teknik Telekomunikasi (ITB, Kelompok 2). HP Android disambungkan ke laptop lewat USB. Script di laptop membaca kualitas sinyal lewat ADB, menguji kecepatan internet lewat USB tethering, menyimpan semua data ke SQLite, mendiagnosis penyebab sinyal buruk, lalu mengirim hasilnya ke dashboard bersama dan bot Telegram.

Alat ini TIDAK memperkuat sinyal. Nilainya ada di diagnosis, perbandingan operator, perbandingan antar-kos, dan bukti tertulis.

## Batasan penting
- Python 3.10+. Harus jalan di Windows, macOS, dan Linux (laptop anggota berbeda-beda; Orange Pi opsional dengan Linux). Pakai `pathlib`, jangan hardcode path atau perintah khusus satu OS.
- Hanya HP Android (iPhone tidak bisa dibaca lewat ADB).
- Satu laptop bisa terhubung ke 1–2 HP. Setiap HP diidentifikasi dengan serial ADB dan dipetakan ke nama operator di config.
- Kalau `kos` masih "belum-diatur", `collect`/`run`/`selftest` harus memberi peringatan jelas.
- Rahasia (token Telegram, kredensial Google) hanya di `.env` / file kredensial lokal. JANGAN pernah di-commit. Commit `.env.example` dan `config.example.yaml` saja.
- Data (`*.db`, folder `data/`) tidak di-commit.
- Dependensi seminimal mungkin. Lebih baik `requests` daripada SDK besar.
- Nama field di output `dumpsys` berbeda antar-versi Android dan merek HP. Parser harus toleran: ambil dengan regex, nilai yang tidak ada jadi `None`, nilai tidak valid (misalnya `2147483647` = unavailable) jadi `None`.

## Arsitektur
```
HP Android ──USB (ADB)──> collector ──> SQLite <── speedtest (lewat tethering)
                                          │
                                   diagnosis (tiap 10 menit)
                                          │
                     ┌────────────────────┼──────────────────┐
                 uploader            bot Telegram        report
           (Google Sheets, store-    (alert + ringkasan   (laporan bukti
            and-forward)              harian)              per kos)
```

## Struktur folder
```
ceksinyal/
├── CLAUDE.md
├── README.md
├── requirements.txt
├── .env.example
├── config.example.yaml
├── ceksinyal/
│   ├── __main__.py        # CLI: python -m ceksinyal <perintah>
│   ├── config.py          # baca .env + config.yaml
│   ├── db.py              # skema dan akses SQLite
│   ├── collector/         # adb.py (jalankan perintah), parser.py (parsing dumpsys)
│   ├── speedtest/         # ping, unduhan kecil, speedtest-cli --source
│   ├── diagnosis/         # rules.py (pohon keputusan), engine.py (jendela 10 menit)
│   ├── uploader/          # sheets.py, kirim baris yang uploaded=0
│   ├── bot/               # telegram.py (sendMessage via requests)
│   └── report/            # laporan bukti per kos (Markdown/HTML)
├── tests/
│   └── fixtures/          # contoh output dumpsys asli dari HP anggota
└── docs/                  # screenshot, catatan uji, lembar validasi
```

## Skema SQLite (semua waktu dalam UTC ISO 8601)
- `measurements`: id, ts, kos, device_serial, operator, rat (LTE/NR/WCDMA/GSM/NONE), rsrp, rsrq, sinr, rssi, pci, cell_id, earfcn, band, sinr_raw (nilai rssnr/sinr mentah dari dumpsys), sinr_unit ('db' / 'deci_db' = 0,1 dB; satuan sinr_raw), raw_ok (0/1), uploaded (0/1)
- Kolom baru ditambahkan ke DB lama lewat `db.migrate()` (ALTER TABLE ADD COLUMN, cek PRAGMA table_info); data lama tidak dihapus.
- `speedtests`: id, ts, kos, operator, method (light/full), source_ip, ping_ms, download_mbps, upload_mbps, ok (0/1), error, uploaded
- `diagnoses`: id, ts, kos, operator, window_start, window_end, n_samples, median_rsrp, median_sinr, median_download, cell_changes, result, advice, detail (catatan aturan, mis. "handover: tidak tersedia"), uploaded
- `events`: id, ts, kos, operator, type (signal_lost/diagnosis_changed/collector_error/daily_summary), detail, notified (0/1). `diagnosis_changed`/`signal_lost` ber-detail `Lama -> Baru; ...` dan ber-ts akhir jendela; `daily_summary` hanya penanda ringkasan sudah terkirim (detail = tanggal lokal)

## Pengumpulan data
- Perintah utama: `adb -s <serial> shell dumpsys telephony.registry`. Ambil dari bagian `mSignalStrength` (CellSignalStrengthLte: rsrp, rsrq, rssnr/sinr, rssi) dan identitas sel (CellIdentityLte: mCi, mPci, mEarfcn, mBands). Dukung juga NR (5G) kalau ada.
- Identitas sel (cell_id, pci, earfcn, band) diambil dari SATU blok yang sama, jangan dicampur antar-blok. Pilih blok dengan skor kelengkapan tertinggi (jumlah ci/pci/earfcn valid); seri → NetworkRegistrationInfo PS > NRI CS > mCellIdentity > mBarringInfo > CellInfo registered (CellInfo bisa basi, contoh Infinix Android 14). Kolom kosong hanya boleh diisi dari blok lain dengan cell_id yang sama; cadangan EARFCN dari `mChannelNumber`.
- Band LTE: kalau EARFCN ada di tabel `LTE_BANDS` (3GPP TS 36.101), band dihitung dari EARFCN; `mBands` hanya dipakai kalau EARFCN kosong/tidak dikenal (Infinix menulis mBands=[1] untuk sel Band 3). Data lama diperbaiki dengan `python -m ceksinyal fix-band`.
- Nama operator dari config; fallback `adb shell getprop gsm.operator.alpha`.
- Interval default 10 detik per HP. Kalau HP terputus, catat event dan coba lagi, jangan crash.
- RSRP tanpa layanan → rat = NONE, nilai sinyal None (ini dihitung sebagai "sinyal hilang").

## Uji kecepatan
- Lewat USB tethering HP, diarahkan ke interface masing-masing operator dengan IP sumber dari config (`speedtest-cli --source <IP>`). Sediakan perintah bantu yang menampilkan daftar interface dan IP (pakai `psutil`) supaya user bisa mengisi config.
- Ringan tiap 15 menit: ping (3 kali; diukur sebagai waktu koneksi TCP dari IP sumber, bukan ICMP, supaya lintas-OS) + unduh file kecil (±1 MB). Penuh tiap 60 menit: `speedtest-cli --json`.
- Kalau gagal, simpan baris dengan ok=0 dan pesan error. HP tanpa `source_ip` di config dilewati (jangan menguji lewat koneksi lain). Jadwal dihitung dari waktu uji terakhir di tabel `speedtests`.
- `python -m ceksinyal speedtest [--full] [--no-save]` menjalankan satu uji sekarang.

## Aturan diagnosis (ambang di config, bisa dikalibrasi)
Dihitung per operator untuk jendela 10 menit terakhir. Periksa berurutan, berhenti di "ya" pertama:
1. Sinyal hilang (≥ 50% sampel rat=NONE) → **Sinyal putus**: catat kejadian, kirim alert, masuk laporan bukti
2. Median RSRP < −110 dBm → **Cakupan lemah**: pindah titik, operator lain, atau ajukan BTS
3. Median SINR < 0 dB → **Interferensi**: coba operator atau band lain
4. Median kecepatan unduh < 50% median harian operator itu (butuh ≥ 6 data kecepatan; kalau kurang, lewati) → **Sel padat**: operator lain atau geser jam
5. Sel berganti ≥ 4 kali dalam jendela → **Handover bolak-balik**: kunci band atau geser posisi. Kalau `cell_id` tersedia, hitung pergantian `cell_id`; kalau tidak (beberapa HP, mis. Xiaomi MIUI, menyembunyikan identitas sel), hitung pergantian `earfcn` sebagai pendekatan; kalau keduanya tidak ada, lewati aturan ini dan catat "handover: tidak tersedia" di hasil diagnosis.
6. Selain itu → **Normal**
Jendela dengan < 20 sampel → hasil "Data kurang". Jendela berada di kisi tetap 10 menit (UTC) dan sampel dihitung apa adanya (data bisa bolong; jangan asumsikan interval rapi). Setiap aturan harus bisa menyatakan "tidak tersedia" (dicatat di kolom `detail`) alih-alih menebak: median butuh ≥ 5 nilai; SINR yang selalu persis 0 dianggap tidak tersedia (Infinix SIM 2); kalau jendela tidak punya uji kecepatan, pakai uji terakhir dalam 20 menit. Pembanding "median harian" = 24 jam sebelum akhir jendela. Perubahan hasil dibandingkan dengan hasil konklusif terakhir ("Data kurang" dilewati). Diagnosis harus bisa dijalankan ulang pada data lama (`python -m ceksinyal diagnose --rerun`) setelah ambang diubah.
Kalau dua operator ada di kos yang sama, buat juga rekomendasi: operator dengan diagnosis terbaik, lalu median RSRP tertinggi.

## Dashboard dan store-and-forward
- Backend utama: Google Sheets (service account, library `gspread`). Satu spreadsheet bersama, tab `measurements`, `speedtests`, `diagnoses`. Setiap baris membawa kolom `kos`.
- Uploader mengirim baris `uploaded=0` per batch, lalu menandai `uploaded=1` hanya setelah berhasil. Internet putus tidak boleh menghilangkan data.
- Untuk measurements, boleh kirim versi rata-rata per menit supaya tidak melebihi kuota API (bawaan: `sheets.aggregate_measurements: true`, per menit per HP; menit yang masih berjalan belum dikirim).
- Spreadsheet ID dari `sheets.spreadsheet_id` (config) atau `GOOGLE_SPREADSHEET_ID` (.env); file kredensial dari `GOOGLE_CREDENTIALS_FILE` (.env). `python -m ceksinyal upload` mengirim sekali sekarang.

## Bot Telegram
- Pakai HTTP API langsung (`requests` ke sendMessage). Token dan chat ID dari `.env`.
- Alert saat diagnosis berubah dari Normal ke bermasalah, saat sinyal putus, dan saat collector error. Maksimal satu alert yang sama per operator per 15 menit.
- Ringkasan harian: diagnosis dominan per operator, persentase waktu RSRP < −100 dBm, jumlah kejadian putus. Dikirim sekali sehari setelah `telegram.daily_summary_time` (waktu lokal, bawaan 21:00).
- Alert yang tertahan batas 15 menit ditandai notified=1 (tidak menumpuk); kegagalan kirim membiarkan notified=0 untuk dicoba lagi; event berumur > 2 jam tidak dikirim. Perubahan kembali ke Normal tidak di-alert. Token tidak boleh muncul di log/pesan error.
- `python -m ceksinyal summary [--send]` menampilkan/mengirim ringkasan sekarang.

## CLI (target)
- `python -m ceksinyal devices` — daftar HP terhubung, serial, operator
- `python -m ceksinyal probe` — baca sekali dari tiap HP, tampilkan hasil parsing, dan simpan output mentah ke `data/raw/` (untuk cek cepat apakah parser cocok dengan HP itu)
- `python -m ceksinyal interfaces` — daftar interface jaringan dan IP (untuk config tethering)
- `python -m ceksinyal collect` — collector saja
- `python -m ceksinyal run` — semuanya: collector, speedtest, diagnosis, uploader, bot
- `python -m ceksinyal diagnose [--rerun] [--db <path>] [--kos <nama>]`
- `python -m ceksinyal report --kos <nama> [--db <path>] [--out <folder>] [--since <tgl>] [--until <tgl>]` — laporan bukti (Markdown + HTML satu file dengan grafik SVG inline, tanpa pustaka grafik) di `data/reports/`; wajib memuat catatan keterbatasan data per HP
- `python -m ceksinyal selftest` — cek ADB, config, koneksi Sheets, bot
- `python -m ceksinyal fix-sinr [--db <path>] [--serial <serial>] [--dry-run]` — koreksi SINR lama 10x lipat (Android 10)
- `python -m ceksinyal fix-band [--db <path>] [--dry-run]` — koreksi band LTE lama yang tidak cocok dengan EARFCN

## Cara kerja yang diharapkan dari Claude Code
- Kerjakan satu fitur per sesi. Rencanakan dulu, lalu tulis kode, lalu tes.
- Setiap fitur punya unit test (`pytest`). Parser dites dengan fixture output dumpsys ASLI di `tests/fixtures/`. Diagnosis dites dengan data sintetis untuk setiap cabang aturan.
- Fixture asli dari HP anggota belum ada. Untuk sementara buat fixture SINTETIS berdasarkan format umum `dumpsys telephony.registry` (Android 9–14, LTE, NR, tanpa layanan, nilai unavailable 2147483647), beri nama `synthetic_*.txt`, dan tulis parser yang toleran. Saat fixture asli (`real_*.txt`) ditambahkan nanti, parser harus lolos untuk semuanya.
- Kalau parsing gagal atau RSRP tidak ditemukan padahal HP terhubung, collector menyimpan output mentah ke `data/raw/<serial>_<waktu>.txt` (maksimal satu file per HP per jam) dan mencatat event. File ini dipakai untuk memperbaiki parser.
- Logging pakai modul `logging`, bukan print. Error satu bagian tidak boleh mematikan bagian lain.
- Perbarui README setiap kali ada perintah atau konfigurasi baru.
- Pesan commit singkat dalam bahasa Indonesia.
