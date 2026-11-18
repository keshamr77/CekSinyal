# CekSinyal

Pemantau sinyal seluler untuk tugas besar ET4132 Kapita Selekta Teknik Telekomunikasi (ITB, Kelompok 2).

HP Android disambungkan ke laptop lewat USB. Laptop membaca kualitas sinyal lewat ADB tiap 10 detik, menguji kecepatan
internet lewat USB tethering, menyimpan semuanya ke SQLite, mendiagnosis penyebab sinyal buruk tiap 10 menit, lalu
mengirim hasilnya ke spreadsheet bersama (Google Sheets) dan bot Telegram. Dari data itu bisa dibuat laporan bukti per kos.

Alat ini **tidak memperkuat sinyal**. Nilainya ada di diagnosis, perbandingan operator, perbandingan antar-kos, dan bukti
tertulis.

```
HP Android ──USB (ADB)──> collector ──> SQLite <── speedtest (lewat USB tethering)
                                          │
                                   diagnosis (tiap 10 menit)
                                          │
                     ┌────────────────────┼──────────────────┐
                 uploader            bot Telegram          report
              (Google Sheets)     (alert + ringkasan)   (laporan bukti)
```

Batasan: hanya HP Android (iPhone tidak bisa dibaca lewat ADB); satu laptop membaca 1–2 HP; jalan di Windows, macOS,
dan Linux dengan Python 3.10+.

## Mulai cepat: dari instalasi sampai jalan di kos

### 1. Pasang

1. **Python 3.10+** dan **Android platform-tools** (`adb`). Kalau `adb` tidak ada di PATH, CekSinyal juga mencarinya di
   lokasi SDK bawaan (`ANDROID_HOME`, `ANDROID_SDK_ROOT`, `%LOCALAPPDATA%\Android\Sdk`, `~/Android/Sdk`,
   `~/Library/Android/sdk`), atau isi `adb_path` di config.
2. Ambil kode ini, lalu di foldernya:

   ```
   python -m venv .venv
   .venv\Scripts\activate          # Windows
   source .venv/bin/activate       # macOS / Linux
   pip install -r requirements.txt
   pip install -r requirements-dev.txt   # hanya kalau mau menjalankan tes
   ```

3. Salin `config.example.yaml` menjadi `config.yaml` dan `.env.example` menjadi `.env`. Kedua file hasil salinan itu
   berisi data pribadi/rahasia dan **tidak di-commit**.

### 2. Siapkan HP

1. Di HP: aktifkan *Opsi pengembang* (ketuk *Nomor build* 7 kali), lalu nyalakan *USB debugging*.
2. Sambungkan USB dan izinkan laptop saat HP bertanya.
3. `python -m ceksinyal devices` → HP muncul dengan status `device`. Catat serialnya.
4. `python -m ceksinyal probe` → pastikan RSRP/RSRQ/SINR terbaca. Kalau "GAGAL", kirim file `data/raw/probe_*.txt`
   ke tim supaya parser diperbaiki.

### 3. Isi `config.yaml`

```yaml
kos: kos-garden-house-a        # WAJIB: nama kos, konsisten di semua sesi
devices:
  - serial: R58M12345AB        # dari `devices`
    operator: Telkomsel
    source_ip: 192.168.42.129  # langkah 4; boleh dilewati kalau tanpa uji kecepatan
```

Kalau `kos` belum diisi, `collect` dan `run` menampilkan peringatan besar dan `selftest` gagal: data tanpa nama kos
tidak bisa dibandingkan. HP dual-SIM: tambahkan `phone_id: 0` atau `1` untuk memilih slot SIM, atau tulis HP yang sama
dua kali untuk membaca kedua SIM sekaligus (lihat "HP dual-SIM: dua operator dari satu HP").

### 4. (Opsional) uji kecepatan, Sheets, dan bot

- **Uji kecepatan**: nyalakan USB tethering di HP (Wi-Fi HP mati), jalankan `python -m ceksinyal interfaces`, salin IP
  interface tethering ke `source_ip`. Rinciannya di bagian "Uji kecepatan". Uji ini memakai kuota data HP.
- **Google Sheets**: isi `GOOGLE_CREDENTIALS_FILE` di `.env` dan `sheets.spreadsheet_id` di config ("Dashboard Google Sheets").
- **Bot Telegram**: isi `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_CHAT_ID` di `.env` ("Bot Telegram").

Bagian yang tidak diisi otomatis dilewati; sisanya tetap jalan.

### 5. Periksa, lalu jalankan

```
python -m ceksinyal selftest     # semua harus OK / LEWATI / PERINGATAN, tidak ada GAGAL
python -m ceksinyal run          # collector + speedtest + diagnosis + uploader + bot
```

Biarkan `run` berjalan (idealnya beberapa jam, mencakup jam sibuk malam). Hentikan dengan **Ctrl+C**; program menunggu
bagian yang sedang berjalan lalu berhenti rapi. Data aman di `data/ceksinyal.db` dan `run` bisa dilanjutkan kapan saja.

Tips di kos:

- Letakkan HP di titik yang biasa dipakai (meja belajar), jangan dipindah-pindah selama merekam.
- Matikan mode hemat baterai laptop/sleep; colok charger. Kabel USB yang longgar akan tercatat sebagai gangguan alat ukur.
- Untuk membandingkan operator, paling akurat pakai dua HP sekaligus. Satu HP dual-SIM juga bisa membaca dua operator
  sekaligus, tetapi hanya SIM data yang bisa diuji kecepatannya (lihat bagian berikut).

### HP dual-SIM: dua operator dari satu HP

Tulis serial yang sama dua kali di `devices`, satu entri per slot SIM, masing-masing dengan `phone_id` (0 = SIM 1,
1 = SIM 2) dan operatornya:

```yaml
devices:
  - serial: RRCW801YXVV
    operator: Telkomsel
    phone_id: 0
    source_ip: 10.20.249.202   # hanya di slot SIM data seluler
  - serial: RRCW801YXVV
    operator: Indosat
    phone_id: 1
```

- `dumpsys` dibaca sekali per HP, lalu disimpan satu baris per slot. Kolom `device_serial` berisi `SERIAL/sim1` dan
  `SERIAL/sim2` (hanya untuk HP yang ditulis dua kali; HP satu entri tetap memakai serial biasa). Dashboard
  menampilkannya sebagai "HP 1 · SIM 1" dan "HP 1 · SIM 2".
