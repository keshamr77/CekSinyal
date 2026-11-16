# PROGRESS — CekSinyal

Untuk melanjutkan di sesi baru: "baca PROGRESS.md dan lanjutkan". Spesifikasi utama tetap `CLAUDE.md`.
Branch kerja: `pengembangan-m1-m7` (dibuat dari `main`; gabungkan dengan `git checkout main && git merge --ff-only pengembangan-m1-m7`).

## Status milestone

| # | Isi | Status |
|---|---|---|
| M1 | Identitas sel satu blok, band dari EARFCN, `fix-band` | selesai |
| M2 | Diagnosis (`diagnosis/rules.py`, `engine.py`, `diagnose [--rerun]`) | selesai |
| M3 | `interfaces` + speedtest ringan/penuh | selesai (perlu uji manual dengan tethering) |
| M4 | `report --kos` (Markdown + HTML) | selesai |
| M5 | Uploader Google Sheets (store-and-forward) | selesai (perlu uji manual dengan spreadsheet asli) |
| M6 | Bot Telegram (alert + ringkasan harian) | selesai (perlu uji manual dengan bot asli) |
| M7 | `run`, `selftest`, README lengkap | selesai (perlu uji manual dengan perangkat asli) |

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

### M3
- "Ping" = waktu jabat tangan TCP ke `1.1.1.1:443` dari socket yang di-bind ke IP sumber (median 3 kali), bukan ICMP.
  Alasan: opsi dan bahasa keluaran `ping` berbeda per OS, dan mengikat IP sumber lewat socket sama di semua OS.
- Unduhan ringan: `requests` dengan adapter `source_address`, URL bawaan `https://speed.cloudflare.com/__down?bytes=1000000`
  (bisa diganti di config). Proxy sistem diabaikan (`trust_env=False`).
- Uji penuh: `python -m speedtest --json --secure --source <IP>` lewat interpreter yang sama (tidak bergantung PATH).
- HP tanpa `source_ip` DILEWATI (tidak diuji lewat koneksi bawaan laptop) karena hasilnya akan menyesatkan.
- Sebelum uji, `source_ip` dicek ada di laptop (psutil); kalau tidak → baris `ok=0` "IP sumber ... tidak ada di laptop".
- Uji ringan dianggap `ok=1` kalau unduhan berhasil; ping yang gagal hanya dicatat di `error`.
- Jadwal dari database: uji pertama ringan, uji penuh pertama satu selang (15 menit) kemudian, lalu tiap 60 menit;
  slot uji penuh menggantikan uji ringan. `full_interval_minutes: 0` mematikan uji penuh.
- Perintah tambahan di luar daftar CLAUDE.md: `speedtest [--full] [--no-save]` untuk uji sekali (berguna saat menyiapkan
  tethering).
- Dependensi baru: `psutil`, `requests`, `speedtest-cli`.

### M4
- Keluaran: `data/reports/laporan_<kos>_<YYYYMMDD-HHMM>.md` dan `.html` (folder `data/` sudah di-.gitignore).
- Grafik: SVG ditulis langsung oleh Python (tanpa matplotlib dsb.). Histogram satu warna; garis RSRP per operator dengan
  warna tetap per urutan operator (biru, oranye; sudah dicek aman untuk buta warna); linimasa diagnosis memakai POSISI
  baris untuk jenis diagnosis (bukan 7 warna, karena 7 warna status tidak bisa dibedakan penderita buta warna).
  HTML punya mode gelap otomatis dan tooltip (JS kecil di dalam file). Markdown memakai histogram teks.
- Deret RSRP diringkas (median per ember) kalau > 720 titik, dan diputus di jeda > max(60 detik, 6× interval).
- "Persentase waktu RSRP lemah" di ringkasan memakai −100 dBm (seperti ringkasan harian bot), berbeda dari ambang
  diagnosis cakupan lemah (−110 dBm).
- `report` menjalankan diagnosis yang tertunda dulu (bisa dimatikan `--no-diagnose`), jadi laporan tidak kosong kalau
  lupa menjalankan `diagnose`.
- Catatan keterbatasan per HP dideteksi dari data: cell_id/EARFCN/SINR kosong atau jarang, SINR selalu 0, baris versi
  lama (sinr_raw kosong) + pola salah skala, band tidak cocok EARFCN, jeda data, belum ada speedtest, rekaman < 1 jam,
  baris raw_ok=0.

