"""Byte-for-byte comparison of the CrossPoint cache with the site's own JavaScript.

usage: python3 parity.py ORIGINAL_BOOKS_DIR CONVERTED_WITH_CACHE_DIR
Needs node and `npm install` in tests/ref. Exits non-zero on any difference.
"""
import io
import random
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from epub_optimize import build_book_bin, pack_pxc  # noqa: E402

REF = str(HERE / "ref" / "ref.js")
src_dir, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
tmp = Path(tempfile.mkdtemp())
ok = True

# book.bin: both sides get the original zip + the rewritten OPF from our output
for book, opf in [("Book A (EPUB3).epub", "OEBPS/content.opf"), ("Book B (EPUB2).epub", "content.opf")]:
    ours = zipfile.ZipFile(out_dir / book)
    new_opf = ours.read(opf).decode()
    (tmp / "opf.xml").write_text(new_opf, encoding="utf-8")
    subprocess.run(["node", REF, "bookbin", str(src_dir / book), opf, str(tmp / "opf.xml"), str(tmp / "ref.bin")],
                   check=True, stdout=subprocess.DEVNULL)
    ref = (tmp / "ref.bin").read_bytes()
    mine = build_book_bin(zipfile.ZipFile(src_dir / book), opf, new_opf)[0]
    inzip = ours.read("META-INF/crosspoint/book.bin")
    same = mine == ref == inzip
    ok &= same
    print("book.bin  %-22s %s" % (book, "identical" if same else "DIFFERENT"))

# .pxc: random noise and real image content, odd widths, both dither modes
cover = zipfile.ZipFile(out_dir / "Book A (EPUB3).epub").read("OEBPS/images/cover.jpg")
for w, h in [(7, 5), (464, 763), (301, 117)]:
    for kind in ("random", "image"):
        if kind == "random":
            rnd = random.Random(w * h)
            g = bytes(rnd.randrange(256) for _ in range(w * h))
        else:
            g = Image.open(io.BytesIO(cover)).convert("L").resize((w, h)).tobytes()
        (tmp / "g.raw").write_bytes(g)
        for mode in ("improved", "ordered"):
            subprocess.run(["node", REF, "pxc", str(tmp / "g.raw"), str(w), str(h), mode, str(tmp / "ref.pxc")],
                           check=True)
            same = pack_pxc(g, w, h, mode) == (tmp / "ref.pxc").read_bytes()
            ok &= same
            print("pxc %dx%d %-6s %-8s %s" % (w, h, kind, mode, "identical" if same else "DIFFERENT"))

print("parity: " + ("all identical" if ok else "MISMATCH"))
sys.exit(0 if ok else 1)
