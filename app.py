from __future__ import annotations

import io
import hashlib
import base64
import json
import os
import re
import shutil
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from statistics import mean
from dataclasses import dataclass

import pymupdf as fitz
import pytesseract
import streamlit as st
from dotenv import load_dotenv
from PIL import Image, ImageEnhance, ImageOps
from docx import Document
from docx.shared import Inches, Pt


APP_TITLE = "Tools Menjernihkan Scanner"
MAX_UPLOAD_MB = 120
MAX_UPLOAD_FILES = 10
load_dotenv(Path(__file__).with_name(".env"), override=True)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()



@dataclass
class PageResult:
    text: str
    words: list[dict]
    pixel_width: int
    pixel_height: int
    rotation: int
    confidence: float
    ai_used: bool = False


def configure_tesseract() -> None:
    """Use the Tesseract executable on PATH, or common Windows install paths."""
    if shutil.which("tesseract"):
        return
    candidates = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            pytesseract.pytesseract.tesseract_cmd = candidate
            return


def preprocess(image: Image.Image, contrast: float, sharpness: float) -> Image.Image:
    """Apply gentle cleanup without amplifying scan grain and paper texture."""
    image = ImageOps.grayscale(image)
    image = ImageOps.autocontrast(image, cutoff=1)
    image = ImageEnhance.Contrast(image).enhance(contrast)
    image = ImageEnhance.Sharpness(image).enhance(sharpness)
    return image


def extract_full_page_scan(page: fitz.Page, target_dpi: int) -> tuple[Image.Image, float] | None:
    """Use the embedded page scan directly to avoid resampling it through PDF geometry."""
    if page.rotation:
        return None
    page_area = page.rect.get_area()
    if page_area <= 0:
        return None
    try:
        images = page.get_image_info(xrefs=True)
    except (AttributeError, RuntimeError):
        return None
    candidates = []
    for image in images:
        bbox = fitz.Rect(image["bbox"])
        if bbox.width <= 0 or bbox.height <= 0 or image["xref"] <= 0:
            continue
        visible_area = (bbox & page.rect).get_area()
        if visible_area / page_area < 0.95:
            continue
        candidates.append((image["width"] * image["height"], image, bbox))
    if not candidates:
        return None
    _, image_info, bbox = max(candidates, key=lambda item: item[0])
    try:
        extracted = page.parent.extract_image(image_info["xref"])
        source = Image.open(io.BytesIO(extracted["image"]))
        source = ImageOps.exif_transpose(source).convert("RGB")
    except (KeyError, OSError, ValueError):
        return None

    source_pixels_per_point = min(
        image_info["width"] / bbox.width,
        image_info["height"] / bbox.height,
    )
    target_pixels_per_point = target_dpi / 72
    scale = min(target_pixels_per_point / source_pixels_per_point, 1.25)
    if abs(scale - 1.0) > 0.01:
        source = source.resize(
            (max(1, round(source.width * scale)), max(1, round(source.height * scale))),
            Image.Resampling.LANCZOS,
        )
    return source, scale


def _ocr_data(image: Image.Image, language: str, psm: int = 3) -> dict:
    return pytesseract.image_to_data(
        image,
        lang=language,
        config=f"--oem 3 --psm {psm}",
        output_type=pytesseract.Output.DICT,
    )


def _ocr_confidence(image: Image.Image, language: str) -> float:
    data = _ocr_data(image, language)
    confidence = []
    for text, value in zip(data["text"], data["conf"]):
        if not text.strip():
            continue
        try:
            score = float(value)
        except (TypeError, ValueError):
            continue
        if score >= 0:
            confidence.append(score)
    return mean(confidence) if confidence else 0.0


def detect_rotation(image: Image.Image, language: str) -> int:
    """Compare all quarter-turn orientations; scan PDFs can mix page rotations."""
    probe = image.copy()
    probe.thumbnail((1500, 1500), Image.Resampling.LANCZOS)
    scores = [(angle, _ocr_confidence(probe.rotate(angle, expand=True) if angle else probe, language))
              for angle in (0, 90, 180, 270)]
    best_angle, best_score = max(scores, key=lambda item: item[1])
    baseline_score = next(score for angle, score in scores if angle == 0)
    if best_angle and best_score < baseline_score + 5:
        return 0
    return best_angle