- **Uji kecepatan hanya untuk SIM data**: USB tethering selalu lewat SIM yang dipilih untuk data seluler di HP. Isi
  `source_ip` hanya di slot itu; slot lain hanya diukur sinyalnya, sehingga aturan "Sel padat" untuk operator itu
  tercatat "tidak tersedia". Kalau SIM data di HP diganti, pindahkan juga `source_ip` di config.
- Satu HP dual-SIM tetap dihitung satu HP untuk `collector.max_devices`. HP yang putus mencatat satu kejadian per operator.
- `probe` dan `selftest` menampilkan hasil per slot; `probe --phone-id 1` hanya membaca satu slot.
- Kalau dua slot ditulis, tiap entri wajib punya `phone_id` berbeda; `source_ip` hanya boleh di satu slot.
- Modem dual-SIM berbagi radio: SIM yang tidak aktif data bisa lebih jarang diperbarui, dan beberapa HP melaporkan nilai
  kosong untuk SIM 2 (Infinix: SINR selalu 0, dianggap "tidak tersedia" oleh diagnosis).

### 6. Lihat hasil

```
python -m ceksinyal diagnose                 # tabel diagnosis per jendela (sudah otomatis saat `run`)
python -m ceksinyal summary                  # ringkasan hari ini
python -m ceksinyal report --kos kos-garden-house-a   # laporan bukti (Markdown + HTML) di data/reports/
```

## `run` dan `selftest`

`python -m ceksinyal run` menjalankan tiap bagian di thread sendiri dengan koneksi database sendiri:

| Bagian | Kapan | Aktif kalau |
|---|---|---|
| collector | tiap `collector.interval_seconds` (10 detik) | selalu |
| diagnosis | dicek tiap menit; menulis jendela 10 menit yang baru selesai | selalu |
| speedtest | dicek tiap 30 detik; ringan tiap 15 menit, penuh tiap 60 menit | ada `devices[].source_ip` dan `speedtest.enabled` |
| uploader | tiap `sheets.interval_minutes` (5 menit) | spreadsheet ID + file kredensial ada |
| bot | tiap 30 detik (alert) + ringkasan harian | token + chat ID ada di `.env` |

- Saat mulai, `run` mencetak bagian mana yang aktif dan alasan untuk tiap bagian yang tidak aktif.
- **Error di satu bagian tidak mematikan bagian lain**: error dicatat di log, bagian itu disiapkan ulang, dan dicoba
  lagi di putaran berikutnya. Uji kecepatan yang lama tidak menunda pembacaan sinyal.
- **Ctrl+C** (atau SIGTERM di Linux) menghentikan semua bagian dengan rapi; bagian yang belum selesai dalam 10 detik
  (mis. uji penuh yang sedang berjalan) ditinggal.

`python -m ceksinyal selftest [--send-test]` memeriksa, tanpa mengubah data:

| Pemeriksaan | GAGAL kalau |
|---|---|
| config | (peringatan saja kalau `config.yaml` tidak ada) |
| kos | `kos` belum diisi |
| database | file database tidak bisa dibuka/ditulis |
| adb, HP | adb tidak ditemukan; HP di config tidak terhubung/unauthorized; parsing dumpsys gagal. Peringatan kalau HP menyembunyikan cell_id/EARFCN |
| tethering | `source_ip` di config tidak ada di laptop (peringatan kalau belum diisi) |
| Google Sheets | dikonfigurasi sebagian, kredensial tidak ada, atau spreadsheet tidak bisa dibuka (LEWATI kalau tidak dipakai) |
| bot Telegram | token/chat ID kurang atau ditolak Telegram (LEWATI kalau tidak dipakai). `--send-test` mengirim satu pesan uji |

Kode keluar 0 kalau tidak ada yang GAGAL.

## Perintah

| Perintah | Fungsi |
|---|---|
| `python -m ceksinyal selftest [--send-test]` | Cek config, kos, database, ADB/HP, tethering, Sheets, dan bot sebelum merekam. |
| `python -m ceksinyal run` | Semuanya sekaligus: collector, speedtest, diagnosis, uploader, bot. Hentikan dengan Ctrl+C. |
| `python -m ceksinyal devices` | Daftar HP terhubung: serial, status ADB, model, operator (dari config dan dari HP). |
| `python -m ceksinyal probe` | Baca sekali dari tiap HP, tampilkan hasil parsing, simpan output mentah ke `data/raw/probe_<serial>_<waktu>.txt`. |
| `python -m ceksinyal probe --file <path>` | Parse file output dumpsys tanpa HP (untuk mengecek fixture). |
| `python -m ceksinyal collect` | Collector saja: baca 1–2 HP tiap 10 detik dan simpan ke tabel `measurements`. Hentikan dengan Ctrl+C. |
| `python -m ceksinyal interfaces` | Daftar interface jaringan laptop dan IPv4-nya, untuk mengisi `devices[].source_ip`. |
| `python -m ceksinyal speedtest [--full] [--no-save]` | Uji kecepatan sekali sekarang untuk tiap HP yang punya `source_ip` (ringan; `--full` = speedtest-cli). |
| `python -m ceksinyal diagnose [--rerun] [--db <path>] [--kos <nama>]` | Diagnosis per jendela 10 menit untuk data yang belum didiagnosis; `--rerun` menghapus hasil lama dan menghitung ulang semuanya (setelah ambang diubah). |
| `python -m ceksinyal report --kos <nama> [--db <path>] [--out <folder>] [--since <tgl>] [--until <tgl>]` | Laporan bukti satu kos: file Markdown dan HTML di `data/reports/`. |
| `python -m ceksinyal upload` | Kirim sekarang semua baris yang belum ter-upload ke Google Sheets. |
| `python -m ceksinyal summary [--send] [--hours <n>] [--db <path>]` | Tampilkan ringkasan hari ini (isi yang sama dengan ringkasan harian bot); `--send` mengirimnya ke Telegram. |
| `python -m ceksinyal dashboard [--db <path>] [--mode sqlite\|sheets] [--no-public] [--port <n>]` | Dashboard web (Streamlit) di laptop ini. Butuh `pip install -r dashboard/requirements.txt`. |
| `python -m ceksinyal fix-sinr [--db <path>] [--serial <serial>] [--dry-run]` | Koreksi SINR LTE lama yang tercatat 10x lipat (HP Android 10, lihat di bawah). Selalu coba `--dry-run` dulu. |
| `python -m ceksinyal fix-band [--db <path>] [--dry-run]` | Koreksi band LTE lama yang tidak cocok dengan EARFCN (lihat "Identitas sel"). Selalu coba `--dry-run` dulu. |

