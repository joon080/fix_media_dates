# media date fixer

파일명에 포함된 13자리 Unix timestamp(밀리초)를 이용해 사진과 동영상의 날짜 메타데이터를 복구하는 Windows용 GUI 프로그램입니다. 메타데이터 읽기와 쓰기에는 [ExifTool](https://exiftool.org/)을 사용합니다.

## 다운로드 및 실행

1. [최신 GitHub Release](https://github.com/joon080/fix_media_dates/releases/latest)에서 ZIP 파일을 다운로드합니다.
2. ZIP 파일의 압축을 모두 풉니다.
3. `Media Date Fixer.exe`를 실행합니다.

Python이나 ExifTool을 별도로 설치할 필요는 없습니다. 다음 파일은 같은 폴더에 있어야 합니다.

```text
media date fixer/
├─ Media Date Fixer.exe
├─ exiftool.exe
└─ exiftool_files/
```

이 프로그램은 코드 서명되지 않았습니다. Windows가 경고를 표시할 수 있으므로 이 저장소의 Release에서 받은 파일인지 확인한 뒤 실행하세요.

## 사용법

1. `찾아보기`를 눌러 미디어 폴더를 선택합니다.
2. 필요하면 `검사 (dry run)`로 수정 대상과 충돌을 먼저 확인합니다.
3. `실제 적용`을 누르고 확인 창에서 적용을 승인합니다.
4. 완료 후 화면의 결과와 CSV 로그를 확인합니다.

Dry Run은 선택 사항이지만 많은 파일에 처음 적용할 때는 먼저 실행하는 것을 권장합니다.

| 항목 | 기본값 | 동작 |
| --- | --- | --- |
| `검사 (dry run)` | - | 파일을 수정하지 않고 예상 결과만 확인합니다. |
| `기존 날짜 덮어쓰기` | 꺼짐 | 파일명과 충돌하는 기존 날짜도 덮어씁니다. |
| `원본 백업 만들기` | 켜짐 | 수정 전 파일을 `<파일명>_original`로 보관합니다. |
| `중단` | - | 현재 파일 처리가 끝난 뒤 다음 파일부터 중단합니다. |

## 지원 파일명

확장자를 제외한 파일명 전체가 다음 패턴과 정확히 일치해야 합니다.

| 패턴 | 지원 형식 | 예시 |
| --- | --- | --- |
| `{timestamp_ms}` | JPG, JPEG, PNG, MP4, MOV | `1579242283509.jpg` |
| `LINE_MOVIE_{timestamp_ms}` | MP4, MOV | `LINE_MOVIE_1545316978976.mp4` |
| `kakaotalk_{timestamp_ms}` | MP4, MOV | `KakaoTalk_1572348095097.MOV` |

다른 문자열이 앞뒤에 붙거나 타임스탬프 자릿수가 다르면 처리하지 않습니다.

## 날짜 처리 방식

- 파일명의 Unix timestamp는 항상 UTC 기준으로 해석합니다.
- JPG/JPEG와 PNG에는 해당 날짜의 Windows 시스템 현지 시각과 UTC 오프셋을 기록합니다.
- MP4와 MOV의 QuickTime 날짜에는 UTC를 기록합니다.
- 사진은 밀리초, 동영상은 초 단위까지 기록합니다.

따라서 한국 시간대로 설정된 Windows에서는 사진에 `+09:00`이 기록되지만, 다른 시간대에서는 해당 지역의 시각과 오프셋이 사용됩니다.

## 안전장치와 로그

- 기존 날짜가 파일명에서 계산한 날짜와 충돌하면 기본적으로 건너뜁니다.
- 원본 백업은 기본으로 활성화됩니다.
- 쓰기 전 파일 변경 여부와 쓰기 후 메타데이터, 파일 크기 및 파일 시스템 시각을 검증합니다.
- OneDrive 온라인 전용 파일과 재분석 지점은 건너뜁니다.
- 로그는 Windows 문서 폴더의 `Media Date Fixer\Logs`에 CSV로 저장됩니다.

> 전체 폴더에 적용하기 전에 OneDrive 동기화를 일시 중지하고 별도 테스트 복사본으로 검증하는 것을 권장합니다. 백업을 끄면 복구 수단이 줄어듭니다.

## 소스 코드로 실행

Python 3과 ExifTool이 설치된 개발 환경에서는 기존 CLI도 사용할 수 있습니다. 기본 실행은 Dry Run입니다.

```powershell
py -3 .\fix_media_dates.py "C:\path\to\media" --exiftool "C:\path\to\exiftool.exe"
```

실제로 적용하려면 `--apply`를 추가합니다.

```powershell
py -3 .\fix_media_dates.py "C:\path\to\media" --apply --exiftool "C:\path\to\exiftool.exe"
```

전체 옵션은 `py -3 .\fix_media_dates.py --help`로 확인할 수 있습니다.

## EXE 빌드

```powershell
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name "Media Date Fixer" .\media_date_fixer.py
```

완성된 파일은 `dist\Media Date Fixer.exe`에 생성됩니다. 배포할 때는 ExifTool의 `exiftool.exe`와 `exiftool_files` 폴더를 실행 파일 옆에 함께 넣어야 합니다.