### M5
- `uploaded=1` ditandai per batch tepat setelah `append_rows` batch itu berhasil; kegagalan apa pun (exception dari
  backend) menghentikan putaran itu tanpa menandai, dan tabel berikutnya menunggu putaran berikut.
- Jaminan "minimal sekali" (bisa ganda kalau jawaban API hilang setelah Sheets menerima); tidak ada dedup di sisi sheet.
- measurements: bawaan rata-rata per menit per HP (kunci: menit, kos, device_serial, operator). Semua baris sumber di
  menit itu ditandai ter-upload bersama. Menit yang sedang berjalan ditahan sampai selesai.
- Maksimal 20 batch per tabel per putaran (kuota tulis Sheets 60/menit); sisanya di putaran berikut.
- Scope service account hanya `spreadsheets` (tanpa Drive), jadi spreadsheet dibuka dengan ID, bukan nama.
- Sheets aktif hanya kalau `spreadsheet_id` DAN `GOOGLE_CREDENTIALS_FILE` ada; kalau tidak, `run` jalan tanpa upload.
- `gspread` diimpor saat dipakai saja, jadi perintah lain tetap jalan tanpa paket itu.
- Perintah tambahan: `upload` (kirim sekali sekarang).
- Dependensi baru: `gspread`.

### M6
- Alert dikirim untuk: `diagnosis_changed` yang hasil barunya bermasalah (termasuk diagnosis pertama yang langsung
  bermasalah), `signal_lost`, dan semua `collector_error`. Kembali ke Normal tidak di-alert.
- Kunci batas frekuensi = (kos, operator, jenis alert); untuk perubahan diagnosis jenisnya memuat hasil baru, jadi
  "→ Interferensi" dan "→ Cakupan lemah" dihitung terpisah. `collector_error` satu kunci per operator apa pun isinya.
- Alert yang tertahan ditandai `notified=1` (tidak dikirim belakangan). Status batas frekuensi disimpan di memori
  (hilang kalau program di-restart).
- Gagal kirim → `notified=0`, berhenti di kegagalan pertama, dicoba lagi putaran berikut; event > 2 jam dibuang.
- Pesan teks polos (tanpa parse_mode) supaya isi `detail` tidak merusak format. Pesan memuat rincian jendela dari
  tabel `diagnoses` (dicocokkan lewat ts event = window_end) dan saran.
- Token: hanya dari lingkungan/.env, `repr=False` di dataclass, dan teks exception `requests` (yang memuat URL) disaring.
- Ringkasan harian: periode 00:00 lokal → saat kirim; penanda terkirim = event `daily_summary` (tipe event baru).
  "RSRP lemah" = `telegram.weak_rsrp_dbm` (−100).
- Perintah tambahan: `summary [--send] [--hours N]`.

### M7
- `run`: satu thread + satu koneksi SQLite (WAL) per bagian: collector (loop sendiri), diagnosis (cek tiap 60 detik),
  speedtest (cek tiap 30 detik), uploader (tiap `sheets.interval_minutes`), bot (tiap 30 detik). Setiap putaran
  dibungkus try/except; kalau error, bagian itu disiapkan ulang (koneksi baru) dan dicoba lagi.
- Berhenti: `threading.Event` dipakai sebagai `sleep` semua bagian, jadi Ctrl+C/SIGTERM langsung membangunkan semuanya.
  Thread ditunggu maksimal 10 detik (uji penuh speedtest yang sedang jalan ditinggal; thread daemon).
- Bagian opsional yang tidak dikonfigurasi dilewati dengan satu baris peringatan saat mulai, bukan error.
- `selftest`: status OK / GAGAL / PERINGATAN / LEWATI. `kos` belum diisi = GAGAL. Sheets/bot yang sama sekali tidak
  dikonfigurasi = LEWATI; dikonfigurasi setengah = GAGAL. Tidak menjalankan uji kecepatan (hanya cek IP sumber ada).
  `--send-test` mengirim satu pesan uji ke Telegram.
- `selftest` membaca tiap HP sekali (dumpsys) dan memberi PERINGATAN kalau cell_id/EARFCN tidak tersedia.

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

