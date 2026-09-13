#!/usr/bin/env python3
"""Restore media capture dates from strictly validated millisecond timestamps."""

import argparse
import csv
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


KST = timezone(timedelta(hours=9), "KST")
UTC = timezone.utc
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".mp4", ".mov"}
VIDEO_EXTENSIONS = {".mp4", ".mov"}
JPEG_TAGS = (
    "EXIF:DateTimeOriginal",
    "EXIF:CreateDate",
    "EXIF:ModifyDate",
    "EXIF:SubSecTimeOriginal",
    "EXIF:SubSecTimeDigitized",
    "EXIF:SubSecTime",
    "EXIF:OffsetTimeOriginal",
    "EXIF:OffsetTimeDigitized",
    "EXIF:OffsetTime",
)
PNG_TAGS = (
    "PNG:CreationTime",
    "XMP-exif:DateTimeOriginal",
    "XMP-xmp:CreateDate",
    "XMP-xmp:ModifyDate",
)
VIDEO_TAGS = (
    "QuickTime:CreateDate",
    "QuickTime:ModifyDate",
    "QuickTime:TrackCreateDate",
    "QuickTime:TrackModifyDate",
    "QuickTime:MediaCreateDate",
    "QuickTime:MediaModifyDate",
)
CSV_FIELDS = (
    "filepath",
    "filename",
    "extension",
    "pattern",
    "timestamp_ms",
    "target_datetime_utc",
    "target_datetime_kst",
    "existing_metadata",
    "planned_tags",
    "action",
    "result",
    "precision",
    "backup_expected",
    "error",
)
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
PLACEHOLDER_FLAGS = (
    FILE_ATTRIBUTE_REPARSE_POINT
    | FILE_ATTRIBUTE_OFFLINE
    | FILE_ATTRIBUTE_RECALL_ON_OPEN
    | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "검증된 파일명의 Unix timestamp(ms)로 사진/동영상 날짜 메타데이터를 복구합니다. "
            "기본 동작은 dry-run입니다. PNG 날짜 표시 여부는 프로그램마다 다를 수 있습니다."
        ),
        epilog=(
            "--no-backup은 ExifTool의 _original 백업을 만들지 않아 원본 복구 수단이 줄어듭니다. "
            "전체 OneDrive 실행 전 동기화를 일시 중지하고 별도 테스트 복사본에서 검증하세요."
        ),
    )
    parser.add_argument("root", nargs="?", help="검사할 루트 폴더")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="실제로 메타데이터를 수정")
    mode.add_argument("--dry-run", action="store_true", help="변경 없이 계획만 기록(기본값)")
    parser.add_argument(
        "--overwrite-existing",
        action="store_true",
        help="파일명과 충돌하는 기존 대상 날짜 태그도 덮어씀",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="위험: ExifTool _original 백업을 만들지 않음",
    )
    parser.add_argument("--log-all-skips", action="store_true", help="패턴 불일치도 CSV에 기록")
    parser.add_argument("--exiftool", default="exiftool", help="ExifTool 실행 파일 경로")
    parser.add_argument("--self-test", action="store_true", help="파일명/시간 변환 자체 검사 후 종료")
    args = parser.parse_args()
    if not args.self_test and not args.root:
        parser.error("root가 필요합니다. 자체 검사만 실행하려면 --self-test를 사용하세요.")
    return args


def match_filename(filename):
    path = Path(filename)
    stem, extension = path.stem, path.suffix.lower()
    match = re.fullmatch(r"[0-9]{13}", stem, re.ASCII)
    if match:
        pattern, timestamp_ms = "numeric_timestamp", stem
    else:
        match = re.fullmatch(r"LINE_MOVIE_([0-9]{13})", stem, re.ASCII)
        if match:
            pattern, timestamp_ms = "line_movie", match.group(1)
        else:
            match = re.fullmatch(
                r"kakaotalk_([0-9]{13})", stem, re.ASCII | re.IGNORECASE
            )
            if not match:
                return None
            pattern, timestamp_ms = "kakaotalk", match.group(1)
    supported = extension in SUPPORTED_EXTENSIONS
    if pattern in {"line_movie", "kakaotalk"} and extension not in VIDEO_EXTENSIONS:
        supported = False
    return {
        "pattern": pattern,
        "timestamp_ms": timestamp_ms,
        "extension": extension,
        "supported": supported,
    }


