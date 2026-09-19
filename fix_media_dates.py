#!/usr/bin/env python3
"""Restore media capture dates from strictly validated filename dates."""

import argparse
import csv
import ctypes
import hashlib
import io
import json
import os
import queue
import re
import stat
import subprocess
import sys
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, UnidentifiedImageError

if os.name == "nt":
    from ctypes import wintypes


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


UTC = timezone.utc
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".mp4", ".mov"}
VIDEO_EXTENSIONS = {".mp4", ".mov"}
PATTERN_NAMES = (
    "numeric_timestamp",
    "line_movie",
    "kakaotalk",
    "video_local_minute",
    "vid_local_millisecond",
    "video_local_second",
)
VIDEO_ONLY_PATTERNS = set(PATTERN_NAMES) - {"numeric_timestamp"}
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
    "target_datetime_local",
    "existing_metadata",
    "planned_tags",
    "action",
    "result",
    "precision",
    "backup_expected",
    "repair_kind",
    "error",
)
REPAIR_KIND_JPEG_OTHER_IMAGE_START = "JPEG_OTHER_IMAGE_START"
ERROR_RESULTS = {"FAILED", "VERIFY_FAILED", "UNREADABLE"}
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
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WINDOWS_EPOCH_100NS = 116444736000000000
if os.name == "nt":
    KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    CREATE_FILE = KERNEL32.CreateFileW
    CREATE_FILE.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    CREATE_FILE.restype = wintypes.HANDLE
    SET_FILE_TIME = KERNEL32.SetFileTime
    SET_FILE_TIME.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    SET_FILE_TIME.restype = wintypes.BOOL
    CLOSE_HANDLE = KERNEL32.CloseHandle
    CLOSE_HANDLE.argtypes = (wintypes.HANDLE,)
    CLOSE_HANDLE.restype = wintypes.BOOL
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "검증된 파일명의 날짜 또는 Unix timestamp(ms)로 사진/동영상 날짜 메타데이터를 복구합니다. "
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
    parser.add_argument(
        "--workers",
        type=int,
        choices=(1, 2, 4),
        default=None,
        help="실제 적용 동시 처리 수(기본값: 2, dry-run은 1만 허용)",
    )
    parser.add_argument("--self-test", action="store_true", help="파일명/시간 변환 자체 검사 후 종료")
    args = parser.parse_args()
    if not args.self_test and not args.root:
        parser.error("root가 필요합니다. 자체 검사만 실행하려면 --self-test를 사용하세요.")
    if not args.self_test and not args.apply and args.workers not in (None, 1):
        parser.error("dry-run에서는 --workers 1만 사용할 수 있습니다.")
    if args.workers is None:
        args.workers = 2 if args.apply else 1
    return args


def local_datetime_timestamp_ms(value, date_format):
    try:
        local = datetime.strptime(value, date_format).astimezone()
        return str(round(local.timestamp() * 1000))
    except (OSError, OverflowError, ValueError):
        return None


def match_filename(filename):
    path = Path(filename)
    stem, extension = path.stem, path.suffix.lower()
    for pattern, expression, flags in (
        ("numeric_timestamp", r"([0-9]{13})", re.ASCII),
        ("line_movie", r"LINE_MOVIE_([0-9]{13})", re.ASCII),
        ("kakaotalk", r"kakaotalk_([0-9]{13})", re.ASCII | re.IGNORECASE),
    ):
        match = re.fullmatch(expression, stem, flags)
        if match:
            timestamp_ms = match.group(1)
            break
    else:
        for pattern, expression, date_format in (
            (
                "video_local_minute",
                r"([0-9]{4}_[0-9]{2}_[0-9]{2} [0-9]{2}_[0-9]{2})(?: \([0-9]+\))?",
                "%Y_%m_%d %H_%M",
            ),
            (
                "vid_local_millisecond",
                r"VID_([0-9]{8}_[0-9]{6}_[0-9]{3})",
                "%Y%m%d_%H%M%S_%f",
            ),
            (
                "video_local_second",
                r"([0-9]{8}_[0-9]{6})",
                "%Y%m%d_%H%M%S",
            ),
        ):
            match = re.fullmatch(expression, stem, re.ASCII)
            if match:
                timestamp_ms = local_datetime_timestamp_ms(match.group(1), date_format)
                if timestamp_ms is None:
                    return None
                break
        else:
            return None
    supported = extension in SUPPORTED_EXTENSIONS
    if pattern in VIDEO_ONLY_PATTERNS and extension not in VIDEO_EXTENSIONS:
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
    return utc, utc.astimezone()


def offset_text(value):
    compact = value.strftime("%z")
    return f"{compact[:3]}:{compact[3:]}"


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


def target_tags(extension, utc, local):
    milliseconds = local.microsecond // 1000
    local_seconds = local.strftime("%Y:%m:%d %H:%M:%S")
    offset = offset_text(local)
    local_with_offset = local_seconds + offset
    local_precise = f"{local_seconds}.{milliseconds:03d}{offset}"
    utc_seconds = utc.strftime("%Y:%m:%d %H:%M:%S")
    if extension in {".jpg", ".jpeg"}:
        values = {
            "DateTimeOriginal": local_seconds,
            "CreateDate": local_seconds,
            "ModifyDate": local_seconds,
            "SubSecTimeOriginal": f"{milliseconds:03d}",
            "SubSecTimeDigitized": f"{milliseconds:03d}",
            "SubSecTime": f"{milliseconds:03d}",
            "OffsetTimeOriginal": offset,
            "OffsetTimeDigitized": offset,
            "OffsetTime": offset,
        }
        return {tag: values[tag.split(":", 1)[1]] for tag in JPEG_TAGS}
    if extension == ".png":
        values = {
            "PNG:CreationTime": local_with_offset,
            "XMP-exif:DateTimeOriginal": local_precise,
            "XMP-xmp:CreateDate": local_precise,
            "XMP-xmp:ModifyDate": local_precise,
        }
        return {tag: values[tag] for tag in PNG_TAGS}
    return {tag: utc_seconds for tag in VIDEO_TAGS}