### M4 (laporan dari salinan kedua DB asli, dicek isinya dan tampilan HTML-nya lewat tangkapan layar)
- Xiaomi "Garden House": 750 sampel, 2 jam 20 menit, median RSRP −91 dBm (rentang −100…−88), median SINR 4,5 dB, 0%
  RSRP < −100, 13 Normal + 2 Data kurang, jeda 15 menit terlihat sebagai garis putus dan tercatat di keterbatasan,
  "cell_id disembunyikan, EARFCN 21% sampel", "487 sampel versi lama". Band: 592 sampel tanpa band, B3 98, B40 60. Masuk akal.
- Infinix "belum-diatur": catatan kos belum diisi muncul di atas; keterbatasan: belum ada speedtest, rekaman hanya 7 menit.

### M7 (uji asap di laptop ini, config dan database sementara, tanpa HP)
- `selftest`: adb ditemukan, 0 HP → "HP GAGAL", Sheets/bot LEWATI, kode keluar 1. `run`: bagian aktif collector +
  diagnosis, tiga bagian lain dilaporkan tidak aktif beserta alasannya, collector mulai. Belum dicoba dengan HP asli.

## Perlu uji manual (oleh tim, dengan perangkat asli)

- [ ] M1: `probe` di Infinix sekarang menampilkan band 3; jalankan `fix-band` pada DB Infinix yang asli (ada backup otomatis).
- [ ] M3: `interfaces` dengan USB tethering menyala: interface HP muncul dan diberi tanda; isi `source_ip`.
- [ ] M3: `speedtest` dan `speedtest --full` lewat tethering (Wi-Fi HP mati). Pastikan trafik benar-benar lewat HP:
      bandingkan dengan Wi-Fi laptop menyala/mati, dan lihat pemakaian data di HP.
- [ ] M3: dua HP tethering bersamaan di satu laptop: tiap `source_ip` mengukur operator yang benar.
- [ ] M3: di Linux/Orange Pi, cek apakah perlu policy routing supaya `source_ip` keluar lewat interface tethering.
- [ ] M3: URL unduhan bawaan (Cloudflare) bisa diakses dari semua operator; kalau tidak, ganti `speedtest.download_url`.
- [ ] M3: perkiraan pemakaian kuota uji penuh per jam; matikan (`full_interval_minutes: 0`) kalau terlalu boros.
- [ ] M7: `selftest` dengan HP asli di tiap laptop anggota (Windows/macOS/Linux): semua OK.
- [ ] M7: `run` ≥ 2 jam dengan 1 HP, lalu dengan 2 HP: tidak ada error di log, jendela diagnosis muncul tiap 10 menit,
      speedtest sesuai jadwal, baris masuk Sheets, alert/ringkasan sampai.
- [ ] M7: Ctrl+C saat `run` (termasuk saat uji penuh berjalan): berhenti rapi, data terakhir tersimpan.
- [ ] M7: cabut-pasang HP dan matikan-nyalakan internet saat `run`: bagian lain tetap jalan, pulih sendiri.
- [ ] M7: laptop sleep/lid tertutup saat `run`: cek perilaku setelah bangun (jendela "Data kurang" diharapkan).
- [ ] M7: Orange Pi/Linux: jalankan sebagai layanan (systemd), SIGTERM berhenti rapi.
- [ ] M6: buat bot + grup, isi `.env`, `summary --send`: pesan sampai, emoji dan tanda minus tampil benar.
- [ ] M6: cabut HP saat `run`: alert "gangguan alat ukur" datang sekali, bukan tiap 10 detik.
- [ ] M6: paksa sinyal putus (mode pesawat ±10 menit): alert 🔴 datang setelah jendela selesai; masuk laporan.
- [ ] M6: ringkasan harian datang sekali pada jam yang diatur (coba set `daily_summary_time` beberapa menit ke depan).
- [ ] M5: buat service account + spreadsheet bersama, isi `.env`/config, jalankan `upload`: tab dan judul kolom dibuat,
      baris masuk, dan `uploaded` menjadi 1 di SQLite.