Opsi umum: `--config <path>` (bawaan `./config.yaml`, atau variabel `CEKSINYAL_CONFIG`) dan `-v` (log debug).
Opsi `probe`: `--serial`, `--phone-id`. Opsi `collect`: `--interval <detik>`, `--iterations <n>`.
Opsi `fix-sinr`: `--db` (bawaan: `database` di config), `--serial`, `--dry-run`.

Kode keluar `probe`: 0 = semua berhasil, 1 = HP/ADB bermasalah, 2 = ada yang parsing-nya gagal.

## Konfigurasi (`config.yaml`)

| Kunci | Bawaan | Arti |
|---|---|---|
| `kos` | `belum-diatur` | Nama lokasi, ikut tersimpan di setiap baris. Wajib diisi (lihat "Mulai cepat"). |
| `database` | `data/ceksinyal.db` | File SQLite (relatif terhadap folder config). |
| `raw_dir` | `data/raw` | Folder output mentah. |
| `report_dir` | `data/reports` | Folder laporan bukti. |
| `adb_path` | `adb` | Path ke adb. Kalau diisi selain `adb`, path harus benar (tidak ada pencarian cadangan). |
| `timezone_offset_hours` | `7` | Zona waktu tampilan/laporan (7 = WIB). Data di database selalu UTC. |
| `diagnosis.*` | lihat "Diagnosis" | Ambang aturan diagnosis. |
| `collector.interval_seconds` | `10` | Jeda antar-pembacaan. |
| `collector.max_devices` | `2` | Jumlah HP maksimal yang dibaca. |
| `collector.adb_timeout_seconds` | `15` | Batas waktu satu perintah adb. |
| `collector.raw_dump_interval_minutes` | `60` | Maksimal satu file output mentah per HP per periode ini. |
| `devices[].serial` | – | Serial ADB HP. |
| `devices[].operator` | – | Nama operator. Kalau kosong: `getprop gsm.operator.alpha`, lalu nama dari dumpsys. |
| `devices[].phone_id` | otomatis | Slot SIM untuk HP dual-SIM (0/1). Kalau kosong, dipilih slot yang punya sinyal dan koneksi data aktif. Wajib kalau serial yang sama ditulis dua kali (dua slot dibaca sekaligus). |
| `devices[].source_ip` | – | IP laptop di interface USB tethering HP ini. Wajib untuk uji kecepatan. |
| `speedtest.*` | lihat "Uji kecepatan" | Jadwal dan target uji kecepatan. |
| `sheets.*` | lihat "Dashboard Google Sheets" | Spreadsheet bersama dan cara upload. |
| `telegram.*` | lihat "Bot Telegram" | Batas frekuensi alert dan jam ringkasan harian. |
| `dashboard.*` | lihat "Dashboard web" | Sumber data, mode publik, alias kos, batas kategori Baik/Cukup/Buruk. |
| `devices[].sinr_unit` | `auto` | Satuan rssnr LTE di dumpsys HP ini: `auto` (dari versi Android), `db`, atau `deci_db` (0,1 dB). Lihat "Satuan SINR LTE". |

Kalau `devices` kosong, collector memakai HP apa saja yang terhubung (maks. `max_devices`).

## Cara kerja collector

- Tiap putaran: `adb devices -l`, lalu `adb -s <serial> shell dumpsys telephony.registry` untuk tiap HP
  (paralel, jadi HP yang lambat tidak menunda HP lain).
- Parser (`ceksinyal/collector/parser.py`) mengambil RSRP/RSRQ/SINR/RSSI dari `mSignalStrength` dan
  PCI/Cell ID/EARFCN/band dari identitas sel yang melayani (lihat "Identitas sel"). Mendukung format Android 9 (kolom posisi,
  SINR dalam 0,1 dB) sampai 14, LTE, NR SA, dan NR NSA. Nilai yang tidak ada, `2147483647`,
  disamarkan (`*****`), atau di luar rentang fisik menjadi kosong (NULL).
- **RAT**: tidak dalam layanan → `NONE` dengan nilai sinyal kosong (dihitung "sinyal hilang").
  NR NSA (5G non-standalone) disimpan sebagai `LTE` dengan nilai anchor LTE; nilai NR hanya tampil di `probe`.
- **Identitas sel**: `pci`, `cell_id`, `earfcn`, dan `band` selalu diambil dari **satu blok** yang sama, tidak
  dicampur. Kandidat bloknya: `NetworkRegistrationInfo` (PS lalu CS) di `mServiceState`, `mCellIdentity`,
  `mBarringInfo`, dan `CellInfo` dengan `mRegistered=YES`. Dipilih blok yang paling lengkap (jumlah cell id, PCI,
  EARFCN yang valid); kalau seri, urutan sumber di atas yang menentukan. `CellInfo` paling akhir karena bisa basi
  (Infinix Android 14: CellInfo menunjuk sel lain padahal HP sudah pindah). Kolom yang kosong hanya diisi dari
  blok lain yang cell id-nya sama.
- **Band LTE** dihitung dari EARFCN (tabel 3GPP TS 36.101) setiap kali EARFCN dikenal; `mBands` dari HP hanya
  dipakai kalau EARFCN kosong atau di luar tabel. Alasannya: Infinix menulis `mBands=[1]` untuk sel EARFCN 1850
  (Band 3). Data lama yang terlanjur salah diperbaiki dengan `fix-band`: untuk baris LTE yang punya EARFCN, band
  diganti dengan hasil hitung dari EARFCN. Database di-backup dulu ke `<nama>.backup-<waktu>.db`.
