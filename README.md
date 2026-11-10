# Frame Board

유튜브 게임 영상에서 캐릭터 애니메이션을 프레임 단위로 잘라 한 보드에 모아 보는 도구입니다.
영역을 한 번 그리면 캐릭터를 따라가며 같은 크기의 PNG로 뽑아 줍니다. 화면 캡처를 하나하나 할 필요가 없습니다.

## 다운로드

[최신 버전 받기 (Releases)](https://github.com/Craft374/frame-board/releases/latest)

**Windows**: `FrameBoard-windows-x64.zip`
1. zip 파일의 압축을 풉니다.
2. `FrameBoard` 폴더 안의 `FrameBoard.exe`를 실행합니다.
- **"Windows의 PC 보호"** 창이 뜨면 **추가 정보 > 실행**을 누릅니다.
- 실행하면 `FrameBoard.exe` 옆에 `data` 폴더가 생깁니다.

**Mac (Apple Silicon, macOS 14 이상)**: `FrameBoard-mac-arm64.zip`
1. zip 파일의 압축을 풀고 `FrameBoard.app`을 **응용 프로그램** 폴더로 옮깁니다.
2. 처음 열 때 "확인되지 않은 개발자" 경고가 뜨면 **시스템 설정 > 개인정보 보호 및 보안** 맨 아래의 **"그래도 열기"**를 누릅니다. (macOS 15 이후)
- 그래도 열리지 않으면 터미널에서 한 번 실행합니다: `xattr -dr com.apple.quarantine /Applications/FrameBoard.app`
- 부드러운 전용 창으로 열려면 **Google Chrome** 또는 **Microsoft Edge**가 설치되어 있어야 합니다. 없으면 기본 브라우저(Safari)로 열립니다.

창을 닫으면 프로그램도 함께 종료됩니다.

## 사용법

1. 유튜브 영상 주소를 붙여넣고 불러옵니다.
2. 영상에서 시작/끝 프레임을 고르고, 캐릭터 위에 영역을 드래그로 그립니다.
3. 추출을 누르면 프레임들이 보드에 깔립니다.

영상은 통째로 내려받지 않고 필요한 부분만 그때그때 읽어 옵니다. 긴 영상도 몇 초면 열리지만, 쓰는 동안 인터넷 연결이 필요합니다.

## 단축키 (보드)

| 키 | 동작 |
|---|---|
| Space + 드래그 | 화면 이동 |
| 휠 / 핀치 | 확대·축소 |
| Cmd/Ctrl + C | 복사 |
| Cmd/Ctrl + D | 복제 |
| Delete | 삭제 |
| Cmd/Ctrl + Z | 실행 취소 |
| F | 전체 보기 |
| 1 | 100% 크기 |

## 저장 공간

내려받은 영상과 뽑은 이미지는 아래 폴더에 쌓입니다. 용량이 부족하면 프로그램을 끈 뒤 폴더 안의 `videos`, `jobs`를 지워도 됩니다.

- Windows: `FrameBoard.exe` 옆의 `data` 폴더 (그 폴더에 쓸 수 없으면 `%LOCALAPPDATA%\FrameBoard`)
- Mac: `~/Library/Application Support/FrameBoard`

## 개발자용

[uv](https://docs.astral.sh/uv/)가 필요합니다.

```sh
uv run app.py                 # 실행 (data/ 폴더 사용)
uv run app.py --selftest      # 점검 (--selftest-online: 유튜브 고화질 포맷까지)
uv run --group build pyinstaller frameboard.spec --noconfirm   # 빌드 → dist/
```

`v*` 태그를 push하면 GitHub Actions가 Windows/Mac 빌드를 만들어 Releases에 올립니다.

## 라이선스

MIT (`LICENSE`). 함께 들어 있는 다른 프로그램의 라이선스는 `THIRD_PARTY_NOTICES.md`에 있습니다.