- [ ] M5: matikan internet saat `run`, nyalakan lagi: baris tertunda terkirim semua, tidak ada yang hilang.
- [ ] M5: dua laptop (dua kos) mengirim ke spreadsheet yang sama bersamaan.
- [ ] M5: jalan seharian: cek tidak kena batas kuota API (429) dengan interval 5 menit.
- [ ] M4: buka laporan HTML di HP dan cetak ke PDF; cek terbaca oleh orang non-teknis (pemilik kos).
- [ ] M4: laporan dengan dua operator di kos yang sama dan data ≥ 1 hari (grafik garis 2 warna, sumbu waktu berhari-hari).
- [ ] M2: jalankan `diagnose` pada data ≥ 1 jam dari dua operator di kos yang sama dan cek rekomendasi operatornya masuk akal.
- [ ] M2: kalibrasi ambang (`diagnosis.*`) dengan lembar validasi; setelah diubah jalankan `diagnose --rerun`.
- [ ] M1: `probe` di HP lain (Samsung/Oppo/Vivo, dan HP 5G) lalu tambahkan fixture `real_*.txt`.

## TODO / di luar cakupan

- HP dual-SIM: hanya satu slot per serial per sesi (`phone_id`); memantau dua SIM dari satu HP sekaligus belum didukung.
- Batas frekuensi alert bot hanya di memori (hilang saat restart).
- `diagnose --rerun` membuat baris diagnosis ganda di Google Sheets (baris lama tidak dihapus dari sheet).
- Uploader "minimal sekali": batch bisa ganda di sheet kalau jawaban API hilang; belum ada dedup.
- Data Xiaomi asli di `data/ceksinyal_xiaomi.db` BELUM dikoreksi (verifikasi dilakukan pada salinan). Untuk memperbaikinya:
  `fix-sinr --db data/ceksinyal_xiaomi.db` (cek `--dry-run` dulu), lalu `diagnose --rerun --db ...`. Sama untuk Infinix:
  `fix-band --db data/ceksinyal_infinix.db`.

- Infinix SIM 2 (Tri): `rssnr=0` di `mSignalStrength` dan di semua CellInfo, kemungkinan nilai kosong, bukan 0 dB sungguhan.
  Parser masih menyimpannya sebagai SINR 0,0. Perlu dicek (bandingkan dengan aplikasi Network Cell Info) kalau SIM 2 ikut dipantau.
- Xiaomi Android 10 (Redmi 9C): `pci`/`cell_id` kosong di semua sampel, `earfcn`/`band` hanya ±21% sampel. Perlu sumber lain.

---

# Dashboard web (Streamlit)

Status: tahap (a) lapisan data, (b) halaman, (c) privasi, (d) deploy + README **selesai dan ter-commit**. Mode sheets dan
deploy ke Streamlit Community Cloud hanya diuji dengan mock: **perlu uji manual**.

## Rencana dan struktur

| File | Isi |
|---|---|
| `ceksinyal/dashboard/data.py` | Lapisan data (pandas saja, tanpa streamlit): pemuat SQLite/Sheets, normalisasi skema, kategori, ringkasan, perbandingan, rekomendasi, kualitas data, alias privasi, konversi waktu |
| `ceksinyal/dashboard/settings.py` | Pengaturan: variabel lingkungan → `st.secrets` → `config.yaml`; pemuat sesuai mode |
| `ceksinyal/dashboard/charts.py` | Grafik Altair |
| `ceksinyal/dashboard/app.py` | Tujuh halaman (tab) + filter |
| `dashboard/streamlit_app.py`, `dashboard/requirements.txt` | Titik masuk (lokal dan Cloud) dan dependensi opsional |
| `.streamlit/secrets.toml.example` | Contoh secrets berisi placeholder (`secrets.toml` asli di-.gitignore) |
| `tests/test_dashboard_data.py`, `_settings.py`, `_app.py`, `dashboard_helpers.py` | Tes lapisan data, pengaturan + mode sheets (gspread palsu), smoke test AppTest, DB sintetis |

Skema DataFrame bersama (waktu = datetime UTC ber-zona):
- `measurements` (satu baris per menit per HP): ts, kos, device, operator, n_samples, n_no_service, n_parse_failed, rat,
  rsrp, rsrp_min, rsrq, sinr, pci, cell_id, earfcn, band
- `speedtests`: ts, kos, operator, method, ping_ms, download_mbps, upload_mbps, ok, error
- `diagnoses`: window_start, window_end, kos, operator, n_samples, median_rsrp, median_sinr, median_download,
  cell_changes, result, advice, detail
- `events`: ts, kos, operator, type, detail

