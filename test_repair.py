import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import fix_media_dates as core


class JpegRepairTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exiftool = Path("exiftool-13.59_64/exiftool.exe").resolve()
        if not cls.exiftool.is_file():
            raise unittest.SkipTest("bundled ExifTool not available")
        Path("build").mkdir(exist_ok=True)

    @staticmethod
    def damaged_jpeg(path, *, conflict=False):
        exif = Image.Exif()
        exif[0x010F] = "Media Date Fixer test"
        exif[0x0201] = 999999
        exif[0x0202] = 100
        if conflict:
            exif[0x0132] = "2010:01:02 03:04:05"
        Image.new("RGB", (16, 12), (12, 34, 56)).save(
            path,
            "JPEG",
            quality=90,
            exif=exif,
            icc_profile=b"test-icc-profile",
        )
        data = bytearray(path.read_bytes())
        tiff = data.index(b"Exif\x00\x00") + 6
        byteorder = "little" if data[tiff : tiff + 2] == b"II" else "big"
        ifd = tiff + int.from_bytes(data[tiff + 4 : tiff + 8], byteorder)
        count = int.from_bytes(data[ifd : ifd + 2], byteorder)
        entries = [
            bytes(data[ifd + 2 + index * 12 : ifd + 14 + index * 12])
            for index in range(count)
        ]
        data[ifd + 2 : ifd + 2 + count * 12] = b"".join(reversed(entries))
        path.write_bytes(data)
        return bytes(data)

    @staticmethod
    def row(path):
        match = core.match_filename(path.name)
        row = core.blank_row(path.resolve())
        row.update(
            pattern=match["pattern"],
            timestamp_ms=match["timestamp_ms"],
            action="SKIPPED",
            result="FAILED",
            error=(
                "Warning: Entries in IFD0 were out of sequence\n"
                "Error reading OtherImageStart data in IFD0"
            ),
        )
        return row

    def test_real_exiftool_rebuild_preserves_pixels_icc_dates_and_backup(self):
        with tempfile.TemporaryDirectory(dir="build") as temporary:
            root = Path(temporary).resolve()
            path = root / "1579242283509.jpg"
            original = self.damaged_jpeg(path)
            original_fingerprint = core._jpeg_fingerprint(original)
            with self.assertRaises(RuntimeError):
                core._validate_jpeg_bytes(str(self.exiftool), original)

            result = core.repair_jpeg_other_image(
                root, self.row(path), exiftool=str(self.exiftool)
            )

            self.assertEqual(result["result"], "REPAIRED", result["error"])
            repaired = path.read_bytes()
            self.assertEqual(core._jpeg_fingerprint(repaired), original_fingerprint)
            core._validate_jpeg_bytes(str(self.exiftool), repaired)
            metadata = core._metadata_from_jpeg_bytes(str(self.exiftool), repaired)
            match = core.match_filename(path.name)
            utc, local = core.timestamp_datetimes(match["timestamp_ms"])
            planned = core.target_tags(".jpg", utc, local)
            self.assertEqual(
                core.metadata_state(metadata, ".jpg", planned, utc, local)[0],
                "ALREADY_CORRECT",
            )
            self.assertEqual(Path(str(path) + "_original").read_bytes(), original)

    def test_initial_apply_failure_is_classified_for_repair(self):
        with tempfile.TemporaryDirectory(dir="build") as temporary:
            root = Path(temporary).resolve()
            path = root / "1579242283509.jpg"
            original = self.damaged_jpeg(path)
            session = core.ExifToolSession(str(self.exiftool))
            try:
                result = core._process_file(
                    (path, path.stat(), "", core.match_filename(path.name)),
                    session=session,
                    exiftool=str(self.exiftool),
                    apply_changes=True,
                    overwrite_existing=False,
                    no_backup=True,
                    log_all_skips=False,
                    lower_bound=core.datetime(2000, 1, 1, tzinfo=core.UTC),
                    upper_bound=core.datetime.now(core.UTC) + core.timedelta(days=1),
                )
            finally:
                session.close()
            row = result[0]
            self.assertEqual((row["action"], row["result"]), ("SKIPPED", "FAILED"))
            self.assertEqual(
                core.classify_repair_kind(row),
                core.REPAIR_KIND_JPEG_OTHER_IMAGE_START,
            )
            self.assertEqual(path.read_bytes(), original)

    def test_conflict_is_preserved_without_permanent_backup(self):
        with tempfile.TemporaryDirectory(dir="build") as temporary:
            root = Path(temporary).resolve()
            path = root / "1579242283509.jpeg"
            self.damaged_jpeg(path, conflict=True)
            with patch.object(
                core,
                "_current_repair_kind",
                return_value=core.REPAIR_KIND_JPEG_OTHER_IMAGE_START,
            ):
                result = core.repair_jpeg_other_image(
                    root,
                    self.row(path),
                    exiftool=str(self.exiftool),
                    no_backup=True,
                )

            self.assertEqual(result["result"], "REPAIRED_CONFLICT", result["error"])
            metadata = core._metadata_from_jpeg_bytes(
                str(self.exiftool), path.read_bytes()
            )
            self.assertEqual(metadata["IFD0:ModifyDate"], "2010:01:02 03:04:05")
            self.assertFalse(Path(str(path) + "_original").exists())
            self.assertFalse(Path(str(path) + ".media-date-fixer-safety").exists())

    def test_post_write_failure_rolls_back_original(self):
        with tempfile.TemporaryDirectory(dir="build") as temporary:
            root = Path(temporary).resolve()
            path = root / "1579242283509.jpg"
            original = self.damaged_jpeg(path)
            verify = core._verify_repair_output
            calls = 0

            def fail_second_verification(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("forced post-write failure")
                return verify(*args, **kwargs)

            with (
                patch.object(
                    core,
                    "_current_repair_kind",
                    return_value=core.REPAIR_KIND_JPEG_OTHER_IMAGE_START,
                ),
                patch.object(
                    core,
                    "_verify_repair_output",
                    side_effect=fail_second_verification,
                ),
            ):
                result = core.repair_jpeg_other_image(
                    root,
                    self.row(path),
                    exiftool=str(self.exiftool),
                    no_backup=True,
                )

            self.assertEqual(result["result"], "REPAIR_FAILED")
            self.assertIn("forced post-write failure", result["error"])
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(Path(str(path) + ".media-date-fixer-safety").exists())

    def test_existing_backup_or_temp_is_never_overwritten(self):
        cases = (
            ("_original", False),
            (".media-date-fixer-safety", True),
            (".media-date-fixer-repair", False),
        )
        for suffix, no_backup in cases:
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory(
                dir="build"
            ) as temporary:
                root = Path(temporary).resolve()
                path = root / "1579242283509.jpg"
                original = self.damaged_jpeg(path)
                protected = Path(str(path) + suffix)
                protected.write_bytes(b"existing")
                with patch.object(
                    core,
                    "_current_repair_kind",
                    return_value=core.REPAIR_KIND_JPEG_OTHER_IMAGE_START,
                ):
                    result = core.repair_jpeg_other_image(
                        root,
                        self.row(path),
                        exiftool=str(self.exiftool),
                        no_backup=no_backup,
                    )
                self.assertEqual(result["result"], "REPAIR_FAILED")
                self.assertIn("덮어쓰지 않음", result["error"])
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(protected.read_bytes(), b"existing")

    def test_exiftool_or_prewrite_verification_failure_keeps_original(self):
        failures = (
            ("_rebuild_jpeg_exif", RuntimeError("forced ExifTool failure")),
            ("_verify_repair_output", RuntimeError("forced prewrite failure")),
        )
        for target, failure in failures:
            with self.subTest(target=target), tempfile.TemporaryDirectory(
                dir="build"
            ) as temporary:
                root = Path(temporary).resolve()
                path = root / "1579242283509.jpg"
                original = self.damaged_jpeg(path)
                with (
                    patch.object(
                        core,
                        "_current_repair_kind",
                        return_value=core.REPAIR_KIND_JPEG_OTHER_IMAGE_START,
                    ),
                    patch.object(core, target, side_effect=failure),
                ):
                    result = core.repair_jpeg_other_image(
                        root, self.row(path), exiftool=str(self.exiftool)
                    )
                self.assertEqual(result["result"], "REPAIR_FAILED")
                self.assertIn(str(failure), result["error"])
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse(Path(str(path) + "_original").exists())

    def test_file_change_during_recheck_stops_before_repair(self):
        with tempfile.TemporaryDirectory(dir="build") as temporary:
            root = Path(temporary).resolve()
            path = root / "1579242283509.jpg"
            self.damaged_jpeg(path)

            def change_file(_executable, current_path, _row):
                current_path.write_bytes(current_path.read_bytes() + b"external-change")
                return core.REPAIR_KIND_JPEG_OTHER_IMAGE_START

            with patch.object(core, "_current_repair_kind", side_effect=change_file):
                result = core.repair_jpeg_other_image(
                    root, self.row(path), exiftool=str(self.exiftool)
                )
            self.assertEqual(result["result"], "REPAIR_FAILED")
            self.assertIn("검사 중 변경", result["error"])
            self.assertTrue(path.read_bytes().endswith(b"external-change"))
            self.assertFalse(Path(str(path) + "_original").exists())

    def test_repair_log_keeps_order_and_stops_before_next_file(self):
        with tempfile.TemporaryDirectory(dir="build") as temporary:
            root = Path(temporary).resolve()
            rows = []
            for index in range(3):
                path = root / f"{1579242283509 + index}.jpg"
                Image.new("RGB", (2, 2)).save(path, "JPEG")
                rows.append(self.row(path))
            source = root / "source.csv"
            with source.open("w", encoding="utf-8-sig", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=core.CSV_FIELDS)
                writer.writeheader()
                writer.writerows(rows)
            calls = []

            def repair(_root, row, **kwargs):
                calls.append(row["filename"])
                result = dict(row)
                result.update(action="REPAIRED", result="REPAIRED", error="")
                return result

            def should_stop():
                return len(calls) == 1

            with patch.object(core, "repair_jpeg_other_image", side_effect=repair):
                code, stats, repair_log = core.repair_from_log(
                    source,
                    root,
                    exiftool=str(self.exiftool),
                    log_dir=root,
                    should_stop=should_stop,
                    emit=lambda *args, **kwargs: None,
                )

            self.assertEqual(code, 130)
            self.assertEqual(stats["cancelled"], 1)
            self.assertEqual(calls, [rows[0]["filename"]])
            with repair_log.open(encoding="utf-8-sig", newline="") as log_file:
                self.assertEqual(
                    [row["filename"] for row in csv.DictReader(log_file)], calls
                )


if __name__ == "__main__":
    unittest.main()
