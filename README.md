# epub-conv

Local, batch version of https://epubdev.pages.dev: optimizes every EPUB in a folder for a grayscale Xteink reader and writes the results to a subfolder. Images are sized for the **X3 (528×792) by default**. Use `-d x4` for the X4's 480×800, which is what the site uses, or `--size WxH` for any other screen.

```bash
./epub-optimize.sh ~/Books                       # -> ~/Books/optimized/
./epub-optimize.sh ~/Books --crosspoint-cache    # also pre-build the CrossPoint cache (book.bin + .pxc)
./epub-optimize.sh ~/Books -q 75 -o ~/Books/q75  # lower quality, custom output folder
./epub-optimize.sh ~/Books -d x4                 # size images for an X4 instead (identical to the site)
./epub-optimize.sh --help                        # all options
```

On its first run the wrapper creates a `.venv` next to the scripts and installs Pillow into it. It installs nothing system-wide. Optional: `brew install mozjpeg` makes the images about 15% smaller at the same quality, and the script uses it automatically once it's installed.

What it does to each book, matching the site's batch mode:
- Every PNG, GIF, WEBP, BMP, JPG and JPEG is flattened onto white, shrunk to fit the screen (never enlarged), turned grayscale and saved as a baseline JPEG (default quality 85).
- Image references in the XHTML, CSS, NCX and OPF are renamed to `.jpg`, and the OPF media types are fixed.
- SVG-wrapped images and calibre-style SVG cover pages become plain `<img>`.
- A small stylesheet is injected so images, tables and code blocks stay on the screen.
- The cover `<meta>` and the NCX `dtb:uid` are repaired.
- `--crosspoint-cache` writes `META-INF/crosspoint/book.bin` and a `.pxc` file per image. These are byte-identical to what the site produces. The site only targets the X4, so test one book before relying on this cache on an X3.

Ways it differs from the site:
- It converts several books in parallel.
- Books already in the output folder are skipped unless you pass `--overwrite`.
- An image that can't be decoded is kept with its original name and its references are left alone. The site renames the references anyway, which breaks the link.
- Text in a non-UTF-8 file gets its encoding declaration corrected.
- `--true-grayscale` writes 1-channel JPEGs, which are slightly smaller. The site actually writes 3-channel JPEGs of gray pixels, so that's the default here.
- The site's single-file "split/rotate wide images" picker isn't included. The site's batch mode doesn't use it either.

Tests: `tests/run_tests.sh` (see [tests/README.md](tests/README.md)).