Grafik: (1) linimasa RSRP/RSRQ/SINR per operator dengan latar periode bermasalah dan garis batas kategori;
(2) batang bertumpuk persen waktu Baik/Cukup/Buruk per kos × operator; (3) unduh, unggah, ping terhadap waktu.

Dependensi baru (opsional, hanya `dashboard/requirements.txt`): `streamlit`. `pandas` dan `altair` sudah ikut terpasang
bersama streamlit, jadi tidak ada pustaka grafik tambahan. `requirements.txt` inti tidak berubah.

## Keputusan desain (default tanpa bertanya)

- **Tab `events` ditambahkan ke uploader** (kejadian putus dibutuhkan dashboard): kolom baru `events.uploaded` lewat
  `db.migrate()`, tab `events` berisi signal_lost/diagnosis_changed/collector_error; `daily_summary` tidak dikirim.
  Kejadian lama di DB ikut terkirim pada upload berikutnya. Dashboard tetap jalan kalau tab `events` belum ada.
- **measurements disamakan ke per menit** di lapisan data dengan memanggil `uploader.sheets.aggregate_minutes` pada data
  SQLite. Ada tes yang membuktikan SQLite → uploader → Sheets palsu → `load_sheets` menghasilkan DataFrame identik dengan
  `load_sqlite`. Spreadsheet yang diisi per sampel (`aggregate_measurements: false`) juga diagregasi saat dimuat.
- Median di dashboard = median dari rata-rata per menit (bukan per sampel seperti `report`), jadi bisa sedikit beda.
- **Kategori**: Baik ≥ batas baik, Buruk < batas buruk, selain itu Cukup. Batas buruk RSRP/SINR = ambang diagnosis;
  batas lain di `dashboard.*` (RSRP baik −100, RSRQ −10/−15, SINR baik 10, unduh 10/2 Mbps). Batas baik tidak bisa lebih
  rendah dari batas buruk.
- **"24 jam terakhir" dihitung dari data terbaru**, bukan jam sekarang; data lama tetap tampil dengan peringatan
  "Alat ukur diam" kalau lebih tua dari `stale_minutes`.
- **Privasi**: mode publik bawaan `true` di mana pun (termasuk lokal; matikan dengan `--no-public` atau
  `dashboard.public: false`). Kos tanpa alias → "Kos N" menurut urutan abjad nama asli. Serial → "HP N" (urut kos,
  operator, serial) dan tidak pernah tampil di mode apa pun. Rincian `collector_error` diganti kalimat umum; IP lokal di
  pesan error speedtest disamarkan; `source_ip` tidak dimuat sama sekali. Pesan error pemuatan hanya menampilkan nama
  kelas exception.
- **Satu halaman dengan 7 tab** (bukan multi-halaman): tab bisa digeser di HP dan filter berlaku untuk semuanya.
- **Linimasa** dirender sebagai tiga grafik terpisah per operator (bukan satu grafik gabungan), karena grafik gabungan
  Vega-Lite tidak mengikuti lebar layar HP. Titik diringkas ke ≤ 600 per seri; jeda data memutus garis.
- **Latar periode bermasalah satu warna** + tabel periode di bawah grafik; jenis diagnosis tidak dibedakan lewat warna
  (7 warna status tidak terbedakan bagi buta warna). Kategori selalu ikon + teks.
- Cache `st.cache_data` dengan TTL `cache_ttl_seconds` (300); kunci cache tidak memuat rahasia; untuk SQLite, waktu ubah
  file ikut jadi kunci supaya data baru langsung terbaca.
- Titik masuk di `dashboard/streamlit_app.py` supaya `dashboard/requirements.txt` berada di folder yang sama (Cloud
  memakai file dependensi di folder file utama), dan `requirements.txt` inti tetap ramping.
- Baris ganda di Sheets (rerun diagnosis, batch terkirim dua kali) dibuang saat dimuat.

## Verifikasi dengan data asli (mode sqlite, salinan DB yang sudah dikoreksi fix-sinr / fix-band)

Semua 7 tab tampil tanpa exception untuk kedua DB (lewat AppTest; Xiaomi juga lewat server sungguhan + tangkapan layar;
lebar HP 390 px dicek dengan DB sintetis dua operator).