- **Identitas sel disembunyikan**: beberapa HP (contohnya Xiaomi MIUI, mis. Redmi 9C Android 10) tidak
  memberikan identitas sel lewat ADB, bahkan saat Lokasi menyala: `CellIdentityLte` hanya berisi
  bandwidth/MCC/MNC/nama operator dan `mCellLocation` berisi `cid=-1`. Akibatnya `pci` dan `cell_id`
  bisa kosong. Sebagai cadangan, EARFCN diambil dari `mChannelNumber` di `mServiceState` saat ini
  (bukan dari riwayat *local logs*; `2147483647` dan nilai di luar rentang diabaikan), lalu band dihitung
  dari EARFCN. Diagnosis handover memakai pergantian EARFCN sebagai pendekatan kalau `cell_id` kosong.
- **Satuan SINR LTE**: modem selalu melapor rssnr dalam 0,1 dB, dan Android baru mengonversinya ke dB
  mulai Android 11 (SDK 30). Satuannya ditentukan **per HP**, bukan ditebak per sampel: collector membaca
  `getprop ro.build.version.sdk` sekali per HP. SDK ≤ 29 (Android 10 ke bawah) → `deci_db` (selalu dibagi 10),
  SDK ≥ 30 → `db`. Kalau SDK tidak terbaca, sementara dianggap `db` dan dicoba dibaca lagi di putaran berikutnya.
  Pengaman: kalau HP mode `db` otomatis mengirim |rssnr| antara 31 dan 300, HP itu dipindah ke `deci_db`
  untuk sisa sesi dan dicatat event `collector_error` (isi `sinr_unit: deci_db` di config kalau memang begitu).
  `sinr_unit` di config selalu menang dan tidak diubah pengaman. Format Android 9 (`ss=`) selalu 0,1 dB;
  SINR NR (`ssSinr`/`csiSinr`) dalam dB dan tidak terpengaruh. Nilai mentah disimpan di kolom `sinr_raw`
  beserta satuannya di `sinr_unit`, jadi `sinr` bisa dihitung ulang kalau satuannya ternyata salah.
- **Data lama (sebelum perbaikan satuan)**: versi lama menebak satuan per sampel, sehingga SINR asli
  -2,0..3,0 dB dari HP Android 10 tercatat 10x lipat (mentah 25 → 25 dB). `fix-sinr` memperbaiki baris LTE
  lama (`sinr_raw` kosong): bilangan bulat dari pola {-20, -18, -17, -15, -13, -12, -10, -8, -7, -5, -3, 0, 2, 5,
  7, 10, 12, 15, 17, 20, 22, 25, 27, 30} yang ≥ 10, ≤ -8, 0, atau 2 dibagi 10. Nilai 5, 7, dan -3 ambigu dan
  dibiarkan. Database di-backup dulu ke `<nama>.backup-<waktu>.db` di folder yang sama. Heuristik ini hanya
  benar untuk HP Android 10 ke bawah: untuk HP Android 11+ nilai bulat seperti 10 atau 25 dB itu asli, jadi
  pakai `--serial`. Kolom PECAHAN di ringkasan membantu: HP yang tidak punya nilai pecahan sama sekali
  kemungkinan Android 11+. Baris yang sudah di-upload tidak ditandai ulang untuk di-upload.
- **Database lama** otomatis mendapat kolom baru (`sinr_raw`, `sinr_unit`) saat dibuka; data tidak dihapus.
- **Parsing gagal** (format tidak dikenali, atau HP dalam layanan tapi tidak ada RSRP): baris tetap disimpan
  dengan `raw_ok=0` dan `rat` kosong, output mentah disimpan ke `data/raw/<serial>_<waktu>.txt`
  (maks. satu per HP per jam, juga setelah restart), dan dicatat sebagai event `collector_error`.
  Diagnosis hanya memakai baris `raw_ok=1`.
- **HP terputus / unauthorized / adb hilang**: dicatat sekali sebagai event `collector_error` saat status
  berubah, lalu dicoba lagi tiap putaran. Program tidak berhenti karena satu HP bermasalah.

## Uji kecepatan

Uji kecepatan dijalankan dari laptop **lewat USB tethering HP**, supaya yang diukur adalah jaringan operator itu,
bukan Wi-Fi laptop. Tiap HP diarahkan lewat IP sumber (`devices[].source_ip`).

Cara menyiapkan:

1. Di HP: matikan Wi-Fi, nyalakan data seluler, lalu nyalakan *USB tethering* (Setelan → Hotspot & tethering).
   USB debugging tetap menyala; ADB dan tethering bisa jalan bersamaan di satu kabel.
2. Jalankan `python -m ceksinyal interfaces`. Interface baru yang muncul (biasanya `192.168.42.x`, bertanda
   "kemungkinan USB tethering Android") adalah milik HP itu. Untuk dua HP, colok satu per satu supaya jelas yang mana.
3. Salin IPv4-nya ke `devices[].source_ip` di `config.yaml`, lalu coba `python -m ceksinyal speedtest`.

| Uji | Jadwal bawaan | Isi | Kuota |
|---|---|---|---|
| Ringan (`light`) | tiap 15 menit | "ping" 3 kali + unduh satu file ±1 MB (`speedtest.download_url`) | ±1 MB |
| Penuh (`full`) | tiap 60 menit | `python -m speedtest --json --secure --source <IP>` (speedtest-cli): ping, unduh, unggah | puluhan–ratusan MB |

- **Ping** di uji ringan adalah waktu jabat tangan TCP ke `ping_host:ping_port` (median dari `ping_count` percobaan),
  bukan ICMP, supaya sama di Windows/macOS/Linux dan bisa diikat ke IP sumber. Nilainya sedikit di atas ping ICMP.
- **Kecepatan unduh ringan** dihitung dari satu file kecil, jadi di jaringan cepat cenderung lebih rendah daripada uji
  penuh (koneksi belum sempat mencapai kecepatan puncak). Yang dipakai diagnosis adalah perbandingan terhadap median
  operator itu sendiri, jadi tetap konsisten. Kalau batas waktu habis sebelum file selesai, kecepatan dihitung dari
  byte yang sudah diterima.
- **Jadwal dihitung dari tabel `speedtests`**, jadi restart tidak mengulang uji yang baru dijalankan. Uji pertama
  selalu ringan; uji penuh pertama menyusul satu selang kemudian. Saat uji penuh jatuh tempo, uji ringan di slot itu
  dilewati. `full_interval_minutes: 0` mematikan uji penuh (hemat kuota).
