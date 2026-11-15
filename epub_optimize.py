#!/usr/bin/env python3
"""
epub_optimize.py - batch EPUB optimizer for small e-ink readers (480x800).

A local port of the batch mode of https://epubdev.pages.dev ("EPUB Optimizer Pro"):

  * every PNG/GIF/WEBP/BMP/JPG/JPEG image is flattened onto white, scaled down
    to fit 480x800 (never up), converted to grayscale and re-encoded as a
    baseline JPEG (mozjpeg's cjpeg is used when it is installed, Pillow otherwise)
  * image references in XHTML, CSS, NCX and the OPF are renamed to .jpg
  * SVG-wrapped images (common calibre cover pages) are replaced with <img>
  * a small stylesheet keeping images/tables/pre inside the screen is injected
  * OPF media-types, "svg" properties, the cover <meta> and the NCX dtb:uid are fixed
  * optionally (--crosspoint-cache) the CrossPoint firmware cache is pre-built:
    META-INF/crosspoint/book.bin plus a 2-bit .pxc next to every .jpg

Usage:
  python3 epub_optimize.py ~/Books                 # writes ~/Books/optimized/*.epub
  python3 epub_optimize.py ~/Books -o ~/Books/x4 -q 75 --crosspoint-cache
"""
from __future__ import annotations

import argparse
import html.entities
import io
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from array import array
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover
    sys.exit("Pillow is required:  python3 -m pip install Pillow   (or run epub-optimize.sh)")

Image.MAX_IMAGE_PIXELS = None  # huge scans are fine, we only shrink them

SCREEN_W, SCREEN_H = 480, 800          # target screen (site: Ot, St)
PXC_W, PXC_H = 464, 763                # CrossPoint image cache box (site: ri, ni)

IMG_RE = re.compile(r"\.(png|gif|webp|bmp|jpg|jpeg)$", re.I)
RENAME_RE = re.compile(r"\.(png|gif|webp|bmp|jpeg)$", re.I)
HTML_RE = re.compile(r"\.(xhtml|html|htm)$", re.I)

INJECT_CSS = ('<style type="text/css">img,svg{max-width:100%;height:auto}'
              'body{overflow-wrap:break-word}table{max-width:100%;table-layout:fixed}'
              'pre,code{white-space:pre-wrap;word-wrap:break-word}*{box-sizing:border-box}</style>')

COVER_PAGE = ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
              '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
              'lang="en" xml:lang="en">\n<head><meta content="text/html; charset=UTF-8" '
              'http-equiv="default-style"/><title>Cover</title></head>\n<body><section epub:type="cover">'
              '<img style="max-width:100%;height:auto" alt="Cover" src="{src}"/></section></body>\n</html>')


def js_round(x: float) -> int:
    """Math.round semantics (halves round up)."""
    return int(math.floor(x + 0.5))


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# --------------------------------------------------------------------------- text

def decode_text(data: bytes) -> tuple[str, bool]:
    """Site's Ut(): strip BOM, strict UTF-8, else sniff a declared charset."""
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    try:
        return data.decode("utf-8"), True
    except UnicodeDecodeError:
        pass
    head = data[:512].decode("ascii", errors="replace")
    m = re.search(r"""encoding=["']([^"']+)["']""", head, re.I) or \
        re.search(r"""charset=["']?([^"'\s;]+)""", head, re.I)
    enc = m.group(1).lower() if m else "windows-1252"
    try:
        return data.decode(enc, errors="replace"), False
    except LookupError:
        return data.decode("iso-8859-1", errors="replace"), False


def to_utf8(text: str, was_utf8: bool) -> bytes:
    # We always write UTF-8; if the source declared another encoding, say so honestly.
    if not was_utf8:
        text = re.sub(r"""(<\?xml[^?]*encoding=["'])[^"']+(["'])""", r"\1utf-8\2", text, count=1)
        text = re.sub(r"""(<meta[^>]*charset=["']?)[^"'\s;>]+""", r"\1utf-8", text, count=1, flags=re.I)
    return text.encode("utf-8")


def rename_refs(text: str, renames: dict[str, str]) -> str:
    """Site behaviour: plain substring replace of every renamed image's basename."""
    for old, new in renames.items():
        ob, nb = old.rsplit("/", 1)[-1], new.rsplit("/", 1)[-1]
        if ob in text:
            text = text.replace(ob, nb)
    return text


# --------------------------------------------------------------------------- XHTML fixes

