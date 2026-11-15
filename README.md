# CekSinyal

Pemantau sinyal seluler untuk tugas besar ET4132 Kapita Selekta Teknik Telekomunikasi (ITB, Kelompok 2).
HP Android disambungkan ke laptop lewat USB; laptop membaca kualitas sinyal lewat ADB dan menyimpannya ke SQLite.

Status: **collector** sudah jalan (`devices`, `probe`, `collect`). Speedtest, diagnosis, uploader, bot, dan laporan belum.

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
| `python -m ceksinyal fix-sinr [--db <path>] [--serial <serial>] [--dry-run]` | Koreksi SINR LTE lama yang tercatat 10x lipat (HP Android 10, lihat di bawah). Selalu coba `--dry-run` dulu. |

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
| `collector.interval_seconds` | `10` | Jeda antar-pembacaan. |
| `collector.max_devices` | `2` | Jumlah HP maksimal yang dibaca. |
| `collector.adb_timeout_seconds` | `15` | Batas waktu satu perintah adb. |
| `collector.raw_dump_interval_minutes` | `60` | Maksimal satu file output mentah per HP per periode ini. |
| `devices[].serial` | – | Serial ADB HP. |
| `devices[].operator` | – | Nama operator. Kalau kosong: `getprop gsm.operator.alpha`, lalu nama dari dumpsys. |
| `devices[].phone_id` | otomatis | Slot SIM untuk HP dual-SIM (0/1). Kalau kosong, dipilih slot yang punya sinyal dan koneksi data aktif. |
| `devices[].sinr_unit` | `auto` | Satuan rssnr LTE di dumpsys HP ini: `auto` (dari versi Android), `db`, atau `deci_db` (0,1 dB). Lihat "Satuan SINR LTE". |

Kalau `devices` kosong, collector memakai HP apa saja yang terhubung (maks. `max_devices`).

## Cara kerja collector

- Tiap putaran: `adb devices -l`, lalu `adb -s <serial> shell dumpsys telephony.registry` untuk tiap HP
  (paralel, jadi HP yang lambat tidak menunda HP lain).
- Parser (`ceksinyal/collector/parser.py`) mengambil RSRP/RSRQ/SINR/RSSI dari `mSignalStrength` dan
  PCI/Cell ID/EARFCN/band dari identitas sel yang melayani (`mCellIdentity`, `CellInfo` dengan
  `mRegistered=YES`, atau `NetworkRegistrationInfo`). Mendukung format Android 9 (kolom posisi,
  SINR dalam 0,1 dB) sampai 14, LTE, NR SA, dan NR NSA. Nilai yang tidak ada, `2147483647`,
  disamarkan (`*****`), atau di luar rentang fisik menjadi kosong (NULL).
- **RAT**: tidak dalam layanan → `NONE` dengan nilai sinyal kosong (dihitung "sinyal hilang").
  NR NSA (5G non-standalone) disimpan sebagai `LTE` dengan nilai anchor LTE; nilai NR hanya tampil di `probe`.
  Band LTE dihitung dari EARFCN kalau HP tidak menampilkan `mBands`.
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

## TODO

- Xiaomi Android 10 (Redmi 9C): `pci` dan `cell_id` kosong di semua sampel, dan `earfcn`/`band` hanya terisi
  ±21% sampel. Perlu dicari sumber lain (mis. `dumpsys phone`,
  `mCellInfo` pada versi MIUI lain) sebelum aturan handover bisa diandalkan di HP ini.

## Tes

```
python -m pytest
```

Fixture ada di `tests/fixtures/`. Sebagian besar sintetis (`synthetic_*.txt`); fixture asli pertama adalah
`real_redmi9c_android10.txt` (Xiaomi Redmi 9C, MIUI, Android 10, MediaTek, Telkomsel). Untuk menambah fixture asli:

1. Jalankan `python -m ceksinyal probe` dengan HP tersambung, lalu ambil file `data/raw/probe_*.txt`.
2. Salin ke `tests/fixtures/real_<merek>_<android>_<operator>.txt`. Hapus dulu data pribadi
   (nomor telepon di `mCallIncomingNumber`, IMEI/IMSI/ICCID, dsb.) kalau ada. Nama aplikasi pihak ketiga
   di `listen logs`/`registrations` juga sebaiknya diganti (mis. `com.example.app1`).
3. Opsional tapi disarankan: buat `real_....expected.json` berisi nilai yang benar (cek di aplikasi
   seperti *Network Cell Info*). Tanpa file itu, tes hanya memastikan fixture berhasil di-parse.
