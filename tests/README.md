# Tests

```bash
tests/run_tests.sh
```

Uses the project's `.venv` if it exists (run `./epub-optimize.sh` once to create it), otherwise `python3` with Pillow. Nothing is written outside a temporary folder.

What it runs:

1. **`make_test_books.py`** builds three test inputs: an EPUB3 (nav, calibre SVG cover, transparent PNG/GIF, WebP, BMP, EXIF-rotated JPEG, CSS image reference, `&nbsp;`), an EPUB2 (NCX only, guide cover, Windows-1252 text, `../images` paths, an existing `.jpg`, a corrupt PNG, a wrong cover meta), and a file that isn't an EPUB.
2. **The converter** at X3 size, at X4 size, both with `--crosspoint-cache`, and once with `--size`, `--true-grayscale` and `--suffix`. Each run must fail only on the non-EPUB and must keep the corrupt PNG. A rerun must skip finished books.
3. **`verify_epub.py`** checks every output: mimetype first and stored, manifest and image references resolve, `.jpg` media types, no leftover SVG wrappers, injected stylesheet, UTF-8 text, baseline grayscale JPEGs within the screen size, and well-formed `.pxc` files.
4. **`parity.py`** (needs `node`; runs `npm install` in `ref/` the first time) compares `META-INF/crosspoint/book.bin` and the `.pxc` encoder byte for byte with `ref/ref.js`. That file holds the site's own functions, copied verbatim from its JavaScript bundle. Set `SKIP_PARITY=1` to skip this step.