def fix_svg_cover(c: str) -> str:
    """Site's nr(): turn a calibre-style SVG cover page into a plain <img> page."""
    if not (("<svg" in c or "<svg:" in c) and "xlink:href" in c):
        return c
    if "calibre:cover" not in c and 'name="cover"' not in c and "<title>Cover</title>" not in c:
        return c
    m = re.search(r"""<(?:\w+:)?image\b[^>]*?(?:xlink:)?href=["']([^"']+)["']""", c) or \
        re.search(r"""xlink:href=["']([^"']+)["']""", c)
    return COVER_PAGE.replace("{src}", m.group(1)) if m else c


SVG_IMG_RE = re.compile(
    r"""<(?:svg:)?svg\b[^>]*>(?:(?!</(?:svg:)?svg>)[\s\S])*?<(?:svg:)?image\b([^>]*)\/?>\s*(?:</(?:svg:)?image>\s*)?</(?:svg:)?svg>""",
    re.I)


def fix_svg_images(c: str) -> str:
    """Site's ir(): replace <svg><image xlink:href=..></svg> wrappers with <img>."""
    if not (("<svg" in c or "<svg:" in c) and "xlink:href" in c):
        return c

    def repl(m: re.Match) -> str:
        attrs = m.group(1)
        h = re.search(r"""(?:xlink:)?href=["']([^"']+)["']""", attrs)
        if not h:
            return m.group(0)
        extra = ""
        for a in ("width", "height"):
            v = re.search(r"""\b%s=["']([^"']+)["']""" % a, attrs)
            if v:
                extra += ' %s="%s"' % (a, v.group(1))
        return '<img style="max-width:100%;height:auto" src="{}" alt=""{} />'.format(h.group(1), extra)

    return SVG_IMG_RE.sub(repl, c)


# --------------------------------------------------------------------------- OPF / NCX fixes

def _attr(tag: str, name: str) -> str | None:
    m = re.search(r"""\s%s\s*=\s*(["'])(.*?)\1""" % re.escape(name), tag, re.S)
    return m.group(2) if m else None


def _set_attr(tag: str, name: str, value: str | None) -> str:
    pat = re.compile(r"""\s%s\s*=\s*(["']).*?\1""" % re.escape(name), re.S)
    if value is None:
        return pat.sub("", tag, count=1)
    if pat.search(tag):
        return pat.sub(lambda m: ' %s="%s"' % (name, value), tag, count=1)
    return re.sub(r"\s*/?>$", lambda m: ' %s="%s"%s' % (name, value, m.group(0)), tag, count=1)


ITEM_RE = re.compile(r"<(?:\w+:)?item\b[^>]*>", re.I)


def fix_opf(opf: str) -> str:
    """Site's cr() + Kn(): media types, svg properties, cover meta."""
    def fix_item(m: re.Match) -> str:
        tag = m.group(0)
        href = _attr(tag, "href") or ""
        mt = _attr(tag, "media-type") or ""
        if href.endswith(".jpg") and re.match(r"^image/(png|gif|webp|bmp)$", mt):
            tag = _set_attr(tag, "media-type", "image/jpeg")
        props = _attr(tag, "properties")
        if props is not None and "svg" in props.split():
            rest = " ".join(p for p in props.split() if p != "svg")
            tag = _set_attr(tag, "properties", rest or None)
        return tag

    opf = ITEM_RE.sub(fix_item, opf)

    # cover meta (Kn): cover-image item, else an image item whose id/href mentions "cover"
    cover_id = None
    items = [m.group(0) for m in ITEM_RE.finditer(opf)]
    for t in items:
        if (_attr(t, "media-type") or "").startswith("image/") and "cover-image" in (_attr(t, "properties") or ""):
            cover_id = _attr(t, "id") or ""
            break
    if cover_id is None:
        for t in items:
            i, h = _attr(t, "id") or "", _attr(t, "href") or ""
            if (_attr(t, "media-type") or "").startswith("image/") and ("cover" in i.lower() or "cover" in h.lower()):
                cover_id = i
                break
    if cover_id is None:
        return opf
    meta = None
    for m in re.finditer(r"<(?:\w+:)?meta\b[^>]*>", opf, re.I):
        if _attr(m.group(0), "name") == "cover":
            meta = m
            break
    if meta:
        if _attr(meta.group(0), "content") == cover_id:
            return opf
        new = _set_attr(meta.group(0), "content", esc(cover_id))
        return opf[:meta.start()] + new + opf[meta.end():]
    close = re.search(r"</((?:\w+:)?)metadata\s*>", opf)
    if not close:
        return opf
    ins = '<%smeta name="cover" content="%s"/>\n  ' % (close.group(1), esc(cover_id))
    return opf[:close.start()] + ins + opf[close.start():]