- **Gagal** (tethering mati, IP sumber tidak ada di laptop, timeout, speedtest-cli error) → baris tetap disimpan dengan
  `ok=0` dan pesan di kolom `error`. HP tanpa `source_ip` dilewati dengan satu peringatan di log.
- **Linux**: mengikat IP sumber saja belum tentu memaksa paket keluar lewat interface tethering kalau ada rute bawaan
  lain (Wi-Fi/Ethernet). Putuskan koneksi lain, atau atur *policy routing* untuk IP itu. Windows dan macOS memilih
  interface dari IP sumber.

## Diagnosis

`python -m ceksinyal diagnose` menghitung diagnosis per operator per kos untuk tiap jendela 10 menit, menyimpannya
ke tabel `diagnoses`, dan mencatat perubahan ke tabel `events`. Aturan diperiksa berurutan, berhenti di "ya" pertama:

| # | Syarat (ambang di `diagnosis.*`) | Hasil |
|---|---|---|
| – | Sampel di jendela < `min_samples` (20) | Data kurang |
| 1 | ≥ `signal_lost_fraction` (50%) sampel tanpa layanan | Sinyal putus |
| 2 | Median RSRP < `rsrp_weak_dbm` (−110 dBm) | Cakupan lemah |
| 3 | Median SINR < `sinr_interference_db` (0 dB) | Interferensi |
| 4 | Median unduh < `congestion_ratio` (50%) × median unduh 24 jam terakhir | Sel padat |
| 5 | Sel berganti ≥ `handover_changes` (4) kali | Handover bolak-balik |
| 6 | Selain itu | Normal |

Hal yang perlu diketahui:

- **Jendela di kisi tetap** (xx:00, xx:10, ... UTC) dan sampel dihitung apa adanya. Kalau collector sempat berhenti,
  jendela yang kosong/berisi sedikit sampel menjadi "Data kurang", bukan diagnosis tebakan. Jendela yang belum selesai
  belum didiagnosis. Hanya baris `raw_ok=1` yang dihitung.
- **Setiap aturan bisa "tidak tersedia"**, karena kelengkapan data berbeda antar-HP. Aturan itu dilewati dan alasannya
  ditulis di kolom `detail` (tampil sebagai CATATAN), misalnya:
  - `rsrp: tidak tersedia` / `sinr: tidak tersedia`: kurang dari `min_metric_samples` (5) nilai di jendela.
  - `sinr: tidak tersedia (selalu 0 ...)`: semua SINR di jendela persis 0. Beberapa HP (Infinix SIM 2) menulis
    `rssnr=0` sebagai nilai kosong, jadi tidak dianggap 0 dB.
  - `kecepatan: tidak tersedia`: tidak ada uji kecepatan untuk jendela itu (uji di jendela itu, atau uji terakhir
    dalam `speed_lookback_minutes`), atau pembanding 24 jam kurang dari `min_speed_samples` (6).
  - `handover: dihitung dari earfcn, cell_id tidak tersedia`: HP menyembunyikan cell id (Xiaomi), jadi pergantian
    EARFCN dipakai sebagai pendekatan. Kalau EARFCN juga jarang terisi, jumlahnya hanya batas bawah.
  - `handover: tidak tersedia`: `cell_id` dan `earfcn` kosong.
- **Event**: perubahan hasil (dibanding hasil konklusif terakhir; "Data kurang" tidak dihitung) dicatat sebagai
  `diagnosis_changed` (detail `Lama -> Baru; ...`), atau `signal_lost` kalau hasil barunya "Sinyal putus". Event dari
  jendela yang sudah lebih dari 30 menit lalu langsung ditandai `notified=1` supaya bot tidak mengirim alert basi.
- **`--rerun`** menghapus semua baris `diagnoses` dan event `diagnosis_changed`/`signal_lost` (untuk `--kos` tertentu
  atau semua), lalu menghitung ulang. Event `collector_error` tidak dihapus. Baris diagnosis baru ber-`uploaded=0`,
  jadi akan dikirim lagi ke Google Sheets (baris lama di sheet tidak terhapus otomatis).
- **Rekomendasi operator**: kalau satu kos punya dua operator, dipilih yang porsi jendela Normal-nya tertinggi; kalau
  sama, yang median RSRP-nya lebih tinggi.

## Dashboard Google Sheets

Semua laptop anggota mengirim data ke **satu spreadsheet bersama**; tiap baris membawa kolom `kos`, jadi data antar-kos
bisa dibandingkan dengan pivot/grafik di Sheets.

Menyiapkan (sekali untuk tim):

1. Di Google Cloud Console: buat project, aktifkan **Google Sheets API**, buat **service account**, lalu buat kunci
   JSON untuknya. Bagikan file JSON itu ke anggota lewat jalur pribadi. **Jangan di-commit** (pola `*credentials*.json`
   dan `service_account*.json` sudah ada di `.gitignore`; simpan dengan nama seperti itu).
2. Buat satu Google Spreadsheet, lalu *Share* ke alamat email service account (`...@...iam.gserviceaccount.com`)
   sebagai **Editor**.
3. Di tiap laptop: isi `.env` dengan `GOOGLE_CREDENTIALS_FILE=<path file JSON>` (relatif terhadap folder config) dan isi
   `sheets.spreadsheet_id` di `config.yaml` (bagian URL di antara `/d/` dan `/edit`), atau `GOOGLE_SPREADSHEET_ID` di `.env`.
4. Cek dengan `python -m ceksinyal upload`.

Cara kerja (store-and-forward):

- Semua data disimpan dulu ke SQLite dengan `uploaded=0`. Uploader mengirim per batch, dan menandai `uploaded=1`
  **hanya setelah** Sheets menerima batch itu. Internet putus → baris tetap `0` dan dikirim di putaran berikutnya;
  tidak ada data yang hilang. Jaminannya "minimal sekali": pada kasus langka (koneksi putus tepat setelah Sheets
  menerima), satu batch bisa muncul dua kali di sheet.
- Tab dibuat otomatis berikut baris judulnya: `measurements`, `speedtests`, `diagnoses`, `events`.
- `events` berisi kejadian `signal_lost`, `diagnosis_changed`, dan `collector_error` (dipakai dashboard web). Penanda
  internal `daily_summary` tidak dikirim. DB lama otomatis mendapat kolom `events.uploaded`; kejadian lama ikut terkirim
  pada upload berikutnya.