def recognize_page(
    page: fitz.Page,
    language: str,
    dpi: int,
    contrast: float,
    sharpness: float,
) -> PageResult:
    extracted = extract_full_page_scan(page, dpi)
    if extracted:
        source, _ = extracted
        full_width, full_height = source.size
        crop_x = round(full_width * 0.015)
        crop_y = round(full_height * 0.015)
        crop_right = full_width - crop_x
        crop_bottom = full_height - crop_y
        source_for_ocr = source
    else:
        pixmap = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False)
        source_for_ocr = Image.open(io.BytesIO(pixmap.tobytes("png")))
        full_width, full_height = source_for_ocr.size
        crop_x = crop_y = 0
        crop_right = full_width
        crop_bottom = full_height

    cleaned = preprocess(source_for_ocr, contrast, sharpness)
    if extracted:
        # Compute auto-contrast from the complete scan before trimming its noisy border.
        cleaned = cleaned.crop((crop_x, crop_y, crop_right, crop_bottom))
    rotation = detect_rotation(cleaned, language)
    if rotation:
        cleaned = cleaned.rotate(rotation, expand=True)

    data = _ocr_data(cleaned, language)
    words: list[dict] = []
    confidences: list[float] = []
    for index, value in enumerate(data["text"]):
        value = value.strip()
        try:
            confidence = float(data["conf"][index])
        except (TypeError, ValueError):
            confidence = -1
        if not value or confidence < 0:
            continue
        confidences.append(confidence)
        words.append(
            {
                "text": value,
                "left": int(data["left"][index]),
                "top": int(data["top"][index]),
                "width": int(data["width"][index]),
                "height": int(data["height"][index]),
                "block": int(data["block_num"][index]),
                "paragraph": int(data["par_num"][index]),
                "line": int(data["line_num"][index]),
                "confidence": confidence,
            }
        )

    if rotation == 0:
        pad_x, pad_y = crop_x, crop_y
        pixel_width, pixel_height = full_width, full_height
    elif rotation == 90:
        pad_x, pad_y = crop_y, full_width - crop_right
        pixel_width, pixel_height = full_height, full_width
    elif rotation == 180:
        pad_x, pad_y = full_width - crop_right, full_height - crop_bottom
        pixel_width, pixel_height = full_width, full_height
    else:
        pad_x, pad_y = full_height - crop_bottom, crop_x
        pixel_width, pixel_height = full_height, full_width
    for word in words:
        word["left"] += pad_x
        word["top"] += pad_y

    return PageResult(
        text="\n".join(
            line.strip()
            for line in pytesseract.image_to_string(
                cleaned, lang=language, config="--oem 3 --psm 3"
            ).splitlines()
            if line.strip()
        ),
        words=words,
        pixel_width=pixel_width,
        pixel_height=pixel_height,
        rotation=rotation,
        confidence=mean(confidences) if confidences else 0.0,
    )


def recognize_with_gemini(result: PageResult, image: Image.Image, api_key: str, language: str) -> None:
    """Replace low-confidence OCR text with Gemini's transcription of the page image."""
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=90)
    payload = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(buffer.getvalue()).decode("ascii")}},
            {"text": f"Transcribe all visible printed text from this scanned document page. Preserve the original language ({language}) and reading order. Keep line breaks where useful. Do not translate, summarize, infer missing text, or add commentary. Return only the transcription."},
        ]}],
        "generationConfig": {"temperature": 0},
    }
    request = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            error_data = json.loads(exc.read().decode("utf-8"))
            message = error_data.get("error", {}).get("message", str(exc))
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = str(exc)
        raise RuntimeError(f"Gemini API {exc.code}: {message}") from exc
    text = "\n".join(
        part.get("text", "") for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    ).strip()
    if text:
        result.text = text
        result.ai_used = True
    else:
        raise RuntimeError("Gemini tidak mengembalikan teks untuk halaman ini.")