def opf_unique_id(opf: str) -> str | None:
    """Site's fr()."""
    m = re.search(r"""<(?:\w+:)?package\b[^>]*unique-identifier=["']([^"']+)["']""", opf, re.I)
    if m:
        v = re.search(r"""<(?:\w+:)?identifier\b[^>]*\bid=["']%s["'][^>]*>([^<]*)<""" % re.escape(m.group(1)), opf, re.I)
        if v and v.group(1).strip():
            return v.group(1).strip()
    v = re.search(r"<(?:\w+:)?identifier\b[^>]*>([^<]*)<", opf, re.I)
    return v.group(1).strip() if v and v.group(1).strip() else None


def fix_ncx_uid(ncx: str, uid: str | None) -> str:
    """Site's ur(): make the NCX dtb:uid match the OPF unique identifier."""
    if not uid:
        return ncx

    def repl(m: re.Match) -> str:
        tag = m.group(0)
        return _set_attr(tag, "content", esc(uid)) if _attr(tag, "name") == "dtb:uid" else tag
    return re.sub(r"<(?:\w+:)?meta\b[^>]*>", repl, ncx, flags=re.I)


# --------------------------------------------------------------------------- images

class JpegEncoder:
    def __init__(self, quality: int, true_gray: bool, use_mozjpeg: bool = True):
        self.quality = max(1, min(100, int(quality)))
        self.true_gray = true_gray
        self.cjpeg = find_mozjpeg() if use_mozjpeg else None

    @property
    def name(self) -> str:
        return ("mozjpeg (%s)" % self.cjpeg) if self.cjpeg else "Pillow/libjpeg"

    def encode(self, gray: Image.Image) -> bytes:
        img = gray if self.true_gray else gray.convert("RGB")
        if self.cjpeg:
            buf = io.BytesIO()
            img.save(buf, format="PPM")  # writes PGM for "L", PPM for "RGB"
            # Same knobs the site's encoder actually ends up with: baseline, quant table 3,
            # input smoothing 2, optimized Huffman, 4:2:0, trellis (mozjpeg default).
            cmd = [self.cjpeg, "-quality", str(self.quality), "-baseline", "-optimize",
                   "-quant-table", "3", "-smooth", "2"]
            if not self.true_gray:
                cmd += ["-sample", "2x2"]
            try:
                out = subprocess.run(cmd, input=buf.getvalue(), capture_output=True, check=True).stdout
                if is_baseline_jpeg(out):
                    return out
            except (OSError, subprocess.CalledProcessError):
                pass
        buf = io.BytesIO()
        kw = {} if self.true_gray else {"subsampling": "4:2:0"}
        img.save(buf, format="JPEG", quality=self.quality, optimize=True, progressive=False, **kw)
        return buf.getvalue()


def find_mozjpeg() -> str | None:
    env = os.environ.get("MOZJPEG_CJPEG")
    cands = [env] if env else []
    cands += ["/opt/homebrew/opt/mozjpeg/bin/cjpeg", "/usr/local/opt/mozjpeg/bin/cjpeg",
              "/opt/mozjpeg/bin/cjpeg"]
    for c in cands:
        if c and os.access(c, os.X_OK):
            return c
    c = shutil.which("cjpeg")
    if c:  # only accept a cjpeg that is really mozjpeg
        try:
            r = subprocess.run([c, "-version"], capture_output=True, text=True, timeout=5)
            if "mozjpeg" in (r.stdout + r.stderr).lower():
                return c
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def is_baseline_jpeg(b: bytes) -> bool:
    """True if the JPEG starts with SOI and its first SOF marker is SOF0/SOF1 (not progressive)."""
    if len(b) < 4 or b[0] != 0xFF or b[1] != 0xD8:
        return False
    i = 2
    while i + 4 <= len(b):
        if b[i] != 0xFF:
            return False
        marker = b[i + 1]
        if marker in (0xC0, 0xC1):
            return True
        if marker in (0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            return False
        seg = struct.unpack(">H", b[i + 2:i + 4])[0]
        i += 2 + seg
    return False


def open_image(data: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(data))
    im.seek(0)  # first frame of animated GIF/WEBP
    try:
        im = ImageOps.exif_transpose(im)  # browsers honour EXIF orientation too
    except Exception:
        pass
    return im


def to_screen_gray(data: bytes) -> Image.Image:
    """Flatten on white, fit inside 480x800 (no upscaling), grayscale (BT.601 weights)."""
    im = open_image(data)
    if im.mode in ("I;16", "I;16B", "I;16L", "I"):
        im = im.point(lambda v: v / 256).convert("L")
    rgba = im.convert("RGBA")
    flat = Image.new("RGB", rgba.size, (255, 255, 255))
    flat.paste(rgba, mask=rgba.getchannel("A"))
    w, h = flat.size
    s = min(SCREEN_W / w, SCREEN_H / h, 1.0)
    nw, nh = max(1, js_round(w * s)), max(1, js_round(h * s))
    if (nw, nh) != (w, h):
        flat = flat.resize((nw, nh), Image.LANCZOS, reducing_gap=3.0)
    return flat.convert("L")


# --------------------------------------------------------------------------- CrossPoint .pxc

def _srgb_to_linear(v: int) -> float:
    k = v / 255
    return k / 12.92 if k <= 0.04045 else ((k + 0.055) / 1.055) ** 2.4


_LIN = array("f", (_srgb_to_linear(v) for v in range(256)))   # Float32Array, like the site
_LEV = [0.0, _LIN[85], _LIN[170], 1.0]
_THR = [(_LEV[0] + _LEV[1]) / 2, (_LEV[1] + _LEV[2]) / 2, (_LEV[2] + _LEV[3]) / 2]
_BAYER = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]


