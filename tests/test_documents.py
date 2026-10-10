"""`documents.py` against bytes built in this file: sniffing, PDF text and rendering, ZIP entries and reads.

No network, no filesystem: every input is a hand-written PDF, a `zipfile`-built archive, raw ZIP headers
assembled here, or PNG bytes assembled from chunks. Expectations describe what the module actually does.
"""
import io
import struct
import sys
import unittest
import warnings
import zipfile
import zlib
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atira_agent.documents import (
    DEFAULT_DOC_LIMIT,
    DocumentError,
    bytes_to_text,
    check_size,
    pdf_page_count,
    pdf_render_png,
    pdf_texts,
    sniff,
    zip_entries,
    zip_read,
)

# --------------------------------------------------------------------------- fixtures

def text_pdf(word="SYNTH-CATALOG-7"):
    """A one-page PDF assembled byte by byte: catalogue, pages, page, one text object, Helvetica, xref."""
    stream = f"BT /F1 24 Tf 72 700 Td ({word}) Tj ET"
    objects = {1: "<< /Type /Catalog /Pages 2 0 R >>",
               2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               3: ("<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 5 0 R >> >> "
                   "/MediaBox [0 0 595 842] /Contents 4 0 R >>"),
               4: f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
               5: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    out = b"%PDF-1.4\n"
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode()
    start_xref = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    out += b"".join(f"{offsets[n]:010d} 00000 n \n".encode() for n in range(1, 6))
    out += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start_xref}\n%%EOF\n".encode()
    return out


TEXT_PDF = text_pdf()


def blank_pdf(sizes):
    """A throwaway PDF, one blank page per (width_pt, height_pt)."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for width, height in sizes:
        writer.add_blank_page(width=width, height=height)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


TWO_PAGE_PDF = blank_pdf([(100, 80), (200, 120)])
ENCRYPTED_PDF = None


def encrypted_pdf():
    """The text PDF encrypted with a password none of our helpers can supply."""
    global ENCRYPTED_PDF
    if ENCRYPTED_PDF is None:
        from pypdf import PdfReader, PdfWriter

        writer = PdfWriter()
        writer.add_page(PdfReader(io.BytesIO(TEXT_PDF)).pages[0])
        out = io.BytesIO()
        writer.encrypt("synthetic-passphrase")
        writer.write(out)
        ENCRYPTED_PDF = out.getvalue()
    return ENCRYPTED_PDF


def make_png(width, height, color_type=2):
    """A valid PNG made of chunks, so tests can check dimensions without Pillow or numpy."""
    def chunk(tag, body):
        return len(body).to_bytes(4, "big") + tag + body + struct.pack(">I", zlib.crc32(tag + body))

    bytes_per_pixel = {0: 1, 2: 3}[color_type]
    raw = (b"\x00" + b"\x00" * (width * bytes_per_pixel)) * height
    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def png_size(data):
    """(width, height, color_type) from a PNG's IHDR."""
    width, height, _depth, color = struct.unpack(">IIBB", data[16:26])
    return width, height, color


