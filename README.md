# Jernihkan & Cari PDF

Aplikasi lokal sederhana untuk membantu OCR membaca scan PDF, memperbaiki orientasi halaman bila perlu, lalu menghasilkan PDF dengan lapisan teks yang bisa dicari dan DOCX yang dapat dibuka di Google Docs.

## Yang dibutuhkan

- Python 3.10 atau lebih baru
- Tesseract OCR
- Data bahasa Tesseract sesuai dokumen: `ind` (Indonesia), `eng` (Inggris), `kor` (Korea), dan/atau `chi_sim` (Mandarin sederhana/China)

## Biaya dan pemrosesan

Aplikasi menggunakan Tesseract secara lokal sebagai OCR utama. Secara opsional, atur `GEMINI_API_KEY` di file `.env`, misalnya `GEMINI_API_KEY=isi_key_di_sini`, lalu mulai ulang aplikasi. Jika keyword tidak ditemukan, klik **Scan lebih lanjut dengan AI** di bawah hasil pencarian untuk mengirim halaman yang belum dipindai AI ke Gemini, lalu mencari keyword itu kembali. Penggunaan API dapat dikenai kuota atau biaya sesuai akun. Tanpa API key, seluruh pemrosesan OCR tetap lokal. PDF searchable mempertahankan gambar scan sumber dan lapisan teks Tesseract; teks hasil Gemini digunakan untuk DOCX, tampilan teks, dan pencarian.

## Instalasi di Windows

1. Pasang Python 3 dari [python.org](https://www.python.org/downloads/).
2. Pasang Tesseract OCR. Saat instalasi, sertakan language data yang diperlukan: Indonesia (`ind`), Inggris (`eng`), Korea (`kor`), atau Mandarin sederhana (`chi_sim`). Nama yang tampil di daftar installer bisa berbeda; pastikan berkas `.traineddata` tersedia di folder `tessdata` Tesseract.
3. Buka PowerShell di folder proyek ini, lalu jalankan:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   py -m pip install -r requirements.txt
   py -m streamlit run app.py
   ```

4. Aplikasi akan terbuka di browser lokal. Pilih satu atau beberapa PDF, atur bahasa dan kualitas, lalu klik **Proses scan dan periksa keterbacaan**.
5. Periksa status OCR per halaman. Target awal 300 DPI dan orientasi 0°, 90°, 180°, serta 270° dibandingkan untuk tiap halaman. Jika satu gambar scan menutupi hampir seluruh halaman PDF, aplikasi membaca gambar tertanam secara langsung, membatasi pembesaran hingga 1,25×, dan memangkas tepi scan 1,5% untuk mengurangi gangguan. Halaman dengan skor keyakinan di bawah 80% ditandai **Perlu dicek**.
6. Masukkan kata atau frasa. Pencarian menghitung jumlah kemunculan per file dan halaman. Untuk halaman berkeyakinan rendah, aplikasi juga membaca ulang baris di sekitar kata yang terdeteksi agar hitungan di tabel lebih lengkap. Pencarian menoleransi hingga dua karakter salah baca untuk kata minimal lima huruf; kecocokan mirip OCR diberi label agar diperiksa pada scan asli. Unduh halaman yang cocok sebagai satu ZIP berisi PDF per dokumen.

Jika perintah `tesseract` tidak ditemukan, pasang ulang Tesseract di lokasi standar `C:\Program Files\Tesseract-OCR` atau tambahkan folder instalasinya ke `PATH`.

### Menggunakan model OCR yang lebih akurat

Model bawaan instalasi Tesseract dapat ditukar dengan model LSTM `tessdata_best`, yang mengutamakan akurasi dengan waktu proses lebih lama. Unduh hanya bahasa yang dipakai (misalnya `eng.traineddata` dan `ind.traineddata`) dari [repositori resmi tessdata_best](https://github.com/tesseract-ocr/tessdata_best), tutup aplikasi, cadangkan berkas lama, lalu salin berkas baru ke folder `C:\Program Files\Tesseract-OCR\tessdata`. Tesseract 4 atau yang lebih baru diperlukan untuk model LSTM tersebut. Jalankan ulang aplikasi setelah mengganti model. Skor keyakinan Tesseract bukan jaminan persentase akurasi; target 80% harus dinilai dengan membandingkan teks OCR dengan scan asli.

### Menambah bahasa Korea atau Mandarin setelah instalasi

Pilihan bahasa di aplikasi memerlukan data OCR Tesseract di komputer yang menjalankan aplikasi. Jika muncul pesan bahwa data `kor` atau `chi_sim` belum tersedia, unduh berkas `kor.traineddata` dan/atau `chi_sim.traineddata` dari [repositori tessdata resmi Tesseract](https://github.com/tesseract-ocr/tessdata), lalu salin ke folder `C:\Program Files\Tesseract-OCR\tessdata`. Tutup dan jalankan ulang aplikasi. Untuk scan yang berisi campuran Inggris, pilih opsi Korea + Inggris atau Mandarin + Inggris.

## Menggunakan DOCX di Google Docs

Unduh berkas DOCX dari aplikasi, unggah ke Google Drive, lalu buka berkas tersebut dengan Google Docs. DOCX berisi teks hasil OCR per halaman; PDF searchable mempertahankan tampilan scan yang sudah diproses.

## Catatan

- File diproses secara lokal oleh aplikasi ini; tidak ada unggahan otomatis ke layanan eksternal.
- OCR bisa keliru pada scan yang sangat buram, miring, tulisan tangan, atau halaman dengan tata letak kompleks. Periksa teks sebelum digunakan.
- Filter kontras dan penajaman membantu OCR, tetapi tidak dapat mengembalikan detail yang tidak tertangkap saat pemindaian.