def _dither_fs(gray: bytes, w: int, h: int) -> bytearray:
    """Site's oi(): serpentine Floyd-Steinberg to 4 levels in linear light."""
    out = bytearray(w * h)
    a = array("f", (_LIN[v] for v in gray))
    t0, t1, t2 = _THR
    lev = _LEV
    for r in range(h):
        row, nxt = r * w, (r + 1) * w
        has_next = r + 1 < h
        if r & 1 == 0:
            rng, step = range(w), 1
        else:
            rng, step = range(w - 1, -1, -1), -1
        for b in rng:
            p = row + b
            g = a[p]
            g = 0.0 if g < 0 else (1.0 if g > 1 else g)
            i = 0 if g < t0 else (1 if g < t1 else (2 if g < t2 else 3))
            out[p] = i
            f = g - lev[i]
            o, u = b + step, b - step
            if 0 <= o < w:
                a[row + o] += f * 7 / 16
            if has_next:
                if 0 <= u < w:
                    a[nxt + u] += f * 3 / 16
                a[nxt + b] += f * 5 / 16
                if 0 <= o < w:
                    a[nxt + o] += f * 1 / 16
    return out


def _dither_ordered(v: int, x: int, y: int) -> int:
    a = v + (_BAYER[y & 3][x & 3] - 8) * 5
    a = 0 if a < 0 else (255 if a > 255 else a)
    return 0 if a < 64 else (1 if a < 128 else (2 if a < 192 else 3))


def build_pxc(jpeg: bytes, mode: str = "improved") -> bytes:
    """Site's sr() + or(): 2 bits/pixel image cache, header = u16 width, u16 height."""
    im = open_image(jpeg).convert("RGB")
    s = min(PXC_W / im.width, PXC_H / im.height, 1.0)
    w, h = js_round(im.width * s), js_round(im.height * s)
    if (w, h) != im.size:
        im = im.resize((w, h), Image.BILINEAR)
    red = im.getchannel("R").tobytes()           # site reads data[p*4] (the R channel)
    return pack_pxc(red, w, h, mode)


def pack_pxc(red: bytes, w: int, h: int, mode: str = "improved") -> bytes:
    rb = (w + 3) // 4
    out = bytearray(4 + rb * h)
    struct.pack_into("<HH", out, 0, w & 0xFFFF, h & 0xFFFF)
    lv = _dither_fs(red, w, h) if mode == "improved" else None
    pos = 4
    for y in range(h):
        for bx in range(rb):
            g = 0
            for i in range(4):
                x = bx * 4 + i
                if x < w:
                    q = lv[y * w + x] if lv is not None else _dither_ordered(red[y * w + x], x, y)
                    g |= q << (6 - i * 2)
            out[pos] = g
            pos += 1
    return bytes(out)


# --------------------------------------------------------------------------- CrossPoint book.bin

_XML5 = {"amp", "lt", "gt", "quot", "apos"}


def _parse_xml(text: str) -> ET.Element:
    text = re.sub(r"&([A-Za-z][A-Za-z0-9]*);",
                  lambda m: m.group(0) if m.group(1) in _XML5 or m.group(1) not in html.entities.name2codepoint
                  else "&#%d;" % html.entities.name2codepoint[m.group(1)], text)
    return ET.fromstring(text.encode("utf-8"))


