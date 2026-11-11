# Frame Board

유튜브 게임 영상이나 GIF에서 캐릭터 애니메이션을 프레임 단위로 잘라 한 보드에 모아 보는 도구입니다.
캐릭터에 박스를 한 번 그리면 박스가 캐릭터를 따라가고, 같은 크기의 PNG로 뽑아 줍니다. 화면 캡처를 하나하나 할 필요가 없습니다.

## 다운로드

[최신 버전 받기 (Releases)](https://github.com/Craft374/frame-board/releases/latest)

**Windows**: `FrameBoard-windows-x64.zip`
1. zip 파일의 압축을 풉니다.
2. `FrameBoard` 폴더 안의 `FrameBoard.exe`를 실행합니다.
- **"Windows의 PC 보호"** 창이 뜨면 **추가 정보 > 실행**을 누릅니다.
- 실행하면 `FrameBoard.exe` 옆에 `data` 폴더가 생깁니다.
- 창은 Windows에 들어 있는 **WebView2**로 뜹니다(Windows 10/11 기본 설치). 없으면 Edge 창으로 대신 열립니다.

**Mac (Apple Silicon, macOS 14 이상)**: `FrameBoard-mac-arm64.zip`
1. zip 파일의 압축을 풀고 `FrameBoard.app`을 **응용 프로그램** 폴더로 옮깁니다.
2. 처음 열 때 "확인되지 않은 개발자" 경고가 뜨면 **시스템 설정 > 개인정보 보호 및 보안** 맨 아래의 **"그래도 열기"**를 누릅니다. (macOS 15 이후)
- 그래도 열리지 않으면 터미널에서 한 번 실행합니다: `xattr -dr com.apple.quarantine /Applications/FrameBoard.app`

프로그램 자체의 창으로 열립니다(브라우저가 따로 필요 없습니다). 창을 닫거나 `⌘Q`를 누르면 종료됩니다.

1.0.x에서 만든 보드는 예전 브라우저 창에 저장되어 있어 새 창으로 옮겨 오지 않습니다. 필요한 프레임은 1.0.x에서 미리 저장해 두세요.

## 사용법

1. **열기**: 유튜브 링크나 GIF 링크를 붙여넣습니다. GIF 파일은 창에 끌어다 놓거나 `⌘V`/`Ctrl+V`로 붙여넣어도 됩니다.
2. **박스 그리기**: 캐릭터에 딱 맞게 드래그합니다(배경이 많이 들어가면 잘 못 따라갑니다). 그 프레임부터 박스가 캐릭터를 따라가기 시작합니다(영상은 2초, GIF는 끝까지).
3. **확인**: 재생하면 박스가 따라가는 모습이 보입니다. 타임라인에서 빨간 곳은 캐릭터를 놓친 곳, 주황색은 박스가 갑자기 튀었거나 잠깐 놓쳤던 곳입니다.
   **확인할 곳 보기**(`N`)를 누르면 그런 구간의 첫 프레임으로 바로 갑니다. 거기서 박스를 캐릭터 위로 끌어다 놓으면, 그 프레임부터 다시 따라갑니다.
   구르기처럼 모습이 크게 바뀌면 박스가 맞는데도 빨강·주황으로 나올 수 있습니다. 맞으면 **잘 따라가고 있어요**(`Y`)를 눌러 그 구간 표시를 지웁니다.
   30초가 넘는 영상은 박스를 그리면 타임라인이 구간 주변만 크게 보여 줍니다(**전체 보기**로 되돌림). `←`/`→`는 1프레임, `Shift`를 함께 누르면 10프레임씩 움직입니다.
4. **프레임 뽑기**: 확인한 박스 위치 그대로 잘려 보드에 깔립니다.

대쉬·구르기처럼 캐릭터가 잠깐 찌그러져도, 원래 모습(좌우 반전 포함)이 다시 보이면 되찾아 이어서 따라갑니다.

유튜브 영상은 통째로 내려받지 않고 필요한 부분만 그때그때 읽어 옵니다. 긴 영상도 몇 초면 열리지만, 쓰는 동안 인터넷 연결이 필요합니다.

## 단축키 (보드)

| 키 | 동작 |
|---|---|
| Space + 드래그 | 화면 이동 |
| 휠 / 핀치 | 확대·축소 |
| Cmd/Ctrl + C | 복사 |
| Cmd/Ctrl + V | 붙여넣기 (링크·GIF면 열기) |
| Cmd/Ctrl + D | 복제 |
| Delete | 삭제 |
| Cmd/Ctrl + Z | 실행 취소 |
| F | 전체 보기 |
| 1 | 100% 크기 |

## 저장 공간

연 GIF와 뽑은 이미지는 아래 폴더에 쌓입니다. 용량이 부족하면 프로그램을 끈 뒤 폴더 안의 `videos`, `jobs`를 지워도 됩니다.

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