def decode_output(data):
    return data.decode("utf-8", errors="replace").strip()


def exiftool_environment():
    if os.name != "nt":
        return None
    environment = os.environ.copy()
    for name in ("LANG", "LC_ALL", "LC_CTYPE"):
        environment.pop(name, None)
    return environment


class ExifToolProcessError(RuntimeError):
    pass


class ExifToolSession:
    def __init__(self, executable):
        try:
            self.process = subprocess.Popen(
                [
                    executable,
                    "-charset",
                    "filename=utf8",
                    "-stay_open",
                    "True",
                    "-@",
                    "-",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                env=exiftool_environment(),
                creationflags=NO_WINDOW,
            )
        except OSError as error:
            raise RuntimeError(f"ExifTool 실행 실패: {error}") from error
        self.command_id = 0
        self.error_lines = queue.SimpleQueue()
        self.error_thread = threading.Thread(target=self._read_errors, daemon=True)
        self.error_thread.start()

    def _read_errors(self):
        for line in self.process.stderr:
            self.error_lines.put(line)
        self.error_lines.put(None)

    def execute(self, arguments):
        if self.process.poll() is not None:
            raise ExifToolProcessError(
                f"ExifTool이 예기치 않게 종료되었습니다: {self.process.returncode}"
            )
        self.command_id += 1
        command_id = self.command_id
        status_marker = f"__MEDIA_DATE_FIXER_STATUS_{command_id}__="
        payload = "\n".join(
            [
                *arguments,
                "-echo4",
                status_marker + "${status}",
                f"-execute{command_id}",
                "",
            ]
        )
        try:
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise ExifToolProcessError(f"ExifTool 명령 전송 실패: {error}") from error

        output_lines = []
        ready = f"{{ready{command_id}}}"
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise ExifToolProcessError(
                    "ExifTool 응답을 받기 전에 프로세스가 종료되었습니다."
                )
            if line.rstrip("\r\n") == ready:
                break
            output_lines.append(line)

        error_lines = []
        while True:
            line = self.error_lines.get()
            if line is None:
                raise ExifToolProcessError("ExifTool 종료 상태를 받지 못했습니다.")
            text = line.rstrip("\r\n")
            if text.startswith(status_marker):
                try:
                    status = int(text[len(status_marker) :])
                except ValueError as error:
                    raise RuntimeError(f"ExifTool 종료 상태 해석 실패: {text}") from error
                break
            error_lines.append(line)
        return status, "".join(output_lines).strip(), "".join(error_lines).strip()

    def close(self):
        process = self.process
        if process.poll() is None:
            try:
                process.stdin.write("-stay_open\nFalse\n")
                process.stdin.flush()
                process.wait(timeout=10)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        self.error_thread.join(timeout=2)
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def run_exiftool(executable, arguments, session=None):
    if session is not None:
        return session.execute(arguments)
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
            env=exiftool_environment(),
            creationflags=NO_WINDOW,
        )
    except OSError as error:
        raise RuntimeError(f"ExifTool 실행 실패: {error}") from error
    return completed.returncode, decode_output(completed.stdout), decode_output(
        completed.stderr
    )


def exiftool_version(executable, session=None):
    code, version, error = run_exiftool(executable, ["-ver"], session)
    if code or not version:
        raise RuntimeError(error or "ExifTool 버전을 확인하지 못했습니다.")
    return version


def read_metadata(executable, path, video=False, session=None):
    arguments = ["-j", "-a", "-G1", "-s"]
    if video:
        arguments += [
            "-api",
            "QuickTimeUTC=1",
            "-d",
            "%Y-%m-%dT%H:%M:%S%z",
        ]
    arguments += ["-time:all", str(path)]
    code, output, error = run_exiftool(executable, arguments, session)
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
            if str(value) == "0000:00:00 00:00:00":
                continue
        entries.append((key, name, str(value)))
    return entries


def metadata_state(metadata, extension, planned, utc, local):
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
            expected = f"{local.microsecond // 1000:03d}"
            if value != expected:
                conflicts.append(f"{key}={value!r}")
        elif name.startswith("OffsetTime"):
            if value != offset_text(local):
                conflicts.append(f"{key}={value!r}")
        else:
            parsed = exif_datetime(value, local.tzinfo)
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


def write_metadata(executable, path, planned, no_backup, session=None):
    arguments = ["-P"]
    if no_backup:
        arguments.append("-overwrite_original")
    arguments += [f"-{tag}={value}" for tag, value in planned.items()]
    arguments.append(str(path))
    return run_exiftool(executable, arguments, session)