def _ln(el: ET.Element) -> str:
    t = el.tag if isinstance(el.tag, str) else ""
    return t.rsplit("}", 1)[-1]


def _all(root: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in root.iter() if _ln(e) == name]


def _text(el: ET.Element) -> str:
    return "".join(el.itertext()).strip()


def _get(el: ET.Element, name: str) -> str:
    return el.get(name) or ""


def build_book_bin(src: zipfile.ZipFile, opf_path: str, opf_text: str) -> tuple[bytes, int, int]:
    """Site's ar(): CrossPoint metadata/spine/TOC cache (version 5)."""
    def norm(p: str) -> str:
        out: list[str] = []
        for seg in p.split("/"):
            if seg == "..":
                if out:
                    out.pop()
            elif seg:
                out.append(seg)
        return "/".join(out)

    def split_anchor(p: str) -> tuple[str, str]:
        i = p.find("#")
        return (p[:i], p[i + 1:]) if i != -1 else (p, "")

    def s(v: str) -> bytes:
        e = v.encode("utf-8")
        return struct.pack("<I", len(e) & 0xFFFFFFFF) + e

    names = {n: n for n in src.namelist()}
    lower = {n.lower(): n for n in reversed(src.namelist())}

    def find(n: str) -> zipfile.ZipInfo | None:
        real = names.get(n) or lower.get(n.lower())
        return src.getinfo(real) if real else None

    base = opf_path[:opf_path.rfind("/") + 1] if "/" in opf_path else ""
    p = _parse_xml(opf_text)
    title = author = lang = cover = text_ref = cover_page = meta_cover = ncx = nav = ""
    t = _all(p, "title")
    if t:
        title = _text(t[0])
    t = _all(p, "creator")
    if t:
        author = _text(t[0])
    t = _all(p, "language")
    if t:
        lang = _text(t[0])
    for m in _all(p, "meta"):
        if m.get("name") == "cover" and not meta_cover:
            meta_cover = _get(m, "content")
    by_id: dict[str, str] = {}
    for it in _all(p, "item"):
        iid, href, mt = _get(it, "id"), _get(it, "href"), _get(it, "media-type")
        props = _get(it, "properties").split()
        path = norm(base + href)
        by_id[iid] = path
        if mt == "application/x-dtbncx+xml" and not ncx:
            ncx = path
        if "nav" in props and not nav:
            nav = path
        if "cover-image" in props and not cover:
            cover = path
    if not cover and meta_cover:
        cover = by_id.get(meta_cover, "")
    spine = [by_id[r.get("idref")] for r in _all(p, "itemref") if r.get("idref") and r.get("idref") in by_id]
    for ref in _all(p, "reference"):
        typ, path = _get(ref, "type"), norm(base + _get(ref, "href"))
        if typ == "text" and not text_ref:
            text_ref = path
        if typ in ("cover", "cover-page") and not cover_page:
            cover_page = path
    if not cover and cover_page:
        zi = find(cover_page)
        if zi:
            page = src.read(zi).decode("utf-8", errors="replace")
            d = cover_page[:cover_page.rfind("/") + 1] if "/" in cover_page else ""
            for key in ('xlink:href="', 'src="'):
                found = False
                c = page.find(key)
                while c != -1:
                    c += len(key)
                    e = page.find('"', c)
                    if e != -1:
                        val = page[c:e]
                        if re.search(r"\.(jpg|jpeg|png|gif)$", val, re.I):
                            cover = norm(d + val)
                            found = True
                            break
                    c = page.find(key, c)
                if found:
                    break

    toc: list[dict] = []
    if nav:
        zi = find(nav)
        if zi:
            ndir = nav[:nav.rfind("/") + 1] if "/" in nav else ""
            try:
                root = _parse_xml(src.read(zi).decode("utf-8", errors="replace"))
            except ET.ParseError:
                root = None

            def find_toc(el: ET.Element):
                typ = el.get("{http://www.idpf.org/2007/ops}type") or el.get("epub:type") or el.get("type") or ""
                if _ln(el) == "nav" and typ == "toc":
                    return el
                for ch in el:
                    r = find_toc(ch)
                    if r is not None:
                        return r
                return None

            def walk(ol: ET.Element, depth: int):
                for li in ol:
                    if _ln(li) != "li":
                        continue
                    ttl = href = ""
                    subs = []
                    for ch in li:
                        if _ln(ch) == "a" and not ttl:
                            href, ttl = _get(ch, "href"), _text(ch)
                        elif _ln(ch) == "ol":
                            subs.append(ch)
                    if ttl and href:
                        h, anc = split_anchor(norm(ndir + href))
                        toc.append({"title": ttl, "href": h, "anchor": anc, "depth": depth})
                    for sub in subs:
                        walk(sub, depth + 1)

            tnode = find_toc(root) if root is not None else None
            if tnode is not None:
                for ch in tnode:
                    if _ln(ch) == "ol":
                        walk(ch, 1)
                        break
    if not toc and ncx:
        zi = find(ncx)
        if zi:
            try:
                root = _parse_xml(src.read(zi).decode("utf-8", errors="replace"))
            except ET.ParseError:
                root = None
            nav_map = next((c for c in root if _ln(c) == "navMap"), None) if root is not None else None

            def walk_ncx(el: ET.Element, depth: int):
                for np_ in el:
                    if _ln(np_) != "navPoint":
                        continue
                    lbl = srcv = ""
                    for ch in np_:
                        if _ln(ch) == "navLabel":
                            tx = next((x for x in ch if _ln(x) == "text"), None)
                            if tx is not None:
                                lbl = _text(tx)
                        elif _ln(ch) == "content":
                            srcv = _get(ch, "src")
                    if lbl and srcv:
                        h, anc = split_anchor(norm(base + srcv))   # site resolves against the OPF dir
                        toc.append({"title": lbl, "href": h, "anchor": anc, "depth": depth})
                    walk_ncx(np_, depth + 1)

            if nav_map is not None:
                walk_ncx(nav_map, 1)

    def i16(v: int) -> bytes:
        v = max(-32768, v)
        v &= 0xFFFF
        return struct.pack("<H", v)

    spine_idx = {}
    for i, path in enumerate(spine):
        spine_idx[path] = i                     # Object.fromEntries: last wins
    toc_spine = [spine_idx.get(e["href"], -1) for e in toc]
    first_toc = [-1] * len(spine)
    for ti, si in enumerate(toc_spine):
        if si != -1 and first_toc[si] == -1:
            first_toc[si] = ti
    cur = -1
    for i in range(len(spine)):
        if first_toc[i] != -1:
            cur = first_toc[i]
        else:
            first_toc[i] = cur
    total = 0
    cum = []
    for path in spine:
        zi = find(path)
        total += zi.file_size if zi else 0
        cum.append(min(total, 0xFFFFFFFF))
    spine_recs = [s(path) + struct.pack("<I", cum[i]) + i16(first_toc[i]) for i, path in enumerate(spine)]
    toc_recs = [s(e["title"]) + s(e["href"]) + s(e["anchor"]) + bytes([e["depth"] & 0xFF]) + i16(toc_spine[i])
                for i, e in enumerate(toc)]
    meta = s(title) + s(author) + s(lang) + s(cover) + s(text_ref)
    meta_end = 9 + len(meta)
    off = meta_end + 4 * (len(spine) + len(toc))
    offsets = []
    for rec in spine_recs + toc_recs:
        offsets.append(off)
        off += len(rec)
    header = struct.pack("<BIHH", 5, meta_end, len(spine) & 0xFFFF, len(toc) & 0xFFFF)
    table = b"".join(struct.pack("<I", o & 0xFFFFFFFF) for o in offsets)
    return header + meta + table + b"".join(spine_recs) + b"".join(toc_recs), len(spine), len(toc)


