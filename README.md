# fix_media_dates

파일명에 포함된 13자리 Unix timestamp(밀리초)를 이용해 사진과 동영상의 촬영 날짜 메타데이터를 복구하는 Python 스크립트입니다. 메타데이터 읽기와 쓰기에는 [ExifTool](https://exiftool.org/)을 사용합니다.

## 지원 파일명

파일명의 확장자를 제외한 전체 부분이 다음 패턴과 정확히 일치해야 합니다.

| 패턴 | 지원 형식 | 예시 |
| --- | --- | --- |
| `{timestamp_ms}` | JPG, JPEG, PNG, MP4, MOV | `1579242283509.jpg` |
| `LINE_MOVIE_{timestamp_ms}` | MP4, MOV | `LINE_MOVIE_1545316978976.mp4` |
| `kakaotalk_{timestamp_ms}` | MP4, MOV | `KakaoTalk_1572348095097.MOV` |

타임스탬프는 UTC 기준 Unix timestamp로 해석합니다. JPG와 PNG에는 KST(`+09:00`) 날짜를, MP4와 MOV의 QuickTime 태그에는 UTC 날짜를 기록합니다.

## 요구 사항

- Python 3
- ExifTool (`exiftool -ver` 명령으로 실행 가능해야 함)

## 사용법

먼저 변경 없이 대상과 충돌 여부를 확인합니다. dry-run이 기본값입니다.

```powershell
py -3 .\fix_media_dates.py "C:\path\to\media"
```

결과 CSV를 검토한 뒤 실제로 적용합니다.

```powershell
py -3 .\fix_media_dates.py "C:\path\to\media" --apply
```

ExifTool 경로를 직접 지정할 수도 있습니다.

```powershell
py -3 .\fix_media_dates.py "C:\path\to\media" --exiftool "C:\path\to\exiftool.exe"
```

주요 옵션:

- `--apply`: 메타데이터를 실제로 수정합니다.
- `--overwrite-existing`: 파일명에서 계산한 날짜와 충돌하는 기존 대상 태그를 덮어씁니다.
- `--no-backup`: ExifTool의 `_original` 백업을 만들지 않습니다.
- `--log-all-skips`: 파일명 패턴이 맞지 않는 파일도 CSV에 기록합니다.
- `--self-test`: 파일명 인식과 시간 변환 자체 검사를 실행합니다.

전체 옵션은 다음 명령으로 확인할 수 있습니다.

```powershell
py -3 .\fix_media_dates.py --help
```

## 안전장치

- 기본 실행은 dry-run이며 파일을 수정하지 않습니다.
- 충돌하는 기존 날짜 태그는 `--overwrite-existing` 없이는 건너뜁니다.
- 기본적으로 수정 전 파일을 `<파일명>_original`로 백업합니다.
- 쓰기 전 원본 변경 여부와 쓰기 후 메타데이터, 파일 크기, 파일 시스템 시각을 검증합니다.
- OneDrive 온라인 전용 파일과 재분석 지점은 건너뜁니다.
- 모든 처리 결과를 현재 디렉터리의 `fix_media_dates_YYYYMMDD_HHMMSS_ffffff.csv`에 기록합니다.

> 전체 OneDrive 폴더에 적용하기 전에 동기화를 일시 중지하고 별도 테스트 복사본으로 검증하세요. `--no-backup`은 복구 수단을 줄이므로 주의해서 사용해야 합니다.

## 기록되는 메타데이터

- JPG/JPEG: EXIF 촬영·생성·수정 시각, 밀리초, `+09:00` 오프셋
- PNG: PNG Creation Time 및 XMP 촬영·생성·수정 시각
- MP4/MOV: QuickTime 생성·수정 시각과 Track/Media 시각

동영상 날짜는 초 단위로 기록됩니다. PNG 날짜가 화면에 표시되는지는 사용하는 프로그램의 메타데이터 지원 범위에 따라 다를 수 있습니다.

## 자체 검사

ExifTool이나 미디어 파일 없이 파일명 패턴과 시간 변환을 검사할 수 있습니다.

```powershell
py -3 .\fix_media_dates.py --self-test
```

성공 시 `SELF_TEST PASSED`가 출력됩니다.
