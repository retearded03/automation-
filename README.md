# yt-autopilot: YouTube Shorts otomatis di GitHub Actions

Sistem ini membuat dan mengunggah **5 YouTube Shorts per hari** secara otomatis. Bahasa videonya Inggris, niche-nya fakta *how things work*, dan semuanya berjalan di GitHub Actions, jadi komputer kamu tidak perlu menyala.

```
plan (1 job)            produce (5 job paralel)                                   finalize (1 job)
trend_finder ─► topic_picker ─► script_writer ─► voice ─► visuals ─► assemble ─► uploader ─► notify
  (tren YT)      (5 topik +      (riset Wikipedia,   (edge-tts)  (Pexels/   (ffmpeg +    (videos.insert  (Telegram +
                  cadangan,       naskah, cek fakta,              Pixabay)   thumbnail)   + publishAt)    history.json)
                  gaya, jadwal)   quality gate)
```

Isi bagian:

- [Aturan konten](#aturan-konten-yang-dijaga-kode)
- [Struktur folder](#struktur-folder)
- [Biaya & kuota](#biaya--kuota)
- [Langkah 1: Google Cloud & YouTube Data API](#langkah-1-google-cloud--youtube-data-api)
- [Langkah 2: Status audit & video private](#langkah-2-status-audit--video-private)
- [Langkah 3: Cek kuota upload](#langkah-3-cek-kuota-upload-terkini)
- [Langkah 4: Kunci API lain](#langkah-4-kunci-api-lain)
- [Langkah 5: Musik latar](#langkah-5-musik-latar)
- [Langkah 6: GitHub Secrets & run pertama](#langkah-6-repo-github-secrets--run-pertama)
- [Menjalankan di komputer lokal](#menjalankan-di-komputer-lokal)
- [Konfigurasi penting](#konfigurasi-penting-configyaml)
- [Risiko & kebijakan](#risiko--kebijakan-yang-perlu-kamu-tahu)
- [Troubleshooting](#troubleshooting)

---

## Aturan konten yang dijaga kode

- **Tidak ada konten orang lain yang diunduh atau diunggah ulang.** Video trending hanya dibaca judul, tag, dan statistiknya sebagai *sinyal topik*. Naskah, suara, dan visual semuanya baru: footage stok berlisensi bebas dari Pexels/Pixabay, dengan atribusi otomatis di deskripsi.
- **Naskah orisinal dan dicek fakta.** Fakta hanya boleh berasal dari artikel Wikipedia yang diambil saat itu. Setiap klaim wajib mengutip kalimat sumbernya. Kode memastikan kutipan itu benar-benar ada di sumber, lalu LLM terpisah memeriksa apakah kutipan itu mendukung klaimnya. Segmen yang memuat klaim tidak terverifikasi dibuang. Sumber dicantumkan di deskripsi.
- **Quality gate.** Naskah diberi skor oleh LLM editor. Skor di bawah `script.min_quality_score` ditulis ulang, dan kalau tetap gagal diganti topik cadangan.
- **Variasi antar video.** Tiap video mendapat kombinasi acak dari jenis pembuka, struktur, suara, gaya subtitle, palet, layout thumbnail, dan musik. Kombinasi yang dipakai dicatat di `history.json` dan tidak diulang dalam 15 video terakhir. Klip footage tidak dipakai ulang selama 60 hari.
- **Topik aman.** Topik tentang tragedi, politik, agama, dan konten berhak cipta (musik, film, olahraga, game) disaring oleh aturan dan juga oleh LLM.

## Struktur folder

```
yt-autopilot/
├── .github/workflows/daily.yml   # cron harian + workflow_dispatch (plan → 5x produce → finalize)
├── .github/workflows/tests.yml   # pytest setiap push
├── pipeline.py                   # orkestrator: plan | produce | finalize | all, opsi --dry-run
├── get_refresh_token.py          # skrip sekali jalan untuk refresh token OAuth
├── config.yaml                   # niche, region, jadwal, format, privacy, gaya
├── history.json                  # riwayat video (di-commit otomatis tiap run)
├── prompts/                      # template prompt LLM (bisa diedit tanpa ubah kode)
├── assets/music/                 # musik bebas royalti milikmu + LICENSES.md
├── assets/fonts/                 # (opsional) font .ttf berlisensi bebas
├── ytauto/                       # satu modul per langkah + utilitas (config, retry, http, llm, history)
└── tests/                        # satu file tes per modul, semua API dipalsukan
```

## Biaya & kuota

| Komponen | Pemakaian per hari (5 video) | Batas gratis |
|---|---|---|
| YouTube `videos.list` (tren) | ±16 unit | 10.000 unit/hari |
| YouTube `videos.insert` | 5 panggilan | **bucket tersendiri: 100 upload/hari** |
| YouTube `thumbnails.set` | 250 unit | (masuk 10.000 unit) |
| Gemini API (default) | ±25 request | gratis (free tier); kadang overload (503), ditangani dengan retry + model cadangan |
| *(opsional)* Claude API | ±25 request | berbayar: perkiraan kasar ±US$0,03–0,05 per video dengan Sonnet 5.5 + Haiku 5.5 |
| Pexels / Pixabay | ±30–60 request | 200 req/jam / 100 req/menit |
| GitHub Actions | ±40–60 menit | repo publik: tanpa batas · repo privat: 2.000 menit/bulan |

Repo publik paling aman dari sisi menit Actions. Secret tetap tidak terlihat oleh orang lain walau repo-nya publik.

---

## Langkah 1: Google Cloud & YouTube Data API

1. **Buat project.** Buka <https://console.cloud.google.com/>, klik pemilih project di kiri atas → **New Project**, beri nama (mis. `yt-autopilot`) → **Create**.
2. **Aktifkan API.** Buka **APIs & Services → Library**, cari **YouTube Data API v3** → **Enable**.
3. **Buat API key** (untuk membaca tren):
   - **APIs & Services → Credentials → Create credentials → API key**.
   - Klik key yang baru dibuat. Di **API restrictions**, pilih *Restrict key* → centang hanya **YouTube Data API v3** → **Save**.
   - Simpan nilainya sebagai secret `YT_API_KEY`.
4. **Atur OAuth consent screen** (menu **Google Auth Platform**):
   - **Branding**: isi nama aplikasi (mis. `yt-autopilot`) dan email kamu.
   - **Audience**: User type **External**.
   - **Data Access → Add or remove scopes**: tambahkan `https://www.googleapis.com/auth/youtube.upload`.
5. **⚠️ Pindahkan ke Production.** Di **Audience**, klik **Publish app** → konfirmasi.
   - Kalau tetap di mode *Testing*, **refresh token kedaluwarsa setelah 7 hari** dan upload akan berhenti.
   - Di mode Production tanpa verifikasi, Google menampilkan layar *"Google hasn't verified this app"* saat kamu login. Untuk aplikasi pribadi ini wajar: klik **Advanced → Go to yt-autopilot (unsafe)**. Verifikasi aplikasi hanya diperlukan kalau dipakai oleh banyak orang.
6. **Buat OAuth client.** Di **Clients → Create client**, pilih Application type **Desktop app** → **Create**. Unduh file JSON-nya (mis. `client_secret.json`). File ini sudah otomatis diabaikan git, jadi tidak akan ter-commit.
7. **Dapatkan refresh token** dengan menjalankan perintah ini di komputermu sendiri:
   ```bash
   pip install google-auth-oauthlib
   python get_refresh_token.py --client-secret client_secret.json
   ```
   Browser akan terbuka. Login dengan akun **pemilik channel**, pilih channel yang benar, lalu izinkan. Salin token yang tercetak.
8. Simpan nilai-nilai ini untuk Langkah 6:
   - `client_id` dari JSON → `YT_CLIENT_ID`
   - `client_secret` → `YT_CLIENT_SECRET`
   - refresh token → `YT_REFRESH_TOKEN`
9. **Verifikasi channel** di <https://www.youtube.com/verify> dengan nomor HP. Ini wajib supaya thumbnail custom bisa dipasang. Kalau belum diverifikasi, video tetap terunggah, tetapi Telegram akan mengirim peringatan.

## Langkah 2: Status audit & video private

**Aturan lama.** Sejak 28 Juli 2020, video yang diunggah lewat API dari project yang **belum diaudit** otomatis **terkunci private**, dan tidak bisa dijadikan publik sampai project lolos *YouTube API Services Audit*.

**Aturan sekarang.** Menurut [Revision History YouTube Data API](https://developers.google.com/youtube/v3/revision_history), sejak **2 November 2026** kebijakan itu **dicabut**: upload dari project yang belum diaudit tidak lagi dibatasi ke private. Halaman referensi [`videos.insert`](https://developers.google.com/youtube/v3/docs/videos/insert) juga menyebut hal yang sama. Karena itu sistem ini bisa langsung menjadwalkan video publik (`privacy_status: scheduled`) tanpa audit. Keadaan ini dicek pada 17 November 2026.

> Kebijakan Google bisa berubah lagi. Kalau suatu hari video yang terunggah muncul dengan status
> **"Locked (private)"** di YouTube Studio, berarti pembatasan berlaku lagi dan kamu perlu audit.

**Kapan audit tetap diperlukan**

- Untuk meminta kuota lebih dari default (lebih dari 100 upload/hari atau lebih dari 10.000 unit), atau
- kalau pembatasan private diberlakukan kembali.

**Cara mengajukan audit**

1. Siapkan halaman kebijakan privasi sederhana dan deskripsi cara aplikasi memakai API. Untuk aplikasi ini: mengunggah video buatan sendiri ke channel sendiri.
2. Isi formulir **YouTube API Services – Audit and Quota Extension Form**: <https://support.google.com/youtube/contact/yt_api_form>.
3. Jelaskan bahwa aplikasi hanya dipakai untuk channel milikmu, mengunggah konten orisinal, dan mematuhi [YouTube API Services Terms](https://developers.google.com/youtube/terms/api-services-terms-of-service) serta [Developer Policies](https://developers.google.com/youtube/terms/developer-policies).
4. Tunggu email dari tim YouTube API Compliance. Prosesnya bisa beberapa minggu dan tidak ada jaminan disetujui. Balas pertanyaan mereka dengan lengkap.

## Langkah 3: Cek kuota upload terkini

1. Cloud Console → **APIs & Services → Enabled APIs & services → YouTube Data API v3**.
2. Buka tab **Quotas & System Limits**. Di sana ada beberapa baris:
   - **Queries per day**: kuota umum 10.000 unit (tren + thumbnail).
   - Baris untuk **video uploads** (`videos.insert`): bucket tersendiri, default 100 panggilan/hari.
3. Grafik **Metrics** menunjukkan pemakaian per hari. Kuota direset tengah malam waktu Pasifik (sekitar 14.00–15.00 WIB).
4. Angka resmi biaya per panggilan ada di <https://developers.google.com/youtube/v3/determine_quota_cost>.

## Langkah 4: Kunci API lain

| Secret | Cara mendapatkan |
|---|---|
| `LLM_API_KEY` | **Gemini (default, gratis):** <https://aistudio.google.com/apikey> → *Create API key*. Kalau kamu mengganti `llm.provider: claude` di config (berbayar), isi dengan key dari <https://console.anthropic.com/>. |
| `CF_ACCOUNT_ID` + `CF_API_TOKEN` | **Gambar AI bergaya 3D (sumber visual utama).** Daftar gratis di <https://dash.cloudflare.com/sign-up>. *Account ID* ada di halaman *Workers & Pages* (kolom kanan). Token: *My Profile → API Tokens → Create Token → template "Workers AI"* → *Create*. Kuota gratis harian, tanpa kartu kredit. Tanpa kedua secret ini, gambar dibuat lewat Pollinations (lebih lambat, kualitas lebih rendah). |
| `PIXABAY_API_KEY` | **Cadangan visual** (footage stok kalau gambar AI gagal). Daftar di <https://pixabay.com/accounts/register/>, lalu buka <https://pixabay.com/api/docs/>; key ada di bagian *Parameters*. |
| `CENTAI_API_KEY` / `GROQ_API_KEY` | *(opsional)* LLM cadangan kalau Gemini overload. |
| `PEXELS_API_KEY` | *(opsional)* <https://www.pexels.com/api/> → *Your API Key*. Pexels sempat menutup pendaftaran key baru; kalau kamu sudah punya key, Pexels dipakai lebih dulu karena punya lebih banyak video vertikal. |

Minimal **salah satu** dari `PIXABAY_API_KEY` / `PEXELS_API_KEY` harus diisi.
| `TELEGRAM_BOT_TOKEN` | Chat **@BotFather** di Telegram → `/newbot` → ikuti petunjuk → salin token. |
| `TELEGRAM_CHAT_ID` | Kirim pesan apa saja ke bot kamu, lalu buka `https://api.telegram.org/bot<TOKEN>/getUpdates` di browser dan salin angka `"chat":{"id": ...}`. |

## Langkah 5: Musik latar

Isi `assets/music/` dengan musik bebas royalti, misalnya dari **YouTube Studio → Audio Library**, lalu catat di `assets/music/LICENSES.md`. Volume musik otomatis turun saat narator bicara. Kalau folder ini kosong, video tetap dibuat tanpa musik.

## Langkah 6: Repo GitHub, Secrets & run pertama

1. Buat repo baru di GitHub (publik direkomendasikan karena menit Actions tidak dibatasi), lalu push folder ini:
   ```bash
   git init && git add . && git commit -m "yt-autopilot"
   git branch -M main
   git remote add origin https://github.com/<user>/<repo>.git
   git push -u origin main
   ```
2. Di repo, buka **Settings → Secrets and variables → Actions → New repository secret**, lalu isi satu per satu:
   `YT_API_KEY`, `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN`, `LLM_API_KEY`, `PIXABAY_API_KEY`, `PEXELS_API_KEY` (opsional), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
3. Buka **Settings → Actions → General → Workflow permissions** dan pastikan *Read and write permissions* diizinkan. Ini dibutuhkan untuk commit `history.json`.
4. **Run pertama (dry-run):**
   - Buka tab **Actions → Daily Shorts → Run workflow**.
   - Biarkan **dry_run** tercentang, isi **count** = `1`, lalu jalankan.
   - Tunggu sekitar 10 menit. Video, judul, dan statusnya akan dikirim ke Telegram, dan file-nya juga tersedia sebagai artifact `result-0` di halaman run.
5. **Run nyata pertama:** jalankan lagi dengan **dry_run** tidak dicentang dan **count** = `1`. Cek YouTube Studio: video seharusnya berstatus *Scheduled* di slot berikutnya.
6. Setelah itu biarkan cron berjalan. Setiap hari pukul 07.00 UTC (14.00 WIB), 5 video dibuat dan dijadwalkan tayang di 08.00, 12.00, 15.00, 18.00, dan 21.00 waktu New York.

## Menjalankan di komputer lokal

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (Linux/Mac: source .venv/bin/activate)
pip install -r requirements-dev.txt
python -m pytest -q               # semua tes, tanpa internet
```

Untuk dry-run penuh di lokal, isi environment variable yang sama dengan secret (cukup `YT_API_KEY`, `LLM_API_KEY`, `PIXABAY_API_KEY`), lalu jalankan:

```bash
python pipeline.py all --dry-run --count 1
```

Hasilnya ada di `work/<run-id>/video_0/` (`final.mp4`, `thumbnail.jpg`, `script.json`, `run.log`). Di lokal, ffmpeg otomatis diambil dari paket `imageio-ffmpeg` kalau ffmpeg belum terpasang.

## Konfigurasi penting (`config.yaml`)

| Kunci | Fungsi |
|---|---|
| `schedule.videos_per_day` | Jumlah video per hari (default 5). |
| `schedule.publish_slots` + `timezone` | Jam tayang di zona waktu penonton. Otomatis menyesuaikan jam musim panas AS. Setelah channel punya data, sesuaikan dengan YouTube Analytics → *When your viewers are on YouTube*. |
| `upload.privacy_status` | `scheduled` (private + publishAt, default), `private`, `unlisted`, atau `public`. |
| `upload.contains_synthetic_media` | `false`: footage asli + narator TTS generik yang tidak meniru orang nyata. Ubah ke `true` kalau menambahkan visual atau suara realistis buatan AI. |
| `visuals.mode` / `ai_style` | `ai_image` (default): satu ilustrasi AI bergaya animasi 3D per adegan, ditulis LLM sesuai isi narasi, lalu diberi efek zoom/pan. `stock`: footage Pixabay/Pexels saja. Ubah `ai_style` untuk mengganti gaya gambar. |
| `trends.regions` / `category_ids` | Sumber sinyal tren (US, GB, CA, AU; kategori Sains & Teknologi, Pendidikan, Otomotif). |
| `llm.provider` | `claude` (default) atau `gemini` (gratis). `LLM_API_KEY` harus sesuai provider. |
| `llm.claude_steps` | Model & effort **per langkah** (`topic_picker`, `script_writer`, `fact_check`, `quality_gate`). Default: Sonnet 5.5 untuk topik & naskah, Haiku 5.5 untuk cek fakta & penilaian. Naikkan `script_writer` ke `claude-opus-5-5` kalau ingin naskah lebih tajam (biaya naik ±3x). |
| `script.min_quality_score` | Ambang quality gate (1–10). |
| `styles.*` | Kumpulan variasi: jenis pembuka, struktur, gaya subtitle, palet, layout thumbnail. |

## Risiko & kebijakan yang perlu kamu tahu

- **Kebijakan "inauthentic content" YouTube.** Konten produksi massal yang berulang bisa tidak dimonetisasi atau terkena sanksi. Sistem ini sudah memakai variasi, cek fakta, dan quality gate, tetapi tetap pantau performa dan komentar di YouTube Studio. Kalau perlu, turunkan `videos_per_day`.
- **edge-tts** memakai layanan text-to-speech Microsoft Edge yang tidak resmi. Layanan ini bisa berhenti atau berubah sewaktu-waktu. Kalau terjadi, langkah `voice` gagal dan Telegram memberi tahu.
- **Cron GitHub sering terlambat** 5–30 menit, bahkan lebih saat sibuk. Karena itu render dimulai jauh sebelum slot tayang pertama. Kalau slot sudah lewat saat upload, video langsung dipublikasikan dan kamu mendapat peringatan.
- **Repo publik yang tidak ada aktivitas selama 60 hari** membuat workflow terjadwal dinonaktifkan GitHub. Commit `history.json` tiap hari menjaga repo tetap aktif. Kalau workflow sempat nonaktif, aktifkan lagi di tab Actions.
- **Footage stok** dipilih otomatis berdasarkan kata kunci, jadi sesekali bisa kurang pas dengan narasi. Kalau ingin meninjau dulu sebelum tayang, gunakan `privacy_status: private`.

## Troubleshooting

| Gejala | Penyebab & solusi |
|---|---|
| `invalid_grant` saat upload | Refresh token kedaluwarsa atau dicabut. Pastikan consent screen sudah **Production**, lalu jalankan ulang `get_refresh_token.py`. |
| `quotaExceeded` | Kuota harian habis. Cek Langkah 3 dan tunggu reset. |
| Thumbnail gagal (403) | Channel belum verifikasi nomor HP (Langkah 1.9). |
| `API key Claude ditolak` | `LLM_API_KEY` bukan key Claude (`sk-ant-...`) atau saldo di console.anthropic.com habis. |
| `503`/`429` dari Gemini | Server overload / rate limit free tier. Program otomatis pindah ke model cadangan; kalau tetap gagal, turunkan `max-parallel` di `daily.yml`. |
| `tidak ada footage` | Kata kunci terlalu spesifik. Tambahkan kata kunci umum di `channel.fallback_visual_keywords`. |
| `hanya N topik layak` | Tren hari itu kurang relevan dan LLM gagal memberi cukup topik. Run berikutnya biasanya normal. |
| Gagal push `history.json` | Izinkan *Read and write permissions* (Langkah 6.3). |