# --------------------------------------------------------------------------- one EPUB

def optimize_epub(src_path: Path, dst_path: Path, opts: dict) -> dict:
    t0 = time.time()
    enc = JpegEncoder(opts["quality"], opts["true_gray"], opts["mozjpeg"])
    warnings: list[str] = []
    with zipfile.ZipFile(src_path) as zin:
        infos = [i for i in zin.infolist() if not i.is_dir()]
        names = {i.filename for i in infos}
        if "mimetype" not in names or b"application/epub+zip" not in zin.read("mimetype"):
            warnings.append("missing or unexpected mimetype file")

        out: list[tuple[str, bytes, int]] = []   # (name, data, compress_type), in write order
        renames: dict[str, str] = {}
        n_img = 0

        # 1) images
        for zi in infos:
            n = zi.filename
            if n == "mimetype" or not IMG_RE.search(n):
                continue
            n_img += 1
            raw = zin.read(zi)
            target = RENAME_RE.sub(".jpg", n)
            if target != n and target in names:  # a .jpg of that name already exists: keep the extension
                target = n
            try:
                data = enc.encode(to_screen_gray(raw))
                out.append((target, data, zipfile.ZIP_STORED))
                if target != n:
                    renames[n] = target
            except Exception as e:  # undecodable image: keep it untouched (and its references intact)
                why = "not a readable image" if "cannot identify image" in str(e) else str(e)
                warnings.append("kept %s unchanged (%s)" % (n.rsplit("/", 1)[-1], why))
                out.append((n, raw, zipfile.ZIP_STORED))

        # 2) XHTML
        opf_name = opf_raw = None
        for zi in infos:
            n = zi.filename
            if HTML_RE.search(n):
                txt, utf8 = decode_text(zin.read(zi))
                txt = fix_svg_images(fix_svg_cover(txt))
                txt = rename_refs(txt, renames)
                if "</head>" in txt:
                    txt = txt.replace("</head>", INJECT_CSS + "</head>", 1)
                out.append((n, to_utf8(txt, utf8), zipfile.ZIP_DEFLATED))
            elif n.lower().endswith(".opf") and opf_name is None:
                opf_name = n
                opf_raw, opf_utf8 = decode_text(zin.read(zi))

        # 3) OPF
        new_opf = None
        if opf_name:
            new_opf = fix_opf(rename_refs(opf_raw, renames))
            out.append((opf_name, to_utf8(new_opf, opf_utf8), zipfile.ZIP_DEFLATED))
        uid = opf_unique_id(opf_raw) if opf_raw else None

        # 4) everything else (CSS + NCX get the renames, NCX gets dtb:uid)
        done = {o[0] for o in out} | ({opf_name} if opf_name else set())
        for zi in infos:
            n = zi.filename
            low = n.lower()
            if n == "mimetype" or IMG_RE.search(n) or HTML_RE.search(n) or low.endswith(".opf") or n in done:
                continue
            data = zin.read(zi)
            if low.endswith(".css"):
                txt, utf8 = decode_text(data)
                data = to_utf8(rename_refs(txt, renames), utf8)
            elif low.endswith(".ncx"):
                txt, utf8 = decode_text(data)
                data = to_utf8(fix_ncx_uid(rename_refs(txt, renames), uid), utf8)
            out.append((n, data, zipfile.ZIP_DEFLATED))

        # 5) optional CrossPoint cache
        if opts["crosspoint"]:
            if opf_name and new_opf:
                try:
                    data, ns, nt = build_book_bin(zin, opf_name, new_opf)
                    out.append(("META-INF/crosspoint/book.bin", data, zipfile.ZIP_STORED))
                except Exception as e:
                    warnings.append("CrossPoint book.bin skipped (%s)" % e)
            for name, data, _ in list(out):
                if name.lower().endswith(".jpg"):
                    try:
                        out.append((re.sub(r"\.jpg$", ".pxc", name, flags=re.I),
                                    build_pxc(data, opts["dither"]), zipfile.ZIP_STORED))
                    except Exception as e:
                        warnings.append("PXC skipped for %s (%s)" % (name.rsplit("/", 1)[-1], e))

        mimetype = zin.read("mimetype") if "mimetype" in names else b"application/epub+zip"

    # write: mimetype first and stored, as the EPUB spec requires
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst_path.with_name(dst_path.name + ".part")
    stamp = time.localtime()[:6]
    with zipfile.ZipFile(tmp, "w") as zout:
        zi = zipfile.ZipInfo("mimetype", stamp)
        zi.compress_type = zipfile.ZIP_STORED
        zout.writestr(zi, mimetype)
        seen = {"mimetype"}
        for name, data, ctype in out:
            if name in seen:
                continue
            seen.add(name)
            zi = zipfile.ZipInfo(name, stamp)
            zi.compress_type = ctype
            zi.external_attr = 0o644 << 16
            if ctype == zipfile.ZIP_DEFLATED:
                zout.writestr(zi, data, compresslevel=8)
            else:
                zout.writestr(zi, data)
    os.replace(tmp, dst_path)
    return {"src": str(src_path), "dst": str(dst_path), "before": src_path.stat().st_size,
            "after": dst_path.stat().st_size, "images": n_img, "seconds": time.time() - t0,
            "warnings": warnings, "encoder": enc.name}


