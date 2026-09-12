"""Video OCR → translate → SRT generator/inserter (v1.02).

Run: python app.py
"""
from __future__ import annotations

import os
import re
import threading
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Optional

import customtkinter as ctk
from PIL import Image, ImageTk

import config
import srt_utils
from llm_engine import LLMError, ProviderCfg, test_connection, translate_image
from video_engine import VideoEngine, VideoEngineError

FONT_BODY = ("Microsoft JhengHei", 13)
FONT_SMALL = ("Microsoft JhengHei", 11)
FONT_MONO = ("Consolas", 12)
FONT_TITLE = ("Microsoft JhengHei", 18, "bold")
FONT_SUBTITLE = ("Microsoft JhengHei", 14, "bold")

KEY_DISPLAY_MAP = {
    "space": "Space (空白鍵)",
    "return": "Enter (回車鍵)",
    "left": "← Left (左方向鍵)",
    "right": "→ Right (右方向鍵)",
    "up": "↑ Up (上方向鍵)",
    "down": "↓ Down (下方向鍵)",
    "bracketleft": "[",
    "bracketright": "]",
    "minus": "-",
    "plus": "+",
    "equal": "=",
    "comma": ",",
    "period": ".",
    "slash": "/",
    "backslash": "\\",
    "semicolon": ";",
    "apostrophe": "'",
    "tab": "Tab",
    "escape": "Esc",
    "backspace": "Backspace",
    "delete": "Delete",
}


def format_key_display(keysym: str) -> str:
    if not keysym:
        return "（未設定）"
    k_lower = keysym.lower()
    if k_lower in KEY_DISPLAY_MAP:
        return KEY_DISPLAY_MAP[k_lower]
    if len(keysym) == 1:
        return keysym.upper()
    return keysym


KEYBINDING_ACTIONS = [
    ("play_pause", "播放 / 暫停"),
    ("step_back_large", "快退大步長 (秒)"),
    ("step_back_small", "快退小步長 (秒)"),
    ("step_back_frame", "倒退幀數 (幀)"),
    ("step_forward_frame", "前進幀數 (幀)"),
    ("step_forward_small", "快進小步長 (秒)"),
    ("step_forward_large", "快進大步長 (秒)"),
    ("set_start", "[ 設為起點"),
    ("set_end", "] 設為迄點"),
    ("jump_start", "跳至起點"),
    ("jump_end", "跳至迄點"),
    ("reset_roi", "重設全畫面 (ROI)"),
    ("apply_last_roi", "套用上次框選"),
    ("add_task", "➕ 新增至任務清單"),
]


def parse_time_input(val: str, default: float = 0.0) -> float:
    """Parse time string like '00:01:23,456', '01:23.456', or '83.456' to seconds."""
    s = val.strip().replace(",", ".")
    if not s:
        return default
    try:
        return max(0.0, float(s))
    except ValueError:
        pass
    # Try HH:MM:SS.mmm or MM:SS.mmm
    parts = s.split(":")
    try:
        if len(parts) == 3:
            h, m, sec = parts
            return int(h) * 3600 + int(m) * 60 + float(sec)
        elif len(parts) == 2:
            m, sec = parts
            return int(m) * 60 + float(sec)
    except Exception:
        pass
    return default