def restore_creation_time(path, timestamp_ns):
    if os.name != "nt":
        return
    ticks = timestamp_ns // 100 + WINDOWS_EPOCH_100NS
    creation = wintypes.FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)
    handle = CREATE_FILE(str(path), 0x100, 0x7, None, 3, 0, None)
    if handle == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not SET_FILE_TIME(handle, ctypes.byref(creation), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        CLOSE_HANDLE(handle)


def restore_access_and_modify_times(path, before):
    restore_creation_time(path, before.st_ctime_ns)
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
        or lower.endswith(".media-date-fixer-repair")
        or lower.endswith(".media-date-fixer-safety")
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


def classify_repair_kind(row):
    if (
        str(row.get("extension", "")).lower() not in {".jpg", ".jpeg"}
        or row.get("action") != "SKIPPED"
        or row.get("result") != "FAILED"
    ):
        return ""
    error = str(row.get("error", ""))
    signature = r"Error reading OtherImageStart data in IFD[01](?=$|[\s;])"
    if len(re.findall(signature, error)) != 1 or "OtherImageLength" in error:
        return ""
    if "Error reading" in re.sub(signature, "", error):
        return ""
    return REPAIR_KIND_JPEG_OTHER_IMAGE_START


def _run_exiftool_bytes(executable, arguments, data):
    try:
        completed = subprocess.run(
            [executable, *arguments, "-"],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
            env=exiftool_environment(),
            creationflags=NO_WINDOW,
        )
    except OSError as error:
        raise RuntimeError(f"ExifTool 실행 실패: {error}") from error
    return completed.returncode, completed.stdout, decode_output(completed.stderr)


def _metadata_from_jpeg_bytes(executable, data):
    code, output, error = _run_exiftool_bytes(
        executable, ["-j", "-a", "-G1", "-s", "-time:all"], data
    )
    if code or error:
        raise RuntimeError(error or f"ExifTool 메타데이터 검사 종료 코드 {code}")
    try:
        metadata = json.loads(output.decode("utf-8", errors="replace"))[0]
    except (json.JSONDecodeError, IndexError, TypeError) as problem:
        raise RuntimeError(f"ExifTool JSON 해석 실패: {problem}") from problem
    return {key: value for key, value in metadata.items() if key != "SourceFile"}


def _validate_jpeg_bytes(executable, data):
    code, output, error = _run_exiftool_bytes(
        executable,
        ["-j", "-a", "-G1", "-s", "-validate", "-warning", "-error"],
        data,
    )
    if code or error:
        raise RuntimeError(error or f"ExifTool 검증 종료 코드 {code}")
    try:
        validation = json.loads(output.decode("utf-8", errors="replace"))[0]
    except (json.JSONDecodeError, IndexError, TypeError) as problem:
        raise RuntimeError(f"ExifTool 검증 JSON 해석 실패: {problem}") from problem
    messages = []
    for key, value in validation.items():
        name = key.rsplit(":", 1)[-1]
        if name.startswith("Error"):
            messages.append(str(value))
        elif name.startswith("Warning") and re.search(
            r"OtherImage(?:Start|Length)|Entries in IFD0 (?:were|are) out of (?:sequence|order)",
            str(value),
        ):
            messages.append(str(value))
    if messages:
        raise RuntimeError("ExifTool 검증 실패: " + "; ".join(messages))


def _jpeg_fingerprint(data):
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "JPEG":
                raise RuntimeError(f"JPEG 형식이 아님: {image.format}")
            image.load()
            icc = image.info.get("icc_profile")
            return (
                image.size,
                image.mode,
                hashlib.sha256(image.tobytes()).hexdigest(),
                None if icc is None else hashlib.sha256(icc).hexdigest(),
            )
    except (OSError, UnidentifiedImageError) as error:
        raise RuntimeError(f"Pillow JPEG 완전 디코드 실패: {error}") from error


def _rebuild_jpeg_exif(executable, data):
    code, output, error = _run_exiftool_bytes(
        executable,
        ["-q", "-q", "-m", "-EXIF:All=", "-All:All<EXIF:All", "-o", "-"],
        data,
    )
    if code or not output:
        raise RuntimeError(error or f"ExifTool EXIF 재구성 종료 코드 {code}")
    return output


def _write_jpeg_dates(executable, data, planned):
    arguments = ["-q", "-q", "-m"]
    arguments += [f"-{tag}={value}" for tag, value in planned.items()]
    arguments += ["-o", "-"]
    code, output, error = _run_exiftool_bytes(executable, arguments, data)
    if code or error or not output:
        raise RuntimeError(error or f"ExifTool 날짜 기록 종료 코드 {code}")
    return output


def _current_repair_kind(executable, path, row):
    code, output, error = run_exiftool(
        executable, ["-j", "-a", "-G1", "-s", "-time:all", str(path)]
    )
    probe = dict(row)
    probe["error"] = error or (output if code else "")
    return classify_repair_kind(probe)


def _repair_path(root, row):
    root = Path(root).resolve(strict=True)
    raw = Path(str(row.get("filepath", "")))
    if not raw.is_absolute() or raw.name != row.get("filename"):
        raise RuntimeError("CSV 파일 경로 또는 파일명이 올바르지 않음")
    path = raw.resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError as error:
        raise RuntimeError("CSV 파일 경로가 선택한 루트 밖에 있음") from error
    if raw.is_symlink() or path.suffix.lower() not in {".jpg", ".jpeg"}:
        raise RuntimeError("복구 대상이 로컬 JPEG 일반 파일이 아님")
    details = path.stat()
    if not stat.S_ISREG(details.st_mode) or getattr(
        details, "st_file_attributes", 0
    ) & FILE_ATTRIBUTE_REPARSE_POINT:
        raise RuntimeError("복구 대상이 로컬 JPEG 일반 파일이 아님")
    if path.suffix.lower() != str(row.get("extension", "")).lower():
        raise RuntimeError("현재 확장자가 CSV 기록과 다름")
    match = match_filename(path.name)
    if (
        not match
        or not match["supported"]
        or match["extension"] not in {".jpg", ".jpeg"}
        or str(match["timestamp_ms"]) != str(row.get("timestamp_ms", ""))
        or match["pattern"] != row.get("pattern")
    ):
        raise RuntimeError("현재 파일명이 CSV의 날짜 정보와 다름")
    return path, match


def _write_exclusive(path, data, mode, timestamps):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    restore_access_and_modify_times(path, timestamps)


def _verify_repair_output(
    executable,
    data,
    original_fingerprint,
    original_size,
    extension,
    planned,
    utc,
    local,
    preserved_entries,
):
    if _jpeg_fingerprint(data) != original_fingerprint:
        raise RuntimeError("픽셀, 크기, 색상 모드 또는 ICC 프로필이 원본과 다름")
    if not reasonable_output_size(original_size, len(data)):
        raise RuntimeError(f"비정상 파일 크기: {original_size} -> {len(data)}")
    _validate_jpeg_bytes(executable, data)
    metadata = _metadata_from_jpeg_bytes(executable, data)
    if preserved_entries is None:
        state, detail = metadata_state(metadata, extension, planned, utc, local)
        if state != "ALREADY_CORRECT":
            raise RuntimeError(detail or "대상 날짜 태그가 모두 기록되지 않음")
    elif sorted(target_entries(metadata, extension)) != preserved_entries:
        raise RuntimeError("기존 충돌 날짜가 보존되지 않음")
    return metadata


def repair_jpeg_other_image(
    root,
    row,
    *,
    exiftool,
    overwrite_existing=False,
    no_backup=False,
):
    result_row = {field: row.get(field, "") for field in CSV_FIELDS}
    result_row["repair_kind"] = classify_repair_kind(row)
    path = None
    before = None
    replaced = False
    safety_path = None
    temp_path = None
    try:
        if result_row["repair_kind"] != REPAIR_KIND_JPEG_OTHER_IMAGE_START:
            raise RuntimeError("지원되는 복구 오류가 아님")
        path, match = _repair_path(root, row)
        before = path.stat()
        if _current_repair_kind(exiftool, path, row) != result_row["repair_kind"]:
            raise RuntimeError("현재 파일의 ExifTool 오류 서명이 CSV 기록과 다름")
        original = path.read_bytes()
        if not same_source_state(before, path.stat()) or len(original) != before.st_size:
            raise RuntimeError("파일이 복구 검사 중 변경됨")
        original_fingerprint = _jpeg_fingerprint(original)
        rebuilt = _rebuild_jpeg_exif(exiftool, original)
        metadata = _metadata_from_jpeg_bytes(exiftool, rebuilt)
        utc, local = timestamp_datetimes(match["timestamp_ms"])
        planned = target_tags(match["extension"], utc, local)
        state, _ = metadata_state(metadata, match["extension"], planned, utc, local)
        preserve_conflict = state == "CONFLICT" and not overwrite_existing
        preserved_entries = (
            sorted(target_entries(metadata, match["extension"]))
            if preserve_conflict
            else None
        )
        repaired = (
            rebuilt
            if preserve_conflict or state == "ALREADY_CORRECT"
            else _write_jpeg_dates(exiftool, rebuilt, planned)
        )
        _verify_repair_output(
            exiftool,
            repaired,
            original_fingerprint,
            before.st_size,
            match["extension"],
            planned,
            utc,
            local,
            preserved_entries,
        )

        backup_path = Path(str(path) + "_original")
        safety_path = Path(str(path) + ".media-date-fixer-safety")
        temp_path = Path(str(path) + ".media-date-fixer-repair")
        protected = backup_path if not no_backup else safety_path
        for candidate in (protected, temp_path):
            if candidate.exists():
                raise RuntimeError(f"기존 백업 또는 임시 파일을 덮어쓰지 않음: {candidate}")
        if path.read_bytes() != original or not same_source_state(before, path.stat()):
            raise RuntimeError("파일이 쓰기 직전 변경됨")
        mode = stat.S_IMODE(before.st_mode)
        _write_exclusive(protected, original, mode, before)
        if protected.read_bytes() != original:
            raise RuntimeError(f"안전 백업 검증 실패: {protected}")
        restore_access_and_modify_times(protected, before)
        _write_exclusive(temp_path, repaired, mode, before)
        os.replace(temp_path, path)
        replaced = True

        final_data = path.read_bytes()
        if final_data != repaired:
            raise RuntimeError("원자적 교체 후 파일 바이트가 준비된 출력과 다름")
        _verify_repair_output(
            exiftool,
            final_data,
            original_fingerprint,
            before.st_size,
            match["extension"],
            planned,
            utc,
            local,
            preserved_entries,
        )
        restore_access_and_modify_times(path, before)
        final = path.stat()
        if final.st_atime_ns != before.st_atime_ns:
            raise RuntimeError("파일 시스템 접근 시각 보존 실패")
        if final.st_mtime_ns != before.st_mtime_ns:
            raise RuntimeError("파일 시스템 수정 시각 보존 실패")
        if os.name == "nt" and final.st_ctime_ns != before.st_ctime_ns:
            raise RuntimeError("파일 시스템 생성 시각 보존 실패")
        if no_backup:
            os.unlink(safety_path)
        result_row.update(
            action="REPAIRED",
            result="REPAIRED_CONFLICT" if preserve_conflict else "REPAIRED",
            error="",
        )
        return result_row
    except (OSError, RuntimeError, ValueError) as error:
        rollback_error = None
        if replaced and path is not None and before is not None:
            try:
                if no_backup:
                    os.replace(safety_path, path)
                else:
                    _write_exclusive(temp_path, original, stat.S_IMODE(before.st_mode), before)
                    os.replace(temp_path, path)
                if path.read_bytes() != original:
                    raise RuntimeError("롤백 후 원본 바이트가 일치하지 않음")
                restore_access_and_modify_times(path, before)
            except (OSError, RuntimeError) as problem:
                rollback_error = problem
        elif path is not None and before is not None and path.exists():
            try:
                restore_access_and_modify_times(path, before)
            except OSError as problem:
                rollback_error = problem
        message = str(error)
        if rollback_error:
            message += f"; 롤백 실패: {rollback_error}"
        result_row.update(action="SKIPPED", result="REPAIR_FAILED", error=message)
        return result_row


def repair_from_log(
    log_path,
    root,
    *,
    exiftool,
    overwrite_existing=False,
    no_backup=False,
    log_dir=None,
    emit=None,
    on_progress=None,
    should_stop=None,
):
    emit = emit or console_emit
    root = Path(root).resolve()
    log_path = Path(log_path)
    log_dir = Path(log_dir) if log_dir else log_path.parent
    try:
        with log_path.open(encoding="utf-8-sig", newline="") as source:
            candidates = [
                row for row in csv.DictReader(source) if classify_repair_kind(row)
            ]
        log_dir.mkdir(parents=True, exist_ok=True)
        repair_log = log_dir / datetime.now().strftime(
            "repair_media_dates_%Y%m%d_%H%M%S_%f.csv"
        )
        stats = {
            "total": len(candidates),
            "repaired": 0,
            "repaired_conflict": 0,
            "repair_failed": 0,
            "cancelled": 0,
        }
        with repair_log.open("x", encoding="utf-8-sig", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for index, row in enumerate(candidates):
                if should_stop and should_stop():
                    stats["cancelled"] = 1
                    break
                repaired = repair_jpeg_other_image(
                    root,
                    row,
                    exiftool=exiftool,
                    overwrite_existing=overwrite_existing,
                    no_backup=no_backup,
                )
                writer.writerow(repaired)
                key = {
                    "REPAIRED": "repaired",
                    "REPAIRED_CONFLICT": "repaired_conflict",
                    "REPAIR_FAILED": "repair_failed",
                }[repaired["result"]]
                stats[key] += 1
                if on_progress:
                    on_progress(index + 1, len(candidates), repaired["filename"])
    except (OSError, csv.Error, KeyError, RuntimeError) as error:
        emit(f"ERROR: JPEG 복구 실행 실패: {error}", error=True)
        return 2, None, None
    emit(f"Repair CSV log: {repair_log}")
    if stats["cancelled"]:
        return 130, stats, repair_log
    return (1 if stats["repair_failed"] else 0), stats, repair_log


def run_self_test():
    accepted = {
        "1579242283509.jpg": "numeric_timestamp",
        "1720495695578.png": "numeric_timestamp",
        "LINE_MOVIE_1545316978976.mp4": "line_movie",
        "LINE_MOVIE_1545649197800.mp4": "line_movie",
        "kakaotalk_1572348095097.mp4": "kakaotalk",
        "KakaoTalk_1572348095097.MOV": "kakaotalk",
        "2025_06_13 22_12.mp4": "video_local_minute",
        "VID_20251001_232540_472.mp4": "vid_local_millisecond",
        "2023_08_04 13_16 (1).mp4": "video_local_minute",
        "20250228_172309.mp4": "video_local_second",
    }
    rejected = (
        "EYS7454015450431906237.mp4",
        "IMG_1579242283509.jpg",
        "1579242283509_1.jpg",
        "LINE_MOVIE_1545316978976_1.mp4",
        "my_kakaotalk_1572348095097.mp4",
        "123456789012.jpg",
        "12345678901234.jpg",
        "2025_02_30 22_12.mp4",
        "VID_20251001_252540_472.mp4",
        "2023_08_04 13_16 (x).mp4",
        "20250228_172309_1.mp4",
    )
    for filename, pattern in accepted.items():
        match = match_filename(filename)
        assert match and match["supported"] and match["pattern"] == pattern, filename
    for filename in rejected:
        assert match_filename(filename) is None, filename
    assert match_filename("LINE_MOVIE_1545316978976.jpg")["supported"] is False
    assert match_filename("20250228_172309.jpg")["supported"] is False
    future = match_filename("VID_23590721_204419_081.mp4")
    assert timestamp_datetimes(future["timestamp_ms"])[0] > datetime.now(UTC) + timedelta(
        days=1
    )
    expected_utc = {
        "1579242283509": "2020-01-17 06:24:43.509",
        "1545316978976": "2018-12-20 14:42:58.976",
        "1545649197800": "2018-12-24 10:59:57.800",
        "1572348095097": "2019-10-29 11:21:35.097",
        "1720495695578": "2024-07-09 03:28:15.578",
    }
    for value, text in expected_utc.items():
        utc, local = timestamp_datetimes(value)
        actual = utc.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        assert actual == text, (value, actual, text)
        assert local.astimezone(UTC) == utc
    expected_local = {
        "2025_06_13 22_12.mp4": "2025-06-13 22:12:00.000",
        "VID_20251001_232540_472.mp4": "2025-10-01 23:25:40.472",
        "2023_08_04 13_16 (1).mp4": "2023-08-04 13:16:00.000",
        "20250228_172309.mp4": "2025-02-28 17:23:09.000",
    }
    for filename, text in expected_local.items():
        match = match_filename(filename)
        utc, local = timestamp_datetimes(match["timestamp_ms"])
        actual = local.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        assert actual == text, (filename, actual, text)
        assert local.astimezone(UTC) == utc
    sample_utc, _ = timestamp_datetimes("1579242283509")
    sample_local = sample_utc.astimezone(timezone(timedelta(hours=-4)))
    jpeg = target_tags(".jpg", sample_utc, sample_local)
    png = target_tags(".png", sample_utc, sample_local)
    video = target_tags(".mp4", sample_utc, sample_local)
    assert offset_text(sample_utc) == "+00:00"
    assert jpeg["EXIF:OffsetTimeOriginal"] == "-04:00"
    assert png["PNG:CreationTime"].endswith("-04:00")
    assert set(video.values()) == {sample_utc.strftime("%Y:%m:%d %H:%M:%S")}
    assert (
        metadata_state(jpeg, ".jpg", jpeg, sample_utc, sample_local)[0]
        == "ALREADY_CORRECT"
    )
    empty_video = {tag: "0000:00:00 00:00:00" for tag in VIDEO_TAGS}
    assert (
        metadata_state(empty_video, ".mp4", video, sample_utc, sample_local)[0]
        == "WRITE"
    )
    if os.name == "nt":
        assert not {"LANG", "LC_ALL", "LC_CTYPE"} & exiftool_environment().keys()
    repairable = {
        "extension": ".jpg",
        "action": "SKIPPED",
        "result": "FAILED",
        "error": (
            "Warning: Entries in IFD0 were out of sequence\n"
            "Error reading OtherImageStart data in IFD1"
        ),
    }
    assert classify_repair_kind(repairable) == REPAIR_KIND_JPEG_OTHER_IMAGE_START
    for change in (
        {"extension": ".png"},
        {"action": "MODIFIED"},
        {"result": "VERIFY_FAILED"},
        {"error": "Error reading OtherImageLength data in IFD0"},
        {"error": "Error reading OtherImageStart data in IFD2"},
        {
            "error": (
                "Error reading OtherImageStart data in IFD0; "
                "Error reading directory"
            )
        },
    ):
        assert not classify_repair_kind(repairable | change), change
    print("SELF_TEST PASSED")


def console_emit(message, error=False):
    print(message, file=sys.stderr if error else sys.stdout)


def print_summary(stats, emit=console_emit):
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
        ("Cancelled", "cancelled"),
    )
    emit("\nSummary")
    for label, key in labels:
        emit(f"{label}: {stats[key]}")
    emit("Patterns")
    for pattern in PATTERN_NAMES:
        emit(f"{pattern}: {stats['patterns'][pattern]}")


def _new_stats():
    return {
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
        "cancelled": 0,
        "patterns": {pattern: 0 for pattern in PATTERN_NAMES},
    }


def _skip_row(row, result, error=""):
    row.update(action="SKIPPED", result=result, error=error)
    return row


def _process_file(
    item,
    *,
    session,
    exiftool,
    apply_changes,
    overwrite_existing,
    no_backup,
    log_all_skips,
    lower_bound,
    upper_bound,
):
    path, scanned_details, scan_error, match = item
    row = blank_row(path)
    counts = {"patterns": {}}
    session_broken = False

    def count(key):
        counts[key] = counts.get(key, 0) + 1

    if scanned_details is None:
        count("failed")
        return _skip_row(row, "UNREADABLE", scan_error), counts, False

    count("total")
    if is_temporary_or_backup(path.name):
        count("pattern_mismatch")
        return (
            _skip_row(row, "SKIPPED", "TEMP_OR_BACKUP") if log_all_skips else None,
            counts,
            False,
        )
    if not match:
        count("pattern_mismatch")
        return (
            _skip_row(row, "SKIPPED", "PATTERN_MISMATCH") if log_all_skips else None,
            counts,
            False,
        )

    count("matched")
    counts["patterns"][match["pattern"]] = 1
    row.update(
        pattern=match["pattern"],
        timestamp_ms=match["timestamp_ms"],
        precision="seconds" if match["extension"] in VIDEO_EXTENSIONS else "milliseconds",
        backup_expected="no" if no_backup else "yes",
    )
    if not match["supported"]:
        count("unsupported")
        return _skip_row(row, "UNSUPPORTED_EXTENSION"), counts, False

    attributes = getattr(scanned_details, "st_file_attributes", 0)
    if scan_error == "NOT_LOCAL" or attributes & PLACEHOLDER_FLAGS:
        count("not_local")
        return _skip_row(row, "NOT_LOCAL"), counts, False
    if not os.access(path, os.R_OK):
        count("not_local")
        return _skip_row(row, "UNREADABLE"), counts, False

    try:
        utc, local = timestamp_datetimes(match["timestamp_ms"])
    except (OverflowError, OSError, ValueError) as error:
        count("out_of_range")
        return _skip_row(row, "OUT_OF_RANGE", str(error)), counts, False
    row.update(
        target_datetime_utc=utc.isoformat(timespec="milliseconds"),
        target_datetime_local=local.isoformat(timespec="milliseconds"),
    )
    if not (lower_bound <= utc <= upper_bound):
        count("out_of_range")
        return _skip_row(row, "OUT_OF_RANGE"), counts, False

    planned = target_tags(match["extension"], utc, local)
    row["planned_tags"] = compact_json(planned)
    video = match["extension"] in VIDEO_EXTENSIONS
    try:
        before = path.stat()
        try:
            metadata = read_metadata(exiftool, path, video, session)
        finally:
            restore_access_and_modify_times(path, before)
        after_read = path.stat()
    except (OSError, RuntimeError) as error:
        count("failed")
        session_broken = isinstance(error, ExifToolProcessError)
        return _skip_row(row, "FAILED", str(error)), counts, session_broken
    row["existing_metadata"] = compact_json(metadata)
    if not same_source_state(before, after_read):
        count("failed")
        return (
            _skip_row(row, "FAILED", "파일 크기 또는 수정 시각이 메타데이터 검사 중 변경됨"),
            counts,
            False,
        )

    state, detail = metadata_state(metadata, match["extension"], planned, utc, local)
    if state == "ALREADY_CORRECT":
        count("already")
        return _skip_row(row, "ALREADY_CORRECT"), counts, False
    if state == "CONFLICT" and not overwrite_existing:
        count("conflicts")
        return _skip_row(row, "CONFLICT", detail), counts, False
    if not apply_changes:
        count("dry_run")
        row.update(
            action="DRY_RUN",
            result="DRY_RUN",
            error=("overwrite enabled: " + detail) if detail else "",
        )
        return row, counts, False

    original_system_times = system_times(metadata)
    if os.name == "nt" and "FileCreateDate" not in original_system_times:
        count("failed")
        return (
            _skip_row(row, "FAILED", "Windows FileCreateDate를 읽어 보존 여부를 확인할 수 없음"),
            counts,
            False,
        )
    backup_path = Path(str(path) + "_original")
    if not no_backup and backup_path.exists():
        count("failed")
        return (
            _skip_row(row, "FAILED", f"기존 백업을 덮어쓰지 않음: {backup_path}"),
            counts,
            False,
        )
    try:
        current = path.stat()
        if not same_source_state(before, current):
            raise RuntimeError("파일 크기 또는 수정 시각이 쓰기 직전 변경됨")
        code, output, warning = write_metadata(
            exiftool, path, planned, no_backup, session
        )
        restore_access_and_modify_times(path, before)
        if code:
            raise RuntimeError(warning or output or f"ExifTool 쓰기 종료 코드 {code}")
        verified_metadata = read_metadata(exiftool, path, video, session)
        restore_access_and_modify_times(path, before)
        final_details = path.stat()
        verify_state, verify_detail = metadata_state(
            verified_metadata, match["extension"], planned, utc, local
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
        if os.name == "nt" and final_details.st_ctime_ns != before.st_ctime_ns:
            verification_errors.append("파일 시스템 생성 시각 보존 실패")
        if original_system_times != final_system_times:
            verification_errors.append(
                "파일 시스템 생성/수정 시각 보존 실패: "
                f"{original_system_times!r} -> {final_system_times!r}"
            )
        if not no_backup and not backup_path.exists():
            verification_errors.append("예상한 _original 백업이 생성되지 않음")
        if warning:
            row["error"] = f"ExifTool warning: {warning}"
        if verification_errors:
            count("verify_failed")
            row.update(
                action="MODIFIED",
                result="VERIFY_FAILED",
                error="; ".join(
                    ([row["error"]] if row["error"] else []) + verification_errors
                )
                + (f"; 백업 확인: {backup_path}" if not no_backup else ""),
            )
        else:
            count("modified")
            row.update(action="MODIFIED", result="MODIFIED")
    except (OSError, RuntimeError) as error:
        session_broken = isinstance(error, ExifToolProcessError)
        try:
            if path.exists():
                restore_access_and_modify_times(path, before)
        except OSError as restore_error:
            error = RuntimeError(f"{error}; 파일 시각 복원 실패: {restore_error}")
        count("failed")
        row.update(action="MODIFIED", result="FAILED", error=str(error))
    return row, counts, session_broken


def _record_result(stats, writer, result):
    row, counts, _ = result
    for key, value in counts.items():
        if key == "patterns":
            for pattern, amount in value.items():
                stats["patterns"][pattern] += amount
        else:
            stats[key] += value
    if row is not None:
        row["repair_kind"] = classify_repair_kind(row)
        writer.writerow(row)


def _start_session(exiftool):
    session = ExifToolSession(exiftool)
    try:
        return session, exiftool_version(exiftool, session)
    except RuntimeError:
        session.close()
        raise


def process_media(
    root,
    *,
    apply_changes=False,
    overwrite_existing=False,
    no_backup=False,
    log_all_skips=False,
    exiftool="exiftool",
    log_dir=None,
    emit=console_emit,
    on_progress=None,
    should_stop=None,
    workers=1,
):
    if workers not in (1, 2, 4):
        emit("ERROR: workers는 1, 2, 4 중 하나여야 합니다.", error=True)
        return 2, None, None
    actual_workers = workers if apply_changes else 1
    root = Path(os.path.abspath(root))
    if not root.exists() or not root.is_dir():
        emit(f"ERROR: 루트 폴더가 없거나 디렉터리가 아닙니다: {root}", error=True)
        return 2, None, None
    root_details = root.stat()
    if getattr(root_details, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        emit(f"ERROR: 루트가 재분석 지점입니다: {root}", error=True)
        return 2, None, None
    upper_bound = datetime.now(UTC) + timedelta(days=1)
    lower_bound = datetime(2000, 1, 1, tzinfo=UTC)
    log_dir = Path(log_dir) if log_dir else Path.cwd()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        emit(f"ERROR: 로그 폴더 생성 실패: {error}", error=True)
        return 2, None, None
    log_path = log_dir / datetime.now().strftime(
        "fix_media_dates_%Y%m%d_%H%M%S_%f.csv"
    )
    sessions = []
    try:
        for _ in range(actual_workers):
            session, current_version = _start_session(exiftool)
            sessions.append(session)
            if len(sessions) == 1:
                version = current_version
    except RuntimeError as error:
        for session in sessions:
            session.close()
        emit(f"ERROR: {error}", error=True)
        return 2, None, None
    stats = _new_stats()

    emit(f"ExifTool: {version}")
    emit(f"Mode: {'APPLY' if apply_changes else 'DRY-RUN'}")
    emit(f"Workers: {actual_workers}")
    emit(f"Root: {root}")
    emit(f"CSV: {log_path}")
    emit("주의: 전체 적용 전 OneDrive 동기화를 일시 중지하고 테스트 복사본으로 검증하세요.")
    if apply_changes:
        emit(
            "Windows 생성 시각은 ExifTool -P와 설치된 Win32 API 지원에 의존하며, "
            "각 파일에서 적용 전후 값을 확인합니다. 확인할 수 없으면 수정하지 않습니다."
        )

    groups = {"jpeg": [], "png": [], "video": [], "other": []}
    if on_progress:
        on_progress(0, None, "파일 이름 분석 중")
    scanned = 0
    for path, scanned_details, scan_error in scan_files(root):
        if should_stop and should_stop():
            stats["cancelled"] = 1
            for group in groups.values():
                group.clear()
            break
        scanned += 1
        match = (
            match_filename(path.name)
            if scanned_details is not None and not is_temporary_or_backup(path.name)
            else None
        )
        if not match or not match["supported"]:
            family = "other"
        elif match["extension"] in {".jpg", ".jpeg"}:
            family = "jpeg"
        elif match["extension"] == ".png":
            family = "png"
        else:
            family = "video"
        groups[family].append((path, scanned_details, scan_error, match))
        if on_progress and scanned % 100 == 0:
            on_progress(scanned, None, "파일 이름 분석 중")
    files = [item for group in groups.values() for item in group]
    total_files = len(files)

    processed = 0
    worker_failure = False
    try:
        with log_path.open("x", encoding="utf-8-sig", newline="") as log_file:
            writer = csv.DictWriter(log_file, fieldnames=CSV_FIELDS)
            writer.writeheader()
            common = {
                "exiftool": exiftool,
                "apply_changes": apply_changes,
                "overwrite_existing": overwrite_existing,
                "no_backup": no_backup,
                "log_all_skips": log_all_skips,
                "lower_bound": lower_bound,
                "upper_bound": upper_bound,
            }
            if not apply_changes:
                for item in files:
                    if should_stop and should_stop():
                        stats["cancelled"] = 1
                        break
                    result = _process_file(item, session=sessions[0], **common)
                    _record_result(stats, writer, result)
                    processed += 1
                    if on_progress:
                        on_progress(processed, total_files, item[0].name)
            else:
                available = list(sessions)
                pending = {}
                buffered = {}
                next_submit = 0
                next_write = 0
                stop_scheduling = False
                with ThreadPoolExecutor(max_workers=actual_workers) as executor:
                    while pending or (
                        next_submit < total_files and not stop_scheduling
                    ):
                        if should_stop and should_stop():
                            stats["cancelled"] = 1
                            stop_scheduling = True
                        while (
                            available
                            and next_submit < total_files
                            and not stop_scheduling
                        ):
                            session = available.pop()
                            future = executor.submit(
                                _process_file,
                                files[next_submit],
                                session=session,
                                **common,
                            )
                            pending[future] = (next_submit, session)
                            next_submit += 1
                        if not pending:
                            break
                        done, _ = wait(pending, return_when=FIRST_COMPLETED)
                        for future in done:
                            index, session = pending.pop(future)
                            result = future.result()
                            buffered[index] = result
                            processed += 1
                            if on_progress:
                                on_progress(processed, total_files, files[index][0].name)
                            if result[2]:
                                session.close()
                                try:
                                    replacement, _ = _start_session(exiftool)
                                except RuntimeError as error:
                                    emit(
                                        f"ERROR: ExifTool 워커 재시작 실패: {error}",
                                        error=True,
                                    )
                                    worker_failure = True
                                    stop_scheduling = True
                                else:
                                    sessions.remove(session)
                                    sessions.append(replacement)
                                    available.append(replacement)
                            else:
                                available.append(session)
                        while next_write in buffered:
                            _record_result(stats, writer, buffered.pop(next_write))
                            next_write += 1
    finally:
        for session in sessions:
            session.close()

    if on_progress:
        on_progress(processed, total_files, "중단됨" if stats["cancelled"] else "완료")
    if stats["cancelled"]:
        emit("취소 요청에 따라 파일 사이에서 안전하게 중단했습니다.")
    print_summary(stats, emit)
    emit(f"CSV log: {log_path}")
    if worker_failure:
        code = 1
    elif stats["cancelled"]:
        code = 130
    elif stats["failed"] or stats["verify_failed"]:
        code = 1
    else:
        code = 0
    return code, stats, log_path


def main():
    args = parse_args()
    if args.self_test:
        run_self_test()
        return 0
    code, _, _ = process_media(
        args.root,
        apply_changes=args.apply,
        overwrite_existing=args.overwrite_existing,
        no_backup=args.no_backup,
        log_all_skips=args.log_all_skips,
        exiftool=args.exiftool,
        workers=args.workers,
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
