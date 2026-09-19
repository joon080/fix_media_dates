#!/usr/bin/env python3
"""Windows GUI for fix_media_dates."""

import csv
import ctypes
import os
import queue
import sys
import threading
import tkinter as tk
from collections import Counter
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from fix_media_dates import (
    ERROR_RESULTS,
    classify_repair_kind,
    process_media,
    repair_from_log,
)


APP_TITLE = "media date fixer"
WINDOWS_BLUE = "#0078d4"
WINDOWS_GREEN = "#107c10"
PREVIEW_ROW_LIMIT = 1000


def application_directory():
    source = sys.executable if getattr(sys, "frozen", False) else __file__
    return Path(source).resolve().parent


def bundled_exiftool(base=None):
    base = Path(base) if base else application_directory()
    executable = base / "exiftool.exe"
    support = base / "exiftool_files"
    missing = []
    if not executable.is_file():
        missing.append("exiftool.exe")
    if not support.is_dir():
        missing.append("exiftool_files")
    if missing:
        raise FileNotFoundError(
            "배포 폴더에서 찾을 수 없습니다: "
            + ", ".join(missing)
            + "\nMedia Date Fixer.exe와 같은 폴더에 필요한 파일을 넣어 주세요."
        )
    return executable


def log_directory():
    buffer = ctypes.create_unicode_buffer(260)
    if os.name == "nt" and ctypes.windll.shell32.SHGetFolderPathW(
        None, 5, None, 0, buffer
    ) == 0:
        documents = Path(buffer.value)
    else:
        documents = Path.home() / "Documents"
    return documents / "Media Date Fixer" / "Logs"


def summary_values(stats):
    skipped = sum(
        stats[key]
        for key in (
            "already",
            "conflicts",
            "pattern_mismatch",
            "unsupported",
            "out_of_range",
            "not_local",
            "failed",
            "verify_failed",
        )
    )
    return stats["matched"], stats["conflicts"], stats["modified"], skipped


def read_log_rows(path):
    rows = []
    with Path(path).open(encoding="utf-8-sig", newline="") as log_file:
        for row in csv.DictReader(log_file):
            rows.append(
                (
                    row["filename"],
                    row["target_datetime_local"] or row["target_datetime_utc"],
                    row["result"],
                )
            )
            # ponytail: cap the preview; the CSV remains the complete result.
            if len(rows) == PREVIEW_ROW_LIMIT:
                break
    return rows


def error_summary(path):
    errors = []
    with Path(path).open(encoding="utf-8-sig", newline="") as log_file:
        for row in csv.DictReader(log_file):
            if row.get("result") in ERROR_RESULTS:
                errors.append(row)
    extensions = Counter(row.get("extension") or "(확장자 없음)" for row in errors)
    return {
        "total": len(errors),
        "repairable": sum(bool(classify_repair_kind(row)) for row in errors),
        "extensions": extensions,
    }


def error_message(summary, *, apply_changes):
    lines = [f"{summary['total']:,}개의 파일에 오류가 발생하였습니다."]
    repairable = summary["repairable"]
    if summary["extensions"] and not (apply_changes and repairable):
        lines.append(
            "형식별 오류: "
            + ", ".join(
                f"{extension} {count:,}개"
                for extension, count in sorted(summary["extensions"].items())
            )
        )
    lines += [
        f"자동 복구 가능 JPEG: {repairable:,}개",
        f"현재 미지원: {summary['total'] - repairable:,}개",
    ]
    if apply_changes and repairable:
        lines.append("복구 가능한 파일을 복구하시겠습니까?")
    elif apply_changes:
        lines.append("현재 지원되는 자동 복구가 없습니다.")
    else:
        lines.append("Dry Run에서는 파일을 변경하거나 복구하지 않습니다.")
    return "\n".join(lines)


def checkbox_image(master, selected):
    image = tk.PhotoImage(master=master, width=20, height=16)
    image.put("white", to=(0, 0, 20, 16))
    image.put("#767676", to=(0, 0, 16, 16))
    image.put("white", to=(1, 1, 15, 15))
    if selected:
        for x, y in (
            (3, 7),
            (4, 8),
            (5, 9),
            (6, 10),
            (7, 9),
            (8, 8),
            (9, 7),
            (10, 6),
            (11, 5),
            (12, 4),
        ):
            image.put(WINDOWS_GREEN, to=(x, y, x + 2, y + 2))
    return image


