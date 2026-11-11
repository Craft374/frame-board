# Third-party notices

Frame Board itself is MIT-licensed (see `LICENSE`). The release builds bundle the
following third-party components, each under its own license.

| Component | License | Source |
|---|---|---|
| Python 3.12 (interpreter and standard library) | PSF-2.0 | https://www.python.org |
| OpenSSL (bundled with Python) | Apache-2.0 | https://www.openssl.org |
| OpenCV + opencv-contrib (`opencv-contrib-python-headless`) | Apache-2.0 | https://github.com/opencv/opencv-python |
| FFmpeg and its libraries, bundled inside the OpenCV wheels | LGPL-2.1-or-later (see note) | https://ffmpeg.org |
| NumPy | BSD-3-Clause (bundled parts: 0BSD, MIT, Zlib, CC0-1.0) | https://numpy.org |
| yt-dlp | Unlicense | https://github.com/yt-dlp/yt-dlp |
| yt-dlp-ejs | Unlicense AND MIT AND ISC (bundles meriyah: ISC, astring: MIT) | https://github.com/yt-dlp/ejs |
| Deno | MIT | https://github.com/denoland/deno |
| certifi | MPL-2.0 | https://github.com/certifi/python-certifi |
| Brotli | MIT | https://github.com/google/brotli |
| websockets | BSD-3-Clause | https://github.com/python-websockets/websockets |
| Requests | Apache-2.0 | https://github.com/psf/requests |
| urllib3 | MIT | https://github.com/urllib3/urllib3 |
| idna | BSD-3-Clause | https://github.com/kjd/idna |
| charset-normalizer | MIT | https://github.com/jawah/charset_normalizer |
| PyCryptodome (`pycryptodomex`) | BSD, Public Domain | https://github.com/Legrandin/pycryptodome |
| pywebview | BSD-3-Clause | https://github.com/r0x0r/pywebview |
| Bottle, proxy_tools (pywebview dependencies) | MIT | https://github.com/bottlepy/bottle |
| typing_extensions | PSF-2.0 | https://github.com/python/typing_extensions |
| PyObjC (macOS: pyobjc-core, Cocoa, WebKit, Quartz, Security, UniformTypeIdentifiers) | MIT | https://github.com/ronaldoussoren/pyobjc |
| pythonnet, clr-loader, cffi, pycparser (Windows) | MIT (pycparser: BSD-3-Clause) | https://github.com/pythonnet/pythonnet |
| Microsoft Edge WebView2 SDK DLLs, bundled inside pywebview (Windows) | BSD-3-Clause-style Microsoft license | https://www.nuget.org/packages/Microsoft.Web.WebView2 |
| PyInstaller bootloader | GPL-2.0-or-later with the bootloader exception | https://github.com/pyinstaller/pyinstaller |

## FFmpeg note

The OpenCV wheels link FFmpeg dynamically, as shared libraries you can replace. The full
license texts for OpenCV's bundled libraries ship in OpenCV's `LICENSE-3RD-PARTY.txt`
(https://github.com/opencv/opencv-python/blob/4.x/LICENSE-3RD-PARTY.txt).

The macOS OpenCV wheel also bundles GPL-licensed codec libraries alongside FFmpeg (x264, x265,
libpostproc, rubberband, vid.stab). That makes the macOS build as a whole subject to
GPL-2.0-or-later. Frame Board's own source code is still MIT and is public at
https://github.com/Craft374/frame-board. Each library's source is available from its project page.