def timestamp_datetimes(timestamp_ms):
    seconds, milliseconds = divmod(int(timestamp_ms), 1000)
    utc = datetime.fromtimestamp(seconds, tz=UTC).replace(
        microsecond=milliseconds * 1000
    )
    return utc, utc.astimezone(KST)


def exif_datetime(value, assume_tz=None):
    if not isinstance(value, str):
        return None
    match = re.search(
        r"(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})"
        r"(?:\.(\d+))?(Z|[+\-]\d{2}:?\d{2})?",
        value,
    )
    if not match:
        return None
    year, month, day, hour, minute, second = map(int, match.groups()[:6])
    fraction, offset = match.group(7), match.group(8)
    microsecond = int(((fraction or "") + "000000")[:6])
    tz = assume_tz
    if offset == "Z":
        tz = UTC
    elif offset:
        compact = offset.replace(":", "")
        sign = 1 if compact[0] == "+" else -1
        tz = timezone(
            sign * timedelta(hours=int(compact[1:3]), minutes=int(compact[3:5]))
        )
    try:
        return datetime(year, month, day, hour, minute, second, microsecond, tz)
    except ValueError:
        return None


def target_tags(extension, utc, kst):
    milliseconds = kst.microsecond // 1000
    local_seconds = kst.strftime("%Y:%m:%d %H:%M:%S")
    local_offset = local_seconds + "+09:00"
    local_precise = f"{local_seconds}.{milliseconds:03d}+09:00"
    utc_seconds = utc.strftime("%Y:%m:%d %H:%M:%S")
    if extension in {".jpg", ".jpeg"}:
        values = {
            "DateTimeOriginal": local_seconds,
            "CreateDate": local_seconds,
            "ModifyDate": local_seconds,
            "SubSecTimeOriginal": f"{milliseconds:03d}",
            "SubSecTimeDigitized": f"{milliseconds:03d}",
            "SubSecTime": f"{milliseconds:03d}",
            "OffsetTimeOriginal": "+09:00",
            "OffsetTimeDigitized": "+09:00",
            "OffsetTime": "+09:00",
        }
        return {tag: values[tag.split(":", 1)[1]] for tag in JPEG_TAGS}
    if extension == ".png":
        values = {
            "PNG:CreationTime": local_offset,
            "XMP-exif:DateTimeOriginal": local_precise,
            "XMP-xmp:CreateDate": local_precise,
            "XMP-xmp:ModifyDate": local_precise,
        }
        return {tag: values[tag] for tag in PNG_TAGS}
    return {tag: utc_seconds for tag in VIDEO_TAGS}


def decode_output(data):
    return data.decode("utf-8", errors="replace").strip()


def run_exiftool(executable, arguments):
    # stdin is an UTF-8 ExifTool argument file, avoiding Windows code-page loss.
    payload = ("\n".join(arguments) + "\n").encode("utf-8")
    try:
        completed = subprocess.run(
            [executable, "-charset", "filename=utf8", "-@", "-"],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
        )
    except OSError as error:
        raise RuntimeError(f"ExifTool 실행 실패: {error}") from error
    return completed.returncode, decode_output(completed.stdout), decode_output(
        completed.stderr
    )