def render_page_image(page: fitz.Page, dpi: int, rotation: int) -> Image.Image:
    pixmap = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False)
    image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")
    return image.rotate(rotation, expand=True) if rotation else image


def find_unicode_font() -> str | None:
    candidates = [
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial.ttf",
    ]
    return next((path for path in candidates if os.path.isfile(path)), None)


def make_searchable_pdf(source_pdf: bytes, results: list[PageResult]) -> bytes:
    # Keep the source scan untouched. OCR cleanup is only used for recognition so
    # the searchable PDF does not become blurrier from rasterizing/recompressing it.
    output = fitz.open(stream=source_pdf, filetype="pdf")
    fontfile = find_unicode_font()
    for page, result in zip(output, results):
        # PIL angles are counter-clockwise; PDF page rotations are clockwise.
        page.set_rotation((page.rotation - result.rotation) % 360)
        scale_x = page.rect.width / result.pixel_width
        scale_y = page.rect.height / result.pixel_height
        for word in result.words:
            left = word["left"] * scale_x
            top = word["top"] * scale_y
            width = word["width"] * scale_x
            height = word["height"] * scale_y
            baseline = fitz.Point(left, top + height * 0.82) * page.derotation_matrix
            bounds = fitz.Rect(left, top, left + width, top + height) * page.derotation_matrix
            options = {
                "fontsize": max(4, min(bounds.width, bounds.height) * 0.78),
                "render_mode": 3,
                "overlay": True,
            }
            if fontfile:
                options["fontfile"] = fontfile
            page.insert_text(
                baseline,
                word["text"],
                **options,
            )
    result = output.tobytes(deflate=True, garbage=4)
    output.close()
    return result


def make_docx(results: list[PageResult]) -> bytes:
    document = Document()
    section = document.sections[0]
    section.top_margin = Inches(0.65)
    section.bottom_margin = Inches(0.65)
    normal = document.styles["Normal"]
    normal.font.name = "Arial"
    normal.font.size = Pt(10)

    for index, result in enumerate(results):
        if index:
            document.add_page_break()
        document.add_paragraph(f"Halaman {index + 1}", style="Heading 2")
        text = result.text.strip()
        document.add_paragraph(text if text else "[Tidak ada teks yang berhasil dikenali pada halaman ini]")

    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def make_matching_pages_pdf(searchable_pdf: bytes, page_indexes: list[int]) -> bytes:
    source = fitz.open(stream=searchable_pdf, filetype="pdf")
    selected = fitz.open()
    for page_index in page_indexes:
        selected.insert_pdf(source, from_page=page_index, to_page=page_index)
    result = selected.tobytes(deflate=True, garbage=4)
    selected.close()
    source.close()
    return result


def _edit_distance(left: str, right: str, limit: int) -> int:
    """Return Levenshtein distance, stopping once it exceeds the small OCR tolerance."""
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        row_min = row
        for column, right_char in enumerate(right, start=1):
            value = min(
                current[column - 1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left_char != right_char),
            )
            current.append(value)
            row_min = min(row_min, value)
        if row_min > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _ocr_token_distance(query: str, candidate: str, limit: int) -> int:
    distance = _edit_distance(query, candidate, limit)
    # Tesseract sometimes merges a word with a number/unit, e.g. "Absent10gm".
    if len(candidate) > len(query) and any(char.isdigit() for char in candidate):
        for split_at in range(max(1, len(query) - limit), min(len(candidate), len(query) + limit) + 1):
            if any(char.isdigit() for char in candidate[split_at:]):
                distance = min(distance, _edit_distance(query, candidate[:split_at], limit))
    return distance


