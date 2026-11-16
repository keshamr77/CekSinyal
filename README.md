# CekSinyal

Pemantau sinyal seluler untuk tugas besar ET4132 Kapita Selekta Teknik Telekomunikasi (ITB, Kelompok 2).
HP Android disambungkan ke laptop lewat USB; laptop membaca kualitas sinyal lewat ADB dan menyimpannya ke SQLite.

Status: **collector** (`devices`, `probe`, `collect`), **diagnosis** (`diagnose`), dan **uji kecepatan** (`interfaces`, `speedtest`) sudah jalan. Uploader, bot, dan laporan belum.

## Persiapan

1. Python 3.10+.
2. Android platform-tools (`adb`). Kalau tidak ada di PATH, CekSinyal juga mencari di lokasi SDK
   bawaan (`ANDROID_HOME`, `ANDROID_SDK_ROOT`, `%LOCALAPPDATA%\Android\Sdk`, `~/Android/Sdk`,
   `~/Library/Android/sdk`), atau isi `adb_path` di config.
3. Di HP: aktifkan *Opsi pengembang* lalu *USB debugging*, sambungkan USB, dan izinkan laptop saat diminta.
4. Pasang dependensi:

   ```
   python -m venv .venv
   .venv\Scripts\activate          # Windows
   source .venv/bin/activate       # macOS / Linux
   pip install -r requirements.txt
   pip install -r requirements-dev.txt   # untuk menjalankan tes
   ```

5. Salin `config.example.yaml` menjadi `config.yaml`, lalu isi `kos` dan daftar `devices`
   (serial dilihat dengan `python -m ceksinyal devices`). Salin `.env.example` menjadi `.env`
   untuk fitur yang butuh rahasia nanti. Kedua file itu tidak di-commit.

## Perintah

| Perintah | Fungsi |
|---|---|
| `python -m ceksinyal devices` | Daftar HP terhubung: serial, status ADB, model, operator (dari config dan dari HP). |
| `python -m ceksinyal probe` | Baca sekali dari tiap HP, tampilkan hasil parsing, simpan output mentah ke `data/raw/probe_<serial>_<waktu>.txt`. |
| `python -m ceksinyal probe --file <path>` | Parse file output dumpsys tanpa HP (untuk mengecek fixture). |
| `python -m ceksinyal collect` | Collector: baca 1–2 HP tiap 10 detik dan simpan ke tabel `measurements`. Hentikan dengan Ctrl+C. |
| `python -m ceksinyal interfaces` | Daftar interface jaringan laptop dan IPv4-nya, untuk mengisi `devices[].source_ip`. |
| `python -m ceksinyal speedtest [--full] [--no-save]` | Uji kecepatan sekali sekarang untuk tiap HP yang punya `source_ip` (ringan; `--full` = speedtest-cli). |
| `python -m ceksinyal diagnose [--rerun] [--db <path>] [--kos <nama>]` | Diagnosis per jendela 10 menit untuk data yang belum didiagnosis; `--rerun` menghapus hasil lama dan menghitung ulang semuanya (setelah ambang diubah). |
| `python -m ceksinyal fix-sinr [--db <path>] [--serial <serial>] [--dry-run]` | Koreksi SINR LTE lama yang tercatat 10x lipat (HP Android 10, lihat di bawah). Selalu coba `--dry-run` dulu. |
| `python -m ceksinyal fix-band [--db <path>] [--dry-run]` | Koreksi band LTE lama yang tidak cocok dengan EARFCN (lihat "Identitas sel"). Selalu coba `--dry-run` dulu. |

Opsi umum: `--config <path>` (bawaan `./config.yaml`, atau variabel `CEKSINYAL_CONFIG`) dan `-v` (log debug).
Opsi `probe`: `--serial`, `--phone-id`. Opsi `collect`: `--interval <detik>`, `--iterations <n>`.
Opsi `fix-sinr`: `--db` (bawaan: `database` di config), `--serial`, `--dry-run`.

Kode keluar `probe`: 0 = semua berhasil, 1 = HP/ADB bermasalah, 2 = ada yang parsing-nya gagal.

## Konfigurasi (`config.yaml`)

| Kunci | Bawaan | Arti |
|---|---|---|
| `kos` | `belum-diatur` | Nama lokasi, ikut tersimpan di setiap baris. |
| `database` | `data/ceksinyal.db` | File SQLite (relatif terhadap folder config). |
| `raw_dir` | `data/raw` | Folder output mentah. |
| `adb_path` | `adb` | Path ke adb. Kalau diisi selain `adb`, path harus benar (tidak ada pencarian cadangan). |
| `timezone_offset_hours` | `7` | Zona waktu tampilan/laporan (7 = WIB). Data di database selalu UTC. |
| `diagnosis.*` | lihat "Diagnosis" | Ambang aturan diagnosis. |
| `collector.interval_seconds` | `10` | Jeda antar-pembacaan. |
| `collector.max_devices` | `2` | Jumlah HP maksimal yang dibaca. |
| `collector.adb_timeout_seconds` | `15` | Batas waktu satu perintah adb. |
| `collector.raw_dump_interval_minutes` | `60` | Maksimal satu file output mentah per HP per periode ini. |
| `devices[].serial` | – | Serial ADB HP. |
| `devices[].operator` | – | Nama operator. Kalau kosong: `getprop gsm.operator.alpha`, lalu nama dari dumpsys. |
| `devices[].phone_id` | otomatis | Slot SIM untuk HP dual-SIM (0/1). Kalau kosong, dipilih slot yang punya sinyal dan koneksi data aktif. |
| `devices[].source_ip` | – | IP laptop di interface USB tethering HP ini. Wajib untuk uji kecepatan. |
| `speedtest.*` | lihat "Uji kecepatan" | Jadwal dan target uji kecepatan. |
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
  Diagnosis nanti sebaiknya hanya memakai baris `raw_ok=1`.
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

## TODO

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