def exiftool_version(executable):
    try:
        completed = subprocess.run(
            [executable, "-ver"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
        )
    except OSError as error:
        raise RuntimeError(f"ExifTool을 찾거나 실행할 수 없습니다: {error}") from error
    version = decode_output(completed.stdout)
    error = decode_output(completed.stderr)
    if completed.returncode or not version:
        raise RuntimeError(error or "ExifTool 버전을 확인하지 못했습니다.")
    return version


def read_metadata(executable, path, video=False):
    arguments = ["-j", "-a", "-G1", "-s"]
    if video:
        arguments += [
            "-api",
            "QuickTimeUTC=1",
            "-d",
            "%Y-%m-%dT%H:%M:%S%z",
        ]
    arguments += ["-time:all", str(path)]
    code, output, error = run_exiftool(executable, arguments)
    if code:
        raise RuntimeError(error or output or f"ExifTool 읽기 종료 코드 {code}")
    if error:
        raise RuntimeError(f"ExifTool 읽기 경고: {error}")
    try:
        items = json.loads(output)
        metadata = items[0]
    except (json.JSONDecodeError, IndexError, TypeError) as problem:
        raise RuntimeError(f"ExifTool JSON 해석 실패: {problem}") from problem
    return {key: value for key, value in metadata.items() if key != "SourceFile"}


def target_entries(metadata, extension):
    entries = []
    for key, value in metadata.items():
        group, separator, name = key.rpartition(":")
        if not separator:
            continue
        if extension in {".jpg", ".jpeg"}:
            allowed = {tag.split(":", 1)[1] for tag in JPEG_TAGS}
            if group not in {"EXIF", "ExifIFD", "IFD0"} or name not in allowed:
                continue
        elif extension == ".png":
            allowed = set(PNG_TAGS)
            if key not in allowed:
                continue
        else:
            allowed = {tag.split(":", 1)[1] for tag in VIDEO_TAGS}
            if not (group == "QuickTime" or re.fullmatch(r"Track\d+", group)):
                continue
            if name not in allowed:
                continue
        entries.append((key, name, str(value)))
    return entries


def metadata_state(metadata, extension, planned, utc, kst):
    entries = target_entries(metadata, extension)
    found_names = {name for _, name, _ in entries}
    planned_names = {tag.split(":", 1)[1] for tag in planned}
    conflicts = []
    normalized_video_values = set()
    for key, name, value in entries:
        if extension in VIDEO_EXTENSIONS:
            parsed = exif_datetime(value)
            if not parsed or parsed.tzinfo is None:
                conflicts.append(f"{key}={value!r} has no verifiable UTC offset")
                continue
            normalized = parsed.astimezone(UTC).replace(microsecond=0)
            normalized_video_values.add(normalized)
            if normalized != utc.replace(microsecond=0):
                conflicts.append(f"{key}={value!r}")
        elif name.startswith("SubSecTime"):
            expected = f"{kst.microsecond // 1000:03d}"
            if value != expected:
                conflicts.append(f"{key}={value!r}")
        elif name.startswith("OffsetTime"):
            if value != "+09:00":
                conflicts.append(f"{key}={value!r}")
        else:
            parsed = exif_datetime(value, KST)
            if not parsed or parsed.astimezone(UTC).replace(microsecond=0) != utc.replace(
                microsecond=0
            ):
                conflicts.append(f"{key}={value!r}")
    if len(normalized_video_values) > 1:
        conflicts.append("QuickTime target tags disagree with each other")
    if conflicts:
        return "CONFLICT", "; ".join(conflicts)
    if planned_names.issubset(found_names):
        return "ALREADY_CORRECT", ""
    return "WRITE", ""


def system_times(metadata):
    result = {}
    for key, value in metadata.items():
        name = key.rsplit(":", 1)[-1]
        if name in {"FileCreateDate", "FileModifyDate"}:
            result[name] = str(value)
    return result


def write_metadata(executable, path, planned, no_backup):
    arguments = ["-P"]
    if no_backup:
        arguments.append("-overwrite_original")
    arguments += [f"-{tag}={value}" for tag, value in planned.items()]
    arguments.append(str(path))
    return run_exiftool(executable, arguments)


def restore_access_and_modify_times(path, before):
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


def same_source_state(first, second):
    return first.st_size == second.st_size and first.st_mtime_ns == second.st_mtime_ns


def reasonable_output_size(before_size, after_size):
    if after_size <= 0:
        return False
    lower = max(1, before_size // 2)
    upper = max(before_size * 2, before_size + 16 * 1024 * 1024)
    return lower <= after_size <= upper


def is_temporary_or_backup(name):
    lower = name.lower()
    return (
        lower.endswith("_original")
        or lower.startswith("~$")
        or Path(lower).suffix in {".tmp", ".temp", ".part", ".partial", ".crdownload"}
    )


def scan_files(root):
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError as error:
            yield Path(folder), None, f"디렉터리 읽기 실패: {error}"
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                details = entry.stat(follow_symlinks=False)
            except OSError as error:
                yield path, None, f"파일 상태 읽기 실패: {error}"
                continue
            attributes = getattr(details, "st_file_attributes", 0)
            directory = stat.S_ISDIR(details.st_mode) or bool(
                attributes & FILE_ATTRIBUTE_DIRECTORY
            )
            reparse = entry.is_symlink() or bool(
                attributes & FILE_ATTRIBUTE_REPARSE_POINT
            )
            if directory:
                if not reparse:
                    stack.append(path)
                continue
            if stat.S_ISREG(details.st_mode) or reparse:
                yield path, details, "NOT_LOCAL" if reparse else ""


def blank_row(path):
    row = {field: "" for field in CSV_FIELDS}
    row.update(
        filepath=str(path),
        filename=path.name,
        extension=path.suffix.lower(),
    )
    return row


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def record_skip(writer, row, result, error=""):
    row.update(action="SKIPPED", result=result, error=error)
    writer.writerow(row)


def run_self_test():
    accepted = {
        "1579242283509.jpg": "numeric_timestamp",
        "1720495695578.png": "numeric_timestamp",
        "LINE_MOVIE_1545316978976.mp4": "line_movie",
        "kakaotalk_1572348095097.mp4": "kakaotalk",
        "KakaoTalk_1572348095097.MOV": "kakaotalk",
    }
    rejected = (
        "EYS7454015450431906237.mp4",
        "VID_23590721_204419_081.mp4",
        "IMG_1579242283509.jpg",
        "1579242283509_1.jpg",
        "LINE_MOVIE_1545316978976_1.mp4",
        "my_kakaotalk_1572348095097.mp4",
        "123456789012.jpg",
        "12345678901234.jpg",
    )
    for filename, pattern in accepted.items():
        match = match_filename(filename)
        assert match and match["supported"] and match["pattern"] == pattern, filename
    for filename in rejected:
        assert match_filename(filename) is None, filename
    assert match_filename("LINE_MOVIE_1545316978976.jpg")["supported"] is False
    expected = {
        "1579242283509": "2020-01-17 15:24:43.509",
        "1545316978976": "2018-12-20 23:42:58.976",
        "1572348095097": "2019-10-29 20:21:35.097",
        "1720495695578": "2024-07-09 12:28:15.578",
    }
    for value, text in expected.items():
        _, kst = timestamp_datetimes(value)
        actual = kst.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        assert actual == text, (value, actual, text)
    print("SELF_TEST PASSED")


def print_summary(stats):
    labels = (
        ("Total scanned", "total"),
        ("Matched", "matched"),
        ("Modified", "modified"),
        ("Dry-run candidates", "dry_run"),
        ("Already correct", "already"),
        ("Conflicts", "conflicts"),
        ("Skipped pattern mismatch", "pattern_mismatch"),
        ("Unsupported extension", "unsupported"),
        ("Out of range", "out_of_range"),
        ("Not local or unreadable", "not_local"),
        ("Failed", "failed"),
        ("Verification failed", "verify_failed"),
    )
    print("\nSummary")
    for label, key in labels:
        print(f"{label}: {stats[key]}")
    print("Patterns")
    for pattern in ("numeric_timestamp", "line_movie", "kakaotalk"):
        print(f"{pattern}: {stats['patterns'][pattern]}")


def main():
    args = parse_args()
    if args.self_test:
        run_self_test()
        return 0

    root = Path(os.path.abspath(args.root))
    if not root.exists() or not root.is_dir():
        print(f"ERROR: 루트 폴더가 없거나 디렉터리가 아닙니다: {root}", file=sys.stderr)
        return 2
    root_details = root.stat()
    if getattr(root_details, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        print(f"ERROR: 루트가 재분석 지점입니다: {root}", file=sys.stderr)
        return 2
    try:
        version = exiftool_version(args.exiftool)
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    apply_changes = args.apply
    upper_bound = datetime.now(KST) + timedelta(days=1)
    lower_bound = datetime(2000, 1, 1, tzinfo=KST)
    log_path = Path.cwd() / datetime.now().strftime(
        "fix_media_dates_%Y%m%d_%H%M%S_%f.csv"
    )
    stats = {
        "total": 0,
        "matched": 0,
        "modified": 0,
        "dry_run": 0,
        "already": 0,
        "conflicts": 0,
        "pattern_mismatch": 0,
        "unsupported": 0,
        "out_of_range": 0,
        "not_local": 0,
        "failed": 0,
        "verify_failed": 0,
        "patterns": {"numeric_timestamp": 0, "line_movie": 0, "kakaotalk": 0},
    }

    print(f"ExifTool: {version}")
    print(f"Mode: {'APPLY' if apply_changes else 'DRY-RUN'}")
    print(f"Root: {root}")
    print(f"CSV: {log_path}")
    print("주의: 전체 적용 전 OneDrive 동기화를 일시 중지하고 테스트 복사본으로 검증하세요.")
    if apply_changes:
        print(
            "Windows 생성 시각은 ExifTool -P와 설치된 Win32 API 지원에 의존하며, "
            "각 파일에서 적용 전후 값을 확인합니다. 확인할 수 없으면 수정하지 않습니다."
        )

    with log_path.open("x", encoding="utf-8-sig", newline="") as log_file:
        writer = csv.DictWriter(log_file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for path, scanned_details, scan_error in scan_files(root):
            row = blank_row(path)
            if scanned_details is None:
                stats["failed"] += 1
                row.update(action="SKIPPED", result="UNREADABLE", error=scan_error)
                writer.writerow(row)
                continue

            stats["total"] += 1
            if is_temporary_or_backup(path.name):
                stats["pattern_mismatch"] += 1
                if args.log_all_skips:
                    record_skip(writer, row, "SKIPPED", "TEMP_OR_BACKUP")
                continue

            match = match_filename(path.name)
            if not match:
                stats["pattern_mismatch"] += 1
                if args.log_all_skips:
                    record_skip(writer, row, "SKIPPED", "PATTERN_MISMATCH")
                continue

            stats["matched"] += 1
            stats["patterns"][match["pattern"]] += 1
            row.update(
                pattern=match["pattern"],
                timestamp_ms=match["timestamp_ms"],
                precision="seconds" if match["extension"] in VIDEO_EXTENSIONS else "milliseconds",
                backup_expected="no" if args.no_backup else "yes",
            )
            if not match["supported"]:
                stats["unsupported"] += 1
                record_skip(writer, row, "UNSUPPORTED_EXTENSION")
                continue

            attributes = getattr(scanned_details, "st_file_attributes", 0)
            if scan_error == "NOT_LOCAL" or attributes & PLACEHOLDER_FLAGS:
                stats["not_local"] += 1
                record_skip(writer, row, "NOT_LOCAL")
                continue
            if not os.access(path, os.R_OK):
                stats["not_local"] += 1
                record_skip(writer, row, "UNREADABLE")
                continue

            try:
                utc, kst = timestamp_datetimes(match["timestamp_ms"])
            except (OverflowError, OSError, ValueError) as error:
                stats["out_of_range"] += 1
                record_skip(writer, row, "OUT_OF_RANGE", str(error))
                continue
            row.update(
                target_datetime_utc=utc.isoformat(timespec="milliseconds"),
                target_datetime_kst=kst.isoformat(timespec="milliseconds"),
            )
            if not (lower_bound <= kst <= upper_bound):
                stats["out_of_range"] += 1
                record_skip(writer, row, "OUT_OF_RANGE")
                continue

            planned = target_tags(match["extension"], utc, kst)
            row["planned_tags"] = compact_json(planned)
            video = match["extension"] in VIDEO_EXTENSIONS
            try:
                before = path.stat()
                try:
                    metadata = read_metadata(args.exiftool, path, video)
                finally:
                    restore_access_and_modify_times(path, before)
                after_read = path.stat()
            except (OSError, RuntimeError) as error:
                stats["failed"] += 1
                row.update(action="SKIPPED", result="FAILED", error=str(error))
                writer.writerow(row)
                continue
            row["existing_metadata"] = compact_json(metadata)
            if not same_source_state(before, after_read):
                stats["failed"] += 1
                row.update(
                    action="SKIPPED",
                    result="FAILED",
                    error="파일 크기 또는 수정 시각이 메타데이터 검사 중 변경됨",
                )
                writer.writerow(row)
                continue

            state, detail = metadata_state(
                metadata, match["extension"], planned, utc, kst
            )
            if state == "ALREADY_CORRECT":
                stats["already"] += 1
                record_skip(writer, row, "ALREADY_CORRECT")
                continue
            if state == "CONFLICT" and not args.overwrite_existing:
                stats["conflicts"] += 1
                record_skip(writer, row, "CONFLICT", detail)
                continue

            if not apply_changes:
                stats["dry_run"] += 1
                row.update(
                    action="DRY_RUN",
                    result="DRY_RUN",
                    error=("overwrite enabled: " + detail) if detail else "",
                )
                writer.writerow(row)
                continue

            original_system_times = system_times(metadata)
            if os.name == "nt" and "FileCreateDate" not in original_system_times:
                stats["failed"] += 1
                row.update(
                    action="SKIPPED",
                    result="FAILED",
                    error="Windows FileCreateDate를 읽어 보존 여부를 확인할 수 없음",
                )
                writer.writerow(row)
                continue
            backup_path = Path(str(path) + "_original")
            if not args.no_backup and backup_path.exists():
                stats["failed"] += 1
                row.update(
                    action="SKIPPED",
                    result="FAILED",
                    error=f"기존 백업을 덮어쓰지 않음: {backup_path}",
                )
                writer.writerow(row)
                continue
            try:
                current = path.stat()
                if not same_source_state(before, current):
                    raise RuntimeError("파일 크기 또는 수정 시각이 쓰기 직전 변경됨")
                code, output, warning = write_metadata(
                    args.exiftool, path, planned, args.no_backup
                )
                restore_access_and_modify_times(path, before)
                if code:
                    raise RuntimeError(warning or output or f"ExifTool 쓰기 종료 코드 {code}")
                verified_metadata = read_metadata(args.exiftool, path, video)
                restore_access_and_modify_times(path, before)
                final_details = path.stat()
                verify_state, verify_detail = metadata_state(
                    verified_metadata, match["extension"], planned, utc, kst
                )
                final_system_times = system_times(verified_metadata)
                verification_errors = []
                if verify_state != "ALREADY_CORRECT":
                    verification_errors.append(verify_detail or "대상 태그가 모두 기록되지 않음")
                if not reasonable_output_size(before.st_size, final_details.st_size):
                    verification_errors.append(
                        f"비정상 파일 크기: {before.st_size} -> {final_details.st_size}"
                    )
                if final_details.st_mtime_ns != before.st_mtime_ns:
                    verification_errors.append("파일 시스템 수정 시각 보존 실패")
                if original_system_times != final_system_times:
                    verification_errors.append(
                        "파일 시스템 생성/수정 시각 보존 실패: "
                        f"{original_system_times!r} -> {final_system_times!r}"
                    )
                if not args.no_backup and not backup_path.exists():
                    verification_errors.append("예상한 _original 백업이 생성되지 않음")
                if warning:
                    row["error"] = f"ExifTool warning: {warning}"
                if verification_errors:
                    stats["verify_failed"] += 1
                    row.update(
                        action="MODIFIED",
                        result="VERIFY_FAILED",
                        error="; ".join(
                            ([row["error"]] if row["error"] else []) + verification_errors
                        )
                        + (f"; 백업 확인: {backup_path}" if not args.no_backup else ""),
                    )
                else:
                    stats["modified"] += 1
                    row.update(action="MODIFIED", result="MODIFIED")
            except (OSError, RuntimeError) as error:
                try:
                    if path.exists():
                        restore_access_and_modify_times(path, before)
                except OSError as restore_error:
                    error = RuntimeError(f"{error}; 파일 시각 복원 실패: {restore_error}")
                stats["failed"] += 1
                row.update(action="MODIFIED", result="FAILED", error=str(error))
            writer.writerow(row)

    print_summary(stats)
    print(f"CSV log: {log_path}")
    return 1 if stats["failed"] or stats["verify_failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