| | Xiaomi (`Garden House` → tampil "Kos 1") | Infinix (`belum-diatur` → tampil "Kos 1") |
|---|---|---|
| Baris per menit | 127 | 9 |
| Diagnosis terakhir | Normal (jendela sampai 15/11 17:50 WIB) | Normal (sampai 15/11 23:30 WIB) |
| Median RSRP | −91,5 dBm → Baik | −64,8 dBm → Baik |
| Median RSRQ | −12,8 dB → Cukup | −10,8 dB → Cukup |
| Median SINR | 3,9 dB → Cukup | 11,8 dB → Baik |
| Unduh terakhir | Tidak ada data | Tidak ada data |
| Terakhir terlihat | 15/11 17:49 WIB, peringatan "Alat ukur diam" | 15/11 23:24 WIB, peringatan "Alat ukur diam" |
| Waktu RSRP Baik/Cukup/Buruk | 100 / 0 / 0 % | 100 / 0 / 0 % |
| Waktu RSRQ Baik/Cukup/Buruk | 0,8 / 96,1 / 3,1 % | 0 / 100 / 0 % |
| Waktu SINR Baik/Cukup/Buruk | 0,8 / 92,1 / 7,1 % | 100 / 0 / 0 % |
| Jendela diagnosis | 13 Normal, 2 Data kurang; 0 periode bermasalah | 1 Normal, 1 Data kurang |
| Rekomendasi | belum bisa (hanya 1 operator) | belum bisa (hanya 1 operator) |
| Kejadian / speedtest | 0 / 0 | 0 / 0 |
| Kualitas data (HP 1) | RSRP/RSRQ/SINR 100%, PCI 0%, identitas sel 0%, EARFCN/band 22% menit; 1 jeda 15:35–15:49 WIB (14 menit) | semua kolom 100%, tanpa jeda |

Catatan pencocokan: laporan (`report`) memberi median per sampel −91 dBm / 4,5 dB untuk Xiaomi; dashboard memberi
−91,5 dBm / 3,9 dB karena memakai rata-rata per menit. EARFCN "22% menit" vs "21% sampel" dengan alasan yang sama.

## Perlu uji manual (dashboard)

- [ ] Mode sheets dengan spreadsheet asli: service account khusus baca (Viewer), `python -m ceksinyal dashboard --mode sheets`.
- [ ] Jalankan `upload` sekali setelah pembaruan ini: tab `events` terbentuk dan kejadian lama terkirim.
- [ ] Deploy ke Streamlit Community Cloud: main file `dashboard/streamlit_app.py`; pastikan Cloud memasang dari
      `dashboard/requirements.txt` (kalau tidak, salin isinya ke berkas dependensi yang diminta Cloud).
- [ ] Secrets di Cloud: `private_key` satu baris dengan `\n`; nama kos tampil sebagai alias; tidak ada serial/nama asli.
- [ ] Pengaturan Sharing di Cloud: cek apakah akun bisa membatasi penonton; putuskan publik atau terbatas.
- [ ] Buka di HP sungguhan (Android + iPhone): tab bisa digeser, panel filter terbuka, grafik bisa disentuh.
- [ ] Data ≥ 1 hari dari ≥ 2 kos dan ≥ 2 operator: cek kecepatan muat, rekomendasi, dan perbandingan antar-kos.
- [ ] Minta satu orang non-teknik membaca tab Ringkasan dan "Cara membaca": apakah kalimatnya dimengerti.
- [ ] Kuota baca Sheets saat banyak penonton (TTL 300 detik seharusnya cukup).

## TODO / keterbatasan dashboard

- Di laptop ini Windows Application Control memblokir DLL pandas 3.1.1 dan pyarrow 26; tes dijalankan dengan
  pandas 2.2.3 + pyarrow 18.1.0. Kode ditulis untuk pandas 2 dan 3, tetapi pandas 3 belum pernah dijalankan di sini.
- Alias otomatis "Kos N" bisa bergeser kalau kos baru ditambahkan; daftarkan alias secara eksplisit.
- Seluruh isi tab Sheets dibaca tiap muat ulang; untuk data berbulan-bulan perlu pembatasan rentang di sisi pemuat.
- Warna grafik memakai palet mode terang; di tema gelap masih terbaca tetapi belum disetel khusus.
- Tabel lebar (perbandingan, kualitas data) digeser menyamping di HP.