def zip_bytes(entries, compression=zipfile.ZIP_DEFLATED):
    """An in-memory archive from (name, payload) pairs."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return buffer.getvalue()


def raw_local_header_zip(name, payload):
    """An archive holding one raw local-file-header entry, whose name `zipfile`'s reader keeps verbatim.

    Needed because the unsafe names a reader must refuse cannot be written by the standard writer as entries;
    a reader without its own name guard would happily extract them.
    """
    encoded = name.encode()
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    head = (b"PK\x03\x04" + struct.pack("<HHHHH", 20, 0, 0, 0, 0)
            + struct.pack("<III", crc, len(payload), len(payload)) + struct.pack("<HH", len(encoded), 0))
    assert len(head) == 30, len(head)
    return head + encoded + payload


# --------------------------------------------------------------------------- sniffing


class Sniff(unittest.TestCase):
    def test_magic_beats_everything(self):
        self.assertEqual(sniff(b"%PDF-1.7\n%\xc2\xa0\n"), "pdf")
        self.assertEqual(sniff(b"PK\x03\x04\x14\x00"), "zip")
        self.assertEqual(sniff(make_png(9, 4)), "image")
        self.assertEqual(sniff(b"\xff\xd8\xff\xe0synthetic"), "image")
        self.assertEqual(sniff(b"GIF87a...."), "image")
        self.assertEqual(sniff(b"GIF89a...."), "image")
        self.assertEqual(sniff(b"BMsynthetic"), "image")
        # a magic beats a content type that contradicts it
        self.assertEqual(sniff(TEXT_PDF, "text/html; charset=utf-8"), "pdf")
        self.assertEqual(sniff(make_png(4, 4), "application/pdf"), "image")
        self.assertEqual(sniff(b"PK\x03\x04", "text/plain"), "zip")

    def test_riff_is_only_webp(self):
        self.assertEqual(sniff(b"RIFF\x24\x00\x00\x00WEBPVP8 "), "image")
        self.assertEqual(sniff(b"RIFF\x24\x00\x00\x00WAVEfmt "), "other")
        self.assertEqual(sniff(b"RIFF\x24\x00\x00\x00WEBPVP8 ", "application/octet-stream"), "image")

    def test_zip_magic_needs_the_local_header_signature(self):
        self.assertEqual(sniff(b"PK\x03\x05"), "other")  # an empty ZIP: not the local-header magic
        self.assertEqual(sniff(b"garbage", "application/zip"), "zip")

    def test_content_type_fallback(self):
        cases = {
            "application/pdf": "pdf", "APPLICATION/PDF": "pdf", "inline; type=pdf": "pdf",
            "application/zip": "zip", "application/x-zip-compressed": "zip",
            "application/x-compressed": "zip", "application/x-gzip": "zip",
            "image/png": "image", "IMAGE/JPEG": "image", "image/svg+xml": "image",
            "text/plain": "text", "text/csv; charset=utf-8": "text",
            "application/json": "text", "application/xml": "text", "application/javascript": "text",
        }
        for content_type, expected in cases.items():
            self.assertEqual(sniff(b"\x00\x01\x02binary-ish", content_type), expected, content_type)

    def test_content_type_without_text_hint_falls_back_to_the_heuristic(self):
        self.assertEqual(sniff(b"\x00\x01", "application/octet-stream"), "other")
        self.assertEqual(sniff(b"plain words", "application/octet-stream"), "text")
        self.assertEqual(sniff(b"\x00\x01", "application/pdf"), "pdf")

    def test_text_heuristic_without_a_content_type(self):
        self.assertEqual(sniff("Grüße, Kataloggruppe 12\nZweite Zeile\tTab\r".encode()), "text")
        self.assertEqual(sniff(b""), "text")           # empty bytes decode as "" and hold no control bytes
        self.assertEqual(sniff(b"a\x00b"), "other")    # NUL is not a text control byte
        self.assertEqual(sniff(b"a\x1bb"), "other")    # ESC neither
        self.assertEqual(sniff(b"caf\xe9"), "other")   # not valid UTF-8
        self.assertEqual(sniff(b"\x00\x01\x02\x03"), "other")
        self.assertEqual(sniff(b"\xff\xd8\xff"), "image")  # magic checked before the heuristic

    def test_limit_applies_to_sniff(self):
        self.assertEqual(sniff(b"hello"), "text")
        with self.assertRaisesRegex(DocumentError, "exceeds the limit"):
            sniff(b"x" * (DEFAULT_DOC_LIMIT + 1))


class SizeGuard(unittest.TestCase):
    def test_check_size(self):
        self.assertIsNone(check_size(b"small"))
        self.assertIsNone(check_size(bytearray(b"small")))
        self.assertIsNone(check_size(memoryview(b"small")))
        self.assertIsNone(check_size(b"x" * DEFAULT_DOC_LIMIT))
        with self.assertRaisesRegex(DocumentError, "expected document bytes, got str"):
            check_size("a string, not bytes")
        with self.assertRaisesRegex(DocumentError, "expected document bytes, got NoneType"):
            check_size(None)
        with self.assertRaisesRegex(DocumentError, f"{DEFAULT_DOC_LIMIT + 1} bytes"):
            check_size(b"x" * (DEFAULT_DOC_LIMIT + 1))

    def test_every_entry_point_enforces_the_limit_before_parsing(self):
        oversized = b"x" * (DEFAULT_DOC_LIMIT + 1)
        calls = {"sniff": lambda: sniff(oversized),
                 "pdf_page_count": lambda: pdf_page_count(oversized),
                 "pdf_texts": lambda: pdf_texts(oversized),
                 "pdf_render_png": lambda: pdf_render_png(oversized, 0),
                 "zip_entries": lambda: zip_entries(oversized),
                 "zip_read": lambda: zip_read(oversized, "any.txt")}
        for name, call in calls.items():
            with self.assertRaisesRegex(DocumentError, "exceeds the limit", msg=name):
                call()
        # the size complaint wins over the one about the content
        oversized_zip = b"PK\x03\x04" + b"x" * DEFAULT_DOC_LIMIT
        with self.assertRaisesRegex(DocumentError, "exceeds the limit"):
            zip_entries(oversized_zip)


class BytesToText(unittest.TestCase):
    def test_replaces_undecodable_bytes(self):
        self.assertEqual(bytes_to_text(b"caf\xe9"), "caf\ufffd")
        self.assertEqual(bytes_to_text(b"a\x80\x81b"), "a\ufffd\ufffdb")
        self.assertEqual(bytes_to_text(b"gr\xc3\xbc\xdfe"), "gr\u00fc\ufffde")

    def test_valid_text_and_empty_input(self):
        self.assertEqual(bytes_to_text(b"Zeile 1\nZeile 2"), "Zeile 1\nZeile 2")
        self.assertEqual(bytes_to_text(b""), "")
        self.assertEqual(bytes_to_text(bytearray(b"bytes-like")), "bytes-like")
        self.assertEqual(bytes_to_text(memoryview(b"view")), "view")


# --------------------------------------------------------------------------- PDFs


class PdfText(unittest.TestCase):
    def test_hand_written_pdf_extracts_the_expected_word(self):
        result = pdf_texts(TEXT_PDF)
        self.assertEqual(result["page_count"], 1)
        self.assertEqual(list(result["pages"]), ["0"])
        self.assertIn("SYNTH-CATALOG-7", result["pages"]["0"])

    def test_page_count(self):
        self.assertEqual(pdf_page_count(TEXT_PDF), 1)
        self.assertEqual(pdf_page_count(TWO_PAGE_PDF), 2)

    def test_two_blank_pages_differ_in_size_and_select_by_index(self):
        self.assertEqual(pdf_page_count(TWO_PAGE_PDF), 2)
        one = pdf_texts(TWO_PAGE_PDF, pages=[0])
        self.assertEqual(one["page_count"], 2)
        self.assertEqual(list(one["pages"]), ["0"])
        both = pdf_texts(TWO_PAGE_PDF)
        self.assertEqual(sorted(both["pages"]), ["0", "1"])
        self.assertEqual(both["pages"]["1"], "")  # blank pages yield empty text, never missing keys

    def test_selected_pages_are_sorted_deduped_and_capped(self):
        self.assertEqual(list(pdf_texts(TWO_PAGE_PDF, pages=[1, 0])["pages"]), ["0", "1"])
        self.assertEqual(list(pdf_texts(TWO_PAGE_PDF, pages=[1, 1, 0])["pages"]), ["0", "1"])
        self.assertEqual(list(pdf_texts(TWO_PAGE_PDF, pages=[1, 0], max_pages=1)["pages"]), ["0"])
        self.assertEqual(pdf_texts(TWO_PAGE_PDF, pages=[])["pages"], {})
        self.assertEqual(list(pdf_texts(TWO_PAGE_PDF, max_pages=1)["pages"]), ["0"])
        self.assertEqual(list(pdf_texts(TWO_PAGE_PDF, max_pages=0)["pages"]), [])

    def test_out_of_range_and_bad_page_indexes(self):
        for bad in ([2], [-1], [0, 5], [99]):
            with self.assertRaisesRegex(DocumentError, "out of range for a PDF of 2 page"):
                pdf_texts(TWO_PAGE_PDF, pages=bad)
        for bad in ([True], [1.5], ["0"], [None]):
            with self.assertRaisesRegex(DocumentError, "out of range"):
                pdf_texts(TWO_PAGE_PDF, pages=bad)

    def test_non_pdf_and_truncated_input(self):
        for bad in (b"not a pdf at all", make_png(8, 8), b"PK\x03\x04fake"):
            with self.assertRaisesRegex(DocumentError, "not a PDF: missing the %PDF header"):
                pdf_page_count(bad)
            with self.assertRaisesRegex(DocumentError, "not a PDF"):
                pdf_texts(bad)
        self.assertEqual(sniff(b"not a pdf at all"), "text")  # sniff and the parsers disagree on purpose
        truncated = TWO_PAGE_PDF[:60]
        self.assertEqual(sniff(truncated), "pdf")
        for call in (lambda: pdf_page_count(truncated), lambda: pdf_texts(truncated)):
            with self.assertRaisesRegex(DocumentError, "not a readable PDF"):
                call()

    def test_encrypted_pdf_says_so(self):
        encrypted = encrypted_pdf()
        self.assertEqual(sniff(encrypted), "pdf")
        for call in (lambda: pdf_page_count(encrypted), lambda: pdf_texts(encrypted)):
            with self.assertRaisesRegex(DocumentError, "(?i)encrypt"):
                call()


class PdfRender(unittest.TestCase):
    def test_scale_two_gives_plausible_dimensions(self):
        for index, (width_pt, height_pt) in enumerate(((100, 80), (200, 120))):
            png = pdf_render_png(TWO_PAGE_PDF, index, scale=2.0)
            self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(png[-8:-4], b"IEND")
            width, height, color = png_size(png)
            self.assertEqual((width, height), (width_pt * 2, height_pt * 2))
            self.assertEqual(png[24], 8)                       # 8 bits per channel
            self.assertIn(color, (0, 2, 6))                    # grayscale, RGB or RGB-alpha

    def test_max_side_caps_both_sides(self):
        width, height, _ = png_size(pdf_render_png(TWO_PAGE_PDF, 1, scale=2.0, max_side=150))
        self.assertLessEqual(max(width, height), 150)
        self.assertEqual((width, height), (150, 90))           # the 200x120 pt page, halved
        width, height, _ = png_size(pdf_render_png(TWO_PAGE_PDF, 0, scale=2.0, max_side=160))
        self.assertEqual((width, height), (160, 128))
        width, height, _ = png_size(pdf_render_png(TWO_PAGE_PDF, 0, scale=10.0, max_side=40))
        self.assertLessEqual(max(width, height), 40)

    def test_small_scale_and_odd_page_sizes(self):
        self.assertEqual(png_size(pdf_render_png(TWO_PAGE_PDF, 0, scale=0.25))[:2], (25, 20))
        self.assertEqual(png_size(pdf_render_png(blank_pdf([(21, 13)]), 0, scale=1.0))[:2], (21, 13))
        self.assertEqual(png_size(pdf_render_png(blank_pdf([(21, 13)]), 0, scale=0.1))[:2], (3, 2))
        self.assertLessEqual(max(png_size(pdf_render_png(TWO_PAGE_PDF, 1, scale=1.5, max_side=200))[:2]), 200)

    def test_out_of_range_and_bad_arguments(self):
        with self.assertRaisesRegex(DocumentError, "out of range for a PDF of 2 page"):
            pdf_render_png(TWO_PAGE_PDF, 2)
        with self.assertRaisesRegex(DocumentError, "non-negative int"):
            pdf_render_png(TWO_PAGE_PDF, -1)
        for bad_page in (True, 0.5, "0", None):
            with self.assertRaisesRegex(DocumentError, "non-negative int"):
                pdf_render_png(TWO_PAGE_PDF, bad_page)
        for bad_scale in (0, -1, 10.5, 0.0):
            with self.assertRaisesRegex(DocumentError, r"scale .* outside"):
                pdf_render_png(TWO_PAGE_PDF, 0, scale=bad_scale)
        for bad_cap in (0, -5, 8001):
            with self.assertRaisesRegex(DocumentError, r"max_side .* outside"):
                pdf_render_png(TWO_PAGE_PDF, 0, max_side=bad_cap)

    def test_non_pdf_and_truncated_pdf(self):
        with self.assertRaisesRegex(DocumentError, "not a PDF: missing the %PDF header"):
            pdf_render_png(b"plain text pretending", 0)
        with self.assertRaisesRegex(DocumentError, "not a readable PDF"):
            pdf_render_png(TWO_PAGE_PDF[:60], 0)

    def test_encrypted_pdf_render_mentions_the_load_failure(self):
        with self.assertRaisesRegex(DocumentError, "not a readable PDF"):
            pdf_render_png(encrypted_pdf(), 0)

    def test_oversize_data_is_refused_before_rendering(self):
        with self.assertRaisesRegex(DocumentError, "exceeds the limit"):
            pdf_render_png(b"%PDF-" + b"x" * DEFAULT_DOC_LIMIT, 0)


# --------------------------------------------------------------------------- ZIPs


class Zip(unittest.TestCase):
    ARCHIVE: ClassVar = [("top.txt", b"top-level"), ("docs/report.txt", b"alpha-report"),
               ("docs/deep/nested/inner.bin", bytes(range(64))), ("docs/other.md", b"# note")]

    def test_entries_are_archive_order_with_size_and_safety(self):
        entries = zip_entries(zip_bytes(self.ARCHIVE))
        self.assertEqual([e["name"] for e in entries], [name for name, _ in self.ARCHIVE])
        self.assertEqual(entries[0], {"name": "top.txt", "size": 9, "safe": True, "index": 0})
        self.assertEqual([e["size"] for e in entries], [len(payload) for _, payload in self.ARCHIVE])
        self.assertTrue(all(e["safe"] for e in entries))
        self.assertEqual(len(entries[2]["name"].split("/")), 4)  # nested directories keep their path

    def test_entries_are_capped(self):
        archive = zip_bytes([(f"n{i}.txt", b"x") for i in range(5)])
        self.assertEqual([e["name"] for e in zip_entries(archive, max_entries=2)], ["n0.txt", "n1.txt"])
        self.assertEqual(zip_entries(archive, max_entries=0), [])
        self.assertEqual(len(zip_entries(archive, max_entries=99)), 5)

    def test_read_exact_names_only(self):
        archive = zip_bytes(self.ARCHIVE)
        self.assertEqual(zip_read(archive, "docs/report.txt"), b"alpha-report")
        self.assertEqual(zip_read(archive, "docs/deep/nested/inner.bin"), bytes(range(64)))
        for wrong in ("report.txt", "docs//report.txt", "docs/./report.txt", "DOCS/report.txt",
                      "./top.txt", "top.txt "):
            with self.assertRaisesRegex(DocumentError, "no ZIP entry named"):
                zip_read(archive, wrong)
        for not_even_a_name in ("", "..", "/etc/passwd"):  # refused before the archive is opened
            with self.assertRaisesRegex(DocumentError, "refusing unsafe ZIP entry name"):
                zip_read(archive, not_even_a_name)

    def test_read_size_guard_on_stored_and_compressed_entries(self):
        with self.assertRaisesRegex(DocumentError, "is 1000 bytes, over the limit of 10"):
            zip_read(zip_bytes([("big.txt", b"0" * 1000)], zipfile.ZIP_STORED), "big.txt", max_bytes=10)
        # a compressed entry is guarded on its uncompressed size, so the cap cannot be bypassed by packing
        packed = zip_bytes([("packed.txt", b"9" * 10_000)], zipfile.ZIP_DEFLATED)
        self.assertLess(len(packed), 1000)
        with self.assertRaisesRegex(DocumentError, "over the limit of 10"):
            zip_read(packed, "packed.txt", max_bytes=10)
        self.assertEqual(zip_read(packed, "packed.txt", max_bytes=20_000), b"9" * 10_000)

    def test_unsafe_names_are_rejected_and_reported_unsafe(self):
        archive = zip_bytes([("../evil.txt", b"evil"), ("C:\\evil.txt", b"c"), ("windows\\path.txt", b"w"),
                             ("..\\up.txt", b"u"), ("ok/fine.txt", b"fine")])
        self.assertEqual([(e["name"], e["safe"]) for e in zip_entries(archive)],
                         [("../evil.txt", False), ("C:\\evil.txt", False), ("windows\\path.txt", False),
                          ("..\\up.txt", False), ("ok/fine.txt", True)])
        for name in ("../evil.txt", "../deep/../evil.txt", "ok/../../evil.txt", "/etc/passwd", "//server/share",
                     "C:\\evil.txt", "windows\\path.txt", "..\\up.txt", "..", ""):
            with self.assertRaisesRegex(DocumentError, "refusing unsafe ZIP entry name"):
                zip_read(archive, name)
        self.assertEqual(zip_read(archive, "ok/fine.txt"), b"fine")
        # a slash spelling of an unsafe entry stays unsafe: it still reaches the archive's own guard
        with self.assertRaisesRegex(DocumentError, "refusing unsafe ZIP entry name"):
            zip_read(raw_local_header_zip("ok/../../escape.txt", b"e"), "ok/../../escape.txt")
        # ... while a slash spelling that is merely safe-but-absent is reported as missing
        with self.assertRaisesRegex(DocumentError, "no ZIP entry named"):
            zip_read(archive, "windows/path.txt")

    def test_duplicate_entry_names_are_refused_not_silently_first(self):
        # An archive can hold the same name twice; reading the first would let an earlier decoy shadow the
        # governing entry. The reader must refuse and say which indexes the model can choose between.
        buffer = io.BytesIO()
        with warnings.catch_warnings():  # zipfile warns on its own about writing a duplicate name
            warnings.simplefilter("ignore")
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("spec.txt", b"decoy: see the other entry")
                archive.writestr("spec.txt", b"THE GOVERNING SPECIFICATION" * 40)
        archive = buffer.getvalue()
        self.assertEqual([e["size"] for e in zip_entries(archive)], [26, 1080])
        self.assertEqual([e["index"] for e in zip_entries(archive)], [0, 1])
        self.assertNotIn("duplicate_of", zip_entries(archive)[0])
        self.assertEqual(zip_entries(archive)[1]["duplicate_of"], 0)
        with self.assertRaisesRegex(DocumentError, r"2 ZIP entries share the name 'spec.txt' at indexes \[0, 1\]"):
            zip_read(archive, "spec.txt")
        # ... and the model can then address the one it judged governing.
        self.assertEqual(zip_read(archive, "spec.txt", index=1), b"THE GOVERNING SPECIFICATION" * 40)
        self.assertEqual(len(zip_read(archive, "spec.txt", index=1)), 1080)
        self.assertEqual(zip_read(archive, None, index=0), b"decoy: see the other entry")
        with self.assertRaisesRegex(DocumentError, "entry index 9 out of range"):
            zip_read(archive, None, index=9)
        with self.assertRaisesRegex(DocumentError, "index True must be None"):
            zip_read(archive, "spec.txt", index=True)
        # an index is no way around the name guard (that helper's lone local header is unreadable, so
        # zipfile-written archives do the name checking: the raw-header refusal is covered separately)
        with self.assertRaisesRegex(DocumentError, "refusing unsafe ZIP entry name"):
            zip_read(zip_bytes([("../shadow.txt", b"s")]), None, index=0)
        # a unique name still reads as before
        self.assertEqual(zip_read(zip_bytes(self.ARCHIVE), "docs/report.txt"), b"alpha-report")

    def test_index_must_address_the_named_entry(self):
        # `index` narrows a duplicated name; pointing it at an unrelated entry must not silently return
        # those bytes under the requested name (evidence would misattribute them).
        archive = zip_bytes([("first.txt", b"first"), ("second.txt", b"second")])
        with self.assertRaisesRegex(
                DocumentError, r"entry at index 1 is named 'second.txt', not 'first.txt'"):
            zip_read(archive, "first.txt", index=1)
        # the correct pairing still works (it is just how a unique entry reads when addressed by index)
        self.assertEqual(zip_read(archive, "first.txt", index=0), b"first")
        self.assertEqual(zip_read(archive, "second.txt", index=1), b"second")
        # a duplicate's chosen index must be one of that duplicate's own indexes
        buffer = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as w:
                w.writestr("spec.txt", b"decoy")
                w.writestr("other.txt", b"unrelated")
        bad = buffer.getvalue()
        with self.assertRaisesRegex(
                DocumentError, r"entry at index 1 is named 'other.txt', not 'spec.txt'"):
            zip_read(bad, "spec.txt", index=1)

    def test_names_that_look_awkward_but_are_safe(self):
        safe = ("a..b.txt", "..writable.txt", "dots/..hidden", "sp ace/note.txt", "end..")
        archive = zip_bytes([(name, name.encode()) for name in safe])
        self.assertEqual([e["name"] for e in zip_entries(archive)], list(safe))
        self.assertTrue(all(e["safe"] for e in zip_entries(archive)))
        for name in safe:
            self.assertEqual(zip_read(archive, name), name.encode())

    def test_the_reader_guard_holds_for_names_the_writer_cannot_produce(self):
        with self.assertRaisesRegex(DocumentError, "refusing unsafe ZIP entry name"):
            zip_read(raw_local_header_zip("../evil.txt", b"evil"), "../evil.txt")

    def test_a_writer_cannot_hide_an_unsafe_name_in_its_own_archive(self):
        archive = zip_bytes([("../via-writer.txt", b"z")])  # zipfile keeps the name as written
        self.assertEqual([e["name"] for e in zip_entries(archive)], ["../via-writer.txt"])
        self.assertFalse(zip_entries(archive)[0]["safe"])
        with self.assertRaisesRegex(DocumentError, "refusing unsafe ZIP entry name"):
            zip_read(archive, "../via-writer.txt")

    def test_not_a_zip_and_truncated_zip(self):
        # zipfile reads a lone EOCD as an empty archive, so the reader reports the entry as missing
        self.assertEqual(zip_entries(b"PK\x05\x06" + b"\x00" * 30), [])
        with self.assertRaisesRegex(DocumentError, "no ZIP entry named"):
            zip_read(b"PK\x05\x06" + b"\x00" * 30, "anything.txt")
        # bytes with no usable archive structure are refused, whether or not they carry the magic
        for bad in (make_png(40, 30), b"", b"garbage" * 20, b"PK\x03\x04but not really a zip"):
            with self.assertRaisesRegex(DocumentError, "not a readable ZIP"):
                zip_entries(bad)
            with self.assertRaisesRegex(DocumentError, "not a readable ZIP"):
                zip_read(bad, "anything.txt")
        truncated = zip_bytes(self.ARCHIVE)[:12]
        self.assertEqual(sniff(truncated), "zip")
        with self.assertRaisesRegex(DocumentError, "not a readable ZIP"):
            zip_entries(truncated)

    def test_entry_point_honours_the_document_limit(self):
        oversized = zip_bytes([("a.txt", b"1")]) + b"y" * DEFAULT_DOC_LIMIT
        with self.assertRaisesRegex(DocumentError, "exceeds the limit"):
            zip_entries(oversized)


if __name__ == "__main__":
    unittest.main()