- `measurements` dikirim sebagai **rata-rata per menit per HP** supaya hemat kuota API: `ts_minute` (UTC), `kos`,
  `device_serial`, `operator`, `n_samples`, `n_no_service`, `n_parse_failed`, `rat` (terbanyak), `rsrp_avg`, `rsrp_min`,
  `rsrq_avg`, `sinr_avg`, lalu `pci`/`cell_id`/`earfcn`/`band` terakhir di menit itu. Menit yang sedang berjalan baru
  dikirim setelah selesai. `sheets.aggregate_measurements: false` mengirim setiap sampel apa adanya.
- Waktu di sheet dalam UTC ISO 8601 (WIB = UTC+7).
- `diagnose --rerun` membuat baris diagnosis baru (`uploaded=0`), jadi baris diagnosis lama di sheet perlu dihapus manual
  kalau tidak ingin ganda.

## Bot Telegram

Menyiapkan:

1. Buat bot lewat **@BotFather** (`/newbot`) dan salin tokennya.
2. Masukkan bot ke grup kelompok (atau chat langsung dengan bot), kirim satu pesan, lalu ambil *chat ID*-nya, misalnya
   dengan membuka `https://api.telegram.org/bot<TOKEN>/getUpdates` di browser (`chat.id`; ID grup bernilai negatif).
3. Isi `.env`: `TELEGRAM_BOT_TOKEN=...` dan `TELEGRAM_CHAT_ID=...`. Token hanya dibaca dari `.env`/lingkungan, tidak dari
   `config.yaml`, dan tidak pernah ditulis ke log.
4. Cek dengan `python -m ceksinyal summary --send`.

Yang dikirim bot (saat `run` berjalan):

| Pesan | Pemicu |
|---|---|
| ⚠️ Diagnosis berubah | Event `diagnosis_changed` ke hasil bermasalah (mis. Normal → Interferensi), berisi jendela, median RSRP/SINR, dan saran. Kembali ke Normal tidak di-alert. |
| 🔴 Sinyal putus | Event `signal_lost` (≥ 50% sampel jendela tanpa layanan). |
| ⚙️ Gangguan alat ukur | Event `collector_error` (HP terputus/unauthorized, adb hilang, parsing gagal). |
| 📊 Ringkasan harian | Sekali sehari setelah `telegram.daily_summary_time` (bawaan 21:00 waktu lokal): per operator diagnosis dominan, persentase waktu RSRP < −100 dBm, jumlah kejadian putus; plus rekomendasi operator kalau ada dua. Periode: 00:00 sampai saat dikirim. |

- **Batas frekuensi**: maksimal satu alert *yang sama* per operator per `alert_cooldown_minutes` (15 menit). "Sama" =
  jenis event yang sama (dan untuk perubahan diagnosis, hasil baru yang sama). Alert yang tertahan tidak dikirim
  belakangan; event-nya tetap ada di tabel `events` dan masuk laporan. Batas ini diingat selama program berjalan.
- **Internet mati**: pengiriman gagal → event tetap `notified=0` dan dicoba lagi di putaran berikut; event yang sudah
  lebih dari 2 jam tidak dikirim lagi. Ringkasan harian dicoba ulang sampai berhasil pada hari itu, dan ditandai di
  tabel `events` (tipe `daily_summary`) supaya tidak terkirim dua kali walau program di-restart.
- Diagnosis yang dihitung dari data lama (`diagnose --rerun`, atau jendela berumur > 30 menit) tidak memicu alert.

## Dashboard web

Dashboard web melengkapi spreadsheet bersama: Google Sheets tetap tempat data semua laptop berkumpul, dan dashboard
menampilkannya secara visual untuk dosen, tim, dan warga kos (termasuk yang bukan orang teknik). Setiap angka punya
kategori **Baik / Cukup / Buruk** dan penjelasan singkat.

| Tab | Isi |
|---|---|
| Ringkasan | Kartu per kos × operator: diagnosis terakhir + saran, median RSRP/RSRQ/SINR 24 jam dengan kategori, kecepatan unduh terakhir, "terakhir terlihat" (peringatan kalau alat ukur diam > 15 menit). |
| Linimasa | RSRP, RSRQ, SINR terhadap waktu per operator; latar berwarna = periode diagnosis bermasalah (plus tabelnya). |
| Perbandingan | Rekomendasi operator per kos (aturan sama dengan diagnosis), persentase waktu Baik/Cukup/Buruk per kos × operator, tabel perbandingan. |
| Kecepatan | Unduh, unggah, ping terhadap waktu; daftar uji yang gagal. |
| Kejadian | Sinyal putus dan perubahan diagnosis; gangguan alat ukur dipisah. |
| Kualitas data | Per HP: kolom apa yang tersedia (mis. Xiaomi tanpa PCI/identitas sel), jeda data, catatan keterbatasan. |
| Cara membaca | Arti RSRP/RSRQ/SINR dan tiap diagnosis dalam bahasa awam, dengan ambang yang sedang dipakai. |

Filter kos, operator, dan rentang waktu ada di panel samping (di HP: tombol `>>` kiri atas). Semua waktu ditampilkan
dalam WIB (`timezone_offset_hours`); data tetap UTC.

### Menjalankan dashboard (lokal)

```
pip install -r dashboard/requirements.txt        # sekali; tidak diperlukan di laptop yang hanya merekam
python -m ceksinyal dashboard --db data/ceksinyal.db      # mode sqlite: baca file DB lokal
python -m ceksinyal dashboard                             # sumber dari config (lihat di bawah)
```

Browser terbuka di `http://localhost:8501`. Hentikan dengan Ctrl+C. Opsi: `--port <n>`, `--no-browser`,
`--mode sqlite|sheets`, `--no-public` (tampilkan nama kos asli; hanya untuk di laptop sendiri).

Sumber data ditentukan berurutan: opsi/variabel lingkungan → `.streamlit/secrets.toml` → `config.yaml`.

| Mode | Kapan dipakai | Yang dibutuhkan |
|---|---|---|
| `sqlite` | Demo dan pengembangan; data satu laptop | `--db <path>` atau `CEKSINYAL_DASHBOARD_DB`, atau `database` di config. DB dibuka hanya-baca. |
| `sheets` | Data semua kos dari spreadsheet bersama | `sheets.spreadsheet_id` + `GOOGLE_CREDENTIALS_FILE` di `.env` (sama seperti uploader), atau secrets. |

