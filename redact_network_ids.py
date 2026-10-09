#!/usr/bin/env python3
"""Find and blur IPv4, IPv6 and MAC addresses using Tesseract + ImageMagick.

The GUI supports selecting one image or a folder, and an output folder. OCR uses
word bounding boxes; matched adjacent OCR words are grouped into one rectangle.
ImageMagick blurs each detected rectangle. Original images are never modified.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import re
import shutil
import subprocess
import sys
import threading
import queue
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    from PIL import Image, ImageOps, ImageEnhance, ImageFilter
    import pytesseract
    from pytesseract import Output
except ImportError as exc:
    print("Missing Python package. Install dependencies with: python -m pip install Pillow pytesseract", file=sys.stderr)
    raise SystemExit(2) from exc

LOG = logging.getLogger("redact_network_ids")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
IPV4_RE = re.compile(r"(?<![\d.])(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}(?![\d.])")
IPV6_RE = re.compile(r"(?<![0-9a-f:])(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}(?:%[\w.-]+)?(?![0-9a-f:])", re.IGNORECASE)
MAC_RE = re.compile(r"(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])", re.IGNORECASE)


@dataclass(frozen=True)
class Word:
    text: str
    left: int
    top: int
    width: int
    height: int
    block: int
    paragraph: int
    line: int
    pass_id: int = 0


@dataclass(frozen=True)
class Finding:
    value: str
    kind: str
    left: int
    top: int
    right: int
    bottom: int


def choose_paths() -> tuple[Path, Path]:
    """Open native dialogs to select a source image/folder and output folder."""
    import tkinter as tk
    from tkinter import filedialog, messagebox

    root = tk.Tk()
    root.withdraw()
    root.update()
    source_kind = messagebox.askyesno("เลือกต้นทาง", "เลือก 'ใช่' เพื่อเลือกโฟลเดอร์\nเลือก 'ไม่ใช่' เพื่อเลือกไฟล์ภาพ")
    if source_kind:
        source_text = filedialog.askdirectory(title="เลือกโฟลเดอร์ต้นทาง")
    else:
        source_text = filedialog.askopenfilename(
            title="เลือกไฟล์ภาพ", filetypes=[("Image files", "*.jpg *.jpeg *.png"), ("All files", "*.*")]
        )
    if not source_text:
        root.destroy()
        raise SystemExit("ยกเลิกการเลือกต้นทาง")
    output_text = filedialog.askdirectory(title="เลือกโฟลเดอร์ส่งออก")
    root.destroy()
    if not output_text:
        raise SystemExit("ยกเลิกการเลือกโฟลเดอร์ส่งออก")
    return Path(source_text), Path(output_text)


def launch_gui(lang: str = "eng", tesseract_cmd: str | None = None) -> int:
    """Launch a compact batch GUI with source/output, progress and live status."""
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from PIL import ImageTk

    root = tk.Tk()
    root.title("IP / IPv6 / MAC Image Blur")
    root.geometry("920x640")
    root.minsize(760, 520)
    events: queue.Queue = queue.Queue()
    worker_thread: threading.Thread | None = None
    stop_event = threading.Event()
    source_var = tk.StringVar()
    source_paths: list[Path] = []
    output_var = tk.StringVar()
    export_parent_var = tk.StringVar()
    last_export_dir: Path | None = None
    processing_mode = tk.StringVar(value="ocr")
    fixed_area: tuple[float, float, float, float] | None = None
    fixed_area_dimensions: tuple[float, float] | None = None
    fixed_area_var = tk.StringVar(value="ยังไม่ได้กำหนดพื้นที่")
    status_var = tk.StringVar(value="พร้อมใช้งาน")
    count_var = tk.StringVar(value="0 / 0 ไฟล์")
    language = tk.StringVar(value="ENG")
    translations = {
        "ENG": {
            "app_title": "Image Privacy Processor", "subtitle": "Find IPv4 · IPv6 · MAC and blur automatically",
            "paths": "File locations", "inputs": "Input sources", "add_file": "Add files", "add_folder": "Add folder",
            "remove": "Remove selected", "output": "Export location", "choose_folder": "Choose folder",
            "mode": "Detection and blur mode", "ocr": "Find with OCR (IPv4 / IPv6 / MAC)",
            "fixed": "Fixed Area: same area in every image", "select_area": "Select area on first image",
            "area_none": "No area selected", "ready": "Ready", "start": "▶  Start processing", "stop": "Stop",
            "open_input": "Open Input", "open_output": "Open Export", "results": "Processing list",
            "file": "File", "status": "Status", "matches": "Findings", "log": "Log",
            "status_ready": "Ready", "status_running": "Processing...", "status_stopping": "Stopping after current image...",
            "status_stopped": "Stopped", "status_done": "Completed", "status_failed": "Completed with {count} errors",
            "status_cannot_start": "Could not start", "queued": "Queued", "working": "Processing",
            "no_match": "No address found", "completed_file": "Completed", "area_label": "Selected area: {width} × {height} px (preview)",
            "select_area_hint": "Click and drag to draw an area; drag again to replace it",
            "choose_input_for_area": "Add an image or folder before selecting the area",
            "preview_title": "Select area to blur", "preview_image": "Preview (first image): {path}",
            "area_help": "Drag a rectangle over the area to blur",
            "area_warning_title": "No area selected", "area_warning": "Drag over the image to set an area first",
            "use_area": "Use this area", "cancel": "Cancel", "preview_error": "Could not open preview",
            "input_error": "Input error: {error}", "missing_paths_title": "Missing information",
            "missing_paths": "Select input sources and an export location",
            "no_images_title": "No images", "no_images": "No .jpg, .jpeg or .png images were found in Input",
            "area_required_title": "Area not selected", "area_required": "Choose Fixed Area and select an area on the first image before starting",
            "output_inside_title": "Export location", "output_inside": "The export location is inside an input folder and may be scanned. Choose a different location.",
            "create_export_error_title": "Could not create export folder", "create_export_error": "Could not create or write to:\n{path}\n\n{error}",
            "preflight": "Preflight passed: {count} images found and output paths validated",
            "input_log": "Input: {paths}", "export_log": "Export location: {path}",
            "start_log": "Starting: {count} images from {sources} input sources", "mode_log": "Mode: {mode}",
            "ocr_mode": "OCR", "fixed_mode": "Fixed Area", "done_log": "{status} | Export: {path}",
            "error_log": "ERROR: {error}", "start_error_title": "Could not start", "no_address": "No address found",
            "select_input_title": "Select one or more images", "select_input": "Images", "select_folder_title": "Select Input folder (add more folders as needed)",
            "select_export_title": "Select parent folder for export", "export_created": "Created export folder: {path}",
            "no_folder_title": "Folder unavailable", "no_folder": "Choose a folder first",
            "plan_error_title": "Export validation failed", "plan_error": "Output paths contain duplicates or escape the export folder. Review the input list.",
        },
        "THAI": {
            "app_title": "โปรแกรมเบลอข้อมูลเครือข่ายในภาพ", "subtitle": "ค้นหา IPv4 · IPv6 · MAC แล้วเบลออัตโนมัติ",
            "paths": "ตำแหน่งไฟล์", "inputs": "แหล่ง Input", "add_file": "เพิ่มไฟล์", "add_folder": "เพิ่มโฟลเดอร์",
            "remove": "ลบที่เลือก", "output": "ตำแหน่ง Export", "choose_folder": "เลือกโฟลเดอร์",
            "mode": "วิธีค้นหาและกำหนดพื้นที่เบลอ", "ocr": "ค้นหาด้วย OCR (IPv4 / IPv6 / MAC)",
            "fixed": "Fixed Area: ใช้พื้นที่เดียวกันทุกภาพ", "select_area": "เลือกพื้นที่จากภาพแรก",
            "area_none": "ยังไม่ได้กำหนดพื้นที่", "ready": "พร้อมใช้งาน", "start": "▶  เริ่มประมวลผล", "stop": "หยุด",
            "open_input": "เปิด Input", "open_output": "เปิด Output", "results": "รายการประมวลผล",
            "file": "ไฟล์", "status": "สถานะ", "matches": "ตรวจพบ", "log": "Log",
            "status_ready": "พร้อมใช้งาน", "status_running": "กำลังประมวลผล...", "status_stopping": "กำลังหยุดหลังจบภาพปัจจุบัน...",
            "status_stopped": "หยุดแล้ว", "status_done": "เสร็จสมบูรณ์", "status_failed": "เสร็จโดยมีข้อผิดพลาด {count} รายการ",
            "status_cannot_start": "เริ่มงานไม่สำเร็จ", "queued": "รอคิว", "working": "กำลังประมวลผล",
            "no_match": "ไม่พบ address", "completed_file": "สำเร็จ", "area_label": "พื้นที่ที่เลือก: {width} × {height} px (ภาพตัวอย่าง)",
            "select_area_hint": "คลิกแล้วลากเพื่อวาดกรอบ; ลากใหม่เพื่อเปลี่ยนกรอบ",
            "choose_input_for_area": "เพิ่มไฟล์หรือโฟลเดอร์ Input ก่อนกำหนดพื้นที่",
            "preview_title": "เลือกพื้นที่ที่ต้องการเบลอ", "preview_image": "ภาพตัวอย่าง (ไฟล์แรก): {path}",
            "area_help": "ลากกรอบครอบบริเวณที่ต้องการเบลอ",
            "area_warning_title": "ยังไม่ได้เลือกกรอบ", "area_warning": "ลากเมาส์บนภาพเพื่อกำหนดกรอบก่อน",
            "use_area": "ใช้พื้นที่นี้", "cancel": "ยกเลิก", "preview_error": "เปิดภาพตัวอย่างไม่สำเร็จ",
            "input_error": "Input error: {error}", "missing_paths_title": "ข้อมูลไม่ครบ",
            "missing_paths": "กรุณาเลือก Input และตำแหน่ง Export",
            "no_images_title": "ไม่พบภาพ", "no_images": "ไม่พบไฟล์ .jpg, .jpeg หรือ .png ใน Input",
            "area_required_title": "ยังไม่ได้กำหนดพื้นที่", "area_required": "เลือก Fixed Area แล้วกำหนดพื้นที่จากภาพแรกก่อนเริ่ม",
            "output_inside_title": "ตำแหน่ง Output", "output_inside": "ตำแหน่ง Export อยู่ในโฟลเดอร์ Input และอาจถูกสแกน กรุณาเลือกตำแหน่งอื่น",
            "create_export_error_title": "สร้างโฟลเดอร์ Export ไม่สำเร็จ", "create_export_error": "ไม่สามารถสร้างหรือเขียนไฟล์ใน:\n{path}\n\n{error}",
            "preflight": "ตรวจสอบก่อนเริ่มผ่าน: พบ {count} ภาพและตรวจพาธปลายทางแล้ว",
            "input_log": "Input: {paths}", "export_log": "ตำแหน่ง Export: {path}",
            "start_log": "เริ่มงาน: {count} ภาพ จาก {sources} แหล่ง Input", "mode_log": "โหมด: {mode}",
            "ocr_mode": "OCR", "fixed_mode": "Fixed Area", "done_log": "{status} | Output: {path}",
            "error_log": "ERROR: {error}", "start_error_title": "เริ่มงานไม่สำเร็จ", "no_address": "ไม่พบ address",
            "select_input_title": "เลือกภาพหนึ่งไฟล์หรือหลายไฟล์", "select_input": "ไฟล์ภาพ", "select_folder_title": "เลือกโฟลเดอร์ Input (เลือกเพิ่มได้หลายโฟลเดอร์)",
            "select_export_title": "เลือกโฟลเดอร์หลักสำหรับ Export", "export_created": "สร้างโฟลเดอร์ Export แล้ว: {path}",
            "no_folder_title": "ยังไม่มีโฟลเดอร์", "no_folder": "เลือกโฟลเดอร์ที่ต้องการก่อน",
            "plan_error_title": "ตรวจสอบแผน Export ไม่ผ่าน", "plan_error": "พบชื่อไฟล์ผลลัพธ์ซ้ำหรือพาธอยู่นอกโฟลเดอร์ Export กรุณาตรวจรายการ Input ใหม่",
        },
    }
    localized_widgets: list[tuple[object, str]] = []

    def tr(key: str, **values) -> str:
        return translations[language.get()][key].format(**values)

    def localize(widget, key: str):
        widget.configure(text=tr(key))
        localized_widgets.append((widget, key))
        return widget

    status_state: dict[str, object] = {"key": "status_ready", "values": {}}

    def set_status(key: str, **values) -> None:
        status_state["key"] = key
        status_state["values"] = values
        status_var.set(tr(key, **values))

    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    style.configure("Header.TLabel", font=("Segoe UI", 17, "bold"))
    style.configure("Hint.TLabel", foreground="#526174")
    outer = ttk.Frame(root, padding=18)
    outer.pack(fill="both", expand=True)
    header = ttk.Frame(outer)
    header.pack(fill="x")
    title_label = localize(ttk.Label(header, style="Header.TLabel"), "app_title")
    title_label.pack(side="left", anchor="w")
    language_button = ttk.Button(header, text="THAI")
    language_button.pack(side="right")
    subtitle_label = localize(ttk.Label(outer, style="Hint.TLabel"), "subtitle")
    subtitle_label.pack(anchor="w", pady=(2, 14))

    paths = ttk.LabelFrame(outer, padding=12)
    localize(paths, "paths")
    paths.pack(fill="x")
    localize(ttk.Label(paths), "inputs").grid(row=0, column=0, sticky="nw", padx=(0, 8), pady=5)
    source_list = tk.Listbox(paths, height=3, selectmode="extended", exportselection=False)
    source_list.grid(row=0, column=1, sticky="ew", pady=5)
    source_buttons = ttk.Frame(paths)
    source_buttons.grid(row=0, column=2, columnspan=2, sticky="n", padx=5)
    localize(ttk.Button(source_buttons, command=lambda: pick_file()), "add_file").pack(fill="x", pady=2)
    localize(ttk.Button(source_buttons, command=lambda: pick_source_folder()), "add_folder").pack(fill="x", pady=2)
    localize(ttk.Button(source_buttons, command=lambda: remove_sources()), "remove").pack(fill="x", pady=2)
    localize(ttk.Label(paths), "output").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=5)
    output_entry = ttk.Entry(paths, textvariable=output_var)
    output_entry.grid(row=1, column=1, sticky="ew", pady=5)
    output_entry.bind("<KeyRelease>", lambda _event: mark_export_location_unready())
    localize(ttk.Button(paths, command=lambda: pick_output()), "choose_folder").grid(row=1, column=2, columnspan=2, sticky="ew", padx=5)
    paths.columnconfigure(1, weight=1)

    area_controls = ttk.LabelFrame(outer, padding=8)
    localize(area_controls, "mode")
    area_controls.pack(fill="x", pady=(10, 0))
    localize(ttk.Radiobutton(area_controls, variable=processing_mode,
                   value="ocr", command=lambda: update_mode()), "ocr").pack(side="left", padx=(0, 14))
    localize(ttk.Radiobutton(area_controls, variable=processing_mode,
                   value="fixed", command=lambda: update_mode()), "fixed").pack(side="left", padx=(0, 10))
    fixed_button = localize(ttk.Button(area_controls, command=lambda: choose_fixed_area()), "select_area")
    fixed_button.pack(side="left")
    fixed_area_var.set(tr("area_none"))
    ttk.Label(area_controls, textvariable=fixed_area_var, style="Hint.TLabel").pack(side="left", padx=10)

    controls = ttk.Frame(outer)
    controls.pack(fill="x", pady=12)
    start_button = localize(ttk.Button(controls, command=lambda: start_processing()), "start")
    start_button.pack(side="left")
    stop_button = localize(ttk.Button(controls, command=lambda: stop_processing(), state="disabled"), "stop")
    stop_button.pack(side="left", padx=7)
    localize(ttk.Button(controls, command=lambda: open_folder(str(source_paths[0]) if source_paths else "")), "open_input").pack(side="right", padx=4)
    localize(ttk.Button(controls, command=lambda: open_folder(str(last_export_dir) if last_export_dir and last_export_dir.exists() else output_var.get())), "open_output").pack(side="right", padx=4)

    progress = ttk.Progressbar(outer, mode="determinate", maximum=100)
    progress.pack(fill="x")
    status_line = ttk.Frame(outer)
    status_line.pack(fill="x", pady=(5, 10))
    ttk.Label(status_line, textvariable=status_var).pack(side="left")
    ttk.Label(status_line, textvariable=count_var).pack(side="right")

    results_box = ttk.LabelFrame(outer, padding=6)
    localize(results_box, "results")
    results_box.pack(fill="both", expand=True)
    columns = ("file", "status", "matches")
    tree = ttk.Treeview(results_box, columns=columns, show="headings", height=9)
    tree.heading("file", text=tr("file"))
    tree.heading("status", text=tr("status"))
    tree.heading("matches", text=tr("matches"))
    tree.column("file", width=330)
    tree.column("status", width=150, anchor="center")
    tree.column("matches", width=350)
    yscroll = ttk.Scrollbar(results_box, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=yscroll.set)
    tree.pack(side="left", fill="both", expand=True)
    yscroll.pack(side="right", fill="y")

    log_box = ttk.LabelFrame(outer, padding=6)
    localize(log_box, "log")
    log_box.pack(fill="x", pady=(10, 0))
    log_text = tk.Text(log_box, height=5, wrap="word", state="disabled", font=("Consolas", 9))
    log_scroll = ttk.Scrollbar(log_box, orient="vertical", command=log_text.yview)
    log_text.configure(yscrollcommand=log_scroll.set)
    log_text.pack(side="left", fill="both", expand=True)
    log_scroll.pack(side="right", fill="y")

    def log(message: str) -> None:
        log_text.configure(state="normal")
        log_text.insert("end", message + "\n")
        log_text.see("end")
        log_text.configure(state="disabled")

    def toggle_language() -> None:
        language.set("THAI" if language.get() == "ENG" else "ENG")
        root.title(tr("app_title"))
        language_button.configure(text="THAI" if language.get() == "ENG" else "ENG")
        for widget, key in localized_widgets:
            try:
                widget.configure(text=tr(key))
            except tk.TclError:
                pass
        tree.heading("file", text=tr("file"))
        tree.heading("status", text=tr("status"))
        tree.heading("matches", text=tr("matches"))
        for item in tree.get_children():
            row_status = tree.set(item, "status")
            if row_status in {"รอคิว", "Queued"}:
                tree.set(item, "status", tr("queued"))
            elif row_status in {"สำเร็จ", "Completed"}:
                tree.set(item, "status", tr("completed_file"))
            elif row_status in {"กำลังประมวลผล", "Processing"}:
                tree.set(item, "status", tr("working"))
        if fixed_area_dimensions is None:
            fixed_area_var.set(tr("area_none"))
        else:
            fixed_area_var.set(tr("area_label", width=round(fixed_area_dimensions[0]),
                                  height=round(fixed_area_dimensions[1])))
        set_status(status_state["key"], **status_state["values"])

    language_button.configure(command=toggle_language)
    status_var.set(tr("status_ready"))
    fixed_area_var.set(tr("area_none"))

    def pick_file() -> None:
        values = filedialog.askopenfilenames(title=tr("select_input_title"), filetypes=[(tr("select_input"), "*.jpg *.jpeg *.png")])
        for value in values:
            path = Path(value)
            if path not in source_paths:
                source_paths.append(path)
        if values:
            update_source_list()
            refresh_files()

    def pick_source_folder() -> None:
        value = filedialog.askdirectory(title=tr("select_folder_title"))
        if value:
            path = Path(value)
            if path not in source_paths:
                source_paths.append(path)
            update_source_list()
            refresh_files()

    def update_source_list() -> None:
        source_list.delete(0, "end")
        for path in source_paths:
            source_list.insert("end", str(path))
        source_var.set("; ".join(str(path) for path in source_paths))

    def remove_sources() -> None:
        selected = list(source_list.curselection())
        for index in reversed(selected):
            del source_paths[index]
        update_source_list()
        refresh_files()

    def pick_output() -> None:
        value = filedialog.askdirectory(title=tr("select_export_title"))
        if value:
            export_parent_var.set(value)
            output_var.set(value)

    def update_mode() -> None:
        state = "normal" if processing_mode.get() == "fixed" else "disabled"
        fixed_button.configure(state=state)

    def choose_fixed_area() -> None:
        nonlocal fixed_area
        files = gather_files()
        if not files:
            messagebox.showwarning(tr("no_images_title"), tr("choose_input_for_area"))
            return
        preview_path = files[0][0]
        try:
            with Image.open(preview_path) as im:
                image = ImageOps.exif_transpose(im).convert("RGB")
                original_width, original_height = image.size
                max_width, max_height = 1000, 650
                factor = min(max_width / original_width, max_height / original_height, 1.0)
                shown_size = (max(1, round(original_width * factor)), max(1, round(original_height * factor)))
                display_image = image.resize(shown_size, Image.Resampling.LANCZOS)
        except Exception as exc:
            messagebox.showerror(tr("preview_error"), str(exc))
            return

        dialog = tk.Toplevel(root)
        dialog.title(tr("preview_title"))
        dialog.transient(root)
        dialog.grab_set()
        ttk.Label(dialog, text=tr("preview_image", path=preview_path)).pack(anchor="w", padx=10, pady=(10, 4))
        canvas = tk.Canvas(dialog, width=shown_size[0], height=shown_size[1], cursor="crosshair", highlightthickness=1)
        canvas.pack(padx=10, pady=5)
        photo = ImageTk.PhotoImage(display_image)
        canvas.create_image(0, 0, image=photo, anchor="nw")
        canvas.image = photo
        hint = tk.StringVar(value=tr("select_area_hint"))
        ttk.Label(dialog, textvariable=hint).pack(anchor="w", padx=10)
        selection: dict[str, float | int | None] = {"x0": None, "y0": None, "rect": None, "x1": None, "y1": None}

        def drag_start(event):
            selection["x0"] = min(max(event.x, 0), shown_size[0])
            selection["y0"] = min(max(event.y, 0), shown_size[1])
            if selection["rect"] is not None:
                canvas.delete(selection["rect"])
            selection["rect"] = canvas.create_rectangle(event.x, event.y, event.x, event.y,
                                                         outline="#ff3030", width=3)

        def drag_move(event):
            if selection["x0"] is None:
                return
            x = min(max(event.x, 0), shown_size[0])
            y = min(max(event.y, 0), shown_size[1])
            selection["x1"], selection["y1"] = x, y
            canvas.coords(selection["rect"], selection["x0"], selection["y0"], x, y)

        def drag_end(event):
            drag_move(event)
            x0, y0, x1, y1 = (selection[k] for k in ("x0", "y0", "x1", "y1"))
            if None not in (x0, y0, x1, y1) and abs(x1 - x0) >= 3 and abs(y1 - y0) >= 3:
                hint.set(tr("area_label", width=abs(x1-x0), height=abs(y1-y0)))

        canvas.bind("<ButtonPress-1>", drag_start)
        canvas.bind("<B1-Motion>", drag_move)
        canvas.bind("<ButtonRelease-1>", drag_end)

        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=10, pady=10)

        def accept_area():
            nonlocal fixed_area, fixed_area_dimensions
            x0, y0, x1, y1 = (selection[k] for k in ("x0", "y0", "x1", "y1"))
            if None in (x0, y0, x1, y1) or abs(x1 - x0) < 3 or abs(y1 - y0) < 3:
                messagebox.showwarning(tr("area_warning_title"), tr("area_warning"), parent=dialog)
                return
            fixed_area = (min(x0, x1) / shown_size[0], min(y0, y1) / shown_size[1],
                          max(x0, x1) / shown_size[0], max(y0, y1) / shown_size[1])
            fixed_area_dimensions = (abs(x1-x0), abs(y1-y0))
            fixed_area_var.set(tr("area_label", width=abs(x1-x0), height=abs(y1-y0)))
            processing_mode.set("fixed")
            update_mode()
            dialog.destroy()

        ttk.Button(buttons, text=tr("cancel"), command=dialog.destroy).pack(side="right", padx=(5, 0))
        ttk.Button(buttons, text=tr("use_area"), command=accept_area).pack(side="right")

    def mark_export_location_unready() -> None:
        export_parent_var.set(output_var.get().strip())

    def gather_files() -> list[tuple[Path, Path]]:
        """Collect paths with a relative path for collision-safe export."""
        result: list[tuple[Path, Path]] = []
        seen: set[Path] = set()
        for source_index, source in enumerate(source_paths, start=1):
            prefix = Path(source.name) if len(source_paths) == 1 else Path(f"source_{source_index}_{source.name}")
            if source.is_file():
                candidates = [(source, prefix / source.name)]
            elif source.is_dir():
                candidates = [(p, prefix / p.relative_to(source))
                              for p in sorted(source.rglob("*"))
                              if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS]
            else:
                log(tr("input_error", error=f"source not found: {source}"))
                continue
            for image, relative in candidates:
                if image.suffix.lower() in IMAGE_EXTENSIONS and image not in seen:
                    result.append((image, relative))
                    seen.add(image)
        return result

    def _is_relative_to(path: Path, parent: Path) -> bool:
        try:
            path.resolve().relative_to(parent.resolve())
            return True
        except (ValueError, OSError):
            return False

    def refresh_files() -> list[tuple[Path, Path]]:
        tree.delete(*tree.get_children())
        files = gather_files()
        for path, relative in files:
            tree.insert("", "end", iid=str(path), values=(path.name, tr("queued"), ""))
        count_var.set(f"0 / {len(files)}")
        return files

    def open_folder(text: str) -> None:
        path = Path(text) if text else None
        if path and path.exists():
            subprocess.Popen(["explorer", str(path)])
        else:
            messagebox.showinfo(tr("no_folder_title"), tr("no_folder"))

    def start_processing() -> None:
        nonlocal worker_thread
        if worker_thread and worker_thread.is_alive():
            return
        output_text = output_var.get().strip()
        if not source_paths or not output_text:
            messagebox.showwarning(tr("missing_paths_title"), tr("missing_paths"))
            return
        output_candidate = Path(output_text).resolve()
        for source in source_paths:
            if source.is_dir() and _is_relative_to(output_candidate, source):
                messagebox.showwarning(tr("output_inside_title"), tr("output_inside"))
                return
        files = refresh_files()
        if not files:
            messagebox.showwarning(tr("no_images_title"), tr("no_images"))
            return
        selected_fixed_area = fixed_area if processing_mode.get() == "fixed" else None
        if processing_mode.get() == "fixed" and selected_fixed_area is None:
            messagebox.showwarning(tr("area_required_title"), tr("area_required"))
            return
        # Create a fresh timestamped export directory only after Start is clicked.
        export_parent = Path(export_parent_var.get().strip() or output_text).resolve()
        export_name = datetime.now().strftime("output_%Y%m%d_%H%M%S")
        export_dir = export_parent / export_name
        suffix = 1
        while export_dir.exists():
            export_dir = export_parent / f"{export_name}_{suffix:02d}"
            suffix += 1
        try:
            export_dir.mkdir(parents=True, exist_ok=False)
            probe = export_dir / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except Exception as exc:
            messagebox.showerror(tr("create_export_error_title"),
                                 tr("create_export_error", path=export_dir, error=exc))
            return
        # Show the actual location as soon as processing starts.
        output_var.set(str(export_dir))
        export_parent_var.set(str(export_parent))
        stop_event.clear()
        # When the chosen output is the parent of an input folder, remove the
        # duplicated input-folder prefix from exported relative paths.
        export_files: list[tuple[Path, Path]] = []
        single_source = len(source_paths) == 1
        for image_path, relative_path in files:
            matching_source = next((src for src in source_paths if src.is_dir() and _is_relative_to(image_path, src)), None)
            if single_source and matching_source and _is_relative_to(matching_source, output_candidate):
                relative_path = image_path.relative_to(matching_source)
            export_files.append((image_path, relative_path))
        planned_outputs = [export_dir / relative.parent / f"{relative.stem}_redacted{relative.suffix.lower()}"
                           for _source, relative in export_files]
        duplicate_outputs = len({str(path).casefold() for path in planned_outputs}) != len(planned_outputs)
        unsafe_outputs = [path for path in planned_outputs if not _is_relative_to(path, export_dir)]
        if duplicate_outputs or unsafe_outputs:
            messagebox.showerror(tr("plan_error_title"), tr("plan_error"))
            return
        log(tr("preflight", count=len(files)))
        progress.configure(value=0, maximum=len(files))
        set_status("status_running")
        start_button.configure(state="disabled")
        stop_button.configure(state="normal")
        for path, _relative in files:
            tree.set(str(path), "status", tr("queued"))

        def callback(index, total, path, state, findings):
            events.put(("progress", index, total, path, state, findings))

        def worker():
            try:
                failures = process_many(export_files, export_dir, lang, tesseract_cmd,
                                        fixed_area=selected_fixed_area,
                                        progress_callback=callback, stop_event=stop_event)
                events.put(("done", failures, stop_event.is_set(), export_dir))
            except Exception as exc:
                events.put(("fatal", str(exc)))

        worker_thread = threading.Thread(target=worker, daemon=True)
        log(tr("input_log", paths=' | '.join(str(p) for p in source_paths)))
        log(tr("export_log", path=export_dir))
        log(tr("mode_log", mode=tr("fixed_mode" if selected_fixed_area else "ocr_mode")))
        log(tr("start_log", count=len(files), sources=len(source_paths)))
        worker_thread.start()

    def stop_processing() -> None:
        stop_event.set()
        set_status("status_stopping")
        stop_button.configure(state="disabled")

    def poll_events() -> None:
        try:
            while True:
                event = events.get_nowait()
                if event[0] == "progress":
                    _, index, total, path, state, findings = event
                    progress.configure(value=index)
                    count_var.set(f"{index} / {total}")
                    state_text = tr("completed_file") if state == "สำเร็จ" else state
                    if state == "สำเร็จ":
                        status_var.set(f"{path.name}: {state_text}")
                    else:
                        status_var.set(f"{path.name}: {state_text}")
                    matches = ", ".join(f"{f.kind} {f.value}" for f in findings) or tr("no_address")
                    if tree.exists(str(path)):
                        tree.set(str(path), "status", state)
                        tree.set(str(path), "matches", matches)
                    log(f"{path.name}: {state} | {matches}")
                elif event[0] == "done":
                    nonlocal last_export_dir
                    _, failures, stopped, export_dir = event
                    last_export_dir = export_dir
                    if stopped:
                        set_status("status_stopped")
                    elif not failures:
                        set_status("status_done")
                    else:
                        set_status("status_failed", count=failures)
                    start_button.configure(state="normal")
                    stop_button.configure(state="disabled")
                    log(tr("done_log", status=status_var.get(), path=export_dir))
                elif event[0] == "fatal":
                    set_status("status_cannot_start")
                    start_button.configure(state="normal")
                    stop_button.configure(state="disabled")
                    log(tr("error_log", error=event[1]))
                    messagebox.showerror(tr("start_error_title"), event[1])
        except queue.Empty:
            pass
        root.after(100, poll_events)

    root.after(100, poll_events)
    root.mainloop()
    return 0


def collect_images(source: Path) -> list[Path]:
    """Return supported images from a file or immediate children of a folder."""
    if source.is_file():
        if source.suffix.lower() not in IMAGE_EXTENSIONS:
            raise ValueError(f"ไม่รองรับไฟล์ชนิดนี้: {source.suffix}")
        return [source]
    if source.is_dir():
        return sorted(p for p in source.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS)
    raise FileNotFoundError(f"ไม่พบต้นทาง: {source}")


def read_ocr_words(image_path: Path, lang: str, tesseract_cmd: str | None,
                   thorough: bool = False) -> list[Word]:
    """OCR original plus enhanced/upscaled image variants and merge their boxes.

    Network addresses are usually small UI text. A full-resolution pass, a 2x
    contrast-enhanced pass and a thresholded pass help Tesseract read them.
    Boxes from resized variants are scaled back to original image coordinates.
    """
    resolved_tesseract = tesseract_cmd or shutil.which("tesseract")
    if not resolved_tesseract:
        for candidate in (
            Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
            Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe"),
        ):
            if candidate.is_file():
                resolved_tesseract = str(candidate)
                break
    if not resolved_tesseract:
        raise FileNotFoundError("ไม่พบ Tesseract OCR กรุณาติดตั้งหรือระบุ --tesseract-cmd")
    pytesseract.pytesseract.tesseract_cmd = resolved_tesseract
    with Image.open(image_path) as source:
        original = ImageOps.exif_transpose(source).convert("RGB")
    scale = 2 if max(original.size) < 2600 else 1
    base = original.resize((original.width * scale, original.height * scale), Image.Resampling.LANCZOS) if scale > 1 else original
    enhanced = ImageEnhance.Contrast(base).enhance(1.7).filter(ImageFilter.SHARPEN)
    gray = ImageOps.grayscale(enhanced)
    threshold = gray.point(lambda p: 255 if p > 150 else 0).convert("RGB")
    words: list[Word] = []
    # Sparse text layout, then single-line mode catches labels and values around
    # small UI panels; Tesseract's standard segmentation can miss these.
    passes = ((enhanced, 11), (enhanced, 6), (threshold, 11)) if thorough else ((enhanced, 11), (enhanced, 6))
    for pass_index, (variant, psm) in enumerate(passes):
        data = pytesseract.image_to_data(variant, lang=lang,
                                         config=f"--oem 3 --psm {psm} -c preserve_interword_spaces=1",
                                         output_type=Output.DICT)
        for i, raw in enumerate(data["text"]):
            text = str(raw).strip()
            if not text:
                continue
            left = round(int(data["left"][i]) / scale)
            top = round(int(data["top"][i]) / scale)
            right = round((int(data["left"][i]) + int(data["width"][i])) / scale)
            bottom = round((int(data["top"][i]) + int(data["height"][i])) / scale)
            # Distinguish OCR passes while preserving each pass's line grouping.
            words.append(Word(text, left, top, max(1, right-left), max(1, bottom-top),
                              int(data["block_num"][i]), int(data["par_num"][i]),
                              int(data["line_num"][i]), pass_index))
    return words


def _valid_ip(candidate: str, version: int) -> bool:
    """Use Python's IP parser to reject malformed address-shaped OCR text."""
    try:
        ipaddress.ip_address(candidate.split("%", 1)[0])
        return ipaddress.ip_address(candidate.split("%", 1)[0]).version == version
    except ValueError:
        return False