def find_ocr_keyword_occurrences(text: str, query: str) -> list[tuple[int, bool, int]]:
    """Return each non-overlapping exact or OCR-tolerant occurrence in a text page."""
    folded = text.casefold()
    query_folded = query.casefold().strip()
    query_tokens = re.findall(r"[^\W_]+", query_folded, flags=re.UNICODE)
    text_tokens = list(re.finditer(r"[^\W_]+", folded, flags=re.UNICODE))
    if not query_tokens or not text_tokens:
        return []

    def distance_limit(token: str) -> int:
        if len(token) < 5 or not token.isalpha():
            return 0
        return min(2, max(1, len(token) // 5))

    occurrences = []
    start = 0
    while start < len(text_tokens):
        cursor = start
        fuzzy = False
        for query_token in query_tokens:
            limit = distance_limit(query_token)
            candidates = []
            for token_index in range(cursor, min(cursor + 3, len(text_tokens))):
                word = text_tokens[token_index].group()
                distance = _ocr_token_distance(query_token, word, limit)
                if distance <= limit:
                    candidates.append((distance, token_index))
            if not candidates:
                break
            # Prefer the earliest plausible token so a nearby exact occurrence
            # does not cause preceding fuzzy occurrences to be skipped.
            cursor, distance = min((token_index, distance) for distance, token_index in candidates)
            fuzzy |= distance > 0
            cursor += 1
        else:
            occurrences.append(
                (text_tokens[start].start(), fuzzy, text_tokens[cursor - 1].end())
            )
            start = cursor
            continue
        start += 1
    return occurrences


def table_aware_keyword_count(
    page: fitz.Page,
    result: PageResult,
    query: str,
    language: str,
    dpi: int,
    contrast: float,
    sharpness: float,
    primary_count: int,
) -> tuple[int, int]:
    """Recheck low-confidence table rows with focused OCR to catch missed repeats."""
    if result.confidence >= 80 or not primary_count or len(re.findall(r"[^\W_]+", query)) != 1:
        return primary_count, 0
    extracted = extract_full_page_scan(page, dpi)
    if not extracted:
        return primary_count, 0
    source, _ = extracted
    image = preprocess(source, contrast, sharpness)
    crop_x = round(image.width * 0.015)
    crop_y = round(image.height * 0.015)
    image = image.crop((crop_x, crop_y, image.width - crop_x, image.height - crop_y))

    data = _ocr_data(image, language, psm=11)
    centers = []
    for index, token in enumerate(data["text"]):
        if not token.strip() or not find_ocr_keyword_occurrences(token, query):
            continue
        centers.append(int(data["top"][index]) + int(data["height"][index]) // 2)
    if result.rotation == 0:
        for word in result.words:
            if find_ocr_keyword_occurrences(word["text"], query):
                centers.append(word["top"] + word["height"] // 2 - crop_y)
    if not centers:
        return primary_count, 0

    centers.sort()
    merge_gap = max(12, round(image.height * 0.006))
    rows: list[list[int]] = []
    for center in centers:
        if rows and center - rows[-1][-1] <= merge_gap:
            rows[-1].append(center)
        else:
            rows.append([center])

    row_total = 0
    row_fuzzy_total = 0
    half_height = max(24, round(image.height * 0.009))
    for row in rows:
        center = round(sum(row) / len(row))
        strip = image.crop((0, max(0, center - half_height), image.width, min(image.height, center + half_height)))
        candidates = []
        for psm in (6, 11):
            row_text = pytesseract.image_to_string(
                strip, lang=language, config=f"--oem 3 --psm {psm}"
            )
            row_occurrences = find_ocr_keyword_occurrences(row_text, query)
            candidates.append((len(row_occurrences), sum(fuzzy for _, fuzzy, _ in row_occurrences)))
        row_count, fuzzy_count = max(candidates, key=lambda item: item[0])
        row_total += row_count
        row_fuzzy_total += fuzzy_count
    if row_total > primary_count:
        return row_total, row_fuzzy_total
    return primary_count, 0


def build_match_bundle(
    documents: list[dict],
    query: str,
    language: str,
    dpi: int,
    contrast: float,
    sharpness: float,
) -> tuple[bytes, list[dict]]:
    matches: list[dict] = []
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for document in documents:
            matched_indexes = []
            with fitz.open(stream=document["searchable_pdf"], filetype="pdf") as searchable:
                for page_index, result in enumerate(document["results"]):
                    text = result.text or ""
                    occurrences = find_ocr_keyword_occurrences(text, query)
                    if not occurrences:
                        continue
                    count, fuzzy_count = table_aware_keyword_count(
                        searchable[page_index], result, query, language, dpi, contrast, sharpness, len(occurrences)
                    )
                    matched_indexes.append(page_index)
                    position, _, occurrence_end = occurrences[0]
                    start = max(0, position - 70)
                    end = min(len(text), occurrence_end + 110)
                    if fuzzy_count >= count and fuzzy_count:
                        match_type = "Mirip OCR"
                    elif fuzzy_count:
                        match_type = "Tepat + Mirip OCR"
                    else:
                        match_type = "Tepat"
                    matches.append(
                        {
                            "file": document["name"],
                            "page": page_index + 1,
                            "jumlah": count,
                            "kecocokan": match_type,
                            "snippet": " ".join(text[start:end].split()),
                        }
                    )
            if matched_indexes:
                pages_pdf = make_matching_pages_pdf(document["searchable_pdf"], matched_indexes)
                stem = Path(document["name"]).stem
                page_labels = "_".join(str(index + 1) for index in matched_indexes)
                bundle.writestr(f"{stem}_halaman_{page_labels}.pdf", pages_pdf)
    return archive.getvalue(), matches


def page_readability(result: PageResult) -> tuple[str, str]:
    if result.ai_used:
        return "Dibaca AI", "Teks dibaca ulang oleh Gemini. Periksa kembali nama, angka, dan istilah penting pada scan asli."
    word_count = len(result.words)
    if word_count >= 5 and result.confidence >= 80:
        return "Terbaca", "Keyakinan OCR mencapai 80%; periksa tetap nama, angka, dan istilah penting."
    if word_count:
        return "Perlu dicek", "Keyakinan OCR di bawah 80%; teks terdeteksi, tetapi mungkin ada salah baca."
    return "Tidak terbaca", "Teks belum terdeteksi. Coba scan ulang atau unggah versi yang lebih jelas."


def check_tesseract(language: str) -> tuple[bool, str]:
    configure_tesseract()
    try:
        installed = set(pytesseract.get_languages(config=""))
    except (pytesseract.TesseractNotFoundError, OSError):
        return False, "Tesseract OCR belum terpasang atau tidak ditemukan. Ikuti petunjuk instalasi di README.md."
    requested = set(language.split("+"))
    missing = requested - installed
    if missing:
        return False, f"Data bahasa OCR belum tersedia: {', '.join(sorted(missing))}. Pasang language pack Tesseract yang sesuai."
    return True, ""


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon="ðŸ“„", layout="wide")
    st.title("Document Search")
    st.write(
        "Unggah satu atau beberapa PDF scan, pastikan hasil OCR terbaca, lalu cari kata kunci "
        "untuk menemukan file dan halaman yang cocok."
    )
    st.success(
        "OCR Tesseract berjalan lokal. Opsional: halaman yang tidak terbaca atau keyakinannya di bawah 80% dapat dikirim ke Gemini untuk dibaca ulang."
    )

    with st.sidebar:
        st.header("Pengaturan")
        language = st.selectbox(
            "Bahasa dokumen",
            options=["ind+eng", "ind", "eng", "kor", "chi_sim", "kor+eng", "chi_sim+eng"],
            format_func={
                "ind+eng": "Indonesia + Inggris",
                "ind": "Indonesia",
                "eng": "Inggris",
                "kor": "Korea Selatan",
                "chi_sim": "Mandarin (China, sederhana)",
                "kor+eng": "Korea Selatan + Inggris",
                "chi_sim+eng": "Mandarin (China) + Inggris",
            }.get,
        )
        dpi = st.slider("Target resolusi OCR (DPI)", 100, 400, 300, 25)
        st.caption("Scan yang menutupi hampir seluruh halaman diproses dari gambar sumber, dibatasi maksimal pembesaran 1,25Ã—, dan tepinya dipotong 1,5% untuk mengurangi gangguan.")
        contrast = st.slider("Kontras", 1.0, 2.5, 1.15, 0.05)
        sharpness = st.slider("Penajaman", 1.0, 3.0, 1.15, 0.1)
        if GEMINI_API_KEY:
            st.caption("Halaman dengan OCR kosong atau keyakinan <80% akan dikirim ke Google Gemini.")

    st.subheader("1. Unggah dokumen scan")
    uploaded_files = st.file_uploader(
        f"Pilih hingga {MAX_UPLOAD_FILES} PDF",
        type=["pdf"],
        accept_multiple_files=True,
    )
    if not uploaded_files:
        st.caption("Setelah diproses, aplikasi menandai halaman yang terbaca dan menyediakan pencarian kata kunci.")
        return
    if len(uploaded_files) > MAX_UPLOAD_FILES:
        st.error(f"Maksimal {MAX_UPLOAD_FILES} dokumen PDF per proses. Hapus {len(uploaded_files) - MAX_UPLOAD_FILES} file lalu coba lagi.")
        return

    files_data = []
    for uploaded in uploaded_files:
        payload = uploaded.getvalue()
        if len(payload) > MAX_UPLOAD_MB * 1024 * 1024:
            st.error(f"{uploaded.name} melebihi batas ukuran {MAX_UPLOAD_MB} MB.")
            return
        files_data.append(
            {
                "name": uploaded.name,
                "bytes": payload,
                "digest": hashlib.sha256(payload).hexdigest(),
            }
        )
    upload_signature = hashlib.sha256(
        "|".join(f"{item['name']}:{item['digest']}" for item in files_data).encode("utf-8")
    ).hexdigest()

    if st.button("Proses scan dan periksa keterbacaan", type="primary"):
        ready, message = check_tesseract(language)
        if not ready:
            st.error(message)
            return
        try:
            total_pages = 0
            for item in files_data:
                with fitz.open(stream=item["bytes"], filetype="pdf") as source:
                    total_pages += source.page_count
            if total_pages == 0:
                st.error("PDF yang dipilih tidak memiliki halaman.")
                return

            documents: list[dict] = []
            progress = st.progress(0, text="Memulai OCR")
            completed_pages = 0
            for file_index, item in enumerate(files_data):
                results: list[PageResult] = []
                with fitz.open(stream=item["bytes"], filetype="pdf") as source:
                    for page_index, page in enumerate(source):
                        result = recognize_page(page, language, dpi, contrast, sharpness)
                        results.append(result)
                        completed_pages += 1
                        progress.progress(
                            completed_pages / total_pages,
                            text=f"File {file_index + 1}/{len(files_data)} Â· halaman {page_index + 1}/{source.page_count}",
                        )

                documents.append(
                    {
                        "name": item["name"],
                        "source_pdf": item["bytes"],
                        "results": results,
                        "searchable_pdf": make_searchable_pdf(item["bytes"], results),
                        "docx": make_docx(results),
                    }
                )
            st.session_state["document_search_processed"] = (upload_signature, documents)
            st.session_state.pop("document_search_matches", None)
            st.session_state.pop("document_search_matches_v2", None)
            st.session_state.pop("document_search_matches_v3", None)
            st.success(f"Selesai memproses {len(documents)} file dan {total_pages} halaman.")
        except pytesseract.TesseractNotFoundError:
            st.error("Tesseract OCR belum terpasang atau tidak ditemukan. Ikuti petunjuk instalasi di README.md.")
        except Exception as exc:
            st.error(f"PDF gagal diproses: {exc}")

    processed = st.session_state.get("document_search_processed")
    if not processed or processed[0] != upload_signature:
        return

    _, documents = processed
    st.subheader("2. Periksa hasil scan")
    weak_pages = sum(
        not result.ai_used and (not result.text.strip() or result.confidence < 80)
        for document in documents
        for result in document["results"]
    )
    if not GEMINI_API_KEY:
        st.caption("AI belum aktif. Isi GEMINI_API_KEY di file .env lalu mulai ulang aplikasi.")
    elif weak_pages:
        st.caption(f"{weak_pages} halaman memiliki OCR kosong atau berskor di bawah 80%.")

    st.caption("Keyakinan adalah perkiraan Tesseract, bukan persentase akurasi terverifikasi. Transkripsi AI digunakan untuk teks, DOCX, dan pencarian; PDF searchable tetap memakai lapisan kata Tesseract.")
    readable_page_count = 0
    total_page_count = 0
    for document_index, document in enumerate(documents):
        with st.expander(f"{document['name']} · {len(document['results'])} halaman", expanded=True):
            statuses = []
            for page_index, result in enumerate(document["results"]):
                status, note = page_readability(result)
                total_page_count += 1
                readable_page_count += status in ("Terbaca", "Dibaca AI")
                statuses.append(
                    {
                        "Halaman": page_index + 1,
                        "Status": status,
                        "Kata terdeteksi": len(result.words),
                        "Keyakinan OCR": f"{result.confidence:.0f}%",
                        "AI": "Gemini" if result.ai_used else "—",
                        "Catatan": note,
                    }
                )
            st.dataframe(statuses, hide_index=True, use_container_width=True)
            stem = Path(document["name"]).stem
            pdf_col, docx_col = st.columns(2)
            with pdf_col:
                st.download_button(
                    "Unduh PDF searchable",
                    data=document["searchable_pdf"],
                    file_name=f"{stem}_searchable.pdf",
                    mime="application/pdf",
                    key=f"searchable_{document_index}_{upload_signature[:8]}",
                    use_container_width=True,
                )
            with docx_col:
                st.download_button(
                    "Unduh DOCX",
                    data=document["docx"],
                    file_name=f"{stem}_hasil_ocr.docx",
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    key=f"docx_{document_index}_{upload_signature[:8]}",
                    use_container_width=True,
                )
            with st.expander("Tinjau teks OCR per halaman"):
                for page_index, result in enumerate(document["results"]):
                    status, _ = page_readability(result)
                    st.text_area(
                        f"Halaman {page_index + 1} · {status}",
                        value=result.text,
                        height=160,
                        key=f"ocr_text_{document_index}_{page_index}_{upload_signature[:8]}",
                        disabled=True,
                    )

    if readable_page_count == 0:
        st.warning(
            "Belum ada halaman yang mencapai keyakinan 80%. Pencarian tetap tersedia untuk teks OCR yang terdeteksi, "
            "tetapi hasilnya mungkin perlu diperiksa atau dikoreksi."
        )
    elif readable_page_count < total_page_count:
        st.warning(
            f"{readable_page_count} dari {total_page_count} halaman cukup terbaca. Halaman lain ditandai untuk diperiksa; "
            "pencarian tetap akan mencakup seluruh halaman yang memiliki teks OCR."
        )
    else:
        st.success(f"Semua {total_page_count} halaman cukup terbaca.")
    st.subheader("3. Cari keyword")
    with st.form("keyword_search_form"):
        keyword = st.text_input("Masukkan kata atau frasa", placeholder="Contoh: Sodium Chloride")
        search_clicked = st.form_submit_button("Search Keyword", type="primary")

    if search_clicked:
        if not keyword.strip():
            st.warning("Masukkan kata kunci yang ingin dicari.")
        else:
            bundle, matches = build_match_bundle(documents, keyword, language, dpi, contrast, sharpness)
            st.session_state["document_search_matches_v3"] = (upload_signature, keyword.strip(), bundle, matches)

    search_state = st.session_state.get("document_search_matches_v3")
    if search_state and search_state[0] == upload_signature:
        _, keyword, bundle, matches = search_state
        st.subheader("4. Output file dan halaman yang cocok")
        if matches:
            total_occurrences = sum(match["jumlah"] for match in matches)
            st.write(
                f'Ditemukan **{total_occurrences} kemunculan** pada '
                f'**{len(matches)} halaman** untuk â€œ{keyword}â€.'
            )
            if any("Mirip OCR" in match["kecocokan"] for match in matches):
                st.caption("Kecocokan mirip dapat membantu menemukan kata yang salah dibaca OCR. Pastikan kata tersebut benar pada scan asli.")

            file_summary = {}
            for match in matches:
                summary = file_summary.setdefault(
                    match["file"], {"file": match["file"], "jumlah keyword": 0, "halaman": []}
                )
                summary["jumlah keyword"] += match["jumlah"]
                summary["halaman"].append(str(match["page"]))

            st.markdown("**Ringkasan jumlah per file**")
            st.dataframe(
                [
                    {**summary, "halaman": ", ".join(summary["halaman"])}
                    for summary in file_summary.values()
                ],
                hide_index=True,
                use_container_width=True,
            )
            st.markdown("**Rincian temuan per halaman**")
            st.dataframe(matches, hide_index=True, use_container_width=True)
            st.download_button(
                "Unduh PDF halaman yang cocok (ZIP)",
                data=bundle,
                file_name="hasil_pencarian_halaman.zip",
                mime="application/zip",
                key=f"match_bundle_{upload_signature[:8]}_{hashlib.sha256(keyword.encode()).hexdigest()[:8]}",
            )
        else:
            st.info(f'Tidak ada halaman yang memuat â€œ{keyword}â€. Coba kata kunci lain atau periksa teks OCR.')
            ai_targets = sum(
                not result.ai_used
                for document in documents
                for result in document["results"]
            )
            if ai_targets and GEMINI_API_KEY:
                if st.button("Search lebih lanjut dengan AI", key=f"ai_retry_{upload_signature[:8]}"):
                    ai_progress = st.progress(0, text="Menyiapkan scan AI...")
                    ai_failures = []
                    completed_ai_pages = 0
                    for document in documents:
                        with fitz.open(stream=document["source_pdf"], filetype="pdf") as source:
                            for page_index, (page, result) in enumerate(zip(source, document["results"])):
                                if result.ai_used:
                                    continue
                                try:
                                    recognize_with_gemini(
                                        result,
                                        render_page_image(page, dpi, result.rotation),
                                        GEMINI_API_KEY,
                                        language,
                                    )
                                except Exception as exc:
                                    safe_error = str(exc).replace(GEMINI_API_KEY, "[key disembunyikan]")
                                    ai_failures.append((document["name"], page_index + 1, safe_error))
                                completed_ai_pages += 1
                                ai_progress.progress(
                                    completed_ai_pages / ai_targets,
                                    text=f"Scan AI halaman {completed_ai_pages}/{ai_targets}",
                                )
                        document["docx"] = make_docx(document["results"])
                    st.session_state["document_search_processed"] = (upload_signature, documents)
                    updated_bundle, updated_matches = build_match_bundle(
                        documents, keyword, language, dpi, contrast, sharpness
                    )
                    st.session_state["document_search_matches_v3"] = (
                        upload_signature, keyword, updated_bundle, updated_matches
                    )
                    ai_pages = sum(
                        result.ai_used for document in documents for result in document["results"]
                    )
                    st.success(f"Scan AI selesai. {ai_pages} halaman menggunakan transkripsi Gemini.")
                    if ai_failures:
                        first_failure = ai_failures[0][2]
                        st.warning(
                            f"Gemini gagal membaca {len(ai_failures)} halaman. "
                            f"Detail error: {first_failure}. Cek API key, akses model, kuota, dan koneksi."
                        )
                    st.rerun()
            elif ai_targets and not GEMINI_API_KEY:
                st.caption("Untuk mengaktifkan scan AI, isi GEMINI_API_KEY di file .env lalu mulai ulang aplikasi.")


if __name__ == "__main__":
    main()