Variabel lingkungan: `CEKSINYAL_DASHBOARD_DB`, `CEKSINYAL_DASHBOARD_MODE` (`sqlite`/`sheets`),
`CEKSINYAL_DASHBOARD_PUBLIC` (`1`/`0`). `dashboard.mode: auto` (bawaan) memilih `sheets` kalau spreadsheet dan
kredensial tersedia, selain itu `sqlite`.

Hal yang perlu diketahui:

- **Satu baris = satu menit per HP.** Sheets sudah berisi rata-rata per menit; data SQLite (per sampel) diagregasi
  dengan fungsi yang sama dengan uploader, jadi kedua mode menampilkan angka yang sama. Akibatnya median di dashboard
  adalah median dari rata-rata per menit, yang bisa sedikit berbeda dari median per sampel di `report`
  (contoh data Xiaomi: SINR 3,9 dB di dashboard, 4,5 dB di laporan).
- **Kategori** (bisa diubah di `dashboard.*`): Baik kalau ≥ batas baik, Buruk kalau < batas buruk, di antaranya Cukup.
  Batas buruk RSRP dan SINR **diambil dari ambang diagnosis** (`diagnosis.rsrp_weak_dbm` = −110 dBm,
  `diagnosis.sinr_interference_db` = 0 dB), jadi "Buruk" di dashboard selalu sejalan dengan "Cakupan lemah" dan
  "Interferensi". Bawaan batas baik: RSRP −100 dBm, RSRQ −10 dB (buruk < −15), SINR 10 dB, unduh 10 Mbps (buruk < 2).
- **"24 jam terakhir"** dihitung mundur dari data terbaru yang ada, bukan dari jam sekarang, supaya data lama tetap
  terbaca. Kalau data terbaru lebih tua dari `dashboard.stale_minutes` (15), kartu menampilkan peringatan "Alat ukur diam".
- **Cache**: data dimuat ulang paling cepat tiap `dashboard.cache_ttl_seconds` (300 detik) atau lewat tombol "Muat ulang
  data". Satu kali muat = 4 permintaan baca ke Sheets (satu per tab), jauh di bawah kuota.
- SINR yang selalu persis 0 dianggap "Tidak ada data" (sama dengan aturan diagnosis).
- Baris ganda di spreadsheet (akibat `diagnose --rerun` atau batch yang terkirim dua kali) dibuang saat dimuat.

### Privasi

Repo ini publik dan dashboard bisa dibuka lewat link, jadi:

- **Mode publik aktif secara bawaan** (`dashboard.public: true`). Nama kos selalu diganti alias dari
  `dashboard.kos_aliases` (atau `[kos_aliases]` di secrets). Kos yang belum punya alias otomatis menjadi "Kos 1",
  "Kos 2", ... menurut urutan abjad nama aslinya. Nomor otomatis bisa bergeser kalau ada kos baru, jadi sebaiknya
  daftarkan semua kos secara eksplisit.
- **Serial HP tidak pernah ditampilkan**, juga di mode non-publik; diganti "HP 1", "HP 2", ...
- Rincian gangguan alat ukur (yang bisa memuat serial dan path file di laptop anggota) diganti kalimat umum, dan alamat
  IP lokal di pesan error uji kecepatan disamarkan.
- Pemetaan alias berisi nama kos asli, jadi tempatnya di `config.yaml` atau secrets, **bukan** di file yang di-commit.
- Spreadsheet bersama tetap berisi nama kos dan serial asli. Bagikan spreadsheet hanya ke tim; dashboard cukup diberi
  service account khusus baca.

### Deploy ke Streamlit Community Cloud

1. **Service account khusus baca.** Di Google Cloud Console (project yang sama dengan uploader boleh), buat service
   account baru, mis. `dashboard-baca`, dan unduh kunci JSON-nya. Buka spreadsheet bersama → *Share* → masukkan email
   service account itu sebagai **Viewer**. (Jangan pakai service account uploader yang berhak menulis.)
2. **Push repo ke GitHub**, termasuk folder `dashboard/` dan `.streamlit/secrets.toml.example`. Pastikan
   `.streamlit/secrets.toml`, `.env`, `config.yaml`, dan file JSON kredensial **tidak** ikut (sudah di `.gitignore`).
3. Buka <https://share.streamlit.io>, masuk dengan akun GitHub, lalu **Create app** → *Deploy a public app from GitHub*.
   Isi: *Repository* = repo ini, *Branch* = branch yang dipakai, *Main file path* = `dashboard/streamlit_app.py`.
4. Di **Advanced settings**: pilih Python 3.11 atau 3.12, lalu di kotak **Secrets** tempel isi
   `.streamlit/secrets.toml.example` yang sudah diisi: `spreadsheet_id`, `[kos_aliases]`, dan `[gcp_service_account]`
   (salin tiap kolom dari file JSON; `private_key` ditulis satu baris dengan `\n`).
5. **Deploy.** Dependensi dipasang dari `dashboard/requirements.txt` (file dependensi di folder yang sama dengan file
   utama). Setelah app hidup, buka dan cek ketujuh tab; nama kos harus sudah berupa alias.
6. Mengubah secrets atau alias nanti: menu app → **Settings → Secrets**. App otomatis di-deploy ulang tiap ada push
   ke branch itu.

**Siapa yang boleh membuka app.** App dari repo publik bersifat publik: siapa pun yang punya link bisa membukanya,
karena itu mode publik (alias) wajib tetap aktif. Kalau ingin membatasi, buka menu app → **Settings → Sharing** dan
pilih agar hanya orang tertentu yang bisa melihat, lalu undang alamat email mereka; penonton harus login untuk
membuka. Ketersediaan dan batas fitur ini bergantung pada paket akun (pada paket gratis jumlah app privat terbatas),
jadi cek di halaman Sharing akun Anda. Membatasi penonton tidak mengubah isi repo yang tetap publik.

Uji secrets di laptop sebelum deploy: salin contoh menjadi `.streamlit/secrets.toml`, isi, lalu jalankan
`python -m ceksinyal dashboard --mode sheets`.

## Laporan bukti