def find_findings(words: list[Word], image_size: tuple[int, int]) -> list[Finding]:
    """Match addresses across OCR words and map character spans to word boxes.

    OCR often splits punctuation into separate tokens. We join words from the
    same OCR line with no separator, then map each match's span back to every
    overlapping word. This avoids relying on fixed image coordinates.
    """
    by_line: dict[tuple[int, int, int], list[Word]] = {}
    for word in words:
        by_line.setdefault((word.pass_id, word.block, word.paragraph, word.line), []).append(word)

    width, height = image_size
    found: list[Finding] = []
    seen: set[tuple[str, int, int, int, int]] = set()
    for line_words in by_line.values():
        line_words.sort(key=lambda w: w.left)
        # Joining tokens without spaces handles colon groups split by OCR; map
        # each matched character range back to only the word boxes it overlaps.
        joined = "".join(w.text for w in line_words)
        offsets: list[tuple[int, int, Word]] = []
        cursor = 0
        for word in line_words:
            offsets.append((cursor, cursor + len(word.text), word))
            cursor += len(word.text)
        for regex, kind, version in ((IPV6_RE, "IPv6", 6), (IPV4_RE, "IPv4", 4), (MAC_RE, "MAC", 0)):
            for match in regex.finditer(joined):
                value = match.group(0)
                if version and not _valid_ip(value, version):
                    continue
                overlapping = [w for a, b, w in offsets if a < match.end() and b > match.start()]
                if not overlapping:
                    continue
                pad_x, pad_y = 7, 4
                left = max(0, min(w.left for w in overlapping) - pad_x)
                top = max(0, min(w.top for w in overlapping) - pad_y)
                right = min(width, max(w.left + w.width for w in overlapping) + pad_x)
                bottom = min(height, max(w.top + w.height for w in overlapping) + pad_y)
                key = (value, left, top, right, bottom)
                if key not in seen:
                    found.append(Finding(value, kind, left, top, right, bottom))
                    seen.add(key)
    return found