def _worker(args):
    src, dst, opts = args
    try:
        return optimize_epub(Path(src), Path(dst), opts)
    except zipfile.BadZipFile:
        return {"src": src, "error": "not a valid EPUB (it isn't a zip archive)"}
    except Exception as e:
        return {"src": src, "error": "%s: %s" % (type(e).__name__, e)}


# --------------------------------------------------------------------------- CLI

def human(n: float) -> str:
    n = abs(n)
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024)
    return "%.2f MB" % (n / 1024 / 1024)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Optimize every EPUB in a folder for a 480x800 grayscale e-ink screen "
                    "(local port of epubdev.pages.dev batch mode).")
    ap.add_argument("input", type=Path, help="folder of .epub files (or a single .epub)")
    ap.add_argument("-o", "--output", type=Path,
                    help="output folder (default: <input>/optimized)")
    ap.add_argument("-q", "--quality", type=int, default=85, help="JPEG quality 1-100 (default 85, as on the site)")
    ap.add_argument("--crosspoint-cache", action="store_true",
                    help="pre-build the CrossPoint firmware cache (book.bin + .pxc), like the site's 'CrossPoint Cache: on'")
    ap.add_argument("--dither", choices=["improved", "ordered"], default="improved",
                    help="PXC dithering for --crosspoint-cache (default improved = Floyd-Steinberg)")
    ap.add_argument("--true-grayscale", action="store_true",
                    help="write 1-channel grayscale JPEGs (smaller; the site writes 3-channel JPEGs of gray pixels)")
    ap.add_argument("--no-mozjpeg", action="store_true", help="don't use mozjpeg's cjpeg even if installed")
    ap.add_argument("-r", "--recursive", action="store_true", help="also look in subfolders")
    ap.add_argument("--suffix", default="", help="add this to output file names, e.g. _optimized")
    ap.add_argument("--overwrite", action="store_true", help="redo books that already exist in the output folder")
    ap.add_argument("-j", "--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1),
                    help="books to convert in parallel (default: CPU cores - 1)")
    a = ap.parse_args(argv)

    inp = a.input.expanduser().resolve()
    if inp.is_file():
        books, root = [inp], inp.parent
    elif inp.is_dir():
        root = inp
        books = sorted(inp.rglob("*.epub") if a.recursive else inp.glob("*.epub"), key=lambda p: str(p).lower())
    else:
        ap.error("%s does not exist" % inp)
    out_dir = (a.output.expanduser().resolve() if a.output else root / "optimized")
    books = [b for b in books if out_dir not in b.parents and not b.name.startswith("._")]
    if not books:
        print("No .epub files found in %s" % inp)
        return 1

    opts = {"quality": a.quality, "crosspoint": a.crosspoint_cache, "dither": a.dither,
            "true_gray": a.true_grayscale, "mozjpeg": not a.no_mozjpeg}
    jobs, skipped = [], 0
    for b in books:
        rel = b.relative_to(root)
        dst = out_dir / rel.with_name(rel.stem + a.suffix + ".epub")
        if dst.exists() and not a.overwrite:
            skipped += 1
            continue
        jobs.append((str(b), str(dst), opts))

    enc = JpegEncoder(a.quality, a.true_grayscale, not a.no_mozjpeg)
    print("Encoder: %s | Q=%d | %s%s" % (enc.name, a.quality,
          "true grayscale" if a.true_grayscale else "gray (3-channel, like the site)",
          " | CrossPoint cache (%s dither)" % a.dither if a.crosspoint_cache else ""))
    print("Output:  %s" % out_dir)
    if skipped:
        print("Skipping %d book(s) already in the output folder (use --overwrite to redo)" % skipped)
    if not jobs:
        return 0

    tb = ta = ok = 0
    t0 = time.time()
    width = len(str(len(jobs)))
    with ProcessPoolExecutor(max_workers=max(1, min(a.jobs, len(jobs)))) as ex:
        futs = [ex.submit(_worker, j) for j in jobs]
        for k, f in enumerate(as_completed(futs), 1):
            r = f.result()
            name = Path(r["src"]).name
            if "error" in r:
                print("[%*d/%d] FAILED %s: %s" % (width, k, len(jobs), name, r["error"]))
                continue
            ok += 1
            tb += r["before"]
            ta += r["after"]
            d = r["before"] - r["after"]
            pct = (d / r["before"] * 100) if r["before"] else 0
            print("[%*d/%d] %s  %s -> %s (%s%.1f%%), %d imgs, %.1fs" % (
                width, k, len(jobs), name, human(r["before"]), human(r["after"]),
                "-" if d >= 0 else "+", abs(pct), r["images"], r["seconds"]))
            for w in r["warnings"]:
                print("         warning: %s" % w)
    if ok:
        d = tb - ta
        print("Done: %d/%d books, %s -> %s (%s%.1f%%) in %.1fs" % (
            ok, len(jobs), human(tb), human(ta), "-" if d >= 0 else "+",
            abs(d / tb * 100) if tb else 0, time.time() - t0))
    return 0 if ok == len(jobs) else 2


if __name__ == "__main__":
    sys.exit(main())