@dataclass
class SubtitleTask:
    id: int
    start_sec: float
    end_sec: float
    start_frame_img: Image.Image
    crop_box: Optional[tuple[int, int, int, int]]  # (x1, y1, x2, y2) in video coordinates, or None
    crop_img: Image.Image                          # Cropped PIL Image sent to LLM
    status: str = "待處理"                          # "待處理" | "翻譯中" | "已完成" | "失敗"
    translation: str = ""
    error_msg: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)

    @property
    def start_ts(self) -> str:
        return srt_utils.sec_to_ts(self.start_sec)

    @property
    def end_ts(self) -> str:
        return srt_utils.sec_to_ts(self.end_sec)


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("影片畫面 OCR 翻譯 → SRT 產生器 (v1.02)")
        self.geometry("1160x800")
        self.minsize(980, 680)

        self._settings = config.load_settings()
        ctk.set_appearance_mode(self._settings.get("appearance_mode", "dark"))
        ctk.set_default_color_theme("blue")

        # Runtime State
        self._video_engine: Optional[VideoEngine] = None
        self._video_path: str = ""
        self._is_playing: bool = False
        self._play_after_id = None
        self._current_frame_img: Optional[Image.Image] = None
        self._current_sec: float = 0.0
        self._is_scrubbing: bool = False

        # Range & Crop State
        self._start_sec: float = 0.0
        self._end_sec: float = 2.0
        self._start_frame_img: Optional[Image.Image] = None
        self._crop_box: Optional[tuple[int, int, int, int]] = None
        self._last_crop_box: Optional[tuple[int, int, int, int]] = None

        # Step Settings & Keybindings State
        self._step_settings: dict = dict(self._settings.get("step_settings", config.DEFAULT_STEP_SETTINGS))
        self._keybindings: dict = dict(self._settings.get("keybindings", config.DEFAULT_KEYBINDINGS))
        self._recording_action: Optional[str] = None

        # Canvas drawing & coordinate mapping
        self._disp_scale: float = 1.0
        self._disp_offset_x: float = 0.0
        self._disp_offset_y: float = 0.0
        self._canvas_img_tk = None
        self._crop_thumb_tk = None
        self._drag_start_x: float = 0.0
        self._drag_start_y: float = 0.0
        self._rect_id: Optional[int] = None

        # Tasks and SRT
        self._tasks: list[SubtitleTask] = []
        self._next_task_id: int = 1
        self._srt_path: str = ""
        self._srt_entries: list[dict] = []
        self._busy: bool = False

        # Tabs
        self.tabs = ctk.CTkTabview(self, anchor="nw")
        self.tabs.pack(fill="both", expand=True, padx=10, pady=(8, 10))
        self.tabs.add("  影片與字幕任務  ")
        self.tabs.add("  按鈕與快捷鍵設定  ")
        self.tabs.add("  API 設定  ")
        self.tabs.add("  Prompt 設定  ")
        self.tabs.add("  執行  ")

        self._build_video_tab(self.tabs.tab("  影片與字幕任務  "))
        self._build_keybindings_tab(self.tabs.tab("  按鈕與快捷鍵設定  "))
        self._build_api_tab(self.tabs.tab("  API 設定  "))
        self._build_prompt_tab(self.tabs.tab("  Prompt 設定  "))
        self._build_run_tab(self.tabs.tab("  執行  "))

        self._refresh_step_buttons()
        self._sync_api_form()
        self.bind_all("<KeyPress>", self._on_global_key_press)
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)

    # ------------------------------------------------------------------ #
    #  Tab 1: Video Player & Subtitle Tasks
    # ------------------------------------------------------------------ #
    def _build_video_tab(self, parent):
        # Top bar: Video & SRT selection
        top_bar = ctk.CTkFrame(parent, fg_color=("gray90", "gray17"))
        top_bar.pack(fill="x", padx=10, pady=(6, 6))

        # Row 1 of top bar: Video
        v_row = ctk.CTkFrame(top_bar, fg_color="transparent")
        v_row.pack(fill="x", padx=8, pady=(6, 4))
        ctk.CTkButton(v_row, text="選擇影片", width=110, height=30,
                      font=FONT_BODY, command=self._pick_video).pack(side="left")
        self._video_label = ctk.CTkLabel(v_row, text="（尚未載入影片）", font=FONT_BODY, anchor="w")
        self._video_label.pack(side="left", fill="x", expand=True, padx=(8, 8))
        self._video_spec_label = ctk.CTkLabel(v_row, text="", font=FONT_SMALL,
                                             text_color=("gray40", "#9aa0a6"), anchor="e")
        self._video_spec_label.pack(side="right", padx=(0, 4))

        # Row 2 of top bar: Target SRT (Optional)
        s_row = ctk.CTkFrame(top_bar, fg_color="transparent")
        s_row.pack(fill="x", padx=8, pady=(2, 6))
        ctk.CTkButton(s_row, text="目標 SRT (選填)", width=110, height=28,
                      font=FONT_SMALL, command=self._pick_srt).pack(side="left")
        self._srt_var = ctk.StringVar(value="（未選擇，將直接匯出新 SRT 檔案）")
        ctk.CTkLabel(s_row, textvariable=self._srt_var, font=FONT_SMALL,
                     anchor="w", text_color=("gray40", "#9aa0a6")).pack(side="left", fill="x", expand=True, padx=(8, 8))
        self._clear_srt_btn = ctk.CTkButton(s_row, text="清除", width=60, height=26,
                                            font=FONT_SMALL, command=self._clear_srt)
        self._clear_srt_btn.pack(side="right", padx=(0, 4))

        # Main Split Container: Left = Player & Range/ROI, Right = Tasks
        main_box = ctk.CTkFrame(parent, fg_color="transparent")
        main_box.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        main_box.grid_columnconfigure(0, weight=6, minsize=560)
        main_box.grid_columnconfigure(1, weight=5, minsize=380)
        main_box.grid_rowconfigure(0, weight=1)

        # Left Column: Player & Range Selector
        left_col = ctk.CTkFrame(main_box)
        left_col.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=0)

        # 1. Canvas for Video Display & Interactive ROI Drag
        self._canvas_container = ctk.CTkFrame(left_col, fg_color="black")
        self._canvas_container.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        self._canvas = tk.Canvas(self._canvas_container, bg="#141414", highlightthickness=0, cursor="cross")
        self._canvas.pack(fill="both", expand=True)
        self._canvas.bind("<Configure>", self._on_canvas_resize)
        self._canvas.bind("<ButtonPress-1>", self._on_canvas_click)
        self._canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_canvas_release)

        # Overlay hint on canvas
        self._hint_text_id = self._canvas.create_text(
            300, 160, text="載入影片後，可於預覽框拖曳滑鼠框選翻譯區域（預設全畫面）",
            fill="#888888", font=("Microsoft JhengHei", 12), justify="center"
        )

        # 2. Playback Control Bar
        ctl_frame = ctk.CTkFrame(left_col, fg_color="transparent")
        ctl_frame.pack(fill="x", padx=8, pady=(2, 4))

        # Slider row
        slider_row = ctk.CTkFrame(ctl_frame, fg_color="transparent")
        slider_row.pack(fill="x", pady=(0, 2))
        self._time_slider = ctk.CTkSlider(slider_row, from_=0, to=100,
                                          command=self._on_slider_change)
        self._time_slider.set(0)
        self._time_slider.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._time_slider.bind("<ButtonRelease-1>", self._on_slider_release)
        self._time_label = ctk.CTkLabel(slider_row, text="00:00:00,000 / 00:00:00,000",
                                        font=FONT_MONO, width=190, anchor="e")
        self._time_label.pack(side="right")

        # Button row: Step & Play (7 buttons: -large, -small, -frame, Play, +frame, +small, +large)
        btn_row = ctk.CTkFrame(ctl_frame, fg_color="transparent")
        btn_row.pack(fill="x", pady=(2, 2))

        self._btn_step_back_large = ctk.CTkButton(
            btn_row, text="⏪ -5s", width=62, height=28, font=FONT_SMALL,
            command=self._on_step_back_large)
        self._btn_step_back_large.pack(side="left", padx=(0, 3))

        self._btn_step_back_small = ctk.CTkButton(
            btn_row, text="⏪ -3s", width=62, height=28, font=FONT_SMALL,
            command=self._on_step_back_small)
        self._btn_step_back_small.pack(side="left", padx=(0, 3))

        self._btn_step_back_frame = ctk.CTkButton(
            btn_row, text="◀ -1幀", width=62, height=28, font=FONT_SMALL,
            command=self._on_step_back_frame)
        self._btn_step_back_frame.pack(side="left", padx=(0, 5))

        self._play_btn = ctk.CTkButton(
            btn_row, text="▶ 播放", width=82, height=28, font=FONT_BODY,
            command=self._toggle_play)
        self._play_btn.pack(side="left", padx=(0, 5))

        self._btn_step_forward_frame = ctk.CTkButton(
            btn_row, text="▶ +1幀", width=62, height=28, font=FONT_SMALL,
            command=self._on_step_forward_frame)
        self._btn_step_forward_frame.pack(side="left", padx=(0, 3))

        self._btn_step_forward_small = ctk.CTkButton(
            btn_row, text="⏩ +3s", width=62, height=28, font=FONT_SMALL,
            command=self._on_step_forward_small)
        self._btn_step_forward_small.pack(side="left", padx=(0, 3))

        self._btn_step_forward_large = ctk.CTkButton(
            btn_row, text="⏩ +5s", width=62, height=28, font=FONT_SMALL,
            command=self._on_step_forward_large)
        self._btn_step_forward_large.pack(side="left")

        # 3. Task Configuration Box (Interval Setting & ROI Info)
        task_cfg_box = ctk.CTkFrame(left_col, fg_color=("gray85", "gray20"))
        task_cfg_box.pack(fill="x", padx=8, pady=(4, 8))

        # Time range selection row
        tr_row = ctk.CTkFrame(task_cfg_box, fg_color="transparent")
        tr_row.pack(fill="x", padx=8, pady=(6, 4))

        # Start Time setting
        ctk.CTkButton(tr_row, text="[ 設為起點", width=90, height=28, font=FONT_BODY,
                      fg_color="#1f6aa5", command=self._set_as_start).pack(side="left")
        self._start_var = ctk.StringVar(value="00:00:00,000")
        self._start_entry = ctk.CTkEntry(tr_row, textvariable=self._start_var, width=105, font=FONT_MONO)
        self._start_entry.pack(side="left", padx=(4, 4))
        self._start_entry.bind("<FocusOut>", self._on_start_entry_change)
        ctk.CTkButton(tr_row, text="跳至", width=46, height=28, font=FONT_SMALL,
                      command=self._jump_to_start).pack(side="left", padx=(0, 10))

        # End Time setting
        ctk.CTkButton(tr_row, text="] 設為迄點", width=90, height=28, font=FONT_BODY,
                      fg_color="#1f6aa5", command=self._set_as_end).pack(side="left")
        self._end_var = ctk.StringVar(value="00:00:02,000")
        self._end_entry = ctk.CTkEntry(tr_row, textvariable=self._end_var, width=105, font=FONT_MONO)
        self._end_entry.pack(side="left", padx=(4, 4))
        self._end_entry.bind("<FocusOut>", self._on_end_entry_change)
        ctk.CTkButton(tr_row, text="跳至", width=46, height=28, font=FONT_SMALL,
                      command=self._jump_to_end).pack(side="left", padx=(0, 8))

        # Duration display
        self._dur_disp_label = ctk.CTkLabel(tr_row, text="時長: 2.00s", font=FONT_BODY)
        self._dur_disp_label.pack(side="left")

        # ROI Crop Status & Action row
        roi_row = ctk.CTkFrame(task_cfg_box, fg_color="transparent")
        roi_row.pack(fill="x", padx=8, pady=(2, 6))

        # Crop preview thumbnail canvas
        self._crop_thumb_canvas = tk.Canvas(roi_row, width=80, height=45, bg="#222222", highlightthickness=1, highlightbackground="#444")
        self._crop_thumb_canvas.pack(side="left", padx=(0, 8))

        # ROI info label
        self._roi_label = ctk.CTkLabel(roi_row, text="翻譯範圍：全畫面 (預設)", font=FONT_SMALL, anchor="w")
        self._roi_label.pack(side="left", fill="x", expand=True)

        # ROI reset & reuse buttons
        ctk.CTkButton(roi_row, text="重設全畫面", width=95, height=26, font=FONT_SMALL,
                      command=self._reset_roi).pack(side="left", padx=(4, 4))
        ctk.CTkButton(roi_row, text="套用上次框選", width=95, height=26, font=FONT_SMALL,
                      command=self._apply_last_roi).pack(side="left", padx=(0, 8))

        # Add Task Button
        self._add_task_btn = ctk.CTkButton(task_cfg_box, text="➕ 新增至任務清單", height=34,
                                           font=FONT_SUBTITLE, fg_color="#2e7d32", hover_color="#1b5e20",
                                           command=self._add_task)
        self._add_task_btn.pack(fill="x", padx=8, pady=(0, 8))

        # Right Column: Tasks List
        right_col = ctk.CTkFrame(main_box)
        right_col.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=0)

        # Task list header
        t_hdr = ctk.CTkFrame(right_col, fg_color="transparent")
        t_hdr.pack(fill="x", padx=10, pady=(8, 4))
        self._task_count_label = ctk.CTkLabel(t_hdr, text="待翻譯任務列表 (共 0 組)", font=FONT_SUBTITLE, anchor="w")
        self._task_count_label.pack(side="left")
        ctk.CTkButton(t_hdr, text="清空任務", width=80, height=26, font=FONT_SMALL,
                      fg_color="#b23b3b", hover_color="#8a2e2e",
                      command=self._clear_tasks).pack(side="right")

        # Scrollable Task List
        self._task_scroll = ctk.CTkScrollableFrame(right_col)
        self._task_scroll.pack(fill="both", expand=True, padx=10, pady=(2, 6))

        # Bottom shortcut button
        goto_run_btn = ctk.CTkButton(right_col, text="前往執行翻譯與匯出 →", height=34,
                                     font=FONT_BODY, command=lambda: self.tabs.set("  執行  "))
        goto_run_btn.pack(fill="x", padx=10, pady=(4, 8))

    # ------------------------------------------------------------------ #
    #  Video Loading & Playback Engine Methods
    # ------------------------------------------------------------------ #
    def _pick_video(self):
        initial = self._settings.get("last_video_dir") or os.getcwd()
        path = filedialog.askopenfilename(
            initialdir=initial,
            filetypes=[
                ("影片檔案", "*.mp4 *.mkv *.avi *.mov *.flv *.wmv *.webm"),
                ("所有檔案", "*.*")
            ],
            title="選擇影片檔案",
        )
        if not path:
            return

        self._video_path = path
        self._settings["last_video_dir"] = str(Path(path).parent)
        self._settings["last_video_path"] = path
        config.patch_setting("last_video_dir", self._settings["last_video_dir"])
        config.patch_setting("last_video_path", path)

        self._load_video(path)

    def _load_video(self, path: str):
        self._stop_play()
        if self._video_engine:
            self._video_engine.close()

        try:
            self._video_engine = VideoEngine(path)
        except VideoEngineError as exc:
            messagebox.showerror("開啟影片失敗", str(exc))
            return

        filename = Path(path).name
        dur_str = srt_utils.sec_to_ts(self._video_engine.duration_sec)
        fps = self._video_engine.fps
        w = self._video_engine.width
        h = self._video_engine.height

        self._video_label.configure(text=filename)
        self._video_spec_label.configure(text=f"{w}x{h} | {fps:.2f} FPS | 總長 {dur_str}")
        self._time_slider.configure(to=max(1.0, self._video_engine.duration_sec))

        # Default start and end
        self._current_sec = 0.0
        self._start_sec = 0.0
        self._end_sec = min(2.0, self._video_engine.duration_sec)
        self._update_range_inputs()

        # Remove hint and seek to first frame
        self._canvas.delete(self._hint_text_id)
        self._seek_to(0.0)

    def _seek_to(self, sec: float):
        if not self._video_engine:
            return
        sec = max(0.0, min(sec, self._video_engine.duration_sec))
        ok, img = self._video_engine.seek_time(sec)
        if ok and img:
            self._current_frame_img = img
            self._current_sec = sec
            self._render_current_frame()
            self._update_time_labels()

    def _render_current_frame(self):
        if not self._current_frame_img:
            return

        c_w = self._canvas.winfo_width()
        c_h = self._canvas.winfo_height()
        if c_w <= 10 or c_h <= 10:
            c_w, c_h = 640, 360

        v_w, v_h = self._current_frame_img.size
        scale = min(c_w / v_w, c_h / v_h)
        self._disp_scale = scale
        disp_w = max(1, int(v_w * scale))
        disp_h = max(1, int(v_h * scale))

        self._disp_offset_x = (c_w - disp_w) / 2.0
        self._disp_offset_y = (c_h - disp_h) / 2.0

        resized = self._current_frame_img.resize((disp_w, disp_h), Image.Resampling.BILINEAR)
        self._canvas_img_tk = ImageTk.PhotoImage(resized)

        self._canvas.delete("all")
        self._canvas.create_image(
            int(self._disp_offset_x), int(self._disp_offset_y),
            anchor="nw", image=self._canvas_img_tk
        )

        # If there's an active crop box, redraw it on the canvas
        if self._crop_box:
            self._draw_crop_box_on_canvas(self._crop_box)

    def _on_canvas_resize(self, _event):
        if self._current_frame_img:
            self._render_current_frame()

    def _update_time_labels(self):
        cur_str = srt_utils.sec_to_ts(self._current_sec)
        total_str = srt_utils.sec_to_ts(self._video_engine.duration_sec if self._video_engine else 0.0)
        self._time_label.configure(text=f"{cur_str} / {total_str}")
        if not self._is_scrubbing:
            self._time_slider.set(self._current_sec)

    def _on_slider_change(self, val: float):
        self._is_scrubbing = True
        if self._video_engine:
            self._seek_to(val)

    def _on_slider_release(self, _event):
        self._is_scrubbing = False

    def _toggle_play(self):
        if not self._video_engine:
            return
        if self._is_playing:
            self._stop_play()
        else:
            self._start_play()

    def _start_play(self):
        if not self._video_engine:
            return
        self._is_playing = True
        self._play_btn.configure(text="⏸ 暫停")
        self._play_tick()

    def _stop_play(self):
        self._is_playing = False
        if self._play_after_id:
            self.after_cancel(self._play_after_id)
            self._play_after_id = None
        self._play_btn.configure(text="▶ 播放")

    def _play_tick(self):
        if not self._is_playing or not self._video_engine:
            return

        ok, sec, img = self._video_engine.read_frame()
        if ok and img:
            self._current_frame_img = img
            self._current_sec = sec
            self._render_current_frame()
            self._update_time_labels()

            # Schedule next frame based on FPS
            fps = self._video_engine.fps
            interval = max(15, int(1000.0 / fps))
            self._play_after_id = self.after(interval, self._play_tick)
        else:
            self._stop_play()

    def _step_time(self, delta_sec: float):
        self._stop_play()
        self._seek_to(self._current_sec + delta_sec)

    def _step_frame(self, delta_frames: int):
        self._stop_play()
        if not self._video_engine:
            return
        cur_frame = int(round(self._current_sec * self._video_engine.fps))
        target_frame = cur_frame + delta_frames
        ok, img = self._video_engine.seek_frame(target_frame)
        if ok and img:
            self._current_frame_img = img
            self._current_sec = self._video_engine.current_sec
            self._render_current_frame()
            self._update_time_labels()

    def _on_step_back_large(self):
        sec = float(self._step_settings.get("step_back_large_sec", 5.0))
        self._step_time(-sec)

    def _on_step_back_small(self):
        sec = float(self._step_settings.get("step_back_small_sec", 3.0))
        self._step_time(-sec)

    def _on_step_back_frame(self):
        frames = int(self._step_settings.get("step_back_frames", 1))
        self._step_frame(-frames)

    def _on_step_forward_frame(self):
        frames = int(self._step_settings.get("step_forward_frames", 1))
        self._step_frame(frames)

    def _on_step_forward_small(self):
        sec = float(self._step_settings.get("step_forward_small_sec", 3.0))
        self._step_time(sec)

    def _on_step_forward_large(self):
        sec = float(self._step_settings.get("step_forward_large_sec", 5.0))
        self._step_time(sec)

    def _refresh_step_buttons(self):
        s = self._step_settings
        b_large_sec = float(s.get("step_back_large_sec", 5.0))
        b_small_sec = float(s.get("step_back_small_sec", 3.0))
        b_frames = int(s.get("step_back_frames", 1))
        f_frames = int(s.get("step_forward_frames", 1))
        f_small_sec = float(s.get("step_forward_small_sec", 3.0))
        f_large_sec = float(s.get("step_forward_large_sec", 5.0))

        if hasattr(self, "_btn_step_back_large"):
            self._btn_step_back_large.configure(text=f"⏪ -{b_large_sec:g}s")
        if hasattr(self, "_btn_step_back_small"):
            self._btn_step_back_small.configure(text=f"⏪ -{b_small_sec:g}s")
        if hasattr(self, "_btn_step_back_frame"):
            self._btn_step_back_frame.configure(text=f"◀ -{b_frames}幀")
        if hasattr(self, "_btn_step_forward_frame"):
            self._btn_step_forward_frame.configure(text=f"▶ +{f_frames}幀")
        if hasattr(self, "_btn_step_forward_small"):
            self._btn_step_forward_small.configure(text=f"⏩ +{f_small_sec:g}s")
        if hasattr(self, "_btn_step_forward_large"):
            self._btn_step_forward_large.configure(text=f"⏩ +{f_large_sec:g}s")


    # ------------------------------------------------------------------ #
    #  Range & ROI Crop Logic
    # ------------------------------------------------------------------ #
    def _set_as_start(self):
        self._stop_play()
        self._start_sec = self._current_sec
        # Freeze current frame as the start frame for OCR
        if self._current_frame_img:
            self._start_frame_img = self._current_frame_img.copy()

        # Ensure end_sec is after start_sec
        if self._end_sec <= self._start_sec:
            self._end_sec = self._start_sec + 2.0
            if self._video_engine:
                self._end_sec = min(self._end_sec, self._video_engine.duration_sec)

        self._update_range_inputs()
        self._update_crop_preview()

    def _set_as_end(self):
        self._stop_play()
        if self._current_sec <= self._start_sec:
            messagebox.showwarning("時間錯誤", "迄點時間必須大於起點時間。")
            return
        self._end_sec = self._current_sec
        self._update_range_inputs()

    def _update_range_inputs(self):
        self._start_var.set(srt_utils.sec_to_ts(self._start_sec))
        self._end_var.set(srt_utils.sec_to_ts(self._end_sec))
        dur = max(0.0, self._end_sec - self._start_sec)
        self._dur_disp_label.configure(text=f"時長: {dur:.2f}s")

    def _on_start_entry_change(self, _event=None):
        val = parse_time_input(self._start_var.get(), self._start_sec)
        self._start_sec = val
        self._start_var.set(srt_utils.sec_to_ts(val))
        dur = max(0.0, self._end_sec - self._start_sec)
        self._dur_disp_label.configure(text=f"時長: {dur:.2f}s")

    def _on_end_entry_change(self, _event=None):
        val = parse_time_input(self._end_var.get(), self._end_sec)
        if val <= self._start_sec:
            val = self._start_sec + 1.0
        self._end_sec = val
        self._end_var.set(srt_utils.sec_to_ts(val))
        dur = max(0.0, self._end_sec - self._start_sec)
        self._dur_disp_label.configure(text=f"時長: {dur:.2f}s")

    def _jump_to_start(self):
        self._stop_play()
        self._seek_to(self._start_sec)

    def _jump_to_end(self):
        self._stop_play()
        self._seek_to(self._end_sec)

    # Interactive Canvas Mouse Drag ROI
    def _on_canvas_click(self, event):
        if not self._current_frame_img:
            return
        # If video is playing, pause on click for comfortable selection
        if self._is_playing:
            self._stop_play()

        self._drag_start_x = event.x
        self._drag_start_y = event.y

        if self._rect_id:
            self._canvas.delete(self._rect_id)
            self._rect_id = None

    def _on_canvas_drag(self, event):
        if not self._current_frame_img:
            return

        x0 = min(self._drag_start_x, event.x)
        y0 = min(self._drag_start_y, event.y)
        x1 = max(self._drag_start_x, event.x)
        y1 = max(self._drag_start_y, event.y)

        if not self._rect_id:
            self._rect_id = self._canvas.create_rectangle(
                x0, y0, x1, y1,
                outline="#00e5ff", width=2, dash=(4, 2)
            )
        else:
            self._canvas.coords(self._rect_id, x0, y0, x1, y1)

    def _on_canvas_release(self, event):
        if not self._current_frame_img:
            return

        x0 = min(self._drag_start_x, event.x)
        y0 = min(self._drag_start_y, event.y)
        x1 = max(self._drag_start_x, event.x)
        y1 = max(self._drag_start_y, event.y)

        # Ignore tiny accidental clicks (< 12 pixels)
        if (x1 - x0) < 12 or (y1 - y0) < 12:
            return

        # Convert canvas display coordinates to video native coordinates
        v_w, v_h = self._current_frame_img.size
        s = self._disp_scale
        ox = self._disp_offset_x
        oy = self._disp_offset_y

        vx0 = max(0, min(v_w, int((x0 - ox) / s)))
        vy0 = max(0, min(v_h, int((y0 - oy) / s)))
        vx1 = max(0, min(v_w, int((x1 - ox) / s)))
        vy1 = max(0, min(v_h, int((y1 - oy) / s)))

        if (vx1 - vx0) > 5 and (vy1 - vy0) > 5:
            self._crop_box = (vx0, vy0, vx1, vy1)
            self._last_crop_box = self._crop_box
            # If start_frame_img not set yet, initialize it with current frame
            if self._start_frame_img is None:
                self._start_frame_img = self._current_frame_img.copy()
            self._update_crop_preview()

    def _draw_crop_box_on_canvas(self, box: tuple[int, int, int, int]):
        vx0, vy0, vx1, vy1 = box
        s = self._disp_scale
        ox = self._disp_offset_x
        oy = self._disp_offset_y

        cx0 = ox + vx0 * s
        cy0 = oy + vy0 * s
        cx1 = ox + vx1 * s
        cy1 = oy + vy1 * s

        if self._rect_id:
            self._canvas.delete(self._rect_id)

        self._rect_id = self._canvas.create_rectangle(
            cx0, cy0, cx1, cy1,
            outline="#00e5ff", width=2, dash=(4, 2)
        )

    def _reset_roi(self):
        self._crop_box = None
        if self._rect_id:
            self._canvas.delete(self._rect_id)
            self._rect_id = None
        self._update_crop_preview()

    def _apply_last_roi(self):
        if not self._last_crop_box:
            messagebox.showinfo("提示", "尚無先前的框選範圍。")
            return
        self._crop_box = self._last_crop_box
        self._draw_crop_box_on_canvas(self._crop_box)
        self._update_crop_preview()

    def _get_crop_image(self) -> Image.Image:
        source_img = self._start_frame_img or self._current_frame_img
        if not source_img:
            return Image.new("RGB", (320, 180), color=(30, 30, 30))
        if self._crop_box:
            x1, y1, x2, y2 = self._crop_box
            return source_img.crop((x1, y1, x2, y2))
        return source_img

    def _update_crop_preview(self):
        crop_img = self._get_crop_image()
        # Update preview thumbnail
        thumb = crop_img.copy()
        thumb.thumbnail((80, 45), Image.Resampling.BILINEAR)
        self._crop_thumb_tk = ImageTk.PhotoImage(thumb)
        self._crop_thumb_canvas.delete("all")
        self._crop_thumb_canvas.create_image(40, 22, anchor="center", image=self._crop_thumb_tk)

        if self._crop_box:
            x1, y1, x2, y2 = self._crop_box
            w = x2 - x1
            h = y2 - y1
            self._roi_label.configure(text=f"翻譯範圍：框選區域 [{w}x{h}] ({x1},{y1})")
        else:
            self._roi_label.configure(text="翻譯範圍：全畫面 (預設)")

    # ------------------------------------------------------------------ #
    #  Task Management
    # ------------------------------------------------------------------ #
    def _add_task(self):
        if not self._video_engine:
            messagebox.showwarning("尚未載入影片", "請先選擇並載入影片檔案。")
            return

        if self._end_sec <= self._start_sec:
            messagebox.showwarning("時間區間錯誤", "迄點時間必須大於起點時間。")
            return

        if self._start_frame_img is None:
            if self._current_frame_img:
                self._start_frame_img = self._current_frame_img.copy()
            else:
                self._seek_to(self._start_sec)
                self._start_frame_img = self._current_frame_img.copy() if self._current_frame_img else Image.new("RGB", (640, 360))

        crop_img = self._get_crop_image()

        task = SubtitleTask(
            id=self._next_task_id,
            start_sec=self._start_sec,
            end_sec=self._end_sec,
            start_frame_img=self._start_frame_img.copy(),
            crop_box=self._crop_box,
            crop_img=crop_img.copy(),
            status="待處理"
        )
        self._next_task_id += 1
        self._tasks.append(task)
        self._render_tasks_list()

    def _clear_tasks(self):
        if not self._tasks:
            return
        if messagebox.askyesno("清空任務", "確定要清空所有已建立的任務嗎？"):
            self._tasks.clear()
            self._render_tasks_list()

    def _delete_task(self, task_id: int):
        self._tasks = [t for t in self._tasks if t.id != task_id]
        self._render_tasks_list()

    def _preview_task(self, task: SubtitleTask):
        self._stop_play()
        self._start_sec = task.start_sec
        self._end_sec = task.end_sec
        self._crop_box = task.crop_box
        self._start_frame_img = task.start_frame_img.copy()
        self._update_range_inputs()
        self._seek_to(task.start_sec)
        if task.crop_box:
            self._draw_crop_box_on_canvas(task.crop_box)
        else:
            if self._rect_id:
                self._canvas.delete(self._rect_id)
                self._rect_id = None
        self._update_crop_preview()

    def _render_tasks_list(self):
        for w in self._task_scroll.winfo_children():
            w.destroy()

        self._task_count_label.configure(text=f"待翻譯任務列表 (共 {len(self._tasks)} 組)")
        if hasattr(self, "_task_summary_label"):
            self._task_summary_label.configure(text=f"目前任務：{len(self._tasks)} 組")

        for i, task in enumerate(self._tasks, 1):
            card = ctk.CTkFrame(self._task_scroll, fg_color=("gray88", "gray18"))
            card.pack(fill="x", pady=3)

            # Left: small thumbnail
            thumb_c = tk.Canvas(card, width=70, height=40, bg="#222", highlightthickness=0)
            thumb_c.pack(side="left", padx=6, pady=4)
            thumb = task.crop_img.copy()
            thumb.thumbnail((70, 40), Image.Resampling.BILINEAR)
            thumb_tk = ImageTk.PhotoImage(thumb)
            thumb_c.create_image(35, 20, anchor="center", image=thumb_tk)
            # Retain reference on widget to prevent garbage collection
            thumb_c.image = thumb_tk

            # Middle: Task info
            info_frame = ctk.CTkFrame(card, fg_color="transparent")
            info_frame.pack(side="left", fill="both", expand=True, padx=4, pady=2)

            t_range = f"#{i}  {task.start_ts} → {task.end_ts} ({task.duration:.2f}s)"
            ctk.CTkLabel(info_frame, text=t_range, font=FONT_MONO, anchor="w").pack(fill="x")

            scope_text = f"框選區域" if task.crop_box else "全畫面"
            status_color = {"待處理": "#9aa0a6", "翻譯中": "#2196f3", "已完成": "#4caf50", "失敗": "#f44336"}.get(task.status, "#9aa0a6")
            sub_info = f"範圍: {scope_text} | 狀態: {task.status}"
            ctk.CTkLabel(info_frame, text=sub_info, font=FONT_SMALL,
                         text_color=status_color, anchor="w").pack(fill="x")

            # Right: action buttons
            btn_frame = ctk.CTkFrame(card, fg_color="transparent")
            btn_frame.pack(side="right", padx=6)

            ctk.CTkButton(btn_frame, text="跳至", width=50, height=26, font=FONT_SMALL,
                          command=lambda t=task: self._preview_task(t)).pack(side="left", padx=(0, 4))
            ctk.CTkButton(btn_frame, text="刪除", width=50, height=26, font=FONT_SMALL,
                          fg_color="#b23b3b", hover_color="#8a2e2e",
                          command=lambda tid=task.id: self._delete_task(tid)).pack(side="left")

    # ------------------------------------------------------------------ #
    #  SRT Selection Helpers
    # ------------------------------------------------------------------ #
    def _pick_srt(self):
        initial = self._settings.get("last_srt_path") or self._settings.get("last_video_dir") or os.getcwd()
        p = filedialog.askopenfilename(
            initialdir=initial,
            filetypes=[("SRT 字幕", "*.srt"), ("所有檔案", "*.*")],
            title="選擇目標 SRT 檔案",
        )
        if not p:
            return
        self._srt_path = p
        self._settings["last_srt_path"] = p
        config.patch_setting("last_srt_path", p)
        self._srt_var.set(p)
        try:
            self._srt_entries = srt_utils.parse_srt(p)
        except Exception as exc:
            self._srt_var.set(f"解析失敗：{exc}")
            self._srt_entries = []

    def _clear_srt(self):
        self._srt_path = ""
        self._srt_entries = []
        self._srt_var.set("（未選擇，將直接匯出新 SRT 檔案）")

    # ------------------------------------------------------------------ #
    #  Tab 2: Button Step Settings & Keyboard Shortcuts
    # ------------------------------------------------------------------ #
    def _build_keybindings_tab(self, parent):
        scroll = ctk.CTkScrollableFrame(parent)
        scroll.pack(fill="both", expand=True, padx=14, pady=(10, 10))

        # Title
        ctk.CTkLabel(scroll, text="按鈕步長與鍵盤快捷鍵設定", font=FONT_TITLE, anchor="w").pack(
            fill="x", pady=(4, 4)
        )
        ctk.CTkLabel(scroll, text="自訂時間軸快轉/倒退步長，以及播放器介面所有功能的鍵盤快捷鍵。",
                     font=FONT_SMALL, text_color=("gray40", "#9aa0a6"), anchor="w").pack(
            fill="x", pady=(0, 12)
        )

        # Section 1: Step settings
        sec1_frame = ctk.CTkFrame(scroll)
        sec1_frame.pack(fill="x", pady=(0, 14))

        ctk.CTkLabel(sec1_frame, text="步進秒數與幀數設定", font=FONT_SUBTITLE, anchor="w").pack(
            fill="x", padx=12, pady=(10, 4)
        )
        ctk.CTkLabel(sec1_frame, text="修改後點擊「儲存步長設定」，播放器控制按鈕的文字與步進長度將即時同步更新。",
                     font=FONT_SMALL, text_color=("gray40", "#9aa0a6"), anchor="w").pack(
            fill="x", padx=12, pady=(0, 8)
        )

        grid = ctk.CTkFrame(sec1_frame, fg_color="transparent")
        grid.pack(fill="x", padx=12, pady=(0, 8))
        grid.grid_columnconfigure((0, 2), weight=0)
        grid.grid_columnconfigure((1, 3), weight=1)

        self._s_back_large_var = ctk.StringVar(value=str(self._step_settings.get("step_back_large_sec", 5.0)))
        self._s_back_small_var = ctk.StringVar(value=str(self._step_settings.get("step_back_small_sec", 3.0)))
        self._s_back_frame_var = ctk.StringVar(value=str(self._step_settings.get("step_back_frames", 1)))
        self._s_fwd_frame_var = ctk.StringVar(value=str(self._step_settings.get("step_forward_frames", 1)))
        self._s_fwd_small_var = ctk.StringVar(value=str(self._step_settings.get("step_forward_small_sec", 3.0)))
        self._s_fwd_large_var = ctk.StringVar(value=str(self._step_settings.get("step_forward_large_sec", 5.0)))

        def add_step_field(row, col, label_text, var, unit):
            ctk.CTkLabel(grid, text=label_text, font=FONT_BODY, anchor="e").grid(
                row=row, column=col * 2, sticky="e", padx=(8, 4), pady=6
            )
            sub_f = ctk.CTkFrame(grid, fg_color="transparent")
            sub_f.grid(row=row, column=col * 2 + 1, sticky="w", padx=(0, 16), pady=6)
            e = ctk.CTkEntry(sub_f, textvariable=var, width=80, font=FONT_MONO, justify="center")
            e.pack(side="left")
            ctk.CTkLabel(sub_f, text=unit, font=FONT_SMALL).pack(side="left", padx=(4, 0))

        add_step_field(0, 0, "快退大步長：", self._s_back_large_var, "秒")
        add_step_field(0, 1, "快進大步長：", self._s_fwd_large_var, "秒")
        add_step_field(1, 0, "快退小步長：", self._s_back_small_var, "秒")
        add_step_field(1, 1, "快進小步長：", self._s_fwd_small_var, "秒")
        add_step_field(2, 0, "倒退幀數：", self._s_back_frame_var, "幀")
        add_step_field(2, 1, "前進幀數：", self._s_fwd_frame_var, "幀")

        btn_row = ctk.CTkFrame(sec1_frame, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(4, 10))
        ctk.CTkButton(btn_row, text="儲存步長設定", width=120, height=30, font=FONT_BODY,
                      command=self._save_step_settings).pack(side="left")
        ctk.CTkButton(btn_row, text="恢復預設步長", width=110, height=30, font=FONT_BODY,
                      fg_color="#555", hover_color="#444",
                      command=self._reset_step_settings).pack(side="left", padx=(8, 0))
        self._step_status_label = ctk.CTkLabel(btn_row, text="", font=FONT_SMALL,
                                               text_color=("#0a7d2c", "#5ec97a"), anchor="w")
        self._step_status_label.pack(side="left", padx=(12, 0))

        # Section 2: Keybindings mapping
        sec2_frame = ctk.CTkFrame(scroll)
        sec2_frame.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(sec2_frame, text="播放器按鈕鍵盤快捷鍵對應", font=FONT_SUBTITLE, anchor="w").pack(
            fill="x", padx=12, pady=(10, 4)
        )
        ctk.CTkLabel(sec2_frame, text="點擊「設定按鍵」後直接在鍵盤按下目標鍵即可完成綁定。\n焦點在文字輸入框時，快捷鍵將自動暫停以避免打字衝突。",
                     font=FONT_SMALL, text_color=("gray40", "#9aa0a6"), anchor="w", justify="left").pack(
            fill="x", padx=12, pady=(0, 8)
        )

        top_kb_row = ctk.CTkFrame(sec2_frame, fg_color="transparent")
        top_kb_row.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkButton(top_kb_row, text="全部恢復預設快捷鍵", width=160, height=28, font=FONT_SMALL,
                      fg_color="#555", hover_color="#444",
                      command=self._reset_keybindings).pack(side="left")
        self._kb_status_label = ctk.CTkLabel(top_kb_row, text="", font=FONT_SMALL,
                                             text_color=("#0a7d2c", "#5ec97a"), anchor="w")
        self._kb_status_label.pack(side="left", padx=(12, 0))

        self._kb_table_frame = ctk.CTkFrame(sec2_frame, fg_color="transparent")
        self._kb_table_frame.pack(fill="x", padx=12, pady=(0, 12))

        self._render_keybindings_table()

    def _render_keybindings_table(self):
        for w in self._kb_table_frame.winfo_children():
            w.destroy()

        # Header
        hdr = ctk.CTkFrame(self._kb_table_frame, fg_color=("gray82", "gray22"))
        hdr.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(hdr, text="功能說明", font=FONT_SMALL, width=220, anchor="w").pack(side="left", padx=8, pady=4)
        ctk.CTkLabel(hdr, text="目前快捷鍵", font=FONT_SMALL, width=180, anchor="w").pack(side="left", padx=8)
        ctk.CTkLabel(hdr, text="操作", font=FONT_SMALL, anchor="w").pack(side="left", padx=8)

        for act_id, act_name in KEYBINDING_ACTIONS:
            row = ctk.CTkFrame(self._kb_table_frame, fg_color=("gray88", "gray18"))
            row.pack(fill="x", pady=2)

            ctk.CTkLabel(row, text=act_name, font=FONT_BODY, width=220, anchor="w").pack(side="left", padx=8, pady=4)

            cur_key = self._keybindings.get(act_id, "")
            is_recording = (self._recording_action == act_id)
            if is_recording:
                disp_text = "⚡ 請按下任意鍵... (Esc取消)"
                badge_fg = ("#ffcdd2", "#b71c1c")
                text_color = ("#b71c1c", "#ffcdd2")
            else:
                disp_text = format_key_display(cur_key)
                badge_fg = ("gray80", "gray25")
                text_color = ("black", "white") if cur_key else ("gray50", "gray60")

            key_badge = ctk.CTkLabel(row, text=disp_text, font=FONT_MONO, width=180,
                                     anchor="w", fg_color=badge_fg, corner_radius=4,
                                     text_color=text_color)
            key_badge.pack(side="left", padx=8, pady=4)

            btn_f = ctk.CTkFrame(row, fg_color="transparent")
            btn_f.pack(side="left", padx=4)

            set_btn = ctk.CTkButton(
                btn_f, text="取消" if is_recording else "設定按鍵", width=80, height=26, font=FONT_SMALL,
                fg_color="#b23b3b" if is_recording else "#1f6aa5",
                hover_color="#8a2e2e" if is_recording else "#144870",
                command=lambda a=act_id: self._on_click_record_key(a)
            )
            set_btn.pack(side="left", padx=(0, 4))

            clr_btn = ctk.CTkButton(
                btn_f, text="清除", width=50, height=26, font=FONT_SMALL,
                fg_color="#666", hover_color="#555",
                command=lambda a=act_id: self._clear_keybinding(a)
            )
            clr_btn.pack(side="left")

    def _on_click_record_key(self, action_id: str):
        if self._recording_action == action_id:
            self._recording_action = None
        else:
            self._recording_action = action_id
        self._render_keybindings_table()

    def _clear_keybinding(self, action_id: str):
        self._keybindings[action_id] = ""
        config.patch_setting("keybindings", self._keybindings)
        if self._recording_action == action_id:
            self._recording_action = None
        self._render_keybindings_table()
        if hasattr(self, "_kb_status_label"):
            self._kb_status_label.configure(text="已清除快捷鍵")

    def _reset_keybindings(self):
        self._keybindings = dict(config.DEFAULT_KEYBINDINGS)
        config.patch_setting("keybindings", self._keybindings)
        self._recording_action = None
        self._render_keybindings_table()
        if hasattr(self, "_kb_status_label"):
            self._kb_status_label.configure(text="已恢復預設快捷鍵")

    def _save_step_settings(self):
        try:
            b_l = max(0.1, float(self._s_back_large_var.get().strip()))
            b_s = max(0.1, float(self._s_back_small_var.get().strip()))
            b_f = max(1, int(self._s_back_frame_var.get().strip()))
            f_f = max(1, int(self._s_fwd_frame_var.get().strip()))
            f_s = max(0.1, float(self._s_fwd_small_var.get().strip()))
            f_l = max(0.1, float(self._s_fwd_large_var.get().strip()))
        except ValueError:
            messagebox.showwarning("格式錯誤", "請輸入有效的正數秒數與整數幀數。")
            return

        self._step_settings = {
            "step_back_large_sec": b_l,
            "step_back_small_sec": b_s,
            "step_back_frames": b_f,
            "step_forward_frames": f_f,
            "step_forward_small_sec": f_s,
            "step_forward_large_sec": f_l,
        }
        config.patch_setting("step_settings", self._step_settings)
        self._refresh_step_buttons()
        if hasattr(self, "_step_status_label"):
            self._step_status_label.configure(text="✅ 步長設定已儲存並生效")

    def _reset_step_settings(self):
        self._step_settings = dict(config.DEFAULT_STEP_SETTINGS)
        self._s_back_large_var.set(str(self._step_settings["step_back_large_sec"]))
        self._s_back_small_var.set(str(self._step_settings["step_back_small_sec"]))
        self._s_back_frame_var.set(str(self._step_settings["step_back_frames"]))
        self._s_fwd_frame_var.set(str(self._step_settings["step_forward_frames"]))
        self._s_fwd_small_var.set(str(self._step_settings["step_forward_small_sec"]))
        self._s_fwd_large_var.set(str(self._step_settings["step_forward_large_sec"]))
        config.patch_setting("step_settings", self._step_settings)
        self._refresh_step_buttons()
        if hasattr(self, "_step_status_label"):
            self._step_status_label.configure(text="已恢復預設步長")

    def _on_global_key_press(self, event):
        # 1. Key recording mode for settings page
        if self._recording_action:
            if event.keysym in ("Escape", "Esc"):
                self._recording_action = None
                self._render_keybindings_table()
                return "break"
            if event.keysym in ("Shift_L", "Shift_R", "Control_L", "Control_R",
                                "Alt_L", "Alt_R", "Caps_Lock", "Super_L", "Super_R"):
                return None
            key = event.keysym
            self._keybindings[self._recording_action] = key
            config.patch_setting("keybindings", self._keybindings)
            self._recording_action = None
            self._render_keybindings_table()
            if hasattr(self, "_kb_status_label"):
                self._kb_status_label.configure(text=f"已將快捷鍵設定為：{format_key_display(key)}")
            return "break"

        # 2. Check if focused on entry or text widget
        try:
            focused = self.focus_get()
            if focused is not None:
                cls_name = focused.winfo_class().lower()
                widget_str = str(focused).lower()
                if "entry" in cls_name or "text" in cls_name or "entry" in widget_str or "text" in widget_str:
                    return None
        except Exception:
            pass

        # 3. Match keybinding action
        pressed = event.keysym.lower()
        matched_action = None
        for act_id, bound_key in self._keybindings.items():
            if bound_key and bound_key.lower() == pressed:
                matched_action = act_id
                break

        if not matched_action:
            return None

        # 4. Dispatch action
        if matched_action == "play_pause":
            self._toggle_play()
        elif matched_action == "step_back_large":
            self._on_step_back_large()
        elif matched_action == "step_back_small":
            self._on_step_back_small()
        elif matched_action == "step_back_frame":
            self._on_step_back_frame()
        elif matched_action == "step_forward_frame":
            self._on_step_forward_frame()
        elif matched_action == "step_forward_small":
            self._on_step_forward_small()
        elif matched_action == "step_forward_large":
            self._on_step_forward_large()
        elif matched_action == "set_start":
            self._set_as_start()
        elif matched_action == "set_end":
            self._set_as_end()
        elif matched_action == "jump_start":
            self._jump_to_start()
        elif matched_action == "jump_end":
            self._jump_to_end()
        elif matched_action == "reset_roi":
            self._reset_roi()
        elif matched_action == "apply_last_roi":
            self._apply_last_roi()
        elif matched_action == "add_task":
            self._add_task()

        return "break"

    # ------------------------------------------------------------------ #
    #  Tab 3: API Settings (Maintained from v0.03)
    # ------------------------------------------------------------------ #
    def _build_api_tab(self, parent):
        ctk.CTkLabel(parent, text="API 設定", font=FONT_TITLE, anchor="w").pack(
            fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(parent, text="可設定多組供應商，於下方下拉選單快速切換。",
                     font=FONT_SMALL, text_color=("gray40", "#9aa0a6"), anchor="w").pack(
            fill="x", padx=14)

        switch_row = ctk.CTkFrame(parent, fg_color="transparent")
        switch_row.pack(fill="x", padx=14, pady=(8, 6))
        ctk.CTkLabel(switch_row, text="供應商：", font=FONT_BODY).pack(side="left")
        self._provider_menu = ctk.CTkOptionMenu(switch_row, width=200, height=30,
                                                font=FONT_BODY,
                                                values=["（無）"],
                                                command=self._on_provider_switch)
        self._provider_menu.pack(side="left", padx=(6, 12))
        ctk.CTkButton(switch_row, text="測試連線", width=110, height=30,
                      font=FONT_BODY, command=self._on_test_connection).pack(side="left")

        form = ctk.CTkFrame(parent)
        form.pack(fill="both", expand=True, padx=14, pady=(0, 6))
        form.grid_columnconfigure(1, weight=1)

        self._p_name = ctk.StringVar()
        self._p_url = ctk.StringVar()
        self._p_key = ctk.StringVar()
        self._p_model = ctk.StringVar()

        def add_field(row, label, var, show=None):
            ctk.CTkLabel(form, text=label, font=FONT_BODY, anchor="e").grid(
                row=row, column=0, sticky="e", padx=(10, 8), pady=8
            )
            entry = ctk.CTkEntry(form, textvariable=var, font=FONT_BODY, show=show)
            entry.grid(row=row, column=1, sticky="ew", padx=(0, 10), pady=8)
            return entry

        add_field(0, "名稱：", self._p_name)
        add_field(1, "API 網址：", self._p_url)
        add_field(2, "API 金鑰：", self._p_key, show="•")
        add_field(3, "模型名稱：", self._p_model)

        self._key_hint = ctk.StringVar(value="")
        ctk.CTkLabel(
            form, textvariable=self._key_hint, font=FONT_SMALL,
            text_color=("gray40", "#9aa0a6"), anchor="w"
        ).grid(row=4, column=0, columnspan=2, sticky="w", padx=(10, 10), pady=(0, 4))

        btns = ctk.CTkFrame(parent, fg_color="transparent")
        btns.pack(fill="x", padx=14, pady=(0, 6))
        ctk.CTkButton(btns, text="新增", width=90, height=30, font=FONT_BODY,
                      command=self._new_provider).pack(side="left")
        ctk.CTkButton(btns, text="儲存目前", width=110, height=30, font=FONT_BODY,
                      command=self._save_current_provider).pack(side="left", padx=(8, 0))
        ctk.CTkButton(btns, text="刪除", width=90, height=30, font=FONT_BODY,
                      fg_color="#b23b3b", hover_color="#8a2e2e",
                      command=self._delete_current_provider).pack(side="left", padx=(8, 0))
        ctk.CTkButton(btns, text="設為預設", width=110, height=30, font=FONT_BODY,
                      command=self._set_default_provider).pack(side="left", padx=(8, 0))

        self._test_status = ctk.StringVar(value="")
        self._test_label = ctk.CTkLabel(parent, textvariable=self._test_status,
                                        font=FONT_SMALL, anchor="w")
        self._test_label.pack(fill="x", padx=14, pady=(0, 10))

    def _provider_names(self) -> list[str]:
        return [p["name"] for p in self._settings.get("providers", [])]

    def _refresh_provider_menu(self, keep: str | None = None):
        names = self._provider_names() or ["（無）"]
        self._provider_menu.configure(values=names)
        sel = keep if keep in names else (names[0] if names != ["（無）"] else "（無）")
        self._provider_menu.set(sel)

    def _sync_api_form(self):
        self._refresh_provider_menu()
        p = config.get_default_provider()
        if p:
            self._provider_menu.set(p["name"])
            self._load_provider_into_form(p)

    def _load_provider_into_form(self, p: dict):
        self._p_name.set(p.get("name", ""))
        self._p_url.set(p.get("api_url", ""))
        self._p_key.set(p.get("api_key", ""))
        self._p_model.set(p.get("model", ""))
        env = p.get("api_key_env") or ""
        self._key_hint.set(f"金鑰儲存於 .env：{env}" if env else "金鑰儲存於 .env")

    def _on_provider_switch(self, name: str):
        for p in self._settings.get("providers", []):
            if p.get("name") == name:
                self._load_provider_into_form(p)
                break

    def _form_to_provider(self) -> dict:
        return {
            "name": self._p_name.get().strip(),
            "api_url": self._p_url.get().strip(),
            "api_key": self._p_key.get().strip(),
            "model": self._p_model.get().strip(),
            "is_default": False,
        }

    def _new_provider(self):
        self._p_name.set("")
        self._p_url.set("https://api.openai.com/v1/chat/completions")
        self._p_key.set("")
        self._p_model.set("gpt-4o")
        self._key_hint.set("填入名稱與金鑰後儲存，金鑰將寫入 .env（API_KEY_<名稱>）")

    def _save_current_provider(self):
        p = self._form_to_provider()
        if not p["name"]:
            messagebox.showwarning("缺少名稱", "請先填入供應商「名稱」。")
            return
        if not p["api_url"] or not p["model"]:
            messagebox.showwarning("資料不完整", "API 網址與模型名稱為必填。")
            return
        for ex in self._settings.get("providers", []):
            if ex.get("name") == p["name"]:
                p["is_default"] = ex.get("is_default", False)
                break
        config.upsert_provider(p)
        self._settings = config.load_settings()
        self._refresh_provider_menu(keep=p["name"])

    def _delete_current_provider(self):
        name = self._p_name.get().strip()
        if not name:
            return
        if not messagebox.askyesno("刪除供應商", f"確定刪除「{name}」？"):
            return
        config.delete_provider(name)
        self._settings = config.load_settings()
        self._sync_api_form()

    def _set_default_provider(self):
        name = self._p_name.get().strip()
        if not name:
            return
        p = self._form_to_provider()
        p["is_default"] = True
        config.upsert_provider(p)
        self._settings = config.load_settings()
        self._refresh_provider_menu(keep=name)

    def _on_test_connection(self):
        p = self._form_to_provider()
        if not p["api_url"] or not p["model"]:
            messagebox.showwarning("資料不完整", "請先填入 API 網址與模型名稱。")
            return
        cfg = ProviderCfg.from_dict(p)
        self._test_status.set("測試中…")
        self._test_label.configure(text_color=("#555", "#9aa0a6"))

        def work():
            ok, msg = test_connection(cfg)
            self.after(0, lambda: self._show_test_result(ok, msg))

        threading.Thread(target=work, daemon=True).start()

    def _show_test_result(self, ok: bool, msg: str):
        self._test_status.set(("✅ " if ok else "❌ ") + msg)
        self._test_label.configure(text_color=("#0a7d2c", "#5ec97a") if ok else ("#b23b3b", "#ff8a80"))

    # ------------------------------------------------------------------ #
    #  Tab 3: Prompt Settings (Maintained from v0.03)
    # ------------------------------------------------------------------ #
    def _build_prompt_tab(self, parent):
        ctk.CTkLabel(parent, text="Prompt 與翻譯設定", font=FONT_TITLE, anchor="w").pack(
            fill="x", padx=14, pady=(12, 4))

        lang_row = ctk.CTkFrame(parent, fg_color="transparent")
        lang_row.pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkLabel(lang_row, text="目標語言：", font=FONT_BODY).pack(side="left")
        self._lang_var = ctk.StringVar(value=self._settings.get("target_lang", config.DEFAULT_TARGET_LANG))
        self._lang_menu = ctk.CTkOptionMenu(lang_row, width=180, height=30,
                                            font=FONT_BODY, values=config.TARGET_LANG_PRESETS,
                                            variable=self._lang_var,
                                            command=self._on_lang_change)
        self._lang_menu.pack(side="left", padx=(6, 12))
        ctk.CTkLabel(lang_row, text="或自訂：", font=FONT_BODY).pack(side="left")
        self._lang_custom = ctk.StringVar(value=self._settings.get("target_lang_custom", ""))
        entry = ctk.CTkEntry(lang_row, textvariable=self._lang_custom, width=180,
                             font=FONT_BODY)
        entry.pack(side="left", padx=(6, 0))
        entry.bind("<FocusOut>", lambda _e: config.patch_setting("target_lang_custom", self._lang_custom.get()))

        ctk.CTkLabel(parent, text="System Prompt", font=FONT_BODY, anchor="w").pack(
            fill="x", padx=14, pady=(6, 2))
        self._sys_box = ctk.CTkTextbox(parent, height=110, font=FONT_BODY)
        self._sys_box.pack(fill="x", expand=False, padx=14)
        self._sys_box.insert("1.0", self._settings.get("system_prompt", config.DEFAULT_SYSTEM_PROMPT))

        ur = ctk.CTkFrame(parent, fg_color="transparent")
        ur.pack(fill="x", padx=14, pady=(8, 2))
        ctk.CTkLabel(ur, text="User Prompt（{lang} 會自動帶入目標語言）", font=FONT_BODY,
                     anchor="w").pack(side="left")
        ctk.CTkButton(ur, text="恢復預設", width=90, height=26, font=FONT_SMALL,
                      command=self._reset_prompts).pack(side="right")
        self._usr_box = ctk.CTkTextbox(parent, height=110, font=FONT_BODY)
        self._usr_box.pack(fill="x", expand=False, padx=14)
        self._usr_box.insert("1.0", self._settings.get("user_prompt", config.DEFAULT_USER_PROMPT))

        mode_row = ctk.CTkFrame(parent, fg_color="transparent")
        mode_row.pack(fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(mode_row, text="插入方式（當有指定目標 SRT 時）：", font=FONT_BODY).pack(side="left")
        self._mode_var = ctk.StringVar(value=self._settings.get("insertion_mode", "insert"))
        self._mode_seg = ctk.CTkSegmentedButton(
            mode_row, values=["insert", "append"],
            command=lambda v: self._mode_var.set(v),
            variable=self._mode_var, height=30, font=FONT_BODY)
        self._mode_seg.pack(side="left", padx=(6, 12))
        ctk.CTkLabel(mode_row,
                     text="insert=依時間軸插入新字幕　append=附加至涵蓋時間軸的現有字幕",
                     font=FONT_SMALL, text_color=("gray40", "#9aa0a6")).pack(side="left")

        ctk.CTkButton(parent, text="儲存 Prompt 設定", width=140, height=30, font=FONT_BODY,
                      command=self._save_prompts).pack(padx=14, pady=(8, 10), anchor="w")

    def _current_lang(self) -> str:
        custom = self._lang_custom.get().strip()
        return custom or self._lang_var.get()

    def _on_lang_change(self, _v):
        config.patch_setting("target_lang", self._lang_var.get())

    def _reset_prompts(self):
        self._sys_box.delete("1.0", "end")
        self._sys_box.insert("1.0", config.DEFAULT_SYSTEM_PROMPT)
        self._usr_box.delete("1.0", "end")
        self._usr_box.insert("1.0", config.DEFAULT_USER_PROMPT)

    def _save_prompts(self):
        config.patch_setting("system_prompt", self._sys_box.get("1.0", "end").strip("\n"))
        config.patch_setting("user_prompt", self._usr_box.get("1.0", "end").strip("\n"))
        config.patch_setting("target_lang", self._lang_var.get())
        config.patch_setting("target_lang_custom", self._lang_custom.get().strip())
        config.patch_setting("insertion_mode", self._mode_var.get())
        self._settings = config.load_settings()

    # ------------------------------------------------------------------ #
    #  Tab 4: Execution & Batch LLM Translation
    # ------------------------------------------------------------------ #
    def _build_run_tab(self, parent):
        ctk.CTkLabel(parent, text="執行 OCR 翻譯與 SRT 匯出", font=FONT_TITLE, anchor="w").pack(
            fill="x", padx=14, pady=(12, 4))

        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(0, 8))
        self._run_btn = ctk.CTkButton(row, text="開始 OCR 翻譯", width=160, height=32,
                                      font=FONT_BODY, command=self._start_ocr)
        self._run_btn.pack(side="left")
        self._insert_btn = ctk.CTkButton(row, text="儲存為 SRT 檔案", width=180, height=32,
                                         font=FONT_BODY, state="disabled",
                                         command=self._insert_and_save)
        self._insert_btn.pack(side="left", padx=(8, 0))

        self._task_summary_label = ctk.CTkLabel(row, text="目前任務：0 組", font=FONT_BODY)
        self._task_summary_label.pack(side="left", padx=(14, 0))

        self._progress = ctk.CTkProgressBar(parent, width=400)
        self._progress.set(0)
        self._progress.pack(fill="x", padx=14, pady=(0, 8))

        self._log = ctk.CTkTextbox(parent, font=FONT_MONO, height=180)
        self._log.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        ctk.CTkLabel(parent, text="翻譯結果預覽", font=FONT_BODY, anchor="w").pack(
            fill="x", padx=14)
        self._preview_scroll = ctk.CTkScrollableFrame(parent, height=160)
        self._preview_scroll.pack(fill="both", expand=False, padx=14, pady=(0, 10))

    def _log_line(self, msg: str):
        self.after(0, lambda: self._log.insert("end", msg + "\n"))
        self.after(0, lambda: self._log.see("end"))

    def _set_busy(self, busy: bool):
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.after(0, lambda: self._run_btn.configure(state=state))

    def _start_ocr(self):
        if self._busy:
            return
        if not self._tasks:
            messagebox.showwarning("無任務", "請先在「影片與字幕任務」中設置至少一組畫面轉字幕任務。")
            return

        provider = config.get_default_provider()
        if not provider or not provider.get("api_key"):
            provider = self._form_to_provider()
        if not provider or not provider.get("api_url") or not provider.get("model"):
            messagebox.showwarning("API 未設定", "請先在「API 設定」填入網址、模型與金鑰。")
            return

        self._save_prompts()
        cfg = ProviderCfg.from_dict(provider)
        sys_prompt = self._sys_box.get("1.0", "end").strip("\n") or config.DEFAULT_SYSTEM_PROMPT
        usr_prompt = self._usr_box.get("1.0", "end").strip("\n") or config.DEFAULT_USER_PROMPT
        lang = self._current_lang()
        max_side = int(self._settings.get("image_max_side", 1568))

        self._log.delete("1.0", "end")
        for w in self._preview_scroll.winfo_children():
            w.destroy()
        self._progress.set(0)
        self._insert_btn.configure(state="disabled")
        self._set_busy(True)

        def work():
            total = len(self._tasks)
            self._log_line(f"開始批次 OCR 翻譯，共 {total} 組任務...")
            for i, task in enumerate(self._tasks, 1):
                task.status = "翻譯中"
                self.after(0, self._render_tasks_list)
                scope = f"框選區域" if task.crop_box else "全畫面"
                self._log_line(f"[{i}/{total}] {task.start_ts} → {task.end_ts} ({scope})")

                try:
                    text = translate_image(cfg, task.crop_img, sys_prompt, usr_prompt, lang, max_side)
                    task.translation = text
                    task.status = "已完成"
                except LLMError as exc:
                    task.status = "失敗"
                    task.error_msg = str(exc)
                    self._log_line(f"    ❌ 失敗：{exc}")
                    self.after(0, self._render_tasks_list)
                    continue
                except Exception as exc:  # noqa: BLE001
                    task.status = "失敗"
                    task.error_msg = str(exc)
                    self._log_line(f"    ❌ 例外：{exc}")
                    self.after(0, self._render_tasks_list)
                    continue

                preview = text.replace("\n", " ⏎ ") if text else "（無文字）"
                self._log_line(f"    ✅ {preview}")
                self.after(0, self._render_tasks_list)
                self.after(0, lambda t=task, txt=text: self._add_preview(f"{t.start_ts} → {t.end_ts}", txt))
                self._progress.set(i / total)

            self._log_line("全部任務 OCR 翻譯完成。")
            self.after(0, lambda: self._insert_btn.configure(state="normal"))
            self._set_busy(False)

        threading.Thread(target=work, daemon=True).start()

    def _add_preview(self, ts_range: str, text: str):
        frame = ctk.CTkFrame(self._preview_scroll, fg_color=("gray86", "gray17"))
        frame.pack(fill="x", pady=2)
        ctk.CTkLabel(frame, text=ts_range, font=FONT_MONO, width=220, anchor="w").pack(side="left", padx=8, pady=4)
        ctk.CTkLabel(frame, text=text.replace("\n", " / "), font=FONT_BODY,
                     anchor="w", wraplength=640).pack(side="left", fill="x", expand=True)

    def _insert_and_save(self):
        valid_tasks = [t for t in self._tasks if t.translation.strip()]
        if not valid_tasks:
            messagebox.showinfo("無翻譯內容", "尚無已完成翻譯的任務。")
            return

        mode = self._mode_var.get()
        # Build translations tuples: (start_sec, text, duration)
        trans = [(t.start_sec, t.translation, t.duration) for t in valid_tasks]

        if self._srt_entries:
            # Splicing into existing SRT entries
            entries = srt_utils.insert_translations(
                list(self._srt_entries), trans,
                mode=mode, duration_mode="per_subtitle"
            )
        else:
            # Generate brand new SRT directly from video tasks
            entries = srt_utils.insert_translations(
                [], trans,
                mode="insert", duration_mode="per_subtitle"
            )

        # Output filename suggestion
        if self._srt_path:
            orig = Path(self._srt_path)
            default_name = orig.stem + "_translated.srt"
            initialdir = str(orig.parent)
        elif self._video_path:
            orig = Path(self._video_path)
            default_name = orig.stem + "_translated.srt"
            initialdir = str(orig.parent)
        else:
            default_name = "subtitles_translated.srt"
            initialdir = os.getcwd()

        out = filedialog.asksaveasfilename(
            defaultextension=".srt",
            initialdir=initialdir,
            initialfile=default_name,
            filetypes=[("SRT 字幕", "*.srt")],
            title="另存 SRT 新檔",
        )
        if not out:
            return

        try:
            srt_utils.write_srt(out, entries)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("寫檔失敗", str(exc))
            return

        messagebox.showinfo(
            "匯出完成",
            f"已成功儲存 SRT 字幕：\n{out}\n共包含 {len(entries)} 條字幕。"
        )
        self._log_line(f"已另存：{out}（共 {len(entries)} 條字幕）")

    # ------------------------------------------------------------------ #
    #  Window Cleanup
    # ------------------------------------------------------------------ #
    def _on_window_close(self):
        self.destroy()

    def destroy(self):
        self._stop_play()
        if self._video_engine:
            try:
                self._video_engine.close()
            except Exception:
                pass
            self._video_engine = None
        super().destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
