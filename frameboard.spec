# PyInstaller 빌드 설정: uv run --group build pyinstaller frameboard.spec --noconfirm
# 결과: dist/FrameBoard/FrameBoard.exe (Windows), dist/FrameBoard.app (macOS). 한 폴더(onedir) 방식이라 시작이 빠릅니다.
import os
import shutil
import sys
import sysconfig
import tomllib

from PyInstaller.utils.hooks import collect_all

VERSION = tomllib.load(open(os.path.join(SPECPATH, "pyproject.toml"), "rb"))["project"]["version"]

# yt-dlp가 쓰는 JS 런타임. PyPI "deno" 패키지가 venv scripts 폴더에 넣어 둔 실행 파일을 그대로 넣습니다.
deno = shutil.which("deno") or os.path.join(sysconfig.get_path("scripts"), "deno" + sysconfig.get_config_var("EXE"))
assert os.path.isfile(deno), f"deno not found: {deno}"

# 유튜브 JS 해석 스크립트(패키지 데이터). 빠지면 조용히 저화질 포맷만 받게 됩니다.
ejs_datas, ejs_binaries, ejs_hidden = collect_all("yt_dlp_ejs")

a = Analysis(
    ["app.py"],
    binaries=[(deno, ".")] + ejs_binaries,
    datas=[("index.html", ".")] + ejs_datas,
    hiddenimports=ejs_hidden,
    excludes=["tkinter", "mutagen"],  # mutagen: 태그 기록용(GPL), 영상만 받는 이 앱은 쓰지 않음
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FrameBoard",
    console=False,
    icon=os.path.join(SPECPATH, "assets", "icon.ico" if sys.platform == "win32" else "icon.icns"),
    disable_windowed_traceback=True,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="FrameBoard", upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="FrameBoard.app",
        bundle_identifier="io.github.frameboard",
        icon=os.path.join(SPECPATH, "assets", "icon.icns"),
        info_plist={
            # 창은 pywebview(WKWebView)가 직접 띄우므로 Dock 아이콘 = 이 프로그램입니다 (누르면 이 창이 앞으로).
            "CFBundleName": "Frame Board",  # 메뉴 막대에 보이는 이름
            "CFBundleDisplayName": "Frame Board",
            "NSHighResolutionCapable": True,
            "CFBundleShortVersionString": VERSION,
        },
    )