def find_imagemagick() -> str:
    """Locate ImageMagick 7 (magick) or legacy ImageMagick (convert)."""
    for executable in ("magick", "convert"):
        path = shutil.which(executable)
        if path:
            return path
    raise FileNotFoundError("ไม่พบ ImageMagick: ติดตั้ง ImageMagick และเพิ่มลง PATH")


def redact_with_imagemagick(image_path: Path, output_path: Path, findings: Iterable[Finding], executable: str) -> None:
    """Gaussian-blur each sensitive rectangle using ImageMagick region crops."""
    # Apply EXIF orientation through Pillow before drawing so coordinates match OCR.
    # Save an oriented temporary PNG as ImageMagick input.
    import tempfile
    with Image.open(image_path) as source:
        oriented = ImageOps.exif_transpose(source).convert("RGB")
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
            temp_path = Path(temp_file.name)
        try:
            oriented.save(temp_path, format="PNG")
            args = [executable, str(temp_path)]
            for item in findings:
                region = f"{item.right - item.left}x{item.bottom - item.top}+{item.left}+{item.top}"
                # ImageMagick's region operator limits blur to this rectangle.
                args.extend(["-region", region, "-blur", "0x12", "+region"])
            args.extend(["-quality", "95", str(output_path)])
            subprocess.run(args, check=True, capture_output=True, text=True)
        finally:
            temp_path.unlink(missing_ok=True)


