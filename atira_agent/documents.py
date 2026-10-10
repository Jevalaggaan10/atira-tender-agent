"""Document bytes → text/images/entries, in memory only.

The runtime downloads files through the model-directed browser (`SiteBrowser.download` hands us raw bytes);
nothing here touches the network, the filesystem, OCR or local models. PDF text extraction uses pypdf, page
rendering uses pypdfium2 (PNG for the hosted Qwen vision model), ZIPs use stdlib `zipfile`. Every public entry
point first enforces `DEFAULT_DOC_LIMIT` through `check_size`; anything it cannot do raises `DocumentError`
(a `ValueError`) with a message that says why.
"""
from __future__ import annotations

import io
import re
import struct
import zipfile
import zlib
from pathlib import PurePosixPath

from pypdf import PdfReader
from pypdfium2 import PdfDocument

__all__ = [
    "DEFAULT_DOC_LIMIT",
    "DocumentError",
    "bytes_to_text",
    "check_size",
    "pdf_page_count",
    "pdf_render_png",
    "pdf_texts",
    "sniff",
    "zip_entries",
    "zip_read",
]

DEFAULT_DOC_LIMIT = 40_000_000  # bytes accepted for one document

_PDF_MAGIC = b"%PDF"
_ZIP_MAGIC = b"PK\x03\x04"
_IMAGE_MAGICS: tuple[tuple[bytes, int], ...] = (
    (b"\x89PNG\r\n\x1a\n", 8),  # PNG
    (b"\xff\xd8\xff", 3),  # JPEG
    (b"GIF87a", 6),
    (b"GIF89a", 6),
    (b"BM", 2),  # BMP
    (b"RIFF", 4),  # WEBP (verified below: bytes 8:12 must be "WEBP")
)
_TEXT_CONTENT_TYPES = ("text/", "application/json", "application/xml", "application/javascript", "image/svg")
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


class DocumentError(ValueError):
    """The bytes are not a usable document of the requested kind, or exceed a size limit."""