`python -m ceksinyal report --kos <nama>` membuat dua file di `data/reports/` (tidak di-commit):
`laporan_<kos>_<tanggal-jam>.md` dan `.html`. File HTML berdiri sendiri (grafik SVG di dalam file, tanpa internet,
mengikuti mode terang/gelap, bisa dicetak ke PDF dari browser) sehingga bisa langsung dikirim ke pemilik kos atau operator.

Isi laporan:

- **Ringkasan per operator**: jumlah sampel, durasi, diagnosis dominan, median RSRP, persentase waktu RSRP < −100 dBm,
  median SINR, persentase tanpa layanan, jumlah kejadian putus, median kecepatan unduh; plus rekomendasi operator kalau
  ada dua operator di kos itu.
- **RSRP sepanjang waktu** (HTML): satu garis per operator; garis terputus di tempat data bolong. Arahkan kursor untuk
  melihat nilai.
- **Distribusi RSRP/RSRQ/SINR**: statistik (min, P10, median, P90, maks) dan histogram, dengan garis ambang diagnosis.
- **Linimasa diagnosis**: tiap jendela 10 menit, baris = jenis diagnosis; tabel semua jendela beserta catatannya.
- **Kejadian sinyal putus** dan **gangguan pengumpulan data** (HP terputus, ADB, parsing; ini masalah alat ukur, bukan
  jaringan).
- **Keterbatasan data per HP**: mis. cell_id disembunyikan, EARFCN jarang terisi, SINR selalu 0, sampel versi lama yang
  mungkin perlu `fix-sinr`, band yang perlu `fix-band`, data bolong, belum ada uji kecepatan, rekaman terlalu pendek.
- **Ambang yang dipakai** saat laporan dibuat.

Catatan: `--kos` harus persis sama dengan nama di data (kalau salah, daftar kos yang ada ditampilkan). Sebelum membuat
laporan, jendela yang belum didiagnosis didiagnosis dulu (matikan dengan `--no-diagnose`). `--since`/`--until` memakai
tanggal lokal `YYYY-MM-DD`. Waktu di laporan memakai `timezone_offset_hours`.

## Pemecahan masalah

| Gejala | Yang dicek |
|---|---|
| `devices` kosong / `unauthorized` | Kabel data (bukan kabel cas saja), USB debugging menyala, buka kunci HP dan setujui dialog izin. |
| `probe` GAGAL: format tidak dikenali | Kirim `data/raw/probe_*.txt` ke tim; tambahkan sebagai fixture `real_*.txt` lalu perbaiki parser. |
| `pci`/`cell_id` kosong | HP menyembunyikan identitas sel (Xiaomi). Normal; diagnosis handover memakai EARFCN atau "tidak tersedia". |
| SINR terlihat 10x terlalu besar/kecil | Satuan rssnr HP itu tidak sesuai tebakan dari versi Android: isi `devices[].sinr_unit`. Data lama: `fix-sinr`. |
| Semua diagnosis "Data kurang" | Sampel per jendela < 20: collector baru jalan, sering terputus, atau interval terlalu besar. |
| Speedtest selalu `ok=0` "IP sumber ... tidak ada" | USB tethering mati atau IP berubah setelah HP dicabut: `interfaces`, perbarui `source_ip`. |
| Upload gagal 403/404 | Spreadsheet belum di-*share* ke email service account, atau `spreadsheet_id` salah. |
| Bot diam | `selftest --send-test`; pastikan bot sudah dimasukkan ke grup dan `TELEGRAM_CHAT_ID` benar (ID grup negatif). |
| `dashboard`: "DLL load failed ... Application Control policy" (Windows) | Kebijakan Smart App Control memblokir DLL pandas/pyarrow versi terbaru. Pasang versi yang lebih lama, mis. `pip install "pandas==2.2.3" "pyarrow==18.1.0"`. |
| Dashboard: "Gagal memuat data dari sumber" | Mode sheets: spreadsheet belum di-share ke service account baca, `spreadsheet_id` salah, atau secrets belum lengkap. |
| Kos tertulis `belum-diatur` di data | Isi `kos` di config. Data lama tetap berlabel lama (bisa diubah manual dengan SQL `UPDATE ... SET kos = ...` di salinan DB). |

## TODO

- HP dual-SIM yang dibaca dua slot: hanya SIM data yang bisa diuji kecepatannya (keterbatasan tethering, bukan perangkat lunak).
- Batas frekuensi alert bot disimpan di memori; setelah `run` di-restart, alert yang sama bisa terkirim lagi lebih cepat
  dari 15 menit.
- Infinix Android 14 dual-SIM: SIM 2 (Tri) menunjukkan `rssnr=0` di `mSignalStrength` dan di semua CellInfo.
  Kemungkinan itu nilai kosong, bukan 0 dB sungguhan. Perlu dicek kalau SIM 2 ikut dipantau.
- Xiaomi Android 10 (Redmi 9C): `pci` dan `cell_id` kosong di semua sampel, dan `earfcn`/`band` hanya terisi
  ±21% sampel. Perlu dicari sumber lain (mis. `dumpsys phone`,
  `mCellInfo` pada versi MIUI lain) sebelum aturan handover bisa diandalkan di HP ini.

## Tes

```
python -m pytest
```

Fixture ada di `tests/fixtures/`. Sebagian besar sintetis (`synthetic_*.txt`); fixture asli:
`real_redmi9c_android10.txt` (Xiaomi Redmi 9C, MIUI, Android 10, MediaTek, Telkomsel) dan
`real_infinix_android14_dualsim.txt` (Infinix, Android 14, SIM 1 Telkomsel, SIM 2 Tri, slot ke-3 mati). Untuk menambah fixture asli:

1. Jalankan `python -m ceksinyal probe` dengan HP tersambung, lalu ambil file `data/raw/probe_*.txt`.
2. Salin ke `tests/fixtures/real_<merek>_<android>_<operator>.txt`. Hapus dulu data pribadi
   (nomor telepon di `mCallIncomingNumber`, IMEI/IMSI/ICCID, dsb.) kalau ada. Nama aplikasi pihak ketiga
   di `listen logs`/`registrations` juga sebaiknya diganti (mis. `com.example.app1`).
3. Opsional tapi disarankan: buat `real_....expected.json` berisi nilai yang benar (cek di aplikasi
   seperti *Network Cell Info*). Tanpa file itu, tes hanya memastikan fixture berhasil di-parse.
