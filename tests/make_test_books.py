"""Generate synthetic EPUBs covering awkward cases. usage: python3 make_test_books.py OUTDIR"""
import io, sys, zipfile
from pathlib import Path
from PIL import Image, ImageDraw

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)

def img(fmt, size, mode="RGB", **kw):
    im = Image.new(mode, size, (200, 60, 30, 128) if mode == "RGBA" else (30, 120, 200))
    d = ImageDraw.Draw(im)
    for i in range(0, size[0], 37):
        d.line([(i, 0), (size[0] - i, size[1])], fill=(250, 250, 0) if mode != "P" else 3, width=5)
    d.ellipse([size[0] // 4, size[1] // 4, size[0] * 3 // 4, size[1] * 3 // 4], fill=(10, 200, 90) if mode != "P" else 5)
    b = io.BytesIO(); im.save(b, fmt, **kw); return b.getvalue()

def write(path, files):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        for n, d in files.items():
            z.writestr(n, d, compress_type=zipfile.ZIP_DEFLATED)

X = '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xmlns:xlink="http://www.w3.org/1999/xlink"><head><title>{t}</title><link rel="stylesheet" href="style.css"/></head><body>{b}</body></html>'
container = '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="{p}" media-type="application/oebps-package+xml"/></rootfiles></container>'

# portrait JPEG stored landscape with EXIF orientation=6
ex = Image.new("RGB", (1200, 900), (90, 90, 90)); ImageDraw.Draw(ex).rectangle([0, 0, 300, 900], fill=(255, 0, 0))
exif = ex.getexif(); exif[0x0112] = 6; b = io.BytesIO(); ex.save(b, "JPEG", exif=exif); rotated = b.getvalue()
p = Image.new("P", (300, 200)); p.putpalette([0,0,0, 255,255,255, 255,0,0, 0,255,0, 0,0,255, 255,255,0] + [0]*750)
ImageDraw.Draw(p).rectangle([50, 50, 250, 150], fill=4); gb = io.BytesIO(); p.save(gb, "GIF", transparency=0)

write(out / "Book A (EPUB3).epub", {
    "META-INF/container.xml": container.format(p="OEBPS/content.opf"),
    "OEBPS/content.opf": '''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">urn:uuid:aaaa-1111</dc:identifier>
    <dc:title>Test Book Ä</dc:title><dc:creator>Ann Author</dc:creator><dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="coverpage" href="cover.xhtml" media-type="application/xhtml+xml" properties="svg"/>
    <item id="cover-img" href="images/cover.png" media-type="image/png" properties="cover-image"/>
    <item id="c1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="text/ch2.xhtml" media-type="application/xhtml+xml" properties="svg"/>
    <item id="c3" href="text/ch3.xhtml" media-type="application/xhtml+xml"/>
    <item id="fig1" href="images/fig1.gif" media-type="image/gif"/>
    <item id="photo" href="images/photo.jpeg" media-type="image/jpeg"/>
    <item id="diag" href="images/diag.webp" media-type="image/webp"/>
    <item id="icon" href="images/icon.bmp" media-type="image/bmp"/>
    <item id="bg" href="images/bg.png" media-type="image/png"/>
    <item id="css" href="style.css" media-type="text/css"/>
  </manifest>
  <spine toc="ncx"><itemref idref="coverpage"/><itemref idref="c1"/><itemref idref="c2"/><itemref idref="c3"/></spine>
</package>''',
    "OEBPS/nav.xhtml": X.format(t="Contents", b='<nav epub:type="toc"><h1>Contents</h1><ol><li><a href="text/ch1.xhtml">Chapter One</a><ol><li><a href="text/ch1.xhtml#s2">Section 1.2</a></li></ol></li><li><a href="text/ch2.xhtml">Chapter Two</a></li><li><a href="text/ch3.xhtml#end">Chapter Three</a></li></ol></nav>'),
    "OEBPS/toc.ncx": '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta name="dtb:uid" content="WRONG-UID"/></head><docTitle><text>T</text></docTitle><navMap><navPoint id="n1"><navLabel><text>Chapter One</text></navLabel><content src="text/ch1.xhtml"/></navPoint></navMap></ncx>',
    "OEBPS/cover.xhtml": '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:xlink="http://www.w3.org/1999/xlink"><head><title>Cover</title><meta name="calibre:cover" content="true"/></head><body><div><svg xmlns="http://www.w3.org/2000/svg" version="1.1" width="100%" height="100%" viewBox="0 0 1600 2400"><image width="1600" height="2400" xlink:href="images/cover.png"/></svg></div></body></html>',
    "OEBPS/text/ch1.xhtml": X.format(t="Ch1", b='<h1>One</h1><p>Text&nbsp;with entity &mdash; and a figure:</p><p><img src="../images/fig1.gif" alt="fig"/></p><h2 id="s2">S2</h2><p><img src="../images/photo.jpeg" alt=""/></p>'),
    "OEBPS/text/ch2.xhtml": X.format(t="Ch2", b='<p>SVG wrapped:</p><div><svg xmlns="http://www.w3.org/2000/svg" width="800" height="600" viewBox="0 0 800 600"><image width="800" height="600" xlink:href="../images/diag.webp"/></svg></div>'),
    "OEBPS/text/ch3.xhtml": X.format(t="Ch3", b='<p id="end"><img src="../images/icon.bmp" alt=""/> The end.</p>'),
    "OEBPS/style.css": 'body{background:url("images/bg.png")} h1{color:#333}',
    "OEBPS/images/cover.png": img("PNG", (1600, 2400), "RGBA"),
    "OEBPS/images/fig1.gif": gb.getvalue(),
    "OEBPS/images/photo.jpeg": rotated,
    "OEBPS/images/diag.webp": img("WEBP", (800, 600)),
    "OEBPS/images/icon.bmp": img("BMP", (64, 64)),
    "OEBPS/images/bg.png": img("PNG", (100, 100)),
})

w1252 = X.replace('encoding="utf-8"', 'encoding="windows-1252"').format(t="Ch", b='<p>Café “quotes”</p><p><img src="../images/map.png"/></p><p><img src="../images/broken.png"/></p><p><img src="../images/pic.jpg"/></p>').encode("cp1252")
write(out / "Book B (EPUB2).epub", {
    "META-INF/container.xml": container.format(p="content.opf"),
    "content.opf": '''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>Book B</dc:title><dc:creator>Bob</dc:creator><dc:language>fr</dc:language>
    <dc:identifier id="uid">isbn-978</dc:identifier>
    <meta name="cover" content="nonexistent"/>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="titlepage" href="text/title.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch" href="text/ch.xhtml" media-type="application/xhtml+xml"/>
    <item id="cover" href="images/cover.png" media-type="image/png"/>
    <item id="map" href="images/map.png" media-type="image/png"/>
    <item id="broken" href="images/broken.png" media-type="image/png"/>
    <item id="pic" href="images/pic.jpg" media-type="image/jpeg"/>
  </manifest>
  <spine toc="ncx"><itemref idref="titlepage"/><itemref idref="ch"/></spine>
  <guide><reference type="cover" title="Cover" href="text/title.xhtml"/><reference type="text" href="text/ch.xhtml"/></guide>
</package>''',
    "toc.ncx": '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta content="old" name="dtb:uid"/></head><docTitle><text>B</text></docTitle><navMap><navPoint id="a"><navLabel><text>Title</text></navLabel><content src="text/title.xhtml"/><navPoint id="b"><navLabel><text>Chapter</text></navLabel><content src="text/ch.xhtml#top"/></navPoint></navPoint></navMap></ncx>',
    "text/title.xhtml": X.format(t="Title", b='<div><img src="../images/cover.png" alt="cover"/></div>'),
    "text/ch.xhtml": w1252,
    "images/cover.png": img("PNG", (2000, 1000)),
    "images/map.png": img("PNG", (3000, 4000)),
    "images/broken.png": b"\x89PNG\r\n\x1a\nthis is not really a png",
    "images/pic.jpg": img("JPEG", (640, 480)),
})
(out / "not an epub.epub").write_bytes(b"hello")
print("ok")