def check_size(data: bytes) -> None:
    """Entry-point guard every document handler shares: raise `DocumentError` past `DEFAULT_DOC_LIMIT`."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise DocumentError(f"expected document bytes, got {type(data).__name__}")
    if len(data) > DEFAULT_DOC_LIMIT:
        raise DocumentError(f"document of {len(data)} bytes exceeds the limit of {DEFAULT_DOC_LIMIT}")


def bytes_to_text(data: bytes) -> str:
    """Decode bytes as UTF-8 with errors replaced, for text-like downloads."""
    return bytes(data).decode("utf-8", errors="replace")


def sniff(data: bytes, content_type: str | None = None) -> str:
    """Classify downloaded bytes as "pdf" | "zip" | "text" | "image" | "other" by magic, then content type."""
    check_size(data)
    head = bytes(data[:12])
    if head.startswith(_PDF_MAGIC):
        return "pdf"
    if head.startswith(_ZIP_MAGIC):
        return "zip"
    for magic, _ in _IMAGE_MAGICS:
        if head.startswith(magic):
            if magic == b"RIFF" and head[8:12] != b"WEBP":
                continue  # a RIFF WAV/AVI is not an image we know
            return "image"
    hint = (content_type or "").lower()
    if hint:
        if "pdf" in hint:
            return "pdf"
        if "zip" in hint or hint in {"application/x-compressed", "application/x-gzip"}:
            return "zip"
        if hint.startswith("image/"):
            return "image"
        if any(hint.startswith(t) for t in _TEXT_CONTENT_TYPES):
            return "text"
    if _is_text(data):
        return "text"
    return "other"


def _is_text(data: bytes) -> bool:
    """Text when it decodes as UTF-8 and holds no control bytes beyond tab/newline/carriage-return."""
    if any(b < 0x20 and b not in (0x09, 0x0A, 0x0D) for b in data[:8192]):
        return False
    try:
        bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _open_pdf(data: bytes) -> PdfReader:
    """A decrypted `PdfReader`, or `DocumentError` saying why not (non-PDF, corrupt, encrypted)."""
    check_size(data)
    if not bytes(data[:4]).startswith(_PDF_MAGIC):
        raise DocumentError("not a PDF: missing the %PDF header")
    try:
        reader = PdfReader(io.BytesIO(bytes(data)))
    except DocumentError:
        raise
    except Exception as exc:  # pypdf raises assorted parse errors on corrupt input
        raise DocumentError(f"not a readable PDF: {exc}") from exc
    if reader.is_encrypted:
        try:
            opened = reader.decrypt("") == 1  # empty password: try it, then give up
        except Exception as exc:
            raise DocumentError(f"PDF is encrypted and undecryptable: {exc}") from exc
        if not opened:
            raise DocumentError("PDF is encrypted: a password is required and none is available")
    return reader


def pdf_page_count(data: bytes) -> int:
    """Number of pages; `DocumentError` on non-PDF/encrypted input."""
    return len(_open_pdf(data).pages)


def pdf_texts(data: bytes, pages: list[int] | None = None, max_pages: int = 60) -> dict:
    """{"page_count": n, "pages": {"<idx>": text}} for selected pages (None => first `max_pages`).

    Page indexes are 0-based; out-of-range indexes or undecryptable/encrypted/non-PDF input raise
    `DocumentError`. With explicit `pages` at most the first `max_pages` of them are extracted.
    """
    reader = _open_pdf(data)
    count = len(reader.pages)
    if pages is None:
        wanted = list(range(min(count, max(0, max_pages))))
    else:
        wanted = []
        for index in pages:
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < count:
                raise DocumentError(f"page index {index!r} out of range for a PDF of {count} page(s)")
            if index not in wanted:
                wanted.append(index)
        wanted.sort()
        del wanted[max(0, max_pages):]
    texts: dict[str, str] = {}
    for index in wanted:
        try:
            texts[str(index)] = reader.pages[index].extract_text() or ""
        except Exception as exc:
            raise DocumentError(f"could not extract page {index}: {exc}") from exc
    return {"page_count": count, "pages": texts}


def pdf_render_png(data: bytes, page_index: int, scale: float = 2.0, max_side: int = 1600) -> bytes:
    """Render one page to PNG bytes for hosted Qwen vision, capped so neither side exceeds `max_side` px.

    `scale` multiplies the page's points; the effective scale shrinks further if the cap would be exceeded.
    `DocumentError` on non-PDF input, out-of-range page, or a zero/absurd scale or cap.
    """
    check_size(data)
    if not bytes(data[:4]).startswith(_PDF_MAGIC):
        raise DocumentError("not a PDF: missing the %PDF header")
    if not isinstance(page_index, int) or isinstance(page_index, bool) or page_index < 0:
        raise DocumentError(f"page index {page_index!r} must be a non-negative int")
    if not 0 < float(scale) <= 10:
        raise DocumentError(f"scale {scale!r} outside (0, 10]")
    if not 0 < int(max_side) <= 8000:
        raise DocumentError(f"max_side {max_side!r} outside (0, 8000]")
    try:
        pdf = PdfDocument(io.BytesIO(bytes(data)))
    except Exception as exc:
        raise DocumentError(f"not a readable PDF: {exc}") from exc
    try:
        if not 0 <= page_index < len(pdf):
            raise DocumentError(f"page index {page_index} out of range for a PDF of {len(pdf)} page(s)")
        page = pdf[page_index]
        width_pt, height_pt = page.get_size()
        effective = float(scale)
        widest = max(float(width_pt), float(height_pt)) * effective
        if widest > max_side:  # keep every pixel the model asked for, down to the cap
            effective = effective * (max_side / widest)
        bitmap = page.render(scale=effective)
        return _bitmap_to_png(bitmap)
    except DocumentError:
        raise
    except Exception as exc:  # pdfium raises on encrypted/corrupt pages
        raise DocumentError(f"could not render page {page_index}: {exc}") from exc
    finally:
        pdf.close()


def _bitmap_to_png(bitmap) -> bytes:
    """PNG from a pypdfium2 bitmap with stdlib zlib only (no Pillow/numpy): BGR(A)→RGB, Gray→gray."""
    width, height, channels, stride = bitmap.width, bitmap.height, bitmap.n_channels, bitmap.stride
    raw = bytes(bitmap.buffer)
    row_len = width * channels
    rows = bytearray()
    for y in range(height):
        row = raw[y * stride: y * stride + row_len]
        if channels == 1:
            pixels = row  # already grayscale
            color_type = 0
        elif channels == 3:  # pdfium fills BGR: swap to RGB
            pixels = bytearray(row)
            pixels[0::3], pixels[1::3], pixels[2::3] = row[2::3], row[1::3], row[0::3]
            color_type = 2
        else:  # 4 channels, BGRx/BGRA: swap to RGB, drop the alpha (renders opaque for the vision model)
            pixels = bytearray(width * 3)
            pixels[0::3], pixels[1::3], pixels[2::3] = row[2::4], row[1::4], row[0::4]
            color_type = 2
        rows.append(0)  # filter type 0 (None) per scanline
        rows += pixels
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + _png_chunk(b"IEND", b""))


def _png_chunk(tag: bytes, body: bytes) -> bytes:
    return len(body).to_bytes(4, "big") + tag + body + zlib.crc32(tag + body).to_bytes(4, "big")


def _name_is_safe(name: str) -> bool:
    """False for absolute paths, ".." segments, backslashes or drive-letter prefixes (zip-slip guards)."""
    if not name or "\\" in name or _DRIVE_PREFIX.match(name) or name.startswith("/"):
        return False
    parts = PurePosixPath(name).parts
    return bool(parts) and ".." not in parts and not name.startswith("//")


def _open_zip(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(bytes(data)))
    except Exception as exc:
        raise DocumentError(f"not a readable ZIP: {exc}") from exc


def _entry_summary(info: zipfile.ZipInfo, index: int) -> dict:
    return {"name": info.filename, "size": info.file_size, "safe": _name_is_safe(info.filename),
            "index": index}


def zip_entries(data: bytes, max_entries: int = 500) -> list[dict]:
    """[{"name","size","safe","index"}] in archive order (size = uncompressed bytes); capped at `max_entries`.

    `index` is the entry's position in the archive; a second entry with a name seen before carries
    `duplicate_of`: that earlier index. `zip_read` refuses such a name, so the flag marks what it cannot pick.
    """
    check_size(data)
    entries = []
    with _open_zip(data) as archive:
        seen: dict[str, int] = {}
        for index, info in enumerate(archive.infolist()[: max(0, max_entries)]):
            row = _entry_summary(info, index)
            if info.filename in seen:
                row["duplicate_of"] = seen[info.filename]
            else:
                seen[info.filename] = index
            entries.append(row)
    return entries


def zip_read(data: bytes, name: str, max_bytes: int = 5_000_000, index: int | None = None) -> bytes:
    """Bytes of the exactly-named safe entry, refusing unsafe names and entries over `max_bytes`.

    The name addresses the entry; when it appears twice the choice is not ours, so this raises naming every
    index rather than taking the first (a decoy must not shadow the governing entry). Pass `index` (from
    `zip_entries`) for one entry of a duplicated name; `index` is optional and only narrows a name the
    archive holds more than once. The addressed entry's own name must equal `name` (misattribution guard:
    the bytes returned are attributed to `name` in the evidence) and still goes through the name guard.
    """
    check_size(data)
    if index is not None and (not isinstance(index, int) or isinstance(index, bool) or index < 0):
        raise DocumentError(f"index {index!r} must be None or a non-negative int")
    if name is not None and not _name_is_safe(name):
        raise DocumentError(f"refusing unsafe ZIP entry name: {name!r}")
    with _open_zip(data) as archive:
        infos = list(archive.infolist())
        if index is not None:
            if index >= len(infos):
                raise DocumentError(f"entry index {index} out of range for a ZIP of {len(infos)} entries")
            if name is not None and infos[index].filename != name:
                raise DocumentError(
                    f"entry at index {index} is named {infos[index].filename!r}, not {name!r} — reading it "
                    f"would misattribute those bytes to {name!r}: re-call with the entry's own name "
                    f"(see zip_entries)")
            matches = [(index, infos[index])]
        else:
            matches = [(i, info) for i, info in enumerate(infos) if info.filename == name]
            if not matches:
                raise DocumentError(f"no ZIP entry named {name!r}")
            if len(matches) > 1:
                raise DocumentError(
                    f"{len(matches)} ZIP entries share the name {name!r} at indexes "
                    f"{[i for i, _ in matches]} — the archive does not say which is the governing one, so "
                    f"reading by name alone would take an arbitrary entry (usually the first): re-call with "
                    f"index=N for the entry you want, checked against zip_entries first")
        info = matches[0][1]
        if not _name_is_safe(info.filename):
            raise DocumentError(f"refusing unsafe ZIP entry name: {info.filename!r}")
        if info.file_size > max_bytes:
            raise DocumentError(f"entry {name!r} is {info.file_size} bytes, over the limit of {max_bytes}")
        try:
            payload = archive.open(info).read(max_bytes + 1)  # guard a lying header too
        except Exception as exc:
            raise DocumentError(f"could not read entry {name!r}: {exc}") from exc
        if len(payload) > max_bytes:
            raise DocumentError(f"entry {name!r} is larger than the limit of {max_bytes}")
        return payload
