import csv
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import fix_media_dates as core


class FakeSession:
    attempts = 0
    created = []
    fail_at = None

    def __init__(self, executable):
        type(self).attempts += 1
        if type(self).attempts == type(self).fail_at:
            raise RuntimeError("session start failed")
        self.number = type(self).attempts
        self.closed = False
        type(self).created.append(self)

    def execute(self, arguments):
        return 0, "13.59", ""

    def close(self):
        self.closed = True


class ParallelWorkerTest(unittest.TestCase):
    def setUp(self):
        FakeSession.attempts = 0
        FakeSession.created = []
        FakeSession.fail_at = None

    @staticmethod
    def items(root, count=8):
        return [
            (
                root / f"{1579242283509 + index}.jpg",
                object(),
                "",
                core.match_filename(f"{1579242283509 + index}.jpg"),
            )
            for index in range(count)
        ]

    @staticmethod
    def result(path, *, failed=False, broken=False):
        row = core.blank_row(path)
        row.update(
            action="MODIFIED",
            result="FAILED" if failed else "MODIFIED",
            error="worker stopped" if failed else "",
        )
        counts = {
            "total": 1,
            "matched": 1,
            "failed" if failed else "modified": 1,
            "patterns": {"numeric_timestamp": 1},
        }
        return row, counts, broken

    def run_process(self, root, items, process_file, **options):
        with (
            patch.object(core, "ExifToolSession", FakeSession),
            patch.object(
                core,
                "scan_files",
                return_value=iter(
                    (path, details, error)
                    for path, details, error, _ in items
                ),
            ),
            patch.object(core, "_process_file", side_effect=process_file),
        ):
            return core.process_media(
                root,
                exiftool="fake",
                log_dir=root / "logs",
                emit=lambda *args, **kwargs: None,
                **options,
            )

    def test_sessions_reused_without_overlap_and_csv_stays_ordered(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            items = self.items(root)
            lock = threading.Lock()
            active = 0
            maximum = 0
            busy = set()
            overlap = False
            calls = []

            def process_file(item, *, session, **kwargs):
                nonlocal active, maximum, overlap
                with lock:
                    self.assertEqual(FakeSession.attempts, 4)
                    overlap |= session.number in busy
                    busy.add(session.number)
                    calls.append(session.number)
                    active += 1
                    maximum = max(maximum, active)
                index = items.index(item)
                time.sleep((len(items) - index) * 0.003)
                with lock:
                    active -= 1
                    busy.remove(session.number)
                return self.result(item[0])

            code, stats, log_path = self.run_process(
                root,
                items,
                process_file,
                apply_changes=True,
                workers=4,
            )

            self.assertEqual(code, 0)
            self.assertEqual(stats["modified"], len(items))
            self.assertEqual(FakeSession.attempts, 4)
            self.assertEqual(maximum, 4)
            self.assertFalse(overlap)
            self.assertTrue(any(count > 1 for count in Counter(calls).values()))
            with log_path.open(encoding="utf-8-sig", newline="") as log_file:
                self.assertEqual(
                    [row["filename"] for row in csv.DictReader(log_file)],
                    [item[0].name for item in items],
                )

    def test_dry_run_forces_one_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            items = self.items(root, 3)
            code, _, _ = self.run_process(
                root,
                items,
                lambda item, **kwargs: self.result(item[0]),
                apply_changes=False,
                workers=4,
            )
            self.assertEqual(code, 0)
            self.assertEqual(FakeSession.attempts, 1)

    def test_cancel_stops_new_assignments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            items = self.items(root)
            stop = threading.Event()
            calls = []

            def process_file(item, **kwargs):
                calls.append(item[0].name)
                if item[0] == items[0][0]:
                    stop.set()
                else:
                    time.sleep(0.02)
                return self.result(item[0])

            code, stats, _ = self.run_process(
                root,
                items,
                process_file,
                apply_changes=True,
                workers=4,
                should_stop=stop.is_set,
            )
            self.assertEqual(code, 130)
            self.assertEqual(stats["cancelled"], 1)
            self.assertLessEqual(len(calls), 4)

    def test_initialization_failure_happens_before_file_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            items = self.items(root)
            calls = []
            FakeSession.fail_at = 3
            code, stats, log_path = self.run_process(
                root,
                items,
                lambda item, **kwargs: calls.append(item),
                apply_changes=True,
                workers=4,
            )
            self.assertEqual((code, stats, log_path), (2, None, None))
            self.assertEqual(calls, [])
            self.assertTrue(all(session.closed for session in FakeSession.created))

    def test_stopped_session_is_replaced_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            items = self.items(root, 5)
            broken = False

            def process_file(item, **kwargs):
                nonlocal broken
                if not broken:
                    broken = True
                    return self.result(item[0], failed=True, broken=True)
                return self.result(item[0])

            code, stats, _ = self.run_process(
                root,
                items,
                process_file,
                apply_changes=True,
                workers=2,
            )
            self.assertEqual(code, 1)
            self.assertEqual(stats["failed"], 1)
            self.assertEqual(stats["modified"], 4)
            self.assertEqual(FakeSession.attempts, 3)

    def test_failed_restart_stops_new_assignments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            items = self.items(root, 6)
            calls = []
            FakeSession.fail_at = 3

            def process_file(item, **kwargs):
                calls.append(item[0].name)
                if item[0] == items[0][0]:
                    return self.result(item[0], failed=True, broken=True)
                time.sleep(0.02)
                return self.result(item[0])

            code, _, _ = self.run_process(
                root,
                items,
                process_file,
                apply_changes=True,
                workers=2,
            )
            self.assertEqual(code, 1)
            self.assertEqual(FakeSession.attempts, 3)
            self.assertLessEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