class MediaDateFixerApp:
    def __init__(self, root):
        self.root = root
        self.last_log = None
        self.busy = False
        self.close_when_done = False
        self.cancel_event = threading.Event()
        self.events = queue.Queue()
        self.errors = []
        self.folder = tk.StringVar()
        self.overwrite = tk.BooleanVar(value=False)
        self.backup = tk.BooleanVar(value=True)
        self.workers = tk.StringVar(value="2개")
        self.active_workers = 1
        self.operation = "run"
        self.run_context = None
        self.status = tk.StringVar(value="대기 중")
        self.percent = tk.StringVar(value="0%")
        self.summary = [tk.StringVar(value="0") for _ in range(4)]
        self.checkbox_off = checkbox_image(root, False)
        self.checkbox_on = checkbox_image(root, True)

        root.title(APP_TITLE)
        root.geometry("1000x700")
        root.minsize(850, 600)
        root.configure(background="white")
        root.protocol("WM_DELETE_WINDOW", self._close)
        self._configure_style()
        self._build()

    def _configure_style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10))
        style.configure("TFrame", background="white")
        style.configure("TLabel", background="white", foreground="black")
        style.configure("Header.TLabel", font=("Segoe UI", 20, "bold"))
        style.configure("Section.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure(
            "TButton",
            background="#f3f3f3",
            foreground="black",
            bordercolor="#8a8a8a",
            borderwidth=1,
            padding=(14, 8),
        )
        style.map(
            "TButton",
            background=[("active", "#e5e5e5"), ("pressed", "#d5d5d5")],
        )
        style.configure(
            "Accent.TButton",
            background=WINDOWS_BLUE,
            foreground="white",
            bordercolor=WINDOWS_BLUE,
            padding=(14, 8),
        )
        style.map(
            "Accent.TButton",
            background=[("active", "#106ebe"), ("pressed", "#005a9e")],
        )
        style.layout(
            "Green.TCheckbutton",
            [
                (
                    "Checkbutton.padding",
                    {
                        "sticky": "nswe",
                        "children": [
                            (
                                "Checkbutton.focus",
                                {
                                    "side": "left",
                                    "sticky": "w",
                                    "children": [
                                        ("Checkbutton.label", {"sticky": "nswe"})
                                    ],
                                },
                            )
                        ],
                    },
                )
            ],
        )
        style.configure("Green.TCheckbutton", background="white", foreground="black")
        style.map(
            "Green.TCheckbutton",
            background=[("active", "white")],
            foreground=[("disabled", "#767676")],
        )
        style.configure(
            "Horizontal.TProgressbar",
            background=WINDOWS_BLUE,
            troughcolor="#e5e5e5",
            borderwidth=0,
        )
        style.configure(
            "Treeview",
            background="white",
            fieldbackground="white",
            foreground="black",
            rowheight=28,
            bordercolor="#8a8a8a",
            borderwidth=1,
        )
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

    def _build(self):
        main = ttk.Frame(self.root, padding=20)
        main.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(7, weight=1)

        ttk.Label(main, text=APP_TITLE, style="Header.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        ttk.Label(
            main,
            text="파일명의 타임스탬프로 사진과 동영상의 메타데이터 날짜를 복원합니다.",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 16))
        ttk.Separator(main).grid(row=2, column=0, columnspan=3, sticky="ew")

        ttk.Label(main, text="미디어 폴더", style="Section.TLabel").grid(
            row=3, column=0, sticky="w", pady=16
        )
        ttk.Entry(main, textvariable=self.folder).grid(
            row=3, column=1, sticky="ew", padx=16, pady=16
        )
        self.browse_button = ttk.Button(main, text="찾아보기", command=self._browse)
        self.browse_button.grid(
            row=3, column=2, sticky="ew", pady=16
        )

        actions = ttk.Frame(main)
        actions.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(0, 18))
        self.dry_run_button = ttk.Button(
            actions, text="검사 (dry run)", command=lambda: self._run(False)
        )
        self.dry_run_button.pack(side="left", ipadx=20)
        self.apply_button = ttk.Button(
            actions,
            text="실행",
            style="Accent.TButton",
            command=lambda: self._run(True),
        )
        self.apply_button.pack(side="left", padx=(12, 28), ipadx=28)
        self.overwrite_button = ttk.Checkbutton(
            actions,
            text="기존 날짜 덮어쓰기",
            variable=self.overwrite,
            style="Green.TCheckbutton",
            image=(self.checkbox_off, "selected", self.checkbox_on),
            compound="left",
        )
        self.overwrite_button.pack(side="left", padx=(0, 20))
        self.backup_button = ttk.Checkbutton(
            actions,
            text="원본 백업 만들기",
            variable=self.backup,
            style="Green.TCheckbutton",
            image=(self.checkbox_off, "selected", self.checkbox_on),
            compound="left",
        )
        self.backup_button.pack(side="left")
        ttk.Label(actions, text="적용 동시 처리").pack(side="left", padx=(20, 8))
        self.workers_box = ttk.Combobox(
            actions,
            textvariable=self.workers,
            values=("1개", "2개", "4개"),
            width=4,
            state="readonly",
        )
        self.workers_box.pack(side="left")

        progress_header = ttk.Frame(main)
        progress_header.grid(row=5, column=0, columnspan=3, sticky="ew")
        progress_header.columnconfigure(1, weight=1)
        ttk.Label(progress_header, text="진행 상황", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(progress_header, textvariable=self.percent).grid(
            row=0, column=1, sticky="e", padx=(0, 20)
        )
        ttk.Label(progress_header, textvariable=self.status).grid(
            row=0, column=2, sticky="e"
        )
        self.progress = ttk.Progressbar(main, mode="indeterminate")
        self.progress.grid(
            row=6, column=0, columnspan=3, sticky="ew", pady=(8, 16)
        )

        content = ttk.Frame(main)
        content.grid(row=7, column=0, columnspan=3, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(3, weight=1)
        ttk.Label(content, text="결과 요약", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        cards = ttk.Frame(content)
        cards.grid(row=1, column=0, sticky="ew", pady=(8, 14))
        for column in range(4):
            cards.columnconfigure(column, weight=1)
        for column, label in enumerate(("대상", "충돌", "수정 완료", "전체")):
            self._summary_card(cards, column, label, self.summary[column])

        ttk.Label(content, text="로그", style="Section.TLabel").grid(
            row=2, column=0, sticky="w"
        )
        table = ttk.Frame(content)
        table.grid(row=3, column=0, sticky="nsew", pady=(8, 12))
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.results = ttk.Treeview(
            table,
            columns=("filename", "date", "status"),
            show="headings",
            selectmode="browse",
        )
        self.results.heading("filename", text="파일명")
        self.results.heading("date", text="계산된 날짜")
        self.results.heading("status", text="상태")
        self.results.column("filename", width=380, anchor="w")
        self.results.column("date", width=260, anchor="w")
        self.results.column("status", width=160, anchor="w")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.results.yview)
        self.results.configure(yscrollcommand=scroll.set)
        self.results.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

        bottom = ttk.Frame(content)
        bottom.grid(row=4, column=0, sticky="ew")
        ttk.Button(bottom, text="CSV 로그 열기", command=self._open_log).pack(
            side="left"
        )
        self.close_button = ttk.Button(bottom, text="닫기", command=self._close)
        self.close_button.pack(side="right")
        self.cancel_button = ttk.Button(
            bottom, text="중단", command=self._cancel, state="disabled"
        )
        self.cancel_button.pack(side="right", padx=(0, 10))

    def _summary_card(self, parent, column, label, value):
        card = tk.Frame(
            parent,
            background="white",
            highlightbackground="#8a8a8a",
            highlightthickness=1,
        )
        card.grid(
            row=0,
            column=column,
            sticky="ew",
            padx=(0 if column == 0 else 6, 0 if column == 3 else 6),
        )
        tk.Label(card, text=label, background="white", anchor="w").pack(
            fill="x", padx=14, pady=(10, 0)
        )
        tk.Label(
            card,
            textvariable=value,
            background="white",
            font=("Segoe UI", 20, "bold"),
            anchor="w",
        ).pack(fill="x", padx=14, pady=(0, 10))

    def _browse(self):
        selected = filedialog.askdirectory(title="미디어 폴더 선택")
        if selected:
            self.folder.set(selected)

    def _run(self, apply_changes):
        if self.busy:
            return
        folder = Path(self.folder.get().strip())
        if not folder.is_dir():
            messagebox.showerror(APP_TITLE, "올바른 미디어 폴더를 선택하세요.")
            return
        try:
            exiftool = bundled_exiftool()
        except FileNotFoundError as error:
            messagebox.showerror(APP_TITLE, str(error))
            return
        workers = int(self.workers.get()[0]) if apply_changes else 1
        if apply_changes and not messagebox.askyesno(
            APP_TITLE,
            "선택한 폴더의 메타데이터를 실제로 수정하시겠습니까?\n"
            f"동시 처리: {workers}개",
        ):
            return

        options = {
            "apply_changes": apply_changes,
            "overwrite_existing": self.overwrite.get(),
            "no_backup": not self.backup.get(),
            "exiftool": str(exiftool),
            "log_dir": log_directory(),
            "workers": workers,
        }
        self.active_workers = workers
        self.operation = "run"
        self.run_context = (folder, options)
        self.cancel_event.clear()
        self.errors.clear()
        self.close_when_done = False
        self._set_busy(True)
        self.status.set("파일 이름 분석 중")
        self.percent.set("")
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)
        threading.Thread(
            target=self._worker,
            args=(folder, options),
            daemon=False,
        ).start()
        self.root.after(50, self._poll_worker)

    def _worker(self, folder, options):
        def emit(message, error=False):
            self.events.put(("message", message, error))

        def progress(current, total, detail):
            self.events.put(("progress", current, total, detail))

        try:
            result = process_media(
                folder,
                **options,
                emit=emit,
                on_progress=progress,
                should_stop=self.cancel_event.is_set,
            )
        except Exception as error:
            emit(f"처리 실패: {error}", error=True)
            result = (1, None, None)
        self.events.put(("result", *result))

    def _repair_worker(self, folder, source_log, options, unsupported):
        def emit(message, error=False):
            self.events.put(("message", message, error))

        def progress(current, total, detail):
            self.events.put(("progress", current, total, detail))

        try:
            result = repair_from_log(
                source_log,
                folder,
                exiftool=options["exiftool"],
                overwrite_existing=options["overwrite_existing"],
                no_backup=options["no_backup"],
                log_dir=options["log_dir"],
                emit=emit,
                on_progress=progress,
                should_stop=self.cancel_event.is_set,
            )
        except Exception as error:
            emit(f"JPEG 복구 실패: {error}", error=True)
            result = (2, None, None)
        self.events.put(("repair_result", *result, unsupported))

    def _poll_worker(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "message" and event[2]:
                    self.errors.append(event[1])
                elif event[0] == "progress":
                    self._show_progress(*event[1:])
                elif event[0] == "result":
                    self._finish(*event[1:])
                elif event[0] == "repair_result":
                    self._finish_repair(*event[1:])
        except queue.Empty:
            pass
        if self.busy:
            self.root.after(50, self._poll_worker)

    def _show_progress(self, current, total, detail):
        if total is None:
            self.status.set(f"파일 이름 분석 중 ({current:,}개)")
            return
        self.progress.stop()
        self.progress.configure(
            mode="determinate", maximum=max(total, 1), value=current
        )
        percent = round(current * 100 / total) if total else 0
        self.percent.set(f"{percent}%")
        if detail == "중단됨":
            self.status.set("중단됨")
        elif detail == "완료":
            self.status.set("완료")
        else:
            work = "복구" if self.operation == "repair" else "처리"
            self.status.set(f"{total:,}개 중 {current:,}개 {work} 중")

    def _finish(self, code, stats, path):
        self.progress.stop()
        self.last_log = path
        if stats is not None:
            for variable, value in zip(self.summary, summary_values(stats)):
                variable.set(str(value))
            self._show_log(path)
        if code == 130:
            self.status.set("중단됨")
        elif code == 0:
            self.status.set("완료")
            self.percent.set("100%")
        elif stats is not None:
            error_count = stats["failed"] + stats["verify_failed"]
            self.status.set(f"완료 ({error_count:,}개 오류)")
            self.percent.set("100%")
        else:
            self.status.set("오류")
            self.percent.set("0%")
        if self.errors:
            messagebox.showerror(APP_TITLE, self.errors[-1])
        if stats is not None and code != 130 and not self.errors and path is not None:
            try:
                summary = error_summary(path)
            except (OSError, csv.Error, KeyError) as error:
                messagebox.showerror(APP_TITLE, f"오류 집계 실패: {error}")
            else:
                if summary["total"]:
                    apply_changes = self.run_context[1]["apply_changes"]
                    prompt = error_message(summary, apply_changes=apply_changes)
                    if (
                        apply_changes
                        and summary["repairable"]
                        and messagebox.askyesno(APP_TITLE, prompt)
                    ):
                        self._start_repair(
                            path,
                            summary["total"] - summary["repairable"],
                        )
                        return
                    if not (apply_changes and summary["repairable"]):
                        messagebox.showinfo(APP_TITLE, prompt)
        self._set_busy(False)
        if self.close_when_done:
            self.root.destroy()

    def _start_repair(self, source_log, unsupported):
        folder, options = self.run_context
        self.operation = "repair"
        self.active_workers = 1
        self.errors.clear()
        self.status.set("JPEG 오류 복구 준비 중")
        self.percent.set("0%")
        self.progress.configure(mode="determinate", value=0)
        threading.Thread(
            target=self._repair_worker,
            args=(folder, source_log, options, unsupported),
            daemon=False,
        ).start()

    def _finish_repair(self, code, stats, path, unsupported):
        self.progress.stop()
        self.last_log = path
        if stats is not None and path is not None:
            self._show_log(path)
        if stats is None:
            self.status.set("복구 오류")
            self.percent.set("0%")
            if self.errors:
                messagebox.showerror(APP_TITLE, self.errors[-1])
        elif code == 130:
            self.status.set("복구 중단됨")
        else:
            self.status.set(
                "복구 완료"
                if not stats["repair_failed"]
                else f"복구 완료 ({stats['repair_failed']:,}개 실패)"
            )
            self.percent.set("100%")
            messagebox.showinfo(
                APP_TITLE,
                f"복구 성공: {stats['repaired']:,}개\n"
                f"날짜 충돌 보존: {stats['repaired_conflict']:,}개\n"
                f"실패: {stats['repair_failed']:,}개\n"
                f"미지원 오류: {unsupported:,}개\n"
                f"복구 CSV: {path}",
            )
        self._set_busy(False)
        if self.close_when_done:
            self.root.destroy()

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.dry_run_button.configure(state=state)
        self.apply_button.configure(state=state)
        self.browse_button.configure(state=state)
        self.overwrite_button.configure(state=state)
        self.backup_button.configure(state=state)
        self.workers_box.configure(state="disabled" if busy else "readonly")
        self.cancel_button.configure(state="normal" if busy else "disabled")

    def _cancel(self):
        if self.busy and not self.cancel_event.is_set():
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status.set(
                f"중단 요청됨 (처리 중인 파일 최대 {self.active_workers}개 완료 후)"
            )

    def _close(self):
        if not self.busy:
            self.root.destroy()
            return
        if messagebox.askyesno(
            APP_TITLE,
            f"처리 중인 파일 최대 {self.active_workers}개가 끝난 뒤 "
            "중단하고 창을 닫으시겠습니까?",
        ):
            self.close_when_done = True
            self._cancel()

    def _show_log(self, path):
        self.results.delete(*self.results.get_children())
        try:
            for row in read_log_rows(path):
                self.results.insert("", "end", values=row)
        except (OSError, csv.Error, KeyError) as error:
            messagebox.showerror(APP_TITLE, f"CSV 로그 읽기 실패: {error}")

    def _open_log(self):
        if self.last_log and self.last_log.exists():
            target = self.last_log
        else:
            target = log_directory()
        try:
            if target == log_directory():
                target.mkdir(parents=True, exist_ok=True)
            os.startfile(target)
        except OSError as error:
            messagebox.showerror(APP_TITLE, f"로그 열기 실패: {error}")


def run_self_test():
    stats = {
        "matched": 10,
        "conflicts": 1,
        "modified": 3,
        "already": 2,
        "pattern_mismatch": 4,
        "unsupported": 0,
        "out_of_range": 0,
        "not_local": 0,
        "failed": 0,
        "verify_failed": 0,
    }
    assert summary_values(stats) == (10, 1, 3, 7)
    repairable_summary = {
        "total": 3,
        "repairable": 1,
        "extensions": Counter({".jpg": 1, ".png": 1, ".mp4": 1}),
    }
    apply_prompt = error_message(repairable_summary, apply_changes=True)
    assert "3개의 파일에 오류가 발생하였습니다." in apply_prompt
    assert "자동 복구 가능 JPEG: 1개" in apply_prompt
    assert "현재 미지원: 2개" in apply_prompt
    assert apply_prompt.endswith("복구 가능한 파일을 복구하시겠습니까?")
    dry_prompt = error_message(repairable_summary, apply_changes=False)
    assert "복구하시겠습니까?" not in dry_prompt
    assert "파일을 변경하거나 복구하지 않습니다" in dry_prompt
    unsupported_prompt = error_message(
        {"total": 2, "repairable": 0, "extensions": Counter({".mov": 2})},
        apply_changes=True,
    )
    assert unsupported_prompt.endswith("현재 지원되는 자동 복구가 없습니다.")
    assert application_directory().is_dir()
    assert log_directory().name == "Logs"
    root = tk.Tk()
    root.withdraw()
    try:
        app = MediaDateFixerApp(root)
        root.update_idletasks()
        assert "indicator" not in str(ttk.Style(root).layout("Green.TCheckbutton"))
        assert app.checkbox_on.get(10, 6) == (16, 124, 16)
        assert app.checkbox_off.get(10, 6) == (255, 255, 255)
        assert app.workers.get() == "2개"
        assert tuple(app.workers_box.cget("values")) == ("1개", "2개", "4개")
        app.overwrite_button.invoke()
        assert app.overwrite.get()
        app._show_log = lambda path: None
        app._set_busy = lambda busy: None
        app._finish(1, stats | {"failed": 2, "verify_failed": 1}, None)
        assert app.percent.get() == "100%"
        assert app.status.get() == "완료 (3개 오류)"

        original_error_summary = globals()["error_summary"]
        original_ask = messagebox.askyesno
        original_info = messagebox.showinfo
        original_error = messagebox.showerror
        try:
            starts = []
            infos = []
            asks = []
            globals()["error_summary"] = lambda path: repairable_summary
            messagebox.askyesno = lambda title, text: asks.append(text) or True
            messagebox.showinfo = lambda title, text: infos.append(text)
            messagebox.showerror = lambda title, text: None
            app._start_repair = lambda path, unsupported: starts.append(
                (path, unsupported)
            )
            app.run_context = (Path("."), {"apply_changes": True})
            app.errors.clear()
            fake_log = Path("apply.csv")
            app._finish(1, stats | {"failed": 1, "verify_failed": 0}, fake_log)
            assert asks and starts == [(fake_log, 2)]

            asks.clear()
            infos.clear()
            starts.clear()
            app.run_context = (Path("."), {"apply_changes": False})
            app._finish(1, stats | {"failed": 1, "verify_failed": 0}, fake_log)
            assert not asks and not starts and infos

            asks.clear()
            infos.clear()
            globals()["error_summary"] = lambda path: {
                "total": 2,
                "repairable": 0,
                "extensions": Counter({".mov": 2}),
            }
            app.run_context = (Path("."), {"apply_changes": True})
            app._finish(1, stats | {"failed": 2, "verify_failed": 0}, fake_log)
            assert not asks and infos and "지원되는 자동 복구가 없습니다" in infos[-1]

            calls = []
            globals()["error_summary"] = lambda path: calls.append(path) or repairable_summary
            app.errors[:] = ["fatal"]
            app._finish(1, stats | {"failed": 1, "verify_failed": 0}, fake_log)
            app.errors.clear()
            app._finish(130, stats | {"failed": 1, "verify_failed": 0}, fake_log)
            assert not calls
        finally:
            globals()["error_summary"] = original_error_summary
            messagebox.askyesno = original_ask
            messagebox.showinfo = original_info
            messagebox.showerror = original_error
    finally:
        root.destroy()
    print("GUI SELF_TEST PASSED")


def main():
    if "--self-test" in sys.argv:
        run_self_test()
        return 0
    root = tk.Tk()
    MediaDateFixerApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
