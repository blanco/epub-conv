"""Strict structural checks on converted EPUBs.

usage: python3 verify_epub.py [--max WxH] OUT.epub [OUT2.epub ...]
Exits non-zero if any check fails.
"""
import argparse
import io
import posixpath
import re
import sys
import zipfile
from pathlib import Path
from urllib.parse import unquote

from PIL import Image, ImageChops

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from epub_optimize import is_baseline_jpeg  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--max", default="528x792", help="largest allowed image size (default X3: 528x792)")
ap.add_argument("epubs", nargs="+")
args = ap.parse_args()
MAX_W, MAX_H = (int(v) for v in args.max.lower().split("x"))

fails = 0


def check(cond, msg):
    global fails
    if not cond:
        fails += 1
        print("  FAIL:", msg)


for path in args.epubs:
    print(path)
    z = zipfile.ZipFile(path)
    infos = z.infolist()
    check(z.testzip() is None, "zip CRC")
    check(infos[0].filename == "mimetype" and infos[0].compress_type == 0 and not infos[0].extra,
          "mimetype must be first, stored, no extra field")
    check(z.read("mimetype") == b"application/epub+zip", "mimetype content")
    names = set(z.namelist())
    check(len(names) == len(infos), "duplicate entries")

    cont = z.read("META-INF/container.xml").decode()
    opf = re.search(r'full-path="([^"]+)"', cont).group(1)
    opf_txt = z.read(opf).decode("utf-8")
    base = posixpath.dirname(opf)
    for m in re.finditer(r"<(?:\w+:)?item\b[^>]*>", opf_txt):
        t = m.group(0)
        href = re.search(r'href="([^"]+)"', t).group(1)
        mt = re.search(r'media-type="([^"]+)"', t).group(1)
        full = posixpath.normpath(posixpath.join(base, unquote(href)))
        check(full in names, "manifest item missing: " + full)
        if href.lower().endswith(".jpg"):
            check(mt == "image/jpeg", "media-type for %s is %s" % (href, mt))
        check(not re.search(r'properties="[^"]*\bsvg\b', t), "svg property left: " + t)

    for n in names:
        low = n.lower()
        data = z.read(n)
        if re.search(r"\.(xhtml|html|htm)$", low):
            txt = data.decode("utf-8")  # must be valid UTF-8
            check("max-width:100%;height:auto}body" in txt or "</head>" not in txt, "style not injected: " + n)
            check("<svg" not in txt or "xlink:href" not in txt, "svg image wrapper left in " + n)
            for ref in re.findall(r'(?:src|xlink:href)="([^"]+)"', txt):
                full = posixpath.normpath(posixpath.join(posixpath.dirname(n), unquote(ref.split("#")[0])))
                check(full in names, "%s: broken image ref %s" % (n, ref))
            dec = re.search(r'<\?xml[^>]*encoding="([^"]+)"', txt)
            check(not dec or dec.group(1).lower() == "utf-8", "%s declares %s" % (n, dec and dec.group(1)))
        elif low.endswith(".css"):
            for ref in re.findall(r'url\(["\']?([^"\')]+)', data.decode()):
                full = posixpath.normpath(posixpath.join(posixpath.dirname(n), ref))
                check(full in names, "css ref broken: " + ref)
        elif low.endswith(".jpg"):
            im = Image.open(io.BytesIO(data))
            check(im.width <= MAX_W and im.height <= MAX_H, "%s is %sx%s" % (n, im.width, im.height))
            check(is_baseline_jpeg(data), n + " not baseline")
            if im.mode == "RGB":
                r, g, b = im.split()
                spread = max(ImageChops.difference(r, g).getextrema()[1], ImageChops.difference(g, b).getextrema()[1])
                check(spread <= 8, n + " not gray")
        elif low.endswith(".pxc"):
            w, h = int.from_bytes(data[:2], "little"), int.from_bytes(data[2:4], "little")
            check(len(data) == 4 + (w + 3) // 4 * h and w <= 464 and h <= 763, n + " bad pxc size")
            check(n[:-4] + ".jpg" in names, n + " has no jpg")
            check(z.getinfo(n).compress_type == 0, n + " not stored")
    if "META-INF/crosspoint/book.bin" in names:
        check(all((n[:-4] + ".pxc") in names for n in names if n.lower().endswith(".jpg")), "missing pxc")

print("verify: %d failure(s)" % fails)
sys.exit(1 if fails else 0)