def process(source: Path, output_dir: Path, lang: str = "eng", tesseract_cmd: str | None = None,
            preview: bool = False, progress_callback=None, stop_event=None) -> int:
    """Process all images, write uniquely named outputs and return failure count."""
    images = collect_images(source)
    if not images:
        LOG.warning("ไม่พบไฟล์ .jpg, .jpeg หรือ .png ใน %s", source)
        return 0
    if not preview:
        output_dir.mkdir(parents=True, exist_ok=True)
        imagemagick = find_imagemagick()
    else:
        imagemagick = ""
    resolved_tesseract = tesseract_cmd or shutil.which("tesseract")
    if not resolved_tesseract:
        for candidate in (Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
                          Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe")):
            if candidate.is_file():
                resolved_tesseract = str(candidate)
                break
    if not resolved_tesseract:
        raise FileNotFoundError("ไม่พบ Tesseract OCR กรุณาติดตั้งหรือระบุ --tesseract-cmd")
    pytesseract.pytesseract.tesseract_cmd = resolved_tesseract
    available_langs = pytesseract.get_languages(config="")
    requested_langs = lang.split("+")
    missing_langs = [item for item in requested_langs if item not in available_langs]
    if missing_langs:
        raise RuntimeError(f"Tesseract ไม่มี language data: {', '.join(missing_langs)}; ติดตั้ง language pack หรือเลือก --lang ที่มี")
    failures = 0
    for image_index, image_path in enumerate(images, start=1):
        try:
            if stop_event is not None and stop_event.is_set():
                LOG.info("หยุดตามคำสั่งผู้ใช้")
                break
            with Image.open(image_path) as im:
                # Tesseract boxes are measured in EXIF-corrected orientation.
                oriented = ImageOps.exif_transpose(im)
                image_size = oriented.size
            words = read_ocr_words(image_path, lang, tesseract_cmd)
            findings = find_findings(words, image_size)
            # Only run the slower thresholded pass for images where the first
            # two OCR passes found nothing; keeps recall for hard screenshots.
            if not findings:
                extra_words = read_ocr_words(image_path, lang, tesseract_cmd, thorough=True)
                findings = find_findings(extra_words, image_size)
            if findings:
                LOG.info("%s: พบ %s", image_path.name,
                         ", ".join(f"{f.kind} {f.value} [{f.left},{f.top},{f.right},{f.bottom}]" for f in findings))
            else:
                LOG.info("%s: ไม่พบ IPv4/IPv6/MAC", image_path.name)
            if not preview:
                output_path = output_dir / f"{image_path.stem}_redacted{image_path.suffix.lower()}"
                redact_with_imagemagick(image_path, output_path, findings, imagemagick)
                LOG.info("สำเร็จ: %s", output_path)
            if progress_callback:
                progress_callback(image_index, len(images), image_path, "สำเร็จ", findings)
        except Exception as exc:  # Continue processing remaining images in a batch.
            failures += 1
            LOG.exception("ประมวลผลไม่สำเร็จ %s: %s", image_path.name, exc)
            if progress_callback:
                progress_callback(image_index, len(images), image_path, f"ผิดพลาด: {exc}", [])
    return failures


def process_many(files: list[tuple[Path, Path]], output_dir: Path, lang: str = "eng",
                 tesseract_cmd: str | None = None, fixed_area: tuple[float, float, float, float] | None = None,
                 progress_callback=None, stop_event=None) -> int:
    """Process pre-collected images from multiple roots, preserving subfolders."""
    output_dir.mkdir(parents=True, exist_ok=True)
    imagemagick = find_imagemagick()
    resolved_tesseract = tesseract_cmd or shutil.which("tesseract")
    if fixed_area is None:
        if not resolved_tesseract:
            for candidate in (Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
                              Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe")):
                if candidate.is_file():
                    resolved_tesseract = str(candidate)
                    break
        if not resolved_tesseract:
            raise FileNotFoundError("ไม่พบ Tesseract OCR กรุณาติดตั้งหรือระบุ --tesseract-cmd")
        pytesseract.pytesseract.tesseract_cmd = resolved_tesseract
        missing_langs = [item for item in lang.split("+") if item not in pytesseract.get_languages(config="")]
        if missing_langs:
            raise RuntimeError(f"Tesseract ไม่มี language data: {', '.join(missing_langs)}")
    failures = 0
    for index, (image_path, relative_path) in enumerate(files, start=1):
        if stop_event is not None and stop_event.is_set():
            break
        try:
            with Image.open(image_path) as im:
                image_size = ImageOps.exif_transpose(im).size
            if fixed_area is not None:
                x0, y0, x1, y1 = fixed_area
                left = max(0, min(image_size[0] - 1, round(x0 * image_size[0])))
                top = max(0, min(image_size[1] - 1, round(y0 * image_size[1])))
                right = max(left + 1, min(image_size[0], round(x1 * image_size[0])))
                bottom = max(top + 1, min(image_size[1], round(y1 * image_size[1])))
                findings = [Finding("fixed area", "Fixed Area", left, top, right, bottom)]
            else:
                words = read_ocr_words(image_path, lang, resolved_tesseract)
                findings = find_findings(words, image_size)
                if not findings:
                    findings = find_findings(read_ocr_words(image_path, lang, resolved_tesseract, thorough=True), image_size)
            output_path = output_dir / relative_path.parent / f"{relative_path.stem}_redacted{relative_path.suffix.lower()}"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            redact_with_imagemagick(image_path, output_path, findings, imagemagick)
            if progress_callback:
                progress_callback(index, len(files), image_path, "สำเร็จ",
                                  findings)
        except Exception as exc:
            failures += 1
            LOG.exception("ประมวลผลไม่สำเร็จ %s: %s", image_path.name, exc)
            if progress_callback:
                progress_callback(index, len(files), image_path, f"ผิดพลาด: {exc}", [])
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="ค้นหา IPv4/IPv6/MAC และเบลอด้วย OCR")
    parser.add_argument("source", nargs="?", type=Path, help="ไฟล์ภาพหรือโฟลเดอร์ (หากไม่ระบุจะเปิด GUI)")
    parser.add_argument("output", nargs="?", type=Path, help="โฟลเดอร์ส่งออก")
    parser.add_argument("--lang", default="eng", help="รหัสภาษา Tesseract เช่น eng หรือ eng+tha")
    parser.add_argument("--tesseract-cmd", help="พาธ tesseract.exe หากไม่ได้อยู่ใน PATH")
    parser.add_argument("--preview", action="store_true", help="OCR และแสดงผลใน log โดยไม่เขียนภาพ")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if args.source is None:
        return launch_gui(args.lang, args.tesseract_cmd)
    elif args.output is None:
        parser.error("เมื่อระบุ source ผ่าน command line ต้องระบุ output ด้วย")
    else:
        source, output = args.source, args.output
    return 1 if process(source, output, args.lang, args.tesseract_cmd, args.preview) else 0


if __name__ == "__main__":
    raise SystemExit(main())

