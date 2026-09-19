#!/usr/bin/env python3
"""Windows GUI for fix_media_dates."""

import csv
import ctypes
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from fix_media_dates import process_media


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
            text="실제 적용",
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
        for column, label in enumerate(("대상", "충돌", "수정 완료", "건너뜀")):
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
        if apply_changes and not messagebox.askyesno(
            APP_TITLE,
            "선택한 폴더의 메타데이터를 실제로 수정하시겠습니까?",
        ):
            return

        options = {
            "apply_changes": apply_changes,
            "overwrite_existing": self.overwrite.get(),
            "no_backup": not self.backup.get(),
            "exiftool": str(exiftool),
            "log_dir": log_directory(),
        }
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
            self.status.set(f"{total:,}개 중 {current:,}개 처리 중")

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
        else:
            self.status.set("오류")
            if stats is None:
                self.percent.set("0%")
            if self.errors:
                messagebox.showerror(APP_TITLE, self.errors[-1])
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
        self.cancel_button.configure(state="normal" if busy else "disabled")

    def _cancel(self):
        if self.busy and not self.cancel_event.is_set():
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status.set("중단 요청됨 (현재 파일 완료 후)")

    def _close(self):
        if not self.busy:
            self.root.destroy()
            return
        if messagebox.askyesno(
            APP_TITLE,
            "현재 파일 처리가 끝난 뒤 중단하고 창을 닫으시겠습니까?",
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
        app.overwrite_button.invoke()
        assert app.overwrite.get()
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
