"""Video OCR → translate → SRT generator/inserter (v1.11).

Run: python app.py
Or: PicTrans2Srt.exe [video_path] [-s srt_path] [--play]
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import threading
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

import customtkinter as ctk
from PIL import Image, ImageTk

import capture_manager
import config
import image_gen_engine
from image_gen_engine import P2PError, P2PProviderCfg, test_p2p_connection, generate_image
import monitor_utils
import srt_utils
from llm_engine import LLMError, ProviderCfg, test_connection, translate_image
from video_engine import VideoEngine, VideoEngineError
import vlc_engine
from vlc_engine import VLCPlayer, VLCPlayerError, is_vlc_available


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
    "f11": "F11 (全螢幕)",
    "f5": "F5 (熱重載)",
    "c": "C (快速截圖)",
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
    ("toggle_fullscreen", "切換全螢幕 (全螢幕/退出)"),
    ("hot_reload_sub", "重新載入字幕 (Hot Reload)"),
    ("quick_capture", "📷 擷取畫面至素材庫"),
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
    raw_translation: str = ""                      # v2.06: original LLM output before manual edit
    error_msg: str = ""
    image_path: Optional[str] = None               # v2.01: local saved path in \captured

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
    def __init__(
        self,
        initial_video: Optional[str] = None,
        initial_srt: Optional[str] = None,
        autoplay: bool = False,
    ):
        super().__init__()
        self.title("影片畫面 OCR 翻譯 & AI 圖生圖工作站 (v2.06)")
        self.minsize(980, 680)

        self._settings = config.load_settings()
        self._geo_save_timer: Optional[str] = None
        self._restore_window_geometry()
        self.bind("<Configure>", self._on_window_configure)

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

        # Fullscreen and window scale
        self._is_fullscreen: bool = False
        self._saved_geometry: str = "1160x820"
        self._fullscreen_win: Optional[tk.Toplevel] = None
        self._fullscreen_frame: Optional[tk.Frame] = None
        self._fullscreen_exit_btn: Optional[ctk.CTkButton] = None
        self._fs_bar: Optional[ctk.CTkFrame] = None
        self._fs_time_slider: Optional[ctk.CTkSlider] = None
        self._fs_time_label: Optional[ctk.CTkLabel] = None
        self._fs_play_btn: Optional[ctk.CTkButton] = None
        self._fs_bar_visible: bool = False
        self._fs_exit_timer: Optional[str] = None
        self._fs_bar_timer: Optional[str] = None
        self._fs_poll_after_id: Optional[str] = None
        self._window_scale_var = ctk.StringVar(value="1.0x (原始)")
        self._active_tab_name: str = "  影音播放器  "

        # Subtitle inline editor state
        self._inline_entry: Optional[tk.Entry] = None
        self._inline_edit_item: Optional[str] = None
        self._inline_edit_idx: Optional[int] = None

        # Volume initialization from persistent settings
        saved_vol = self._settings.get("last_volume", 80)
        try:
            saved_vol = max(0, min(100, int(saved_vol)))
        except Exception:
            saved_vol = 80

        # VLC Player (Hardware accelerated audio/video playback engine)
        self._vlc_player: Optional[VLCPlayer] = None
        if is_vlc_available():
            try:
                self._vlc_player = VLCPlayer(initial_volume=saved_vol)
            except Exception:
                self._vlc_player = None

        self._sub_render_var = ctk.BooleanVar(value=bool(self._settings.get("sub_render_enabled", True)))
        if self._vlc_player:
            self._vlc_player.set_subtitle_visible(self._sub_render_var.get())

        self._volume_var = ctk.DoubleVar(value=float(saved_vol))
        self._playback_rate_var = ctk.StringVar(value="1.0x")
        self._is_muted: bool = (saved_vol == 0)
        self._active_display_mode: str = "canvas"  # "vlc" | "canvas"
        self._vol_save_timer: Optional[str] = None

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

        # v2.01 Pic-to-Pic (P2P) Runtime State
        self._p2p_settings = config.load_p2p_settings()
        self._captured_images: list[dict] = []
        self._selected_captured_paths: set[str] = set()
        self._active_edit_path: Optional[str] = None
        self._active_edit_img: Optional[Image.Image] = None
        self._original_active_img: Optional[Image.Image] = None
        self._edit_rotation: int = 0
        self._edit_flip_h: bool = False
        self._p2p_generated_images: list[Image.Image] = []
        self._current_gen_img: Optional[Image.Image] = None
        self._p2p_busy: bool = False
        self._p2p_gallery_checkboxes: dict = {}
        self._p2p_current_models: list[str] = []

        # v2.05 Folder expansion & privacy preview & output gallery state
        storage_cfg = self._settings.get("storage_folders", {})
        self._current_ocr_dir: Path = config.resolve_ocr_dir(self._settings)
        self._current_input_dir: str = str(config.resolve_captured_dir(self._settings))
        self._current_output_dir: str = str(config.resolve_generated_dir(self._settings))
        self._recent_input_dirs: list[str] = list(storage_cfg.get("recent_input_dirs", []))
        self._recent_output_dirs: list[str] = list(storage_cfg.get("recent_output_dirs", []))
        self._output_images: list[dict] = []
        self._selected_output_paths: set[str] = set()
        self._p2p_output_checkboxes: dict = {}
        self._p2p_preview_visible_var = ctk.BooleanVar(value=True)
        self._p2p_custom_w_var = ctk.StringVar(value="1024")
        self._p2p_custom_h_var = ctk.StringVar(value="768")

        # v2.06: .drop History State for Subtitle OCR and P2P
        self._ocr_history_mode: bool = False
        self._selected_ocr_drop_paths: set[str] = set()
        self._ocr_drop_checkboxes: dict = {}
        self._p2p_history_mode: bool = False
        self._selected_p2p_drop_paths: set[str] = set()
        self._p2p_drop_checkboxes: dict = {}

        # Tabs (v2.01 Refined Hierarchical Structure)
        self.tabs = ctk.CTkTabview(self, anchor="nw", command=self._on_tab_change)
        self.tabs.pack(fill="both", expand=True, padx=10, pady=(8, 10))
        self.tabs.add("  影音播放器  ")
        self.tabs.add("  影片截圖視窗  ")
        self.tabs.add("  截圖翻譯字幕任務  ")
        self.tabs.add("  圖生圖任務  ")
        self.tabs.add("  按鈕與快捷鍵設定  ")

        # Sub-tabview for OCR (Execution, API & Prompt settings):
        self.ocr_tabs = ctk.CTkTabview(self.tabs.tab("  截圖翻譯字幕任務  "), anchor="nw", command=self._on_ocr_subtab_change)
        self.ocr_tabs.pack(fill="both", expand=True, padx=4, pady=4)
        self.ocr_tabs.add("  翻譯字幕執行  ")
        self.ocr_tabs.add("  翻譯字幕 API 設定  ")
        self.ocr_tabs.add("  翻譯字幕 Prompt 設定  ")

        # Sub-tabview for P2P:
        self.p2p_tabs = ctk.CTkTabview(self.tabs.tab("  圖生圖任務  "), anchor="nw", command=self._on_p2p_subtab_change)
        self.p2p_tabs.pack(fill="both", expand=True, padx=4, pady=4)
        self.p2p_tabs.add("  編輯及預覽視窗  ")
        self.p2p_tabs.add("  圖生圖 API 設定  ")
        self.p2p_tabs.add("  圖生圖 Prompt 設定  ")

        # Setup tab redirection wrappers for backwards compatibility
        self._setup_tab_compatibility()

        # Lazy Tab Construction (v1.11 & v2.01 startup delay optimization)
        self._built_tabs: set[str] = set()
        self._built_ocr_subtabs: set[str] = set()
        self._built_p2p_subtabs: set[str] = set()
        self._build_player_tab(self.tabs.tab("  影音播放器  "))
        self._built_tabs.add("  影音播放器  ")
        self._refresh_step_buttons()

        self.bind_all("<KeyPress>", self._on_global_key_press)
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)

        if initial_video:
            self.after(200, lambda: self._startup_load(initial_video, initial_srt, autoplay))

    def _startup_load(
        self,
        video_path: str,
        srt_path: Optional[str] = None,
        autoplay: bool = False,
    ):
        if not video_path:
            return
        p = Path(video_path)
        if not p.is_file():
            self._log_line(f"啟動參數指定的影片不存在：{video_path}")
            return

        try:
            self.lift()
            self.focus_force()
        except Exception:
            pass

        self._load_video(str(p.resolve()))

        if srt_path:
            s = Path(srt_path)
            if s.is_file():
                self._load_srt(str(s.resolve()))
            else:
                self._log_line(f"啟動參數指定的字幕不存在：{srt_path}")

        if autoplay:
            self.after(150, self._start_play)

    # ------------------------------------------------------------------ #
    #  Tab 1: Pure Media Player & Live Subtitle Editor (v1.04)
    # ------------------------------------------------------------------ #
    def _build_player_tab(self, parent):
        # Top bar: Video & Subtitle Controls
        top_bar = ctk.CTkFrame(parent, fg_color=("gray90", "gray17"))
        top_bar.pack(fill="x", padx=10, pady=(6, 6))

        # Row 1: Video file picker
        v_row = ctk.CTkFrame(top_bar, fg_color="transparent")
        v_row.pack(fill="x", padx=8, pady=(6, 4))
        ctk.CTkButton(v_row, text="選擇影片", width=110, height=30,
                      font=FONT_BODY, command=self._pick_video).pack(side="left")
        self._player_video_label = ctk.CTkLabel(v_row, text="（尚未載入影片）", font=FONT_BODY, anchor="w")
        self._player_video_label.pack(side="left", fill="x", expand=True, padx=(8, 8))
        self._player_video_spec_label = ctk.CTkLabel(v_row, text="", font=FONT_SMALL,
                                                     text_color=("gray40", "#9aa0a6"), anchor="e")
        self._player_video_spec_label.pack(side="right", padx=(0, 4))

        # Row 2: Subtitle selection & Hot-Reload
        s_row = ctk.CTkFrame(top_bar, fg_color="transparent")
        s_row.pack(fill="x", padx=8, pady=(2, 6))
        ctk.CTkButton(s_row, text="目標字幕", width=110, height=28,
                      font=FONT_SMALL, command=self._pick_srt).pack(side="left")
        self._player_srt_var = ctk.StringVar(value="（未選擇字幕檔案）")
        ctk.CTkLabel(s_row, textvariable=self._player_srt_var, font=FONT_SMALL,
                     anchor="w", text_color=("gray40", "#9aa0a6")).pack(side="left", fill="x", expand=True, padx=(8, 8))

        ctk.CTkButton(s_row, text="清除", width=60, height=26,
                      font=FONT_SMALL, command=self._clear_srt).pack(side="right", padx=(0, 4))
        ctk.CTkButton(s_row, text="🔄 重新載入 (Hot Reload)", width=170, height=26,
                      font=FONT_SMALL, fg_color="#1f6aa5", hover_color="#144870",
                      command=self._hot_reload_subtitle).pack(side="right", padx=(4, 4))
        self._sub_render_switch = ctk.CTkSwitch(
            s_row, text="字幕渲染", font=FONT_SMALL,
            variable=self._sub_render_var,
            command=self._on_sub_render_toggle,
            width=90, height=24,
        )
        self._sub_render_switch.pack(side="right", padx=(4, 8))

        # Main Draggable Split Container: Left = Video + Controls, Right = Subtitle Viewer & Editor
        self._player_paned = tk.PanedWindow(
            parent, orient="horizontal", sashrelief="raised", sashwidth=6,
            bg="#252526", bd=0, opaqueresize=True
        )
        self._player_paned.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        # Left Column: Player Display & Controls
        left_col = ctk.CTkFrame(self._player_paned)

        # 1. Native VLC HWND Frame
        self._player_canvas_container = ctk.CTkFrame(left_col, fg_color="black")
        self._player_canvas_container.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        self._player_vlc_frame = tk.Frame(self._player_canvas_container, bg="black")
        self._player_vlc_frame.pack(fill="both", expand=True)

        # 2. Controls Frame
        ctl_frame = ctk.CTkFrame(left_col, fg_color="transparent")
        ctl_frame.pack(fill="x", padx=8, pady=(2, 4))

        # Slider row
        slider_row = ctk.CTkFrame(ctl_frame, fg_color="transparent")
        slider_row.pack(fill="x", pady=(0, 2))
        self._player_time_slider = ctk.CTkSlider(slider_row, from_=0, to=100,
                                                 command=self._on_slider_change)
        self._player_time_slider.set(0)
        self._player_time_slider.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._player_time_slider.bind("<ButtonRelease-1>", self._on_slider_release)
        self._player_time_label = ctk.CTkLabel(slider_row, text="00:00:00,000 / 00:00:00,000",
                                               font=FONT_MONO, width=190, anchor="e")
        self._player_time_label.pack(side="right")

        # Button row: Step & Play (in player mode, clicking step seeks and keeps/resumes playing)
        btn_row = ctk.CTkFrame(ctl_frame, fg_color="transparent")
        btn_row.pack(fill="x", pady=(2, 2))

        self._player_btn_step_back_large = ctk.CTkButton(
            btn_row, text="⏪ -5s", width=62, height=28, font=FONT_SMALL,
            command=self._on_player_step_back_large)
        self._player_btn_step_back_large.pack(side="left", padx=(0, 3))

        self._player_btn_step_back_small = ctk.CTkButton(
            btn_row, text="⏪ -3s", width=62, height=28, font=FONT_SMALL,
            command=self._on_player_step_back_small)
        self._player_btn_step_back_small.pack(side="left", padx=(0, 3))

        self._player_btn_step_back_frame = ctk.CTkButton(
            btn_row, text="◀ -1幀", width=62, height=28, font=FONT_SMALL,
            command=self._on_player_step_back_frame)
        self._player_btn_step_back_frame.pack(side="left", padx=(0, 5))

        self._player_play_btn = ctk.CTkButton(
            btn_row, text="▶ 播放", width=82, height=28, font=FONT_BODY,
            command=self._toggle_play)
        self._player_play_btn.pack(side="left", padx=(0, 5))

        self._player_btn_step_forward_frame = ctk.CTkButton(
            btn_row, text="▶ +1幀", width=62, height=28, font=FONT_SMALL,
            command=self._on_player_step_forward_frame)
        self._player_btn_step_forward_frame.pack(side="left", padx=(0, 3))

        self._player_btn_step_forward_small = ctk.CTkButton(
            btn_row, text="⏩ +3s", width=62, height=28, font=FONT_SMALL,
            command=self._on_player_step_forward_small)
        self._player_btn_step_forward_small.pack(side="left", padx=(0, 3))

        self._player_btn_step_forward_large = ctk.CTkButton(
            btn_row, text="⏩ +5s", width=62, height=28, font=FONT_SMALL,
            command=self._on_player_step_forward_large)
        self._player_btn_step_forward_large.pack(side="left")

        # Quick capture and ROI navigation buttons (v2.01)
        self._player_btn_roi = ctk.CTkButton(
            btn_row, text="✂️ 前往影片截圖(ROI)", width=150, height=28, font=FONT_SMALL,
            fg_color="#1f6aa5", hover_color="#144d75",
            command=self._nav_to_video_capture)
        self._player_btn_roi.pack(side="right", padx=(6, 0))

        self._player_btn_capture = ctk.CTkButton(
            btn_row, text="📷 截圖至素材庫", width=118, height=28, font=FONT_SMALL,
            fg_color="#2e7d32", hover_color="#1b5e20",
            command=self._quick_capture)
        self._player_btn_capture.pack(side="right", padx=(6, 0))

        # Row 3: Audio, Zoom & Fullscreen
        audio_row = ctk.CTkFrame(ctl_frame, fg_color="transparent")
        audio_row.pack(fill="x", pady=(2, 2))

        init_mute_icon = "🔇" if self._is_muted else "🔊"
        self._player_mute_btn = ctk.CTkButton(
            audio_row, text=init_mute_icon, width=36, height=26, font=FONT_SMALL,
            command=self._toggle_mute)
        self._player_mute_btn.pack(side="left", padx=(0, 4))

        self._player_volume_slider = ctk.CTkSlider(
            audio_row, from_=0, to=100, width=90, height=14,
            variable=self._volume_var, command=self._on_volume_change)
        self._player_volume_slider.pack(side="left", padx=(0, 6))

        self._player_volume_label = ctk.CTkLabel(
            audio_row, text=f"{int(self._volume_var.get())}%", font=FONT_SMALL, width=38, anchor="w")
        self._player_volume_label.pack(side="left", padx=(0, 8))

        ctk.CTkLabel(audio_row, text="倍速：", font=FONT_SMALL).pack(side="left")
        self._player_rate_menu = ctk.CTkOptionMenu(
            audio_row, values=["0.5x", "0.75x", "1.0x", "1.25x", "1.5x", "2.0x"],
            variable=self._playback_rate_var, width=78, height=26, font=FONT_SMALL,
            command=self._on_rate_change)
        self._player_rate_menu.pack(side="left", padx=(2, 10))

        ctk.CTkLabel(audio_row, text="縮放：", font=FONT_SMALL).pack(side="left")
        self._player_scale_menu = ctk.CTkOptionMenu(
            audio_row, values=["0.5x", "0.75x", "1.0x (原始)", "1.5x", "2.0x"],
            variable=self._window_scale_var, width=105, height=26, font=FONT_SMALL,
            command=self._on_window_scale_change)
        self._player_scale_menu.pack(side="left", padx=(2, 8))

        self._player_fullscreen_btn = ctk.CTkButton(
            audio_row, text="⛶ 全螢幕", width=78, height=26, font=FONT_SMALL,
            fg_color="#333333", hover_color="#444444",
            command=self._toggle_fullscreen)
        self._player_fullscreen_btn.pack(side="left", padx=(0, 8))

        self._player_sub_status_label = ctk.CTkLabel(
            audio_row, text="字幕：未掛載", font=FONT_SMALL,
            text_color=("gray50", "gray60"), anchor="e")
        self._player_sub_status_label.pack(side="right", padx=(0, 4))

        # Right Column: Subtitle List & Live Editor
        right_col = ctk.CTkFrame(self._player_paned)

        # Add panes to draggable PanedWindow
        self._player_paned.add(left_col, minsize=480, stretch="always")
        self._player_paned.add(right_col, minsize=260, stretch="always")

        # Header
        r_hdr = ctk.CTkFrame(right_col, fg_color="transparent")
        r_hdr.pack(fill="x", padx=8, pady=(8, 4))
        self._sub_count_label = ctk.CTkLabel(r_hdr, text="字幕清單 (共 0 條)", font=FONT_SUBTITLE, anchor="w")
        self._sub_count_label.pack(side="left")

        # Treeview style
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Player.Treeview",
            background="#1e1e1e",
            foreground="#e0e0e0",
            rowheight=26,
            fieldbackground="#1e1e1e",
            bordercolor="#333333",
            borderwidth=0,
            font=("Segoe UI", 10),
        )
        style.configure(
            "Player.Treeview.Heading",
            background="#2d2d2d",
            foreground="#ffffff",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "Player.Treeview",
            background=[("selected", "#1f6aa5")],
            foreground=[("selected", "#ffffff")],
        )

        # Treeview + Scrollbar Container
        tree_container = tk.Frame(right_col, bg="#1e1e1e")
        tree_container.pack(fill="both", expand=True, padx=8, pady=(2, 4))

        tree_scroll = ttk.Scrollbar(tree_container, orient="vertical")
        tree_scroll.pack(side="right", fill="y")

        self._sub_tree = ttk.Treeview(
            tree_container,
            columns=("time", "text"),
            show="headings",
            selectmode="browse",
            style="Player.Treeview",
            yscrollcommand=tree_scroll.set
        )
        tree_scroll.config(command=self._sub_tree.yview)

        self._sub_tree.heading("time", text="時間區間")
        self._sub_tree.heading("text", text="字幕內容")

        self._sub_tree.column("time", width=190, minwidth=150, anchor="center")
        self._sub_tree.column("text", width=320, minwidth=200, anchor="w")

        self._sub_tree.pack(fill="both", expand=True)

        # Context Menu for Right-click
        self._sub_context_menu = tk.Menu(self, tearoff=0, bg="#2b2b2b", fg="white", activebackground="#1f6aa5", activeforeground="white")
        self._sub_context_menu.add_command(label="✏ 編輯此行字幕", command=self._on_ctx_edit)
        self._sub_context_menu.add_command(label="📋 複製字幕文字", command=self._on_ctx_copy)
        self._sub_context_menu.add_command(label="⏱ 跳至此時間點", command=self._on_ctx_jump)

        # Treeview Events
        self._sub_tree.bind("<ButtonRelease-1>", self._on_sub_left_click)
        self._sub_tree.bind("<Double-Button-1>", self._on_sub_left_double_click)
        self._sub_tree.bind("<Button-3>", self._on_sub_right_click)
        self._sub_tree.bind("<Double-Button-3>", self._on_sub_right_double_click)

        # Footer instructions
        tip_label = ctk.CTkLabel(
            right_col,
            text="💡 左鍵單擊跳轉 | 左鍵雙擊播放 | 右鍵單擊選單 | 右鍵雙擊編輯",
            font=FONT_SMALL, text_color=("gray40", "#9aa0a6"), anchor="w"
        )
        tip_label.pack(fill="x", padx=10, pady=(2, 6))

    # ------------------------------------------------------------------ #
    #  Tab Change & Dual-Tab Sync Methods (v2.01 Refined Architecture)
    # ------------------------------------------------------------------ #
    def _setup_tab_compatibility(self):
        orig_set = self.tabs.set
        orig_tab = self.tabs.tab

        def custom_set(name: str):
            if name in ("  影片截圖視窗  ", "  影片與字幕任務  "):
                orig_set("  影片截圖視窗  ")
                self._ensure_tab_built("  影片截圖視窗  ")
            elif name in ("  翻譯字幕執行  ", "  翻譯字幕執行視窗  ", "  執行  "):
                orig_set("  截圖翻譯字幕任務  ")
                if hasattr(self, "ocr_tabs"):
                    self.ocr_tabs.set("  翻譯字幕執行  ")
                self._ensure_tab_built("  翻譯字幕執行  ")
            elif name in ("  API 設定  ", "  翻譯字幕 API 設定  "):
                orig_set("  截圖翻譯字幕任務  ")
                if hasattr(self, "ocr_tabs"):
                    self.ocr_tabs.set("  翻譯字幕 API 設定  ")
                self._ensure_tab_built("  翻譯字幕 API 設定  ")
            elif name in ("  Prompt 設定  ", "  翻譯字幕 Prompt 設定  "):
                orig_set("  截圖翻譯字幕任務  ")
                if hasattr(self, "ocr_tabs"):
                    self.ocr_tabs.set("  翻譯字幕 Prompt 設定  ")
                self._ensure_tab_built("  翻譯字幕 Prompt 設定  ")
            elif name in ("  圖生圖任務  ", "  編輯及預覽視窗  "):
                orig_set("  圖生圖任務  ")
                if hasattr(self, "p2p_tabs"):
                    self.p2p_tabs.set("  編輯及預覽視窗  ")
                self._ensure_tab_built("  編輯及預覽視窗  ")
            elif name == "  圖生圖 API 設定  ":
                orig_set("  圖生圖任務  ")
                if hasattr(self, "p2p_tabs"):
                    self.p2p_tabs.set("  圖生圖 API 設定  ")
                self._ensure_tab_built("  圖生圖 API 設定  ")
            elif name == "  圖生圖 Prompt 設定  ":
                orig_set("  圖生圖任務  ")
                if hasattr(self, "p2p_tabs"):
                    self.p2p_tabs.set("  圖生圖 Prompt 設定  ")
                self._ensure_tab_built("  圖生圖 Prompt 設定  ")
            else:
                orig_set(name)
                self._on_tab_change()

        def custom_tab(name: str):
            if name in ("  影片截圖視窗  ", "  影片與字幕任務  "):
                return orig_tab("  影片截圖視窗  ")
            elif name in ("  翻譯字幕執行  ", "  翻譯字幕執行視窗  ", "  執行  "):
                return self.ocr_tabs.tab("  翻譯字幕執行  ")
            elif name in ("  API 設定  ", "  翻譯字幕 API 設定  "):
                return self.ocr_tabs.tab("  翻譯字幕 API 設定  ")
            elif name in ("  Prompt 設定  ", "  翻譯字幕 Prompt 設定  "):
                return self.ocr_tabs.tab("  翻譯字幕 Prompt 設定  ")
            elif name in ("  編輯及預覽視窗  ",):
                return self.p2p_tabs.tab("  編輯及預覽視窗  ")
            elif name in ("  圖生圖 API 設定  ",):
                return self.p2p_tabs.tab("  圖生圖 API 設定  ")
            elif name in ("  圖生圖 Prompt 設定  ",):
                return self.p2p_tabs.tab("  圖生圖 Prompt 設定  ")
            return orig_tab(name)

        self.tabs.set = custom_set
        self.tabs.tab = custom_tab

    def _on_ocr_subtab_change(self):
        sub_name = self.ocr_tabs.get()
        self._ensure_ocr_subtab_built(sub_name)

    def _on_p2p_subtab_change(self):
        sub_name = self.p2p_tabs.get()
        self._ensure_p2p_subtab_built(sub_name)

    def _ensure_ocr_subtab_built(self, sub_name: str):
        if not hasattr(self, "_built_ocr_subtabs"):
            self._built_ocr_subtabs = set()
        if sub_name in self._built_ocr_subtabs:
            return
        self._built_ocr_subtabs.add(sub_name)

        if sub_name == "  翻譯字幕執行  ":
            self._build_ocr_exec_tab(self.ocr_tabs.tab("  翻譯字幕執行  "))
            self._sync_ocr_exec_ui()
        elif sub_name == "  翻譯字幕 API 設定  ":
            self._build_api_tab(self.ocr_tabs.tab("  翻譯字幕 API 設定  "))
            self._sync_api_form()
        elif sub_name == "  翻譯字幕 Prompt 設定  ":
            self._build_prompt_tab(self.ocr_tabs.tab("  翻譯字幕 Prompt 設定  "))
            self._sync_prompt_form()
            self._refresh_prompt_tab_providers()

    def _ensure_p2p_subtab_built(self, sub_name: str):
        if not hasattr(self, "_built_p2p_subtabs"):
            self._built_p2p_subtabs = set()
        if sub_name in self._built_p2p_subtabs:
            return
        self._built_p2p_subtabs.add(sub_name)

        if sub_name == "  編輯及預覽視窗  ":
            self._build_p2p_editor_tab(self.p2p_tabs.tab("  編輯及預覽視窗  "))
            self._refresh_p2p_gallery()
            self._refresh_p2p_output_gallery()
            self._sync_p2p_prompt_controls()
        elif sub_name == "  圖生圖 API 設定  ":
            self._build_p2p_api_tab(self.p2p_tabs.tab("  圖生圖 API 設定  "))
            self._sync_p2p_api_form()
        elif sub_name == "  圖生圖 Prompt 設定  ":
            self._build_p2p_prompt_tab(self.p2p_tabs.tab("  圖生圖 Prompt 設定  "))
            self._sync_p2p_prompt_form()

    def _ensure_tab_built(self, tab_name: str):
        if not hasattr(self, "_built_tabs"):
            self._built_tabs = set()
        if tab_name in self._built_tabs:
            return
        self._built_tabs.add(tab_name)

        if tab_name == "  影音播放器  ":
            self._build_player_tab(self.tabs.tab("  影音播放器  "))
            self._refresh_step_buttons()
        elif tab_name in ("  影片截圖視窗  ", "  影片與字幕任務  "):
            self._built_tabs.update(["  影片截圖視窗  ", "  影片與字幕任務  "])
            self._build_video_tab(self.tabs.tab("  影片截圖視窗  "))
            self._refresh_step_buttons()
            self._sync_video_tab_ui()
            self._render_tasks_list()
            self._refresh_capture_gallery()
        elif tab_name in ("  翻譯字幕執行  ", "  翻譯字幕執行視窗  ", "  執行  "):
            self._built_tabs.update(["  翻譯字幕執行  ", "  翻譯字幕執行視窗  ", "  執行  "])
            self._ensure_ocr_subtab_built("  翻譯字幕執行  ")
        elif tab_name == "  截圖翻譯字幕任務  ":
            self._built_tabs.add("  截圖翻譯字幕任務  ")
            if hasattr(self, "ocr_tabs"):
                self._ensure_ocr_subtab_built(self.ocr_tabs.get())
        elif tab_name in ("  API 設定  ", "  翻譯字幕 API 設定  "):
            self._built_tabs.update(["  API 設定  ", "  翻譯字幕 API 設定  "])
            self._ensure_ocr_subtab_built("  翻譯字幕 API 設定  ")
        elif tab_name in ("  Prompt 設定  ", "  翻譯字幕 Prompt 設定  "):
            self._built_tabs.update(["  Prompt 設定  ", "  翻譯字幕 Prompt 設定  "])
            self._ensure_ocr_subtab_built("  翻譯字幕 Prompt 設定  ")
        elif tab_name in ("  圖生圖任務  ", "  編輯及預覽視窗  "):
            self._built_tabs.update(["  圖生圖任務  ", "  編輯及預覽視窗  "])
            self._ensure_p2p_subtab_built("  編輯及預覽視窗  ")
        elif tab_name == "  圖生圖 API 設定  ":
            self._built_tabs.add("  圖生圖 API 設定  ")
            self._ensure_p2p_subtab_built("  圖生圖 API 設定  ")
        elif tab_name == "  圖生圖 Prompt 設定  ":
            self._built_tabs.add("  圖生圖 Prompt 設定  ")
            self._ensure_p2p_subtab_built("  圖生圖 Prompt 設定  ")
        elif tab_name == "  按鈕與快捷鍵設定  ":
            self._build_keybindings_tab(self.tabs.tab("  按鈕與快捷鍵設定  "))
            self._refresh_step_buttons()

    def _switch_to_tab(self, name: str):
        self._active_tab_name = name
        self.tabs.set(name)
        self._ensure_tab_built(name)
        self._on_tab_change()
        self._active_tab_name = name

    def _on_tab_change(self):
        tab_name = self.tabs.get()
        self._active_tab_name = tab_name
        self._ensure_tab_built(tab_name)

        if tab_name == "  影音播放器  ":
            if self._vlc_player:
                if hasattr(self, "_sub_render_var"):
                    self._vlc_player.set_subtitle_visible(self._sub_render_var.get())
                if hasattr(self, "_player_vlc_frame"):
                    self.update_idletasks()
                    self._vlc_player.bind_window(self._player_vlc_frame.winfo_id())
                    self._vlc_player.set_time(self._current_sec)
            self._sync_player_ui()
            self._populate_sub_treeview()
            self._sync_subtitle_highlight()

        elif tab_name in ("  影片截圖視窗  ", "  翻譯字幕執行視窗  ", "  影片與字幕任務  ", "  執行  "):
            if self._vlc_player:
                if hasattr(self, "_sub_render_var"):
                    self._vlc_player.set_subtitle_visible(self._sub_render_var.get())
                if hasattr(self, "_vlc_frame"):
                    self._show_vlc_display()
                    self.update_idletasks()
                    self._vlc_player.bind_window(self._vlc_frame.winfo_id())
                    self._show_canvas_display()
                    self._vlc_player.set_time(self._current_sec)
            self._show_canvas_display()
            if self._video_engine:
                self._seek_to(self._current_sec)
            self._sync_video_tab_ui()
            self._render_tasks_list()
            self._refresh_run_tab_summary()

        elif tab_name == "  執行  ":
            self._refresh_run_tab_summary()

        elif tab_name == "  Prompt 設定  ":
            self._refresh_prompt_tab_providers()

        elif tab_name == "  API 設定  ":
            self._settings = config.load_settings()
            self._sync_api_form()

        elif tab_name in ("  圖生圖任務  ", "  編輯及預覽視窗  "):
            if hasattr(self, "_refresh_p2p_gallery"):
                self._refresh_p2p_gallery()
            if hasattr(self, "_refresh_p2p_output_gallery"):
                self._refresh_p2p_output_gallery()

    def _sync_player_ui(self):
        filename = Path(self._video_path).name if self._video_path else "（尚未載入影片）"
        if hasattr(self, "_player_video_label"):
            self._player_video_label.configure(text=filename)
        if hasattr(self, "_player_video_spec_label") and self._video_engine:
            effective_dur = self._video_engine.duration_sec if self._video_engine.duration_sec > 0 else (self._vlc_player.get_duration() if self._vlc_player else 0.0)
            dur_str = srt_utils.sec_to_ts(effective_dur)
            self._player_video_spec_label.configure(
                text=f"{self._video_engine.width}x{self._video_engine.height} | {self._video_engine.fps:.2f} FPS | 總長 {dur_str}"
            )
        if hasattr(self, "_player_srt_var"):
            self._player_srt_var.set(self._srt_path or "（未選擇字幕檔案）")
        if hasattr(self, "_player_time_slider") and self._video_engine:
            effective_dur = self._video_engine.duration_sec if self._video_engine.duration_sec > 0 else (self._vlc_player.get_duration() if self._vlc_player else 0.0)
            self._player_time_slider.configure(to=max(1.0, effective_dur))
            self._player_time_slider.set(self._current_sec)
        self._update_time_labels()
        self._update_sub_status_label()

    def _sync_video_tab_ui(self):
        filename = Path(self._video_path).name if self._video_path else "（尚未載入影片）"
        if hasattr(self, "_video_label"):
            self._video_label.configure(text=filename)
        if hasattr(self, "_video_spec_label") and self._video_engine:
            effective_dur = self._video_engine.duration_sec if self._video_engine.duration_sec > 0 else (self._vlc_player.get_duration() if self._vlc_player else 0.0)
            dur_str = srt_utils.sec_to_ts(effective_dur)
            self._video_spec_label.configure(
                text=f"{self._video_engine.width}x{self._video_engine.height} | {self._video_engine.fps:.2f} FPS | 總長 {dur_str}"
            )
        if hasattr(self, "_srt_var"):
            self._srt_var.set(self._srt_path or "（未選擇，將直接匯出新 SRT 檔案）")
        if hasattr(self, "_time_slider") and self._video_engine:
            effective_dur = self._video_engine.duration_sec if self._video_engine.duration_sec > 0 else (self._vlc_player.get_duration() if self._vlc_player else 0.0)
            self._time_slider.configure(to=max(1.0, effective_dur))
            self._time_slider.set(self._current_sec)
        self._update_range_inputs()
        self._update_time_labels()
        self._update_sub_status_label()

    # ------------------------------------------------------------------ #
    #  Subtitle Treeview & Inline Editing Methods
    # ------------------------------------------------------------------ #
    def _populate_sub_treeview(self):
        if not hasattr(self, "_sub_tree"):
            return
        for item in self._sub_tree.get_children():
            self._sub_tree.delete(item)

        for i, entry in enumerate(self._srt_entries, 1):
            start = entry.get("start", entry.get("start_sec", 0.0))
            end = entry.get("end", entry.get("end_sec", 0.0))
            t_str = f"{srt_utils.sec_to_ts(start)} → {srt_utils.sec_to_ts(end)}"
            txt = entry.get("text", "").replace("\n", " ⏎ ")
            self._sub_tree.insert("", "end", iid=str(i), values=(t_str, txt))

        if hasattr(self, "_sub_count_label"):
            self._sub_count_label.configure(text=f"字幕清單 (共 {len(self._srt_entries)} 條)")

    def _sync_subtitle_highlight(self):
        if not hasattr(self, "_sub_tree") or not self._srt_entries:
            return
        cur_sec = self._current_sec
        active_idx = None
        for i, ent in enumerate(self._srt_entries):
            start = ent.get("start", ent.get("start_sec", 0.0))
            end = ent.get("end", ent.get("end_sec", 0.0))
            if start <= cur_sec <= end:
                active_idx = i
                break

        if active_idx is not None:
            target_iid = str(active_idx + 1)
            try:
                cur_sel = self._sub_tree.selection()
                if not cur_sel or cur_sel[0] != target_iid:
                    self._sub_tree.selection_set(target_iid)
                    self._sub_tree.see(target_iid)
            except Exception:
                pass

    def _on_sub_left_click(self, event):
        item = self._sub_tree.identify_row(event.y)
        if not item:
            return
        try:
            idx = int(item) - 1
            if 0 <= idx < len(self._srt_entries):
                sec = self._srt_entries[idx].get("start", self._srt_entries[idx].get("start_sec", 0.0))
                self._seek_to(sec)
        except Exception:
            pass

    def _on_sub_left_double_click(self, event):
        item = self._sub_tree.identify_row(event.y)
        if not item:
            return
        try:
            idx = int(item) - 1
            if 0 <= idx < len(self._srt_entries):
                sec = self._srt_entries[idx].get("start", self._srt_entries[idx].get("start_sec", 0.0))
                self._seek_to(sec)
                if not self._is_playing:
                    self._start_play()
        except Exception:
            pass

    def _on_sub_right_click(self, event):
        item = self._sub_tree.identify_row(event.y)
        if item:
            self._sub_tree.selection_set(item)
            self._context_menu_item = item
            try:
                self._sub_context_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self._sub_context_menu.grab_release()

    def _on_sub_right_double_click(self, event):
        item = self._sub_tree.identify_row(event.y)
        if item:
            self._start_inline_sub_edit(item)

    def _on_ctx_edit(self):
        item = getattr(self, "_context_menu_item", None)
        if item:
            self._start_inline_sub_edit(item)

    def _on_ctx_copy(self):
        item = getattr(self, "_context_menu_item", None)
        if not item:
            return
        try:
            idx = int(item) - 1
            if 0 <= idx < len(self._srt_entries):
                txt = self._srt_entries[idx].get("text", "")
                self.clipboard_clear()
                self.clipboard_append(txt)
        except Exception:
            pass

    def _on_ctx_jump(self):
        item = getattr(self, "_context_menu_item", None)
        if not item:
            return
        try:
            idx = int(item) - 1
            if 0 <= idx < len(self._srt_entries):
                sec = self._srt_entries[idx].get("start", self._srt_entries[idx].get("start_sec", 0.0))
                self._seek_to(sec)
                if not self._is_playing:
                    self._start_play()
        except Exception:
            pass

    def _start_inline_sub_edit(self, item: str):
        if not item:
            return
        self._close_inline_sub_edit(commit=False)

        bbox = self._sub_tree.bbox(item, "text")
        if not bbox:
            return
        x, y, w, h = bbox
        try:
            idx = int(item) - 1
            cur_text = self._srt_entries[idx].get("text", "")
        except Exception:
            return

        self._inline_edit_item = item
        self._inline_edit_idx = idx

        self._inline_entry = tk.Entry(
            self._sub_tree, font=("Segoe UI", 10),
            bg="#2c3e50", fg="white", insertbackground="white",
            relief="solid", bd=1
        )
        self._inline_entry.place(x=x, y=y, width=max(w, 240), height=max(h, 24))
        self._inline_entry.insert(0, cur_text.replace("\n", " \\n "))
        self._inline_entry.focus_set()
        self._inline_entry.select_range(0, "end")

        self._inline_entry.bind("<Return>", lambda _e: self._close_inline_sub_edit(commit=True))
        self._inline_entry.bind("<Escape>", lambda _e: self._close_inline_sub_edit(commit=False))
        self._inline_entry.bind("<FocusOut>", lambda _e: self._close_inline_sub_edit(commit=True))

    def _close_inline_sub_edit(self, commit: bool = True):
        if not hasattr(self, "_inline_entry") or self._inline_entry is None:
            return
        entry = self._inline_entry
        self._inline_entry = None
        item = getattr(self, "_inline_edit_item", None)
        idx = getattr(self, "_inline_edit_idx", None)

        if commit and item is not None and idx is not None and 0 <= idx < len(self._srt_entries):
            new_text = entry.get().replace(" \\n ", "\n").strip()
            self._srt_entries[idx]["text"] = new_text
            ent = self._srt_entries[idx]
            start = ent.get("start", ent.get("start_sec", 0.0))
            end = ent.get("end", ent.get("end_sec", 0.0))
            t_str = f"{srt_utils.sec_to_ts(start)} → {srt_utils.sec_to_ts(end)}"
            self._sub_tree.item(item, values=(t_str, new_text.replace("\n", " ⏎ ")))
            if self._srt_path and os.path.isfile(self._srt_path):
                try:
                    srt_utils.write_srt(self._srt_path, self._srt_entries)
                    if self._vlc_player:
                        self._vlc_player.reload_subtitle()
                except Exception:
                    pass

        try:
            entry.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  Player Step Controls (Seek & Auto-Continue Playing)
    # ------------------------------------------------------------------ #
    def _player_step_time(self, delta_sec: float):
        target = self._current_sec + delta_sec
        if self._video_engine:
            target = max(0.0, min(target, self._video_engine.duration_sec))
        self._current_sec = target
        if self._vlc_player:
            self._vlc_player.set_time(target)
            if not self._is_playing:
                self._start_play()
        self._update_time_labels()

    def _player_step_frame(self, delta_frames: int):
        if not self._video_engine:
            return
        fps = self._video_engine.fps or 30.0
        cur_frame = int(round(self._current_sec * fps))
        target_frame = max(0, cur_frame + delta_frames)
        target_sec = target_frame / fps
        target_sec = min(target_sec, self._video_engine.duration_sec)
        self._current_sec = target_sec
        if self._vlc_player:
            self._vlc_player.set_time(target_sec)
            if not self._is_playing:
                self._start_play()
        self._update_time_labels()

    def _on_player_step_back_large(self):
        sec = float(self._step_settings.get("step_back_large_sec", 5.0))
        self._player_step_time(-sec)

    def _on_player_step_back_small(self):
        sec = float(self._step_settings.get("step_back_small_sec", 3.0))
        self._player_step_time(-sec)

    def _on_player_step_back_frame(self):
        frames = int(self._step_settings.get("step_back_frames", 1))
        self._player_step_frame(-frames)

    def _on_player_step_forward_frame(self):
        frames = int(self._step_settings.get("step_forward_frames", 1))
        self._player_step_frame(frames)

    def _on_player_step_forward_small(self):
        sec = float(self._step_settings.get("step_forward_small_sec", 3.0))
        self._player_step_time(sec)

    def _on_player_step_forward_large(self):
        sec = float(self._step_settings.get("step_forward_large_sec", 5.0))
        self._player_step_time(sec)

    def _nav_to_video_capture(self):
        """Navigate from Player directly to Video Capture (ROI) tab at current timestamp."""
        self._stop_play()
        if self._video_path and not self._video_engine:
            self._load_video(self._video_path)
        self._switch_to_tab("  影片截圖視窗  ")
        self._seek_to(self._current_sec)
        self._sync_video_tab_ui()

    # ------------------------------------------------------------------ #
    #  Window Scale & Fullscreen Controls
    # ------------------------------------------------------------------ #
    def _restore_window_geometry(self):
        """Restore window position, size, and maximized state across monitors with safe fallback."""
        geo = self._settings.get("window_geometry")
        default_w, default_h = 1160, 820
        prim_left, prim_top, prim_w, prim_h = monitor_utils.get_primary_monitor_work_rect()

        if isinstance(geo, dict):
            try:
                x = int(geo.get("x", 0))
                y = int(geo.get("y", 0))
                w = max(980, int(geo.get("width", default_w)))
                h = max(680, int(geo.get("height", default_h)))
                is_max = bool(geo.get("is_maximized", False))

                # Check if target window rect intersects a currently connected monitor
                valid, work_area = monitor_utils.is_rect_on_valid_monitor(x, y, w, h)
                if valid and work_area:
                    # Monitor exists: clamp to its work area in case of resolution changes
                    cx, cy, cw, ch = monitor_utils.clamp_to_monitor_work_area(x, y, w, h, work_area)
                    self.geometry(f"{cw}x{ch}+{cx}+{cy}")
                    if is_max:
                        self.after(50, lambda: self.state("zoomed"))
                    return
            except Exception:
                pass

        # Fallback to primary monitor (Monitor 1) centered
        fallback_w = min(default_w, prim_w - 60)
        fallback_h = min(default_h, prim_h - 80)
        fallback_x = prim_left + max(0, (prim_w - fallback_w) // 2)
        fallback_y = prim_top + max(0, (prim_h - fallback_h) // 2)
        self.geometry(f"{fallback_w}x{fallback_h}+{fallback_x}+{fallback_y}")

    def _save_window_geometry(self):
        """Save current window geometry (x, y, width, height, is_maximized) to settings."""
        if self._is_fullscreen:
            return

        try:
            is_max = (self.state() == "zoomed")
            wp = None
            if monitor_utils.IS_WINDOWS:
                try:
                    wp = monitor_utils.get_window_placement_bounds(self.winfo_id())
                except Exception:
                    wp = None

            if wp:
                geo_data = {
                    "x": int(wp["x"]),
                    "y": int(wp["y"]),
                    "width": int(wp["width"]),
                    "height": int(wp["height"]),
                    "is_maximized": is_max,
                }
            else:
                geo_data = {
                    "x": int(self.winfo_x()),
                    "y": int(self.winfo_y()),
                    "width": int(self.winfo_width()),
                    "height": int(self.winfo_height()),
                    "is_maximized": is_max,
                }

            self._settings["window_geometry"] = geo_data
            config.patch_setting("window_geometry", geo_data)
        except Exception:
            pass

    def _schedule_save_window_geometry(self):
        """Debounce saving window geometry after window move or resize (1000ms delay)."""
        if hasattr(self, "_geo_save_timer") and self._geo_save_timer:
            try:
                self.after_cancel(self._geo_save_timer)
            except Exception:
                pass
            self._geo_save_timer = None

        def _do_save():
            self._geo_save_timer = None
            self._save_window_geometry()

        self._geo_save_timer = self.after(1000, _do_save)

    def _on_window_configure(self, event):
        """Handle window position and dimension changes with debounced persistence."""
        if event.widget == self and not self._is_fullscreen:
            self._schedule_save_window_geometry()

    def _on_window_scale_change(self, scale_str: str):
        try:
            val = float(scale_str.split("x")[0].strip())
        except Exception:
            val = 1.0

        vw = self._video_engine.width if self._video_engine else 1280
        vh = self._video_engine.height if self._video_engine else 720

        extra_w = 400
        extra_h = 250

        target_w = int(vw * val) + extra_w
        target_h = int(vh * val) + extra_h

        # Center within the CURRENT monitor where this app window is located
        mon_left, mon_top, mon_w, mon_h = self._get_current_monitor_rect()

        w = max(980, min(target_w, mon_w - 60))
        h = max(680, min(target_h, mon_h - 80))

        x = mon_left + max(0, (mon_w - w) // 2)
        y = mon_top + max(0, (mon_h - h) // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _get_current_monitor_rect(self) -> tuple[int, int, int, int]:
        """Return (left, top, width, height) of the monitor where this app window is located."""
        try:
            self.update_idletasks()
            x = self.winfo_rootx() + max(1, self.winfo_width() // 2)
            y = self.winfo_rooty() + max(1, self.winfo_height() // 2)
            valid, work = monitor_utils.is_rect_on_valid_monitor(x, y, 1, 1)
            if valid and work:
                return work[0], work[1], work[2] - work[0], work[3] - work[1]
        except Exception:
            pass
        return monitor_utils.get_primary_monitor_work_rect()

    def _hide_fs_bar(self):
        if hasattr(self, "_fs_bar_visible") and self._fs_bar_visible and not self._is_scrubbing:
            if hasattr(self, "_fs_bar") and self._fs_bar:
                try:
                    if self._fs_bar.winfo_exists():
                        self._fs_bar.place_forget()
                except Exception:
                    pass
            self._fs_bar_visible = False
            self._fs_bar_timer = None

    def _toggle_fullscreen(self):
        cur_sec = self._current_sec
        if self._vlc_player:
            vlc_t = self._vlc_player.get_time()
            if vlc_t > 0:
                cur_sec = vlc_t
        was_playing = self._is_playing

        if self._is_fullscreen:
            self._is_fullscreen = False

            # Cancel pending fullscreen timers
            if hasattr(self, "_fs_exit_timer") and self._fs_exit_timer and self._fullscreen_win:
                try:
                    self._fullscreen_win.after_cancel(self._fs_exit_timer)
                except Exception:
                    pass
                self._fs_exit_timer = None
            if hasattr(self, "_fs_bar_timer") and self._fs_bar_timer and self._fullscreen_win:
                try:
                    self._fullscreen_win.after_cancel(self._fs_bar_timer)
                except Exception:
                    pass
                self._fs_bar_timer = None
            if hasattr(self, "_fs_poll_after_id") and self._fs_poll_after_id and self._fullscreen_win:
                try:
                    self._fullscreen_win.after_cancel(self._fs_poll_after_id)
                except Exception:
                    pass
                self._fs_poll_after_id = None

            if hasattr(self, "_fullscreen_win") and self._fullscreen_win:
                try:
                    self._fullscreen_win.destroy()
                except Exception:
                    pass
                self._fullscreen_win = None
                self._fullscreen_frame = None
                self._fullscreen_exit_btn = None
                self._fs_bar = None
                self._fs_time_slider = None
                self._fs_time_label = None
                self._fs_play_btn = None
                self._fs_bar_visible = False

            # Restore HWND to active tab frame
            if self._vlc_player:
                self.update_idletasks()
                if self._active_tab_name == "  影音播放器  " and hasattr(self, "_player_vlc_frame"):
                    self._vlc_player.bind_window(self._player_vlc_frame.winfo_id(), start_sec=cur_sec, is_playing=was_playing)
                elif hasattr(self, "_vlc_frame"):
                    self._vlc_player.bind_window(self._vlc_frame.winfo_id(), start_sec=cur_sec, is_playing=was_playing)

            self._current_sec = cur_sec
            self._update_time_labels()

            if hasattr(self, "_player_fullscreen_btn"):
                self._player_fullscreen_btn.configure(text="⛶ 全螢幕")
        else:
            self._is_fullscreen = True
            if hasattr(self, "_player_fullscreen_btn"):
                self._player_fullscreen_btn.configure(text="🗗 退出全螢幕")

            # Detect current monitor bounds (multi-monitor support)
            mon_left, mon_top, mon_w, mon_h = self._get_current_monitor_rect()

            # Create borderless fullscreen Toplevel on target monitor
            fs = tk.Toplevel(self)
            fs.title("全螢幕播放")
            fs.withdraw()  # Hide initially to avoid flickering on primary display
            fs.overrideredirect(True)
            fs.geometry(f"{mon_w}x{mon_h}+{mon_left}+{mon_top}")
            fs.configure(bg="black")
            self._fullscreen_win = fs

            fs_frame = tk.Frame(fs, bg="black")
            fs_frame.pack(fill="both", expand=True)
            self._fullscreen_frame = fs_frame

            fs.deiconify()
            fs.lift()
            fs.focus_force()
            fs.update_idletasks()

            # Floating exit button in top-right corner
            exit_btn = ctk.CTkButton(
                fs, text="🗗 退出全螢幕 (Esc / F11)", width=170, height=32,
                font=FONT_SMALL, fg_color="#222222", hover_color="#444444",
                command=self._toggle_fullscreen
            )
            exit_btn.place(relx=0.98, rely=0.03, anchor="ne")
            exit_btn.lift()
            self._fullscreen_exit_btn = exit_btn

            # Auto-hide exit button after 3 seconds
            def _hide_exit_btn():
                if hasattr(self, "_fullscreen_exit_btn") and self._fullscreen_exit_btn:
                    try:
                        if self._fullscreen_exit_btn.winfo_exists():
                            self._fullscreen_exit_btn.place_forget()
                    except Exception:
                        pass
                self._fs_exit_timer = None

            self._fs_exit_timer = fs.after(3000, _hide_exit_btn)

            # Bottom Control Bar: timeline slider, time label, play button
            fs_bar = ctk.CTkFrame(fs, fg_color="#181818", height=54, corner_radius=0)
            self._fs_bar = fs_bar
            self._fs_bar_visible = False
            self._fs_bar_timer = None

            fs_play_btn = ctk.CTkButton(
                fs_bar, text="⏸ 暫停" if was_playing else "▶ 播放",
                width=72, height=32, font=FONT_SMALL, command=self._toggle_play
            )
            fs_play_btn.pack(side="left", padx=(14, 8), pady=10)
            self._fs_play_btn = fs_play_btn

            dur_val = self._video_engine.duration_sec if self._video_engine else (self._vlc_player.get_duration() if self._vlc_player else 100.0)
            fs_slider = ctk.CTkSlider(
                fs_bar, from_=0, to=max(1.0, dur_val),
                command=self._on_slider_change
            )
            fs_slider.set(cur_sec)
            fs_slider.pack(side="left", fill="x", expand=True, padx=(4, 12), pady=10)
            fs_slider.bind("<ButtonRelease-1>", self._on_slider_release)
            self._fs_time_slider = fs_slider

            time_str = f"{srt_utils.sec_to_ts(cur_sec)} / {srt_utils.sec_to_ts(dur_val)}"
            fs_time_lbl = ctk.CTkLabel(
                fs_bar, text=time_str, font=FONT_SMALL, text_color="white"
            )
            fs_time_lbl.pack(side="left", padx=(0, 16), pady=10)
            self._fs_time_label = fs_time_lbl

            def _show_bar():
                if not self._fs_bar_visible and self._fs_bar and self._fs_bar.winfo_exists():
                    self._fs_bar.place(relx=0.0, rely=1.0, anchor="sw", relwidth=1.0)
                    self._fs_bar.lift()
                    self._fs_bar_visible = True

            def _on_mouse_activity(x: int, y: int):
                if not self._is_fullscreen or not hasattr(self, "_fullscreen_win") or not self._fullscreen_win:
                    return
                # Top-right hover: reveal exit button if mouse is in top-right area
                if y <= 70 and x >= mon_w - 230:
                    if hasattr(self, "_fullscreen_exit_btn") and self._fullscreen_exit_btn and self._fullscreen_exit_btn.winfo_exists():
                        self._fullscreen_exit_btn.place(relx=0.98, rely=0.03, anchor="ne")
                        self._fullscreen_exit_btn.lift()
                        if self._fs_exit_timer:
                            try:
                                fs.after_cancel(self._fs_exit_timer)
                            except Exception:
                                pass
                        self._fs_exit_timer = fs.after(3000, _hide_exit_btn)

                # Bottom hover: reveal timeline bar if within 80px of bottom
                if y >= mon_h - 80:
                    _show_bar()
                    if self._fs_bar_timer:
                        try:
                            fs.after_cancel(self._fs_bar_timer)
                        except Exception:
                            pass
                        self._fs_bar_timer = None
                else:
                    if self._fs_bar_visible and not self._is_scrubbing and not self._fs_bar_timer:
                        self._fs_bar_timer = fs.after(2000, self._hide_fs_bar)

            def _on_motion(event):
                try:
                    px = event.x_root - mon_left
                    py = event.y_root - mon_top
                    _on_mouse_activity(px, py)
                except Exception:
                    pass

            fs.bind("<Motion>", _on_motion)
            fs_frame.bind("<Motion>", _on_motion)
            fs_bar.bind("<Motion>", _on_motion)
            fs_slider.bind("<Motion>", _on_motion)

            def _fs_mouse_poll():
                if not self._is_fullscreen or not hasattr(self, "_fullscreen_win") or not self._fullscreen_win:
                    return
                try:
                    px = fs.winfo_pointerx() - mon_left
                    py = fs.winfo_pointery() - mon_top
                    _on_mouse_activity(px, py)
                except Exception:
                    pass
                if self._is_fullscreen and hasattr(self, "_fullscreen_win") and self._fullscreen_win:
                    self._fs_poll_after_id = fs.after(150, _fs_mouse_poll)

            self._fs_poll_after_id = fs.after(150, _fs_mouse_poll)

            # Keyboard & mouse event bindings on fullscreen window
            fs.bind("<Escape>", lambda _e: self._toggle_fullscreen())
            fs.bind("<F11>", lambda _e: self._toggle_fullscreen())
            fs.bind("<space>", lambda _e: self._toggle_play())
            fs.bind("<Double-Button-1>", lambda _e: self._toggle_fullscreen())
            fs.bind("<Left>", lambda _e: self._on_player_step_back_large())
            fs.bind("<Right>", lambda _e: self._on_player_step_forward_large())
            fs.protocol("WM_DELETE_WINDOW", self._toggle_fullscreen)

            # Redirect VLC output to fullscreen frame with timestamp preserved
            if self._vlc_player:
                self._vlc_player.bind_window(fs_frame.winfo_id(), start_sec=cur_sec, is_playing=was_playing)

            self._current_sec = cur_sec
            self._update_time_labels()

    def _hot_reload_subtitle(self):
        if not self._srt_path or not os.path.isfile(self._srt_path):
            messagebox.showinfo("無字幕檔案", "目前尚未載入外部 SRT 字幕檔案。")
            return
        try:
            self._srt_entries = srt_utils.parse_srt(self._srt_path)
        except Exception as exc:
            messagebox.showerror("解析失敗", f"重新讀取 SRT 失敗：{exc}")
            return

        if self._vlc_player:
            self._vlc_player.reload_subtitle()

        self._populate_sub_treeview()
        self._update_sub_status_label()
        if hasattr(self, "_player_sub_status_label"):
            name = Path(self._srt_path).name
            count = len(self._srt_entries)
            self._player_sub_status_label.configure(
                text=f"字幕：已重載 ({name} · {count} 條)",
                text_color=("#0a7d2c", "#5ec97a")
            )

    def _on_sub_render_toggle(self):
        enabled = bool(self._sub_render_var.get())
        if self._vlc_player:
            self._vlc_player.set_subtitle_visible(enabled)
        self._settings["sub_render_enabled"] = enabled
        config.patch_setting("sub_render_enabled", enabled)
        self._update_sub_status_label()

    # ------------------------------------------------------------------ #
    #  Tab 2: Video Player & Subtitle Tasks
    # ------------------------------------------------------------------ #
    def _build_video_tab(self, parent):
        # Top bar: Video & SRT selection
        top_bar = ctk.CTkFrame(parent, fg_color=("gray90", "gray17"))
        top_bar.pack(fill="x", padx=10, pady=(6, 6))

        # Row 1 of top bar: Video
        v_row = ctk.CTkFrame(top_bar, fg_color="transparent")
        v_row.pack(fill="x", padx=8, pady=(6, 4))
        ctk.CTkButton(v_row, text="選擇影片", width=105, height=30,
                      font=FONT_BODY, command=self._pick_video).pack(side="left")
        self._video_label = ctk.CTkLabel(v_row, text="（尚未載入影片）", font=FONT_BODY, anchor="w")
        self._video_label.pack(side="left", fill="x", expand=True, padx=(8, 8))

        ctk.CTkButton(v_row, text="🎬 影音播放器", width=105, height=28, font=FONT_SMALL,
                      command=lambda: self.tabs.set("  影音播放器  ")).pack(side="right", padx=(4, 0))
        ctk.CTkButton(v_row, text="🎨 前往圖生圖", width=105, height=28, font=FONT_SMALL,
                      fg_color="#1f6aa5", hover_color="#144d75",
                      command=lambda: self.tabs.set("  圖生圖任務  ")).pack(side="right", padx=(4, 0))
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

        # 1. Video Display Container (Dual Layer: VLC HWND Output + Tkinter Canvas for ROI)
        self._canvas_container = ctk.CTkFrame(left_col, fg_color="black")
        self._canvas_container.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        # VLC Player HWND Frame
        self._vlc_frame = tk.Frame(self._canvas_container, bg="black")

        # Static Canvas for frozen frame display & interactive mouse ROI drag
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

        # Row 3: Audio Volume, Mute, Playback Rate, and Subtitle Status
        audio_row = ctk.CTkFrame(ctl_frame, fg_color="transparent")
        audio_row.pack(fill="x", pady=(2, 2))

        init_mute_icon = "🔇" if self._is_muted else "🔊"
        self._mute_btn = ctk.CTkButton(
            audio_row, text=init_mute_icon, width=36, height=26, font=FONT_SMALL,
            command=self._toggle_mute)
        self._mute_btn.pack(side="left", padx=(0, 4))

        self._volume_slider = ctk.CTkSlider(
            audio_row, from_=0, to=100, width=90, height=14,
            variable=self._volume_var, command=self._on_volume_change)
        self._volume_slider.pack(side="left", padx=(0, 6))

        self._volume_label = ctk.CTkLabel(
            audio_row, text=f"{int(self._volume_var.get())}%", font=FONT_SMALL, width=38, anchor="w")
        self._volume_label.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(audio_row, text="倍速：", font=FONT_SMALL).pack(side="left")
        self._rate_menu = ctk.CTkOptionMenu(
            audio_row, values=["0.5x", "0.75x", "1.0x", "1.25x", "1.5x", "2.0x"],
            variable=self._playback_rate_var, width=78, height=26, font=FONT_SMALL,
            command=self._on_rate_change)
        self._rate_menu.pack(side="left", padx=(2, 10))

        self._sub_status_label = ctk.CTkLabel(
            audio_row, text="字幕：未掛載", font=FONT_SMALL,
            text_color=("gray50", "gray60"), anchor="e")
        self._sub_status_label.pack(side="right", padx=(0, 4))

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

        # Dual Action Buttons: Subtitle OCR vs. P2P Capture (v2.01)
        action_btn_row = ctk.CTkFrame(task_cfg_box, fg_color="transparent")
        action_btn_row.pack(fill="x", padx=8, pady=(0, 8))

        self._add_task_btn = ctk.CTkButton(
            action_btn_row, text="➕ 新增至字幕翻譯任務", height=34,
            font=FONT_SUBTITLE, fg_color="#2e7d32", hover_color="#1b5e20",
            command=self._add_task
        )
        self._add_task_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self._capture_p2p_btn = ctk.CTkButton(
            action_btn_row, text="🎨 截取範圍至圖生圖素材庫", height=34,
            font=FONT_SUBTITLE, fg_color="#1f6aa5", hover_color="#144d75",
            command=self._capture_to_p2p_gallery
        )
        self._capture_p2p_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # Right Column: Tasks & Captures Paned Container (v2.06)
        right_col = ctk.CTkFrame(main_box)
        right_col.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=0)

        vpaned = tk.PanedWindow(
            right_col, orient="vertical", sashrelief="raised", sashwidth=6,
            bg="#252526", bd=0, opaqueresize=True
        )
        vpaned.pack(fill="both", expand=True, padx=2, pady=2)

        top_pane = ctk.CTkFrame(vpaned)
        bottom_pane = ctk.CTkFrame(vpaned)

        vpaned.add(top_pane, minsize=160, stretch="always")
        vpaned.add(bottom_pane, minsize=160, stretch="always")

        # Upper Pane: Subtitle Translation Tasks
        t_hdr = ctk.CTkFrame(top_pane, fg_color="transparent")
        t_hdr.pack(fill="x", padx=8, pady=(6, 2))
        self._task_count_label = ctk.CTkLabel(t_hdr, text="待翻譯任務列表 (共 0 組)", font=FONT_SUBTITLE, anchor="w")
        self._task_count_label.pack(side="left")

        ctk.CTkButton(
            t_hdr, text="🚀 前往翻譯字幕執行 ➔", height=26, font=FONT_SMALL,
            fg_color="#1f6aa5", hover_color="#144d75",
            command=self._navigate_to_ocr_exec
        ).pack(side="right", padx=(6, 0))

        ctk.CTkButton(
            t_hdr, text="📂 開啟目錄", width=75, height=26, font=FONT_SMALL,
            command=self._open_current_ocr_folder
        ).pack(side="right", padx=(4, 0))

        ctk.CTkButton(
            t_hdr, text="清空任務", width=70, height=26, font=FONT_SMALL,
            fg_color="#b23b3b", hover_color="#8a2e2e",
            command=self._clear_tasks
        ).pack(side="right")

        self._task_scroll = ctk.CTkScrollableFrame(top_pane)
        self._task_scroll.pack(fill="both", expand=True, padx=6, pady=(2, 4))

        # Lower Pane: P2P Material Captures Gallery
        b_hdr = ctk.CTkFrame(bottom_pane, fg_color="transparent")
        b_hdr.pack(fill="x", padx=8, pady=(6, 2))
        self._capture_gallery_count_label = ctk.CTkLabel(
            b_hdr, text="圖生圖截圖列表 (共 0 張)", font=FONT_SUBTITLE, anchor="w"
        )
        self._capture_gallery_count_label.pack(side="left")

        ctk.CTkButton(
            b_hdr, text="🎨 前往編輯及預覽 ➔", height=26, font=FONT_SMALL,
            fg_color="#2e7d32", hover_color="#1b5e20",
            command=self._navigate_to_p2p_editor
        ).pack(side="right", padx=(6, 0))

        ctk.CTkButton(
            b_hdr, text="📂 開啟目錄", width=75, height=26, font=FONT_SMALL,
            command=self._open_current_input_folder
        ).pack(side="right", padx=(4, 0))

        ctk.CTkButton(
            b_hdr, text="🔄 重新整理", width=75, height=26, font=FONT_SMALL,
            command=self._refresh_capture_gallery
        ).pack(side="right")

        self._capture_gallery_scroll = ctk.CTkScrollableFrame(bottom_pane)
        self._capture_gallery_scroll.pack(fill="both", expand=True, padx=6, pady=(2, 4))

    def _navigate_to_ocr_exec(self):
        self.tabs.set("  截圖翻譯字幕任務  ")
        if hasattr(self, "ocr_tabs"):
            self.ocr_tabs.set("  翻譯字幕執行  ")
        self._ensure_ocr_subtab_built("  翻譯字幕執行  ")
        if hasattr(self, "_render_ocr_exec_tasks"):
            self._render_ocr_exec_tasks()

    def _navigate_to_p2p_editor(self):
        self.tabs.set("  圖生圖任務  ")
        if hasattr(self, "p2p_tabs"):
            self.p2p_tabs.set("  編輯及預覽視窗  ")
        self._ensure_p2p_subtab_built("  編輯及預覽視窗  ")

    def _refresh_capture_gallery(self):
        if not hasattr(self, "_capture_gallery_scroll"):
            return
        target_dir = getattr(self, "_current_input_dir", None) or config.resolve_captured_dir(self._settings)
        self._captured_images_cache = capture_manager.list_images_in_dir(target_dir)
        self._render_capture_gallery()

    def _render_capture_gallery(self):
        if not hasattr(self, "_capture_gallery_scroll"):
            return
        for w in self._capture_gallery_scroll.winfo_children():
            w.destroy()

        images = getattr(self, "_captured_images_cache", [])
        total = len(images)
        if hasattr(self, "_capture_gallery_count_label"):
            self._capture_gallery_count_label.configure(text=f"圖生圖截圖列表 (共 {total} 張)")

        if not images:
            empty_lbl = ctk.CTkLabel(
                self._capture_gallery_scroll,
                text="素材庫中尚無圖片\n點選「🎨 截取範圍至圖生圖素材庫」即可新增",
                font=FONT_SMALL, text_color=("gray40", "#9aa0a6")
            )
            empty_lbl.pack(pady=20)
            return

        self._thumb_cache = getattr(self, "_thumb_cache", {})
        for item in images:
            p = item["path"]
            row = ctk.CTkFrame(self._capture_gallery_scroll, fg_color=("gray88", "gray18"))
            row.pack(fill="x", pady=2, padx=2)

            c = tk.Canvas(row, width=54, height=36, bg="#222", highlightthickness=0)
            c.pack(side="left", padx=4, pady=3)
            p_str = str(p)
            tk_img = self._thumb_cache.get(p_str)
            if not tk_img:
                try:
                    with Image.open(p) as img_obj:
                        th = img_obj.copy()
                        th.thumbnail((54, 36), Image.Resampling.BILINEAR)
                        tk_img = ImageTk.PhotoImage(th)
                        self._thumb_cache[p_str] = tk_img
                except Exception:
                    tk_img = None
            if tk_img:
                c.create_image(27, 18, anchor="center", image=tk_img)
                c.image = tk_img

            info_box = ctk.CTkFrame(row, fg_color="transparent")
            info_box.pack(side="left", fill="both", expand=True, padx=4, pady=2)
            fname = p.name
            ctk.CTkLabel(info_box, text=fname, font=FONT_SMALL, anchor="w").pack(fill="x")
            dim_text = f"{item.get('width', 0)}x{item.get('height', 0)}"
            time_text = item.get("mtime_str", "")
            ctk.CTkLabel(info_box, text=f"{dim_text} | {time_text}", font=("Microsoft JhengHei", 9),
                         text_color=("gray40", "#9aa0a6"), anchor="w").pack(fill="x")

    def _build_ocr_exec_tab(self, parent):
        """v2.06: Dedicated Subtitle Translation & OCR Execution Workstation."""
        top_bar = ctk.CTkFrame(parent, fg_color="transparent")
        top_bar.pack(fill="x", padx=10, pady=(6, 4))
        ctk.CTkLabel(top_bar, text="⚡ 翻譯字幕執行工作站", font=FONT_SUBTITLE, anchor="w").pack(side="left")
        ctk.CTkButton(
            top_bar, text="🎬 前往影片截圖視窗", height=28, font=FONT_SMALL,
            fg_color="#1f6aa5", hover_color="#144d75",
            command=lambda: self.tabs.set("  影片截圖視窗  ")
        ).pack(side="right")

        paned = tk.PanedWindow(
            parent, orient="horizontal", sashrelief="raised", sashwidth=6,
            bg="#252526", bd=0, opaqueresize=True
        )
        paned.pack(fill="both", expand=True, padx=6, pady=(2, 6))

        left_col = ctk.CTkFrame(paned)
        right_col = ctk.CTkFrame(paned)
        paned.add(left_col, minsize=320, stretch="always")
        paned.add(right_col, minsize=460, stretch="always")

        # === LEFT COLUMN: Task List & Preview Canvas ===
        t_hdr = ctk.CTkFrame(left_col, fg_color="transparent")
        t_hdr.pack(fill="x", padx=8, pady=(6, 2))
        self._ocr_exec_task_count_label = ctk.CTkLabel(
            t_hdr, text="待翻譯任務 (共 0 組)", font=FONT_BODY, anchor="w"
        )
        self._ocr_exec_task_count_label.pack(side="left")

        self._ocr_history_toggle_btn = ctk.CTkButton(
            t_hdr, text="📦 讀取歷史截圖", width=105, height=24, font=FONT_SMALL,
            command=self._toggle_ocr_history_mode
        )
        self._ocr_history_toggle_btn.pack(side="right", padx=(4, 0))

        self._ocr_action_btn = ctk.CTkButton(
            t_hdr, text="清空任務", width=70, height=24, font=FONT_SMALL,
            fg_color="#b23b3b", hover_color="#8a2e2e",
            command=self._on_ocr_left_action_btn
        )
        self._ocr_action_btn.pack(side="right")

        # History Toolbar Container (placed between header and scroll frame)
        self._ocr_toolbar_container = ctk.CTkFrame(left_col, fg_color="transparent")
        self._ocr_toolbar_container.pack(fill="x", padx=8, pady=(0, 2))

        self._ocr_history_toolbar = ctk.CTkFrame(self._ocr_toolbar_container, fg_color="transparent")
        ctk.CTkButton(
            self._ocr_history_toolbar, text="☑ 全選", width=55, height=24, font=FONT_SMALL,
            command=self._select_all_ocr_drop
        ).pack(side="left", padx=(0, 4))
        ctk.CTkButton(
            self._ocr_history_toolbar, text="☐ 全不選", width=65, height=24, font=FONT_SMALL,
            command=self._deselect_all_ocr_drop
        ).pack(side="left", padx=(0, 4))
        ctk.CTkButton(
            self._ocr_history_toolbar, text="↩️ 復原所選至任務列表", height=24, font=FONT_SMALL,
            fg_color="#2e7d32", hover_color="#1b5e20",
            command=self._restore_selected_ocr_drop_items
        ).pack(side="left")

        self._ocr_exec_task_scroll = ctk.CTkScrollableFrame(left_col)
        self._ocr_exec_task_scroll.pack(fill="both", expand=True, padx=6, pady=(2, 4))

        # Preview Container
        ctk.CTkLabel(left_col, text="🖼️ 選中任務截圖預覽：", font=FONT_SMALL, anchor="w").pack(
            fill="x", padx=8, pady=(4, 1)
        )
        self._ocr_exec_preview_box = ctk.CTkFrame(left_col, fg_color="black", height=160)
        self._ocr_exec_preview_box.pack(fill="x", padx=8, pady=(0, 4))
        self._ocr_exec_preview_box.pack_propagate(False)

        self._ocr_exec_preview_canvas = tk.Canvas(
            self._ocr_exec_preview_box, bg="#141414", highlightthickness=0
        )
        self._ocr_exec_preview_canvas.pack(fill="both", expand=True)

        self._ocr_exec_preview_info_label = ctk.CTkLabel(
            left_col, text="請從上方選擇一組任務檢視預覽", font=FONT_SMALL,
            text_color=("gray40", "#9aa0a6"), anchor="w"
        )
        self._ocr_exec_preview_info_label.pack(fill="x", padx=8, pady=(0, 6))

        # === RIGHT COLUMN: Dropdowns, Execution, Editable Textbox & SRT Export ===
        right_scroll = ctk.CTkScrollableFrame(right_col)
        right_scroll.pack(fill="both", expand=True, padx=6, pady=(2, 6))

        # 1. Dropdown Settings Card
        cfg_card = ctk.CTkFrame(right_scroll, fg_color=("gray85", "gray18"))
        cfg_card.pack(fill="x", padx=6, pady=(4, 6))

        # Dropdowns row 1: Provider & Model
        drop_row1 = ctk.CTkFrame(cfg_card, fg_color="transparent")
        drop_row1.pack(fill="x", padx=8, pady=(6, 3))

        ctk.CTkLabel(drop_row1, text="API 供應商：", font=FONT_SMALL).pack(side="left")
        all_provs = [p.get("name", "") for p in config.get_providers()] or ["OpenAI"]
        saved_prov = self._settings.get("selected_ocr_provider")
        if not saved_prov:
            saved_prov = config.get_default_provider_name() or all_provs[0]
        default_prov = saved_prov if saved_prov in all_provs else all_provs[0]
        self._ocr_exec_prov_var = ctk.StringVar(value=default_prov)
        self._ocr_exec_prov_menu = ctk.CTkOptionMenu(
            drop_row1, values=all_provs, variable=self._ocr_exec_prov_var, font=FONT_SMALL,
            width=140, height=26, command=self._on_ocr_exec_prov_change
        )
        self._ocr_exec_prov_menu.pack(side="left", padx=(2, 10))

        ctk.CTkLabel(drop_row1, text="模型：", font=FONT_SMALL).pack(side="left")
        self._ocr_exec_model_var = ctk.StringVar(value="")
        self._ocr_exec_model_menu = ctk.CTkOptionMenu(
            drop_row1, values=["default"], variable=self._ocr_exec_model_var, font=FONT_SMALL,
            width=160, height=26, command=self._on_ocr_exec_model_change
        )
        self._ocr_exec_model_menu.pack(side="left", fill="x", expand=True, padx=(2, 0))

        # Dropdowns row 2: Prompt Profile
        drop_row2 = ctk.CTkFrame(cfg_card, fg_color="transparent")
        drop_row2.pack(fill="x", padx=8, pady=(3, 6))

        ctk.CTkLabel(drop_row2, text="Prompt 方案：", font=FONT_SMALL).pack(side="left")
        profiles = config.get_prompt_profiles(self._settings)
        profile_names = [p.get("name", "") for p in profiles] or ["標準通用繁中字幕"]
        saved_prompt_id = self._settings.get("selected_ocr_prompt_id")
        saved_p = config.get_prompt_profile_by_id(saved_prompt_id, self._settings) if saved_prompt_id else None
        default_prompt_name = saved_p.get("name") if saved_p else profile_names[0]
        self._ocr_exec_prompt_var = ctk.StringVar(value=default_prompt_name)
        self._ocr_exec_prompt_menu = ctk.CTkOptionMenu(
            drop_row2, values=profile_names, variable=self._ocr_exec_prompt_var, font=FONT_SMALL,
            height=26, command=self._on_ocr_exec_prompt_change
        )
        self._ocr_exec_prompt_menu.pack(side="left", fill="x", expand=True, padx=(2, 0))

        # Initialize models for the selected provider
        self._update_ocr_exec_model_options()

        # 2. Execution Action & Progress
        run_row = ctk.CTkFrame(right_scroll, fg_color="transparent")
        run_row.pack(fill="x", padx=6, pady=(2, 4))

        self._run_btn = ctk.CTkButton(
            run_row, text="▶ 開始 OCR 翻譯", height=32, font=FONT_SUBTITLE,
            fg_color="#1f6aa5", hover_color="#144d75", command=self._start_ocr
        )
        self._run_btn.pack(side="left", padx=(0, 8))

        self._progress = ctk.CTkProgressBar(run_row)
        self._progress.set(0)
        self._progress.pack(side="left", fill="x", expand=True, padx=(0, 8))

        self._ocr_exec_status_label = ctk.CTkLabel(
            right_scroll, text="準備就緒。點擊「開始 OCR 翻譯」以執行。", font=FONT_SMALL,
            text_color=("gray40", "#9aa0a6"), anchor="w"
        )
        self._ocr_exec_status_label.pack(fill="x", padx=8, pady=(0, 4))

        # 3. Output Result Dialog (方案 B: 結構化純文字段落，可編輯、可複製)
        out_hdr = ctk.CTkFrame(right_scroll, fg_color="transparent")
        out_hdr.pack(fill="x", padx=6, pady=(4, 2))
        ctk.CTkLabel(
            out_hdr, text="📝 輸出結果對話框（可直接修改文字與複製，時間軸由系統自動對齊）：",
            font=FONT_BODY, anchor="w"
        ).pack(side="left")

        ctk.CTkButton(
            out_hdr, text="🔄 還原原始結果", width=105, height=24, font=FONT_SMALL,
            command=self._restore_raw_ocr_translations
        ).pack(side="right", padx=(4, 0))

        ctk.CTkButton(
            out_hdr, text="📋 複製全部文字", width=105, height=24, font=FONT_SMALL,
            fg_color="#2e7d32", hover_color="#1b5e20",
            command=self._copy_ocr_output_text
        ).pack(side="right")

        self._ocr_output_textbox = ctk.CTkTextbox(right_scroll, height=180, font=FONT_MONO)
        self._ocr_output_textbox.pack(fill="x", padx=6, pady=(2, 6))

        # Hidden log reference for backward compatibility
        self._log = ctk.CTkTextbox(right_scroll, height=60, font=FONT_MONO)

        # 4. Save to SRT Configuration Panel
        save_card = ctk.CTkFrame(right_scroll, fg_color=("gray85", "gray18"))
        save_card.pack(fill="x", padx=6, pady=(4, 6))

        # Splicing / Insertion Mode Row
        s_row1 = ctk.CTkFrame(save_card, fg_color="transparent")
        s_row1.pack(fill="x", padx=8, pady=(6, 3))

        ctk.CTkLabel(s_row1, text="時間軸方式：", font=FONT_SMALL).pack(side="left")
        self._mode_var = ctk.StringVar(value=self._settings.get("insertion_mode", "insert"))
        self._mode_seg = ctk.CTkSegmentedButton(
            s_row1, values=["insert", "append"],
            command=lambda v: [self._mode_var.set(v), config.patch_setting("insertion_mode", v)],
            variable=self._mode_var, height=26, font=FONT_SMALL
        )
        self._mode_seg.pack(side="left", padx=(2, 12))

        ctk.CTkLabel(s_row1, text="存檔方式：", font=FONT_SMALL).pack(side="left")
        self._ocr_save_mode_var = ctk.StringVar(value=self._settings.get("srt_save_mode", "save_as"))
        self._ocr_save_mode_seg = ctk.CTkSegmentedButton(
            s_row1, values=["save_as", "overwrite"],
            command=lambda v: [self._ocr_save_mode_var.set(v), config.patch_setting("srt_save_mode", v), self._update_ocr_save_preview()],
            variable=self._ocr_save_mode_var, height=26, font=FONT_SMALL
        )
        self._ocr_save_mode_seg.pack(side="left", padx=(2, 0))

        # Custom Suffix Row
        s_row2 = ctk.CTkFrame(save_card, fg_color="transparent")
        s_row2.pack(fill="x", padx=8, pady=(3, 6))

        ctk.CTkLabel(s_row2, text="另存後綴詞：", font=FONT_SMALL).pack(side="left")
        self._ocr_suffix_var = ctk.StringVar(value=self._settings.get("custom_srt_suffix", "_translated"))
        self._ocr_suffix_entry = ctk.CTkEntry(
            s_row2, textvariable=self._ocr_suffix_var, width=120, height=26, font=FONT_SMALL
        )
        self._ocr_suffix_entry.pack(side="left", padx=(2, 8))
        self._ocr_suffix_entry.bind("<KeyRelease>", lambda _e: self._update_ocr_save_preview())

        self._ocr_save_preview_label = ctk.CTkLabel(
            s_row2, text="", font=FONT_SMALL, text_color=("gray40", "#9aa0a6"), anchor="w"
        )
        self._ocr_save_preview_label.pack(side="left", fill="x", expand=True)

        # Save Button
        self._insert_btn = ctk.CTkButton(
            save_card, text="💾 儲存為 SRT 檔案", height=32, font=FONT_BODY,
            state="disabled", fg_color="#2e7d32", hover_color="#1b5e20",
            command=self._insert_and_save
        )
        self._insert_btn.pack(fill="x", padx=8, pady=(4, 8))

        self._update_ocr_save_preview()
        self._render_ocr_exec_tasks()
        self._render_ocr_output_textbox()

    def _on_ocr_exec_prov_change(self, choice: str):
        config.patch_setting("selected_ocr_provider", choice)
        self._update_ocr_exec_model_options()

    def _update_ocr_exec_model_options(self):
        if not hasattr(self, "_ocr_exec_model_menu"):
            return
        prov_name = self._ocr_exec_prov_var.get() if hasattr(self, "_ocr_exec_prov_var") else ""
        target_prov = None
        for p in config.get_providers():
            if p.get("name") == prov_name:
                target_prov = p
                break
        models = target_prov.get("models", []) if target_prov else []
        if not models and target_prov and target_prov.get("selected_model"):
            models = [target_prov.get("selected_model")]
        if not models:
            models = ["default"]
        self._ocr_exec_model_menu.configure(values=models)
        saved_m = self._settings.get("selected_ocr_model") or (target_prov.get("selected_model") if target_prov else "")
        if saved_m in models:
            self._ocr_exec_model_var.set(saved_m)
        else:
            self._ocr_exec_model_var.set(models[0])

    def _on_ocr_exec_model_change(self, choice: str):
        config.patch_setting("selected_ocr_model", choice)

    def _on_ocr_exec_prompt_change(self, choice: str):
        prof = config.get_prompt_profile_by_name(choice, self._settings)
        if prof and prof.get("id"):
            config.patch_setting("selected_ocr_prompt_id", prof["id"])

    def _select_ocr_exec_task(self, task: SubtitleTask):
        self._selected_ocr_exec_task = task
        self._render_ocr_exec_preview()
        if hasattr(self, "_render_ocr_exec_tasks"):
            self._render_ocr_exec_tasks()

    def _render_ocr_exec_preview(self):
        if not hasattr(self, "_ocr_exec_preview_canvas"):
            return
        task = getattr(self, "_selected_ocr_exec_task", None)
        cw = max(10, self._ocr_exec_preview_canvas.winfo_width())
        ch = max(10, self._ocr_exec_preview_canvas.winfo_height())
        self._ocr_exec_preview_canvas.delete("all")
        if not task or not task.crop_img:
            self._ocr_exec_preview_canvas.create_text(
                cw // 2, ch // 2, text="尚無選中任務截圖", fill="#666666", font=FONT_SMALL
            )
            if hasattr(self, "_ocr_exec_preview_info_label"):
                self._ocr_exec_preview_info_label.configure(text="請從上方選擇一組任務檢視預覽")
            return

        img = task.crop_img.copy()
        iw, ih = img.size
        scale = min(cw / iw, ch / ih) if iw > 0 and ih > 0 else 1.0
        nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
        resized = img.resize((nw, nh), Image.Resampling.BILINEAR)
        tk_img = ImageTk.PhotoImage(resized)
        self._ocr_exec_preview_canvas.create_image(cw // 2, ch // 2, anchor="center", image=tk_img)
        self._ocr_exec_preview_canvas.image = tk_img

        scope = f"框選區域 ({task.crop_box[0]},{task.crop_box[1]}~{task.crop_box[2]},{task.crop_box[3]})" if task.crop_box else "全畫面"
        info = f"任務 #{task.id} ｜ 時間軸: {task.start_ts} → {task.end_ts} ({task.duration:.2f}s) ｜ 範圍: {scope}"
        if hasattr(self, "_ocr_exec_preview_info_label"):
            self._ocr_exec_preview_info_label.configure(text=info)

    def _render_ocr_exec_tasks(self):
        if not hasattr(self, "_ocr_exec_task_scroll"):
            return
        for w in self._ocr_exec_task_scroll.winfo_children():
            w.destroy()

        total = len(self._tasks)
        if hasattr(self, "_ocr_exec_task_count_label"):
            self._ocr_exec_task_count_label.configure(text=f"待翻譯任務 (共 {total} 組)")

        if not self._tasks:
            empty_lbl = ctk.CTkLabel(
                self._ocr_exec_task_scroll,
                text="尚未建立翻譯任務\n請先前往「影片截圖視窗」新增字幕任務",
                font=FONT_SMALL, text_color=("gray40", "#9aa0a6")
            )
            empty_lbl.pack(pady=30)
            self._selected_ocr_exec_task = None
            self._render_ocr_exec_preview()
            return

        selected = getattr(self, "_selected_ocr_exec_task", None)
        if selected not in self._tasks:
            self._selected_ocr_exec_task = self._tasks[0]

        for i, task in enumerate(self._tasks, 1):
            is_sel = (task == getattr(self, "_selected_ocr_exec_task", None))
            bg_color = ("#3a7ebf", "#1f4a75") if is_sel else ("gray88", "gray18")
            card = ctk.CTkFrame(self._ocr_exec_task_scroll, fg_color=bg_color)
            card.pack(fill="x", pady=2, padx=2)

            # Left thumbnail
            c = tk.Canvas(card, width=64, height=36, bg="#222", highlightthickness=0)
            c.pack(side="left", padx=4, pady=3)
            th = task.crop_img.copy()
            th.thumbnail((64, 36), Image.Resampling.BILINEAR)
            th_tk = ImageTk.PhotoImage(th)
            c.create_image(32, 18, anchor="center", image=th_tk)
            c.image = th_tk
            c.bind("<Button-1>", lambda _e, t=task: self._select_ocr_exec_task(t))

            # Info Frame
            info_frame = ctk.CTkFrame(card, fg_color="transparent")
            info_frame.pack(side="left", fill="both", expand=True, padx=4, pady=2)
            info_frame.bind("<Button-1>", lambda _e, t=task: self._select_ocr_exec_task(t))

            time_row = ctk.CTkFrame(info_frame, fg_color="transparent")
            time_row.pack(fill="x", pady=(0, 2))

            lbl_num = ctk.CTkLabel(time_row, text=f"#{i}", font=FONT_MONO, width=28, anchor="w")
            lbl_num.pack(side="left")
            lbl_num.bind("<Button-1>", lambda _e, t=task: self._select_ocr_exec_task(t))

            start_e = ctk.CTkEntry(time_row, width=88, height=22, font=FONT_SMALL)
            start_e.insert(0, task.start_ts)
            start_e.pack(side="left", padx=(0, 2))

            ctk.CTkLabel(time_row, text="→", font=FONT_SMALL).pack(side="left", padx=2)

            end_e = ctk.CTkEntry(time_row, width=88, height=22, font=FONT_SMALL)
            end_e.insert(0, task.end_ts)
            end_e.pack(side="left", padx=(2, 4))

            dur_lbl = ctk.CTkLabel(time_row, text=f"({task.duration:.2f}s)", font=FONT_SMALL, text_color=("gray40", "#9aa0a6"))
            dur_lbl.pack(side="left")

            def make_ocr_time_handler(t=task, se=start_e, ee=end_e, dl=dur_lbl):
                def _handler(_event=None):
                    self._on_task_time_edit(t, se.get(), ee.get(), se, ee, dl)
                return _handler

            h = make_ocr_time_handler()
            start_e.bind("<FocusOut>", h)
            start_e.bind("<Return>", h)
            end_e.bind("<FocusOut>", h)
            end_e.bind("<Return>", h)

            scope_text = "框選區域" if task.crop_box else "全畫面"
            status_color = {"待處理": "#9aa0a6", "翻譯中": "#2196f3", "已完成": "#4caf50", "失敗": "#f44336"}.get(task.status, "#9aa0a6")
            sub_info = f"範圍: {scope_text} ｜ 狀態: {task.status}"
            lbl_sub = ctk.CTkLabel(info_frame, text=sub_info, font=FONT_SMALL, text_color=status_color, anchor="w")
            lbl_sub.pack(fill="x")
            lbl_sub.bind("<Button-1>", lambda _e, t=task: self._select_ocr_exec_task(t))

            # Right action buttons
            b_box = ctk.CTkFrame(card, fg_color="transparent")
            b_box.pack(side="right", padx=4)

            ctk.CTkButton(
                b_box, text="選中", width=42, height=24, font=FONT_SMALL,
                command=lambda t=task: self._select_ocr_exec_task(t)
            ).pack(side="left", padx=(0, 3))

            ctk.CTkButton(
                b_box, text="🗑️", width=30, height=24, font=FONT_SMALL,
                fg_color="#b23b3b", hover_color="#8a2e2e",
                command=lambda tid=task.id: self._delete_task(tid)
            ).pack(side="left")

        self._render_ocr_exec_preview()

    def _toggle_ocr_history_mode(self):
        self._ocr_history_mode = not self._ocr_history_mode
        self._selected_ocr_drop_paths.clear()
        if self._ocr_history_mode:
            self._ocr_history_toggle_btn.configure(text="📋 返回任務列表")
            self._ocr_action_btn.configure(text="🧹 清空暫存")
            self._ocr_history_toolbar.pack(fill="x")
            self._render_ocr_drop_list()
        else:
            self._ocr_history_toggle_btn.configure(text="📦 讀取歷史截圖")
            self._ocr_action_btn.configure(text="清空任務")
            self._ocr_history_toolbar.pack_forget()
            self._render_ocr_exec_tasks()

    def _on_ocr_left_action_btn(self):
        if self._ocr_history_mode:
            self._clear_ocr_history()
        else:
            self._clear_tasks()

    def _select_all_ocr_drop(self):
        ocr_drop_dir = config.resolve_ocr_drop_dir(self._settings)
        items = capture_manager.list_images_in_dir(ocr_drop_dir)
        for it in items:
            self._selected_ocr_drop_paths.add(str(it["path"]))
        for cb in self._ocr_drop_checkboxes.values():
            cb.set(True)
        if hasattr(self, "_ocr_exec_task_count_label"):
            self._ocr_exec_task_count_label.configure(
                text=f"歷史暫存 (.drop) (共 {len(items)} 張 ｜ 已勾選 {len(self._selected_ocr_drop_paths)} 張)"
            )

    def _deselect_all_ocr_drop(self):
        self._selected_ocr_drop_paths.clear()
        for cb in self._ocr_drop_checkboxes.values():
            cb.set(False)
        ocr_drop_dir = config.resolve_ocr_drop_dir(self._settings)
        items = capture_manager.list_images_in_dir(ocr_drop_dir)
        if hasattr(self, "_ocr_exec_task_count_label"):
            self._ocr_exec_task_count_label.configure(
                text=f"歷史暫存 (.drop) (共 {len(items)} 張 ｜ 已勾選 0 張)"
            )

    def _render_ocr_drop_list(self):
        if not hasattr(self, "_ocr_exec_task_scroll"):
            return
        for w in self._ocr_exec_task_scroll.winfo_children():
            w.destroy()
        self._ocr_drop_checkboxes.clear()

        ocr_drop_dir = config.resolve_ocr_drop_dir(self._settings)
        items = capture_manager.list_images_in_dir(ocr_drop_dir)
        total = len(items)
        sel_count = len(self._selected_ocr_drop_paths)

        if hasattr(self, "_ocr_exec_task_count_label"):
            self._ocr_exec_task_count_label.configure(
                text=f"歷史暫存 (.drop) (共 {total} 張 ｜ 已勾選 {sel_count} 張)"
            )

        if not items:
            lbl = ctk.CTkLabel(
                self._ocr_exec_task_scroll,
                text="歷史暫存資料夾 (.drop) 目前沒有截圖檔案。\n刪除或清空的字幕任務將自動保存在此。",
                font=FONT_SMALL, text_color=("gray40", "#9aa0a6")
            )
            lbl.pack(pady=30)
            return

        for item in items:
            p_str = str(item["path"])
            card = ctk.CTkFrame(self._ocr_exec_task_scroll, fg_color=("gray88", "gray18"))
            card.pack(fill="x", pady=2, padx=2)

            # Checkbox
            cb = ctk.CTkCheckBox(
                card, text="", width=24, checkbox_width=20, checkbox_height=20,
                command=lambda p=p_str: self._on_ocr_drop_cb_toggle(p)
            )
            cb.pack(side="left", padx=(6, 2))
            if p_str in self._selected_ocr_drop_paths:
                cb.set(True)
            self._ocr_drop_checkboxes[p_str] = cb

            # Thumbnail
            c = tk.Canvas(card, width=64, height=36, bg="#222", highlightthickness=0)
            c.pack(side="left", padx=4, pady=3)
            try:
                im = Image.open(item["path"]).convert("RGB")
                im.thumbnail((64, 36), Image.Resampling.BILINEAR)
                th_tk = ImageTk.PhotoImage(im)
                c.create_image(32, 18, anchor="center", image=th_tk)
                c.image = th_tk
            except Exception:
                pass

            # Info
            info_f = ctk.CTkFrame(card, fg_color="transparent")
            info_f.pack(side="left", fill="both", expand=True, padx=4, pady=2)

            ts_parsed = capture_manager.parse_ocr_filename(item["name"])
            if ts_parsed:
                st, et = ts_parsed
                ts_text = f"時間軸: {srt_utils.sec_to_ts(st)} → {srt_utils.sec_to_ts(et)}"
            else:
                ts_text = item["name"]

            ctk.CTkLabel(info_f, text=ts_text, font=FONT_MONO, anchor="w").pack(fill="x")
            date_info = f"大小: {max(1, item['size']//1024)} KB ｜ 日期: {item['date_str']}"
            ctk.CTkLabel(info_f, text=date_info, font=FONT_SMALL, text_color=("gray40", "#9aa0a6"), anchor="w").pack(fill="x")

            # Restore single button
            btn_f = ctk.CTkFrame(card, fg_color="transparent")
            btn_f.pack(side="right", padx=6)
            ctk.CTkButton(
                btn_f, text="復原", width=50, height=24, font=FONT_SMALL,
                fg_color="#2e7d32", hover_color="#1b5e20",
                command=lambda p=p_str: self._restore_single_ocr_drop(p)
            ).pack(side="left")

    def _on_ocr_drop_cb_toggle(self, p_str: str):
        if p_str in self._selected_ocr_drop_paths:
            self._selected_ocr_drop_paths.remove(p_str)
        else:
            self._selected_ocr_drop_paths.add(p_str)
        ocr_drop_dir = config.resolve_ocr_drop_dir(self._settings)
        items = capture_manager.list_images_in_dir(ocr_drop_dir)
        if hasattr(self, "_ocr_exec_task_count_label"):
            self._ocr_exec_task_count_label.configure(
                text=f"歷史暫存 (.drop) (共 {len(items)} 張 ｜ 已勾選 {len(self._selected_ocr_drop_paths)} 張)"
            )

    def _restore_single_ocr_drop(self, p_str: str):
        self._restore_ocr_drop_items([p_str])

    def _restore_selected_ocr_drop_items(self):
        if not self._selected_ocr_drop_paths:
            messagebox.showinfo("提示", "請先勾選欲復原至待翻譯列表的截圖。")
            return
        self._restore_ocr_drop_items(list(self._selected_ocr_drop_paths))

    def _restore_ocr_drop_items(self, paths: list[str]):
        restored_count = 0
        for p_str in paths:
            restored_path = capture_manager.safe_restore_from_drop(p_str, self._current_ocr_dir)
            if restored_path and restored_path.is_file():
                ts_parsed = capture_manager.parse_ocr_filename(restored_path.name)
                if ts_parsed:
                    st, et = ts_parsed
                else:
                    fn_ts = srt_utils.parse_filename_timestamp(restored_path)
                    st = fn_ts if fn_ts is not None else 0.0
                    et = st + 2.0

                try:
                    img = Image.open(restored_path).convert("RGB")
                    task = SubtitleTask(
                        id=self._next_task_id,
                        start_sec=st,
                        end_sec=et,
                        start_frame_img=img.copy(),
                        crop_box=None,
                        crop_img=img.copy(),
                        status="待處理",
                        image_path=str(restored_path),
                    )
                    self._next_task_id += 1
                    self._tasks.append(task)
                    restored_count += 1
                except Exception as exc:
                    print(f"Error loading restored image {restored_path}: {exc}")

        self._selected_ocr_drop_paths.clear()
        self._ocr_history_mode = False
        self._ocr_history_toggle_btn.configure(text="📦 讀取歷史截圖")
        self._ocr_action_btn.configure(text="清空任務", command=self._clear_tasks)
        if hasattr(self, "_ocr_history_toolbar"):
            self._ocr_history_toolbar.pack_forget()
        self._render_tasks_list()
        messagebox.showinfo("復原完成", f"已成功復原 {restored_count} 組截圖至待翻譯任務列表！")

    def _clear_ocr_history(self):
        ocr_drop_dir = config.resolve_ocr_drop_dir(self._settings)
        drop_images = capture_manager.list_images_in_dir(ocr_drop_dir)
        if not drop_images:
            messagebox.showinfo("提示", "目前沒有歷史暫存檔案需要清空。")
            return
        if not messagebox.askyesno("清空確認", f"確定要永久清空所有 {len(drop_images)} 個字幕歷史暫存檔案嗎？此動作無法復原。"):
            return
        count = capture_manager.clear_drop_dir(ocr_drop_dir)
        self._selected_ocr_drop_paths.clear()
        self._render_ocr_drop_list()
        messagebox.showinfo("清空完成", f"已永久刪除 {count} 個歷史暫存檔案。")

    def _render_ocr_output_textbox(self):
        if not hasattr(self, "_ocr_output_textbox"):
            return
        self._ocr_output_textbox.delete("1.0", "end")
        if not self._tasks:
            return
        blocks = []
        for i, task in enumerate(self._tasks, 1):
            blocks.append(f"[#{i}] {task.start_ts} --> {task.end_ts}")
            blocks.append(task.translation if task.translation else "（尚未翻譯）")
            blocks.append("")
        self._ocr_output_textbox.insert("1.0", "\n".join(blocks).strip() + "\n")

    def _sync_textbox_to_tasks(self):
        if not hasattr(self, "_ocr_output_textbox"):
            return
        raw_text = self._ocr_output_textbox.get("1.0", "end").strip()
        if not raw_text:
            return
        blocks = re.split(r'\[(?:#|任務\s*)(\d+)\][^\n]*\n', raw_text)
        if len(blocks) >= 3:
            for idx in range(1, len(blocks), 2):
                task_idx_str = blocks[idx]
                task_text = blocks[idx + 1].strip()
                if task_text == "（尚未翻譯）":
                    task_text = ""
                try:
                    num = int(task_idx_str)
                    if 1 <= num <= len(self._tasks):
                        self._tasks[num - 1].translation = task_text
                except (ValueError, IndexError):
                    pass
        else:
            lines = [l.strip() for l in raw_text.splitlines() if l.strip() and not l.strip().startswith("[#")]
            for i, line_text in enumerate(lines):
                if i < len(self._tasks):
                    if line_text != "（尚未翻譯）":
                        self._tasks[i].translation = line_text

    def _copy_ocr_output_text(self):
        if not hasattr(self, "_ocr_output_textbox"):
            return
        self._sync_textbox_to_tasks()
        raw_text = self._ocr_output_textbox.get("1.0", "end").strip()
        if not raw_text:
            messagebox.showinfo("無內容", "輸出結果對話框目前無內容可複製。")
            return
        self.clipboard_clear()
        self.clipboard_append(raw_text)
        if hasattr(self, "_ocr_exec_status_label"):
            self._ocr_exec_status_label.configure(text="📋 已成功複製全部文字至剪貼簿！")
            self.after(3000, self._update_ocr_save_preview)

    def _restore_raw_ocr_translations(self):
        restored = 0
        for task in self._tasks:
            if hasattr(task, "raw_translation") and task.raw_translation:
                task.translation = task.raw_translation
                restored += 1
        self._render_ocr_output_textbox()
        if hasattr(self, "_render_ocr_exec_tasks"):
            self._render_ocr_exec_tasks()
        if hasattr(self, "_ocr_exec_status_label"):
            self._ocr_exec_status_label.configure(text=f"🔄 已還原 {restored} 組 AI 原始翻譯結果。")
            self.after(3000, self._update_ocr_save_preview)

    def _update_ocr_save_preview(self):
        if not hasattr(self, "_ocr_save_preview_label"):
            return
        save_mode = getattr(self, "_ocr_save_mode_var", None)
        mode_val = save_mode.get() if save_mode else self._settings.get("srt_save_mode", "save_as")
        if mode_val == "overwrite":
            if self._srt_path and Path(self._srt_path).exists():
                self._ocr_save_preview_label.configure(
                    text=f"預計：覆蓋現有檔案【{Path(self._srt_path).name}】",
                    text_color=("#1f6aa5", "#4ca6e8")
                )
            else:
                self._ocr_save_preview_label.configure(
                    text="（無載入現有 SRT，執行儲存時將自動轉為另存新檔）",
                    text_color=("gray50", "#e0a92a")
                )
        else:
            suffix = getattr(self, "_ocr_suffix_var", None)
            s_val = suffix.get().strip() if suffix else "_translated"
            if s_val and not s_val.startswith(("_", "-", ".")):
                s_val = "_" + s_val
            if s_val.endswith(".srt"):
                s_val = s_val[:-4]
            if self._srt_path:
                stem = Path(self._srt_path).stem
            elif self._video_path:
                stem = Path(self._video_path).stem
            else:
                stem = "subtitles"
            self._ocr_save_preview_label.configure(
                text=f"預計檔名：{stem}{s_val}.srt",
                text_color=("gray40", "#9aa0a6")
            )

    def _sync_ocr_exec_ui(self):
        """Refresh OCR execution page dropdowns, previews, and task lists."""
        if hasattr(self, "_ocr_exec_prov_menu"):
            all_provs = [p.get("name", "") for p in config.get_providers()] or ["OpenAI"]
            self._ocr_exec_prov_menu.configure(values=all_provs)
            curr = self._ocr_exec_prov_var.get()
            if curr not in all_provs:
                self._ocr_exec_prov_var.set(all_provs[0])
            self._update_ocr_exec_model_options()

        if hasattr(self, "_ocr_exec_prompt_menu"):
            profiles = config.get_prompt_profiles(self._settings)
            profile_names = [p.get("name", "") for p in profiles] or ["標準通用繁中字幕"]
            self._ocr_exec_prompt_menu.configure(values=profile_names)
            curr_p = self._ocr_exec_prompt_var.get()
            if curr_p not in profile_names:
                self._ocr_exec_prompt_var.set(profile_names[0])

        if hasattr(self, "_render_ocr_exec_tasks"):
            self._render_ocr_exec_tasks()
        if hasattr(self, "_render_ocr_output_textbox"):
            self._render_ocr_output_textbox()
        if hasattr(self, "_update_ocr_save_preview"):
            self._update_ocr_save_preview()

    # ------------------------------------------------------------------ #
    #  Video Loading & Playback Engine Methods
    # ------------------------------------------------------------------ #
    def _pick_video(self):
        initial = self._settings.get("last_video_dir") or os.getcwd()
        path = filedialog.askopenfilename(
            initialdir=initial,
            filetypes=[
                ("影片檔案", "*.mp4 *.mkv *.avi *.mov *.flv *.wmv *.webm *.ts *.m2ts *.mts"),
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

        self._video_path = str(path)
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

        if hasattr(self, "_video_label"):
            self._video_label.configure(text=filename)
        if hasattr(self, "_player_video_label"):
            self._player_video_label.configure(text=filename)

        spec_text = f"{w}x{h} | {fps:.2f} FPS | 總長 {dur_str}"
        if hasattr(self, "_video_spec_label"):
            self._video_spec_label.configure(text=spec_text)
        if hasattr(self, "_player_video_spec_label"):
            self._player_video_spec_label.configure(text=spec_text)

        dur_max = max(1.0, self._video_engine.duration_sec)
        if hasattr(self, "_time_slider"):
            self._time_slider.configure(to=dur_max)
        if hasattr(self, "_player_time_slider"):
            self._player_time_slider.configure(to=dur_max)

        # Check for same-named or matching .srt file in the same directory
        p_obj = Path(path)
        found_srt = None
        for cand in [p_obj.with_suffix(".srt"), p_obj.with_suffix(".SRT")]:
            if cand.is_file():
                found_srt = cand
                break

        if not found_srt:
            parent_dir = p_obj.parent
            stem = p_obj.stem
            try:
                for f in parent_dir.iterdir():
                    if f.is_file() and f.suffix.lower() == ".srt" and f.name.startswith(stem):
                        found_srt = f
                        break
            except Exception:
                pass

        if found_srt and found_srt.is_file():
            self._srt_path = str(found_srt)
            self._settings["last_srt_path"] = self._srt_path
            config.patch_setting("last_srt_path", self._srt_path)
            if hasattr(self, "_srt_var"):
                self._srt_var.set(self._srt_path)
            if hasattr(self, "_player_srt_var"):
                self._player_srt_var.set(self._srt_path)
            try:
                self._srt_entries = srt_utils.parse_srt(self._srt_path)
            except Exception:
                self._srt_entries = []

        # Initialize/Load VLC Player if available
        if self._vlc_player:
            if hasattr(self, "_sub_render_var"):
                self._vlc_player.set_subtitle_visible(self._sub_render_var.get())
            self.update_idletasks()
            if self._active_tab_name == "  影音播放器  " and hasattr(self, "_player_vlc_frame"):
                self._vlc_player.bind_window(self._player_vlc_frame.winfo_id())
            elif hasattr(self, "_vlc_frame"):
                self._vlc_player.bind_window(self._vlc_frame.winfo_id())

            self._vlc_player.load_media(path, srt_path=self._srt_path)
            self._vlc_player.set_volume(int(self._volume_var.get()))
            try:
                rate_val = float(self._playback_rate_var.get().replace("x", "").strip())
                self._vlc_player.set_rate(rate_val)
            except Exception:
                pass

            # Fallback duration if VideoEngine couldn't determine it (e.g. MPEG-TS stream without header)
            if self._video_engine.duration_sec <= 0:
                vlc_dur = self._vlc_player.get_duration()
                if vlc_dur > 0:
                    self._video_engine.duration_sec = vlc_dur
                    dur_max = max(1.0, vlc_dur)
                    if hasattr(self, "_time_slider"):
                        self._time_slider.configure(to=dur_max)
                    if hasattr(self, "_player_time_slider"):
                        self._player_time_slider.configure(to=dur_max)

        self._populate_sub_treeview()
        self._update_sub_status_label()
        self._sync_player_ui()

        # Default start and end
        effective_dur = self._video_engine.duration_sec if self._video_engine.duration_sec > 0 else (self._vlc_player.get_duration() if self._vlc_player else 0.0)
        self._current_sec = 0.0
        self._start_sec = 0.0
        self._end_sec = min(2.0, effective_dur) if effective_dur > 0 else 2.0
        self._update_range_inputs()

        # Reset crop ROI and start frame for the newly loaded video
        self._start_frame_img = None
        self._crop_box = None
        if hasattr(self, "_canvas") and getattr(self, "_rect_id", None):
            self._canvas.delete(self._rect_id)
            self._rect_id = None
        self._update_crop_preview()

        # Remove hint and seek to first frame
        if hasattr(self, "_canvas") and hasattr(self, "_hint_text_id"):
            self._canvas.delete(self._hint_text_id)
        if self._active_tab_name in ("  影片截圖視窗  ", "  翻譯字幕執行視窗  ", "  影片與字幕任務  ", "  執行  "):
            self._show_canvas_display()
        self._seek_to(0.0)

    def _show_vlc_display(self):
        if hasattr(self, "_canvas") and hasattr(self, "_vlc_frame"):
            self._canvas.pack_forget()
            self._vlc_frame.pack(fill="both", expand=True)
            self._vlc_frame.update_idletasks()
            if self._vlc_player:
                self._vlc_player.bind_window(self._vlc_frame.winfo_id())
            self._active_display_mode = "vlc"

    def _show_canvas_display(self):
        if hasattr(self, "_canvas") and hasattr(self, "_vlc_frame"):
            self._vlc_frame.pack_forget()
            self._canvas.pack(fill="both", expand=True)
            self._canvas.update_idletasks()
            self._active_display_mode = "canvas"

    def _seek_to(self, sec: float):
        if not self._video_engine:
            return
        effective_dur = self._video_engine.duration_sec
        if effective_dur <= 0 and self._vlc_player:
            effective_dur = self._vlc_player.get_duration()
        if effective_dur > 0:
            sec = max(0.0, min(sec, effective_dur))
        else:
            sec = max(0.0, sec)
        ok, img = self._video_engine.seek_time(sec)
        if ok and img:
            self._current_frame_img = img
            self._current_sec = sec
            self._render_current_frame()
            self._update_time_labels()
            self._update_crop_preview()
        if self._vlc_player:
            self._vlc_player.set_time(sec)

    def _render_current_frame(self):
        if not self._current_frame_img:
            return
        if not hasattr(self, "_canvas"):
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
        if self._current_frame_img and self._active_display_mode == "canvas":
            self._render_current_frame()

    def _update_time_labels(self):
        dur = self._video_engine.duration_sec if self._video_engine else (self._vlc_player.get_duration() if self._vlc_player else 0.0)
        cur_str = srt_utils.sec_to_ts(self._current_sec)
        total_str = srt_utils.sec_to_ts(dur)
        time_text = f"{cur_str} / {total_str}"

        if hasattr(self, "_time_label"):
            self._time_label.configure(text=time_text)
        if hasattr(self, "_player_time_label"):
            self._player_time_label.configure(text=time_text)
        if hasattr(self, "_fs_time_label") and self._fs_time_label:
            try:
                if self._fs_time_label.winfo_exists():
                    self._fs_time_label.configure(text=time_text)
            except Exception:
                pass

        if not self._is_scrubbing:
            if hasattr(self, "_time_slider"):
                self._time_slider.set(self._current_sec)
            if hasattr(self, "_player_time_slider"):
                self._player_time_slider.set(self._current_sec)
            if hasattr(self, "_fs_time_slider") and self._fs_time_slider:
                try:
                    if self._fs_time_slider.winfo_exists():
                        self._fs_time_slider.set(self._current_sec)
                except Exception:
                    pass

        self._sync_subtitle_highlight()

    def _on_slider_change(self, val: float):
        self._is_scrubbing = True
        val_f = float(val)
        if self._is_playing and self._vlc_player:
            self._vlc_player.set_time(val_f)
            self._current_sec = val_f
            self._update_time_labels()
        elif self._is_fullscreen and self._vlc_player:
            self._vlc_player.set_time(val_f)
            self._current_sec = val_f
            self._update_time_labels()
        elif self._video_engine:
            self._seek_to(val_f)

    def _on_slider_release(self, _event):
        self._is_scrubbing = False
        if self._is_fullscreen and hasattr(self, "_fs_bar_visible") and self._fs_bar_visible:
            if hasattr(self, "_fullscreen_win") and self._fullscreen_win:
                if hasattr(self, "_fs_bar_timer") and self._fs_bar_timer:
                    try:
                        self._fullscreen_win.after_cancel(self._fs_bar_timer)
                    except Exception:
                        pass
                self._fs_bar_timer = self._fullscreen_win.after(2000, self._hide_fs_bar)

    def _toggle_play(self):
        if not self._video_engine:
            return
        if self._is_playing:
            self._stop_play()
        else:
            if self._video_engine and self._video_engine.duration_sec > 0 and self._current_sec >= self._video_engine.duration_sec - 0.05:
                self._seek_to(0.0)
            self._start_play()

    def _start_play(self):
        if not self._video_engine:
            return
        self._is_playing = True
        if hasattr(self, "_play_btn"):
            self._play_btn.configure(text="⏸ 暫停")
        if hasattr(self, "_player_play_btn"):
            self._player_play_btn.configure(text="⏸ 暫停")
        if hasattr(self, "_fs_play_btn") and self._fs_play_btn:
            try:
                if self._fs_play_btn.winfo_exists():
                    self._fs_play_btn.configure(text="⏸ 暫停")
            except Exception:
                pass

        if self._vlc_player:
            if self._is_fullscreen and hasattr(self, "_fullscreen_frame") and self._fullscreen_frame:
                self._vlc_player.bind_window(self._fullscreen_frame.winfo_id())
            elif self._active_tab_name in ("  影片截圖視窗  ", "  翻譯字幕執行視窗  ", "  影片與字幕任務  ", "  執行  "):
                self._show_vlc_display()
            else:
                if hasattr(self, "_player_vlc_frame"):
                    self._vlc_player.bind_window(self._player_vlc_frame.winfo_id())
                self._active_display_mode = "vlc"

            self._vlc_player.set_time(self._current_sec)
            self._vlc_player.play()
            self._vlc_play_tick()
        else:
            self._show_canvas_display()
            self._play_tick()

    def _stop_play(self):
        self._is_playing = False
        if self._play_after_id:
            self.after_cancel(self._play_after_id)
            self._play_after_id = None
        if hasattr(self, "_play_btn"):
            self._play_btn.configure(text="▶ 播放")
        if hasattr(self, "_player_play_btn"):
            self._player_play_btn.configure(text="▶ 播放")
        if hasattr(self, "_fs_play_btn") and self._fs_play_btn:
            try:
                if self._fs_play_btn.winfo_exists():
                    self._fs_play_btn.configure(text="▶ 播放")
            except Exception:
                pass

        if self._vlc_player:
            vlc_t = self._vlc_player.get_time()
            self._vlc_player.pause()
            if vlc_t > 0:
                self._current_sec = vlc_t

        if self._active_tab_name in ("  影片截圖視窗  ", "  翻譯字幕執行視窗  ", "  影片與字幕任務  ", "  執行  "):
            self._show_canvas_display()
            if self._video_engine:
                self._seek_to(self._current_sec)
        else:
            self._active_display_mode = "canvas"
            if self._video_engine:
                self._seek_to(self._current_sec)

    def _vlc_play_tick(self):
        if not self._is_playing or not self._vlc_player:
            return

        t = self._vlc_player.get_time()

        # Dynamic fallback duration if VideoEngine has <= 0 (e.g. MPEG-TS stream)
        if self._video_engine and self._video_engine.duration_sec <= 0:
            vlc_dur = self._vlc_player.get_duration()
            if vlc_dur > 0:
                self._video_engine.duration_sec = vlc_dur
                dur_max = max(1.0, vlc_dur)
                if hasattr(self, "_time_slider"):
                    self._time_slider.configure(to=dur_max)
                if hasattr(self, "_player_time_slider"):
                    self._player_time_slider.configure(to=dur_max)
                self._sync_player_ui()

        dur = self._video_engine.duration_sec if (self._video_engine and self._video_engine.duration_sec > 0) else self._vlc_player.get_duration()

        if t > 0:
            self._current_sec = t
            self._update_time_labels()

        if dur > 0 and t >= dur - 0.1:
            self._stop_play()
            return

        self._play_after_id = self.after(50, self._vlc_play_tick)

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

    def _schedule_save_volume(self, vol: int):
        """Debounce writing volume changes to settings.json (500ms delay)."""
        if hasattr(self, "_vol_save_timer") and self._vol_save_timer:
            try:
                self.after_cancel(self._vol_save_timer)
            except Exception:
                pass
            self._vol_save_timer = None

        def _do_save():
            self._vol_save_timer = None
            try:
                config.patch_setting("last_volume", vol)
            except Exception:
                pass

        self._vol_save_timer = self.after(500, _do_save)

    def _on_volume_change(self, val):
        vol = int(float(val))
        if hasattr(self, "_volume_label"):
            self._volume_label.configure(text=f"{vol}%")
        if hasattr(self, "_player_volume_label"):
            self._player_volume_label.configure(text=f"{vol}%")
        if hasattr(self, "_volume_slider"):
            self._volume_slider.set(vol)
        if hasattr(self, "_player_volume_slider"):
            self._player_volume_slider.set(vol)

        if self._vlc_player:
            self._vlc_player.set_volume(vol)
        if vol == 0:
            self._is_muted = True
            if hasattr(self, "_mute_btn"):
                self._mute_btn.configure(text="🔇")
            if hasattr(self, "_player_mute_btn"):
                self._player_mute_btn.configure(text="🔇")
        else:
            self._is_muted = False
            if hasattr(self, "_mute_btn"):
                self._mute_btn.configure(text="🔊")
            if hasattr(self, "_player_mute_btn"):
                self._player_mute_btn.configure(text="🔊")

        self._settings["last_volume"] = vol
        self._schedule_save_volume(vol)

    def _toggle_mute(self):
        if self._vlc_player:
            is_muted = self._vlc_player.toggle_mute()
            self._is_muted = is_muted
            vol = self._vlc_player.get_volume()
            self._volume_var.set(vol)
            if hasattr(self, "_volume_label"):
                self._volume_label.configure(text=f"{vol}%")
            if hasattr(self, "_player_volume_label"):
                self._player_volume_label.configure(text=f"{vol}%")
            if hasattr(self, "_volume_slider"):
                self._volume_slider.set(vol)
            if hasattr(self, "_player_volume_slider"):
                self._player_volume_slider.set(vol)

            icon = "🔇" if is_muted else "🔊"
            if hasattr(self, "_mute_btn"):
                self._mute_btn.configure(text=icon)
            if hasattr(self, "_player_mute_btn"):
                self._player_mute_btn.configure(text=icon)
            self._settings["last_volume"] = vol
            self._schedule_save_volume(vol)
        else:
            self._is_muted = not self._is_muted
            icon = "🔇" if self._is_muted else "🔊"
            if hasattr(self, "_mute_btn"):
                self._mute_btn.configure(text=icon)
            if hasattr(self, "_player_mute_btn"):
                self._player_mute_btn.configure(text=icon)

    def _on_rate_change(self, val: str):
        try:
            rate = float(val.replace("x", "").strip())
            if self._vlc_player:
                self._vlc_player.set_rate(rate)
            if hasattr(self, "_rate_menu"):
                self._rate_menu.set(val)
            if hasattr(self, "_player_rate_menu"):
                self._player_rate_menu.set(val)
        except Exception:
            pass

    def _update_sub_status_label(self):
        render_on = self._sub_render_var.get() if hasattr(self, "_sub_render_var") else True
        render_suffix = "" if render_on else " [渲染已關閉]"
        if self._srt_path and os.path.isfile(self._srt_path):
            name = Path(self._srt_path).name
            count = len(self._srt_entries)
            text = f"字幕：已掛載 ({name} · {count} 條){render_suffix}"
            color = ("#0a7d2c", "#5ec97a") if render_on else ("#b8860b", "#e0a820")
        else:
            text = f"字幕：未掛載{render_suffix}"
            color = ("gray50", "gray60")

        if hasattr(self, "_sub_status_label"):
            self._sub_status_label.configure(text=text, text_color=color)
        if hasattr(self, "_player_sub_status_label"):
            self._player_sub_status_label.configure(text=text, text_color=color)

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
            if self._vlc_player:
                self._vlc_player.set_time(self._current_sec)

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

        # Video tab buttons
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

        # Player tab buttons
        if hasattr(self, "_player_btn_step_back_large"):
            self._player_btn_step_back_large.configure(text=f"⏪ -{b_large_sec:g}s")
        if hasattr(self, "_player_btn_step_back_small"):
            self._player_btn_step_back_small.configure(text=f"⏪ -{b_small_sec:g}s")
        if hasattr(self, "_player_btn_step_back_frame"):
            self._player_btn_step_back_frame.configure(text=f"◀ -{b_frames}幀")
        if hasattr(self, "_player_btn_step_forward_frame"):
            self._player_btn_step_forward_frame.configure(text=f"▶ +{f_frames}幀")
        if hasattr(self, "_player_btn_step_forward_small"):
            self._player_btn_step_forward_small.configure(text=f"⏩ +{f_small_sec:g}s")
        if hasattr(self, "_player_btn_step_forward_large"):
            self._player_btn_step_forward_large.configure(text=f"⏩ +{f_large_sec:g}s")

        if hasattr(self, "_player_btn_capture"):
            cap_key = config.get_quick_capture_key_display(self._settings)
            self._player_btn_capture.configure(text=f"📷 截圖至素材庫 ({cap_key})")


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
        if hasattr(self, "_start_var"):
            self._start_var.set(srt_utils.sec_to_ts(self._start_sec))
        if hasattr(self, "_end_var"):
            self._end_var.set(srt_utils.sec_to_ts(self._end_sec))
        dur = max(0.0, self._end_sec - self._start_sec)
        if hasattr(self, "_dur_disp_label"):
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
            if self._current_frame_img:
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

    def _get_crop_image(self, source_override: Optional[Image.Image] = None) -> Image.Image:
        source_img = source_override or self._current_frame_img or self._start_frame_img
        if not source_img:
            return Image.new("RGB", (320, 180), color=(30, 30, 30))
        if self._crop_box:
            x1, y1, x2, y2 = self._crop_box
            return source_img.crop((x1, y1, x2, y2))
        return source_img

    def _update_crop_preview(self):
        if not hasattr(self, "_crop_thumb_canvas"):
            return
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
    #  Task Management & Quick Capture (v2.01)
    # ------------------------------------------------------------------ #
    def _quick_capture(self):
        if not self._video_engine:
            messagebox.showinfo("提示", "請先載入影片再進行截圖。")
            return

        target_frame = self._current_frame_img
        if not target_frame and self._video_engine:
            ok, frame = self._video_engine.seek_time(self._current_sec)
            if ok and frame:
                target_frame = frame
                self._current_frame_img = frame

        img = None
        if self._crop_box:
            img = self._get_crop_image(source_override=target_frame)
        elif target_frame:
            img = target_frame.copy()
        else:
            img = Image.new("RGB", (640, 360), color=(30, 30, 30))

        is_crop = bool(self._crop_box)
        saved_path = capture_manager.save_captured_image(
            img=img,
            video_path=self._video_path,
            sec=self._current_sec,
            is_crop=is_crop,
            crop_box=self._crop_box,
            target_dir=getattr(self, "_current_input_dir", None),
        )

        msg = f"📷 已截圖並儲存至：{saved_path.name}"
        if hasattr(self, "_log_line"):
            self._log_line(msg)
        if hasattr(self, "_player_video_spec_label"):
            self._player_video_spec_label.configure(text=f"已截圖：{saved_path.name}")
            self.after(3000, lambda: self._sync_player_ui() if hasattr(self, "_sync_player_ui") else None)

        if hasattr(self, "_refresh_p2p_gallery"):
            self._refresh_p2p_gallery()

    def _capture_to_p2p_gallery(self):
        r"""Capture current ROI (or full frame) directly into \captured for P2P generation,
        WITHOUT creating a subtitle OCR task. Prompts user to jump to P2P Editor."""
        if not self._video_engine:
            messagebox.showinfo("提示", "請先載入影片再進行截圖。")
            return

        target_frame = self._current_frame_img
        if not target_frame and self._video_engine:
            ok, frame = self._video_engine.seek_time(self._current_sec)
            if ok and frame:
                target_frame = frame
                self._current_frame_img = frame

        img = None
        if self._crop_box:
            img = self._get_crop_image(source_override=target_frame)
        elif target_frame:
            img = target_frame.copy()
        else:
            img = Image.new("RGB", (640, 360), color=(30, 30, 30))

        is_crop = bool(self._crop_box)
        saved_path = capture_manager.save_captured_image(
            img=img,
            video_path=self._video_path,
            sec=self._current_sec,
            is_crop=is_crop,
            crop_box=self._crop_box,
            target_dir=getattr(self, "_current_input_dir", None),
        )

        msg = f"🎨 已截取範圍並存入圖生圖素材庫：{saved_path.name}"
        if hasattr(self, "_log_line"):
            self._log_line(msg)
        if hasattr(self, "_player_video_spec_label"):
            self._player_video_spec_label.configure(text=f"已截圖至素材庫：{saved_path.name}")
            self.after(3000, lambda: self._sync_player_ui() if hasattr(self, "_sync_player_ui") else None)
        if hasattr(self, "_video_spec_label"):
            self._video_spec_label.configure(text=f"🎨 已存入素材庫：{saved_path.name}")
            self.after(3000, lambda: self._sync_video_tab_ui() if hasattr(self, "_sync_video_tab_ui") else None)

        if hasattr(self, "_refresh_p2p_gallery"):
            self._refresh_p2p_gallery()
        if hasattr(self, "_refresh_capture_gallery"):
            self._refresh_capture_gallery()
        if hasattr(self, "_set_active_p2p_image"):
            self._set_active_p2p_image(str(saved_path))

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

        crop_img = self._get_crop_image(source_override=self._start_frame_img)

        # v2.06: Save captured crop image into \ocr2srt directory
        saved_img_path = None
        try:
            saved_p = capture_manager.save_captured_image(
                img=crop_img,
                video_path=self._video_path,
                sec=self._start_sec,
                end_sec=self._end_sec,
                is_crop=bool(self._crop_box),
                crop_box=self._crop_box,
                category="ocr",
                target_dir=self._current_ocr_dir,
            )
            saved_img_path = str(saved_p)
        except Exception:
            pass

        task = SubtitleTask(
            id=self._next_task_id,
            start_sec=self._start_sec,
            end_sec=self._end_sec,
            start_frame_img=self._start_frame_img.copy(),
            crop_box=self._crop_box,
            crop_img=crop_img.copy(),
            status="待處理",
            image_path=saved_img_path,
        )
        self._next_task_id += 1
        self._tasks.append(task)
        self._render_tasks_list()

    def _clear_tasks(self):
        if not self._tasks:
            return
        if messagebox.askyesno("清空任務", "確定要清空所有已建立的任務嗎？截圖將移至歷史暫存備份。"):
            ocr_drop_dir = config.resolve_ocr_drop_dir(self._settings)
            for t in self._tasks:
                if t.image_path and os.path.isfile(t.image_path):
                    capture_manager.safe_move_to_drop(t.image_path, ocr_drop_dir)
            self._tasks.clear()
            self._render_tasks_list()
            if hasattr(self, "_render_ocr_exec_tasks"):
                self._render_ocr_exec_tasks()
            if hasattr(self, "_render_ocr_output_textbox"):
                self._render_ocr_output_textbox()

    def _delete_task(self, task_id: int):
        target_task = next((t for t in self._tasks if t.id == task_id), None)
        if target_task and target_task.image_path and os.path.isfile(target_task.image_path):
            ocr_drop_dir = config.resolve_ocr_drop_dir(self._settings)
            capture_manager.safe_move_to_drop(target_task.image_path, ocr_drop_dir)
        self._tasks = [t for t in self._tasks if t.id != task_id]
        self._render_tasks_list()
        if hasattr(self, "_render_ocr_exec_tasks"):
            self._render_ocr_exec_tasks()
        if hasattr(self, "_render_ocr_output_textbox"):
            self._render_ocr_output_textbox()

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
        if not hasattr(self, "_task_scroll"):
            return
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

            time_row = ctk.CTkFrame(info_frame, fg_color="transparent")
            time_row.pack(fill="x", pady=(0, 2))

            ctk.CTkLabel(time_row, text=f"#{i}", font=FONT_MONO, width=28, anchor="w").pack(side="left")
            start_e = ctk.CTkEntry(time_row, width=88, height=22, font=FONT_SMALL)
            start_e.insert(0, task.start_ts)
            start_e.pack(side="left", padx=(0, 2))

            ctk.CTkLabel(time_row, text="→", font=FONT_SMALL).pack(side="left", padx=2)

            end_e = ctk.CTkEntry(time_row, width=88, height=22, font=FONT_SMALL)
            end_e.insert(0, task.end_ts)
            end_e.pack(side="left", padx=(2, 4))

            dur_lbl = ctk.CTkLabel(time_row, text=f"({task.duration:.2f}s)", font=FONT_SMALL, text_color=("gray40", "#9aa0a6"))
            dur_lbl.pack(side="left")

            def make_task_time_handler(t=task, se=start_e, ee=end_e, dl=dur_lbl):
                def _handler(_event=None):
                    self._on_task_time_edit(t, se.get(), ee.get(), se, ee, dl)
                return _handler

            h = make_task_time_handler()
            start_e.bind("<FocusOut>", h)
            start_e.bind("<Return>", h)
            end_e.bind("<FocusOut>", h)
            end_e.bind("<Return>", h)

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

    def _on_task_time_edit(self, task: SubtitleTask, start_str: str, end_str: str, start_e, end_e, dur_lbl):
        st = srt_utils.flexible_ts_to_sec(start_str)
        et = srt_utils.flexible_ts_to_sec(end_str)
        if st is not None and et is not None and et > st:
            task.start_sec = st
            task.end_sec = et
            dur_lbl.configure(text=f"({task.duration:.2f}s)")
            if hasattr(self, "_render_ocr_output_textbox"):
                self._render_ocr_output_textbox()
            if getattr(self, "_selected_ocr_exec_task", None) == task and hasattr(self, "_render_ocr_exec_preview"):
                self._render_ocr_exec_preview()
        else:
            start_e.delete(0, "end")
            start_e.insert(0, task.start_ts)
            end_e.delete(0, "end")
            end_e.insert(0, task.end_ts)

        if hasattr(self, "_render_ocr_exec_tasks"):
            self._render_ocr_exec_tasks()
        if hasattr(self, "_render_ocr_output_textbox"):
            self._render_ocr_output_textbox()

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
        self._load_srt(p)

    def _load_srt(self, p: str):
        if not p or not os.path.exists(p):
            return
        self._srt_path = str(p)
        self._settings["last_srt_path"] = self._srt_path
        if hasattr(self, "_srt_var"):
            self._srt_var.set(self._srt_path)
        try:
            self._srt_entries = srt_utils.parse_srt(self._srt_path)
        except Exception as exc:
            if hasattr(self, "_srt_var"):
                self._srt_var.set(f"解析失敗：{exc}")
            self._srt_entries = []

        if hasattr(self, "_player_srt_var"):
            self._player_srt_var.set(self._srt_path)

        if self._vlc_player and self._srt_path:
            self._vlc_player.set_subtitle_file(self._srt_path)
        self._populate_sub_treeview()
        self._update_sub_status_label()

    def _clear_srt(self):
        self._srt_path = ""
        self._srt_entries = []
        if hasattr(self, "_srt_var"):
            self._srt_var.set("（未選擇，將直接匯出新 SRT 檔案）")
        if hasattr(self, "_player_srt_var"):
            self._player_srt_var.set("（未選擇字幕檔案）")
        if self._vlc_player:
            self._vlc_player.clear_subtitle()
        self._populate_sub_treeview()
        self._update_sub_status_label()

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

        # Section 3: Default Storage and Material Folders (v2.05 & v2.06)
        sec3_frame = ctk.CTkFrame(scroll)
        sec3_frame.pack(fill="x", pady=(0, 14))

        ctk.CTkLabel(sec3_frame, text="預設素材庫與生成資料夾設定", font=FONT_SUBTITLE, anchor="w").pack(
            fill="x", padx=12, pady=(10, 4)
        )
        ctk.CTkLabel(sec3_frame, text="自訂字幕素材庫 (OCR)、截圖素材庫與 AI 生成圖片的預設儲存路徑，留空則使用程式目錄下的 \\ocr2srt, \\captured 與 \\generated。",
                     font=FONT_SMALL, text_color=("gray40", "#9aa0a6"), anchor="w").pack(
            fill="x", padx=12, pady=(0, 8)
        )

        f_grid = ctk.CTkFrame(sec3_frame, fg_color="transparent")
        f_grid.pack(fill="x", padx=12, pady=(0, 8))

        ctk.CTkLabel(f_grid, text="預設字幕素材庫 (OCR)：", font=FONT_BODY, width=170, anchor="w").grid(row=0, column=0, padx=(0, 4), pady=4, sticky="w")
        self._s_ocr_dir_var = ctk.StringVar(value=self._settings.get("storage_folders", {}).get("default_ocr_dir", ""))
        self._s_ocr_dir_entry = ctk.CTkEntry(f_grid, textvariable=self._s_ocr_dir_var, placeholder_text="預設: \\ocr2srt", font=FONT_SMALL)
        self._s_ocr_dir_entry.grid(row=0, column=1, padx=(0, 6), pady=4, sticky="ew")
        ctk.CTkButton(f_grid, text="📂 瀏覽...", width=75, height=26, font=FONT_SMALL,
                      command=self._browse_default_ocr_dir).grid(row=0, column=2, padx=(0, 4), pady=4)

        ctk.CTkLabel(f_grid, text="預設截圖素材庫 (P2P)：", font=FONT_BODY, width=170, anchor="w").grid(row=1, column=0, padx=(0, 4), pady=4, sticky="w")
        self._s_cap_dir_var = ctk.StringVar(value=self._settings.get("storage_folders", {}).get("default_captured_dir", ""))
        self._s_cap_dir_entry = ctk.CTkEntry(f_grid, textvariable=self._s_cap_dir_var, placeholder_text="預設: \\captured", font=FONT_SMALL)
        self._s_cap_dir_entry.grid(row=1, column=1, padx=(0, 6), pady=4, sticky="ew")
        ctk.CTkButton(f_grid, text="📂 瀏覽...", width=75, height=26, font=FONT_SMALL,
                      command=self._browse_default_captured_dir).grid(row=1, column=2, padx=(0, 4), pady=4)

        ctk.CTkLabel(f_grid, text="預設 AI 生成庫：", font=FONT_BODY, width=170, anchor="w").grid(row=2, column=0, padx=(0, 4), pady=4, sticky="w")
        self._s_gen_dir_var = ctk.StringVar(value=self._settings.get("storage_folders", {}).get("default_generated_dir", ""))
        self._s_gen_dir_entry = ctk.CTkEntry(f_grid, textvariable=self._s_gen_dir_var, placeholder_text="預設: \\generated", font=FONT_SMALL)
        self._s_gen_dir_entry.grid(row=2, column=1, padx=(0, 6), pady=4, sticky="ew")
        ctk.CTkButton(f_grid, text="📂 瀏覽...", width=75, height=26, font=FONT_SMALL,
                      command=self._browse_default_generated_dir).grid(row=2, column=2, padx=(0, 4), pady=4)

        f_grid.grid_columnconfigure(1, weight=1)

        f_btn_row = ctk.CTkFrame(sec3_frame, fg_color="transparent")
        f_btn_row.pack(fill="x", padx=12, pady=(4, 10))
        ctk.CTkButton(f_btn_row, text="儲存資料夾設定", width=130, height=30, font=FONT_BODY,
                      command=self._save_storage_folder_settings).pack(side="left")
        ctk.CTkButton(f_btn_row, text="恢復預設路徑", width=110, height=30, font=FONT_BODY,
                      fg_color="#555", hover_color="#444",
                      command=self._reset_storage_folder_settings).pack(side="left", padx=(8, 0))
        self._storage_status_label = ctk.CTkLabel(f_btn_row, text="", font=FONT_SMALL,
                                                  text_color=("#0a7d2c", "#5ec97a"), anchor="w")
        self._storage_status_label.pack(side="left", padx=(12, 0))

    def _browse_default_ocr_dir(self):
        chosen = filedialog.askdirectory(title="選擇預設字幕素材庫 (OCR) 資料夾")
        if chosen:
            self._s_ocr_dir_var.set(chosen)

    def _browse_default_captured_dir(self):
        chosen = filedialog.askdirectory(title="選擇預設截圖素材庫資料夾")
        if chosen:
            self._s_cap_dir_var.set(chosen)

    def _browse_default_generated_dir(self):
        chosen = filedialog.askdirectory(title="選擇預設 AI 生成圖庫資料夾")
        if chosen:
            self._s_gen_dir_var.set(chosen)

    def _save_storage_folder_settings(self):
        ocr_val = self._s_ocr_dir_var.get().strip() if hasattr(self, "_s_ocr_dir_var") else ""
        cap_val = self._s_cap_dir_var.get().strip()
        gen_val = self._s_gen_dir_var.get().strip()
        sf = self._settings.setdefault("storage_folders", {})
        sf["default_ocr_dir"] = ocr_val
        sf["default_captured_dir"] = cap_val
        sf["default_generated_dir"] = gen_val
        config.save_settings(self._settings)
        self._current_ocr_dir = config.resolve_ocr_dir(self._settings)
        self._current_input_dir = str(config.resolve_captured_dir(self._settings))
        self._current_output_dir = str(config.resolve_generated_dir(self._settings))
        if hasattr(self, "_populate_p2p_input_dir_menu"):
            self._populate_p2p_input_dir_menu()
        if hasattr(self, "_populate_p2p_output_dir_menu"):
            self._populate_p2p_output_dir_menu()
        if hasattr(self, "_refresh_p2p_gallery"):
            self._refresh_p2p_gallery()
        if hasattr(self, "_refresh_p2p_output_gallery"):
            self._refresh_p2p_output_gallery()
        if hasattr(self, "_storage_status_label"):
            self._storage_status_label.configure(text="✅ 資料夾設定已儲存並生效")

    def _reset_storage_folder_settings(self):
        if hasattr(self, "_s_ocr_dir_var"):
            self._s_ocr_dir_var.set("")
        self._s_cap_dir_var.set("")
        self._s_gen_dir_var.set("")
        self._save_storage_folder_settings()
        if hasattr(self, "_storage_status_label"):
            self._storage_status_label.configure(text="已恢復預設資料夾路徑")

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
        self._settings["keybindings"] = self._keybindings
        config.patch_setting("keybindings", self._keybindings)
        if self._recording_action == action_id:
            self._recording_action = None
        self._render_keybindings_table()
        self._refresh_step_buttons()
        if hasattr(self, "_render_p2p_gallery"):
            self._render_p2p_gallery()
        if hasattr(self, "_kb_status_label"):
            self._kb_status_label.configure(text="已清除快捷鍵")

    def _reset_keybindings(self):
        self._keybindings = dict(config.DEFAULT_KEYBINDINGS)
        self._settings["keybindings"] = self._keybindings
        config.patch_setting("keybindings", self._keybindings)
        self._recording_action = None
        self._render_keybindings_table()
        self._refresh_step_buttons()
        if hasattr(self, "_render_p2p_gallery"):
            self._render_p2p_gallery()
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
            self._settings["keybindings"] = self._keybindings
            config.patch_setting("keybindings", self._keybindings)
            self._recording_action = None
            self._render_keybindings_table()
            self._refresh_step_buttons()
            if hasattr(self, "_render_p2p_gallery"):
                self._render_p2p_gallery()
            if hasattr(self, "_kb_status_label"):
                self._kb_status_label.configure(text=f"已將快捷鍵設定為：{format_key_display(key)}")
            return "break"

        # Check Escape for fullscreen
        if event.keysym in ("Escape", "Esc") and self._is_fullscreen:
            self._toggle_fullscreen()
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
        if matched_action == "toggle_fullscreen":
            self._toggle_fullscreen()
        elif matched_action == "hot_reload_sub":
            self._hot_reload_subtitle()
        elif matched_action == "play_pause":
            self._toggle_play()
        elif matched_action == "step_back_large":
            if self._active_tab_name == "  影音播放器  ":
                self._on_player_step_back_large()
            else:
                self._on_step_back_large()
        elif matched_action == "step_back_small":
            if self._active_tab_name == "  影音播放器  ":
                self._on_player_step_back_small()
            else:
                self._on_step_back_small()
        elif matched_action == "step_back_frame":
            if self._active_tab_name == "  影音播放器  ":
                self._on_player_step_back_frame()
            else:
                self._on_step_back_frame()
        elif matched_action == "step_forward_frame":
            if self._active_tab_name == "  影音播放器  ":
                self._on_player_step_forward_frame()
            else:
                self._on_step_forward_frame()
        elif matched_action == "step_forward_small":
            if self._active_tab_name == "  影音播放器  ":
                self._on_player_step_forward_small()
            else:
                self._on_step_forward_small()
        elif matched_action == "step_forward_large":
            if self._active_tab_name == "  影音播放器  ":
                self._on_player_step_forward_large()
            else:
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
        elif matched_action == "quick_capture":
            self._quick_capture()

        return "break"

    # ------------------------------------------------------------------ #
    #  Tab 3: API Settings (v1.07)
    # ------------------------------------------------------------------ #
    def _build_api_tab(self, parent):
        ctk.CTkLabel(parent, text="API 設定", font=FONT_TITLE, anchor="w").pack(
            fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(parent, text="可自訂多組供應商與模型，金鑰將自動寫入 .env 檔案中。",
                     font=FONT_SMALL, text_color=("gray40", "#9aa0a6"), anchor="w").pack(
            fill="x", padx=14)

        # 常用範本快速填入
        preset_row = ctk.CTkFrame(parent, fg_color="transparent")
        preset_row.pack(fill="x", padx=14, pady=(8, 2))
        ctk.CTkLabel(preset_row, text="常用範本快速填入：", font=FONT_BODY).pack(side="left")
        preset_names = ["（選擇常用範本…）"] + [p["name"] for p in config.PROVIDER_PRESETS]
        self._preset_menu = ctk.CTkOptionMenu(
            preset_row, width=220, height=30, font=FONT_BODY,
            values=preset_names, command=self._on_select_preset
        )
        self._preset_menu.pack(side="left", padx=(6, 12))

        ctk.CTkLabel(
            preset_row,
            text="⚠️ 提示：範本僅供快速填寫參考，端點 URL、金鑰與支援模型請以各供應商官方最新文件為準。",
            font=FONT_SMALL, text_color=("#b45309", "#f59e0b")
        ).pack(side="left")

        # 供應商切換列
        switch_row = ctk.CTkFrame(parent, fg_color="transparent")
        switch_row.pack(fill="x", padx=14, pady=(6, 6))
        ctk.CTkLabel(switch_row, text="供應商：", font=FONT_BODY).pack(side="left")
        self._provider_menu = ctk.CTkOptionMenu(switch_row, width=200, height=30,
                                                font=FONT_BODY,
                                                values=["（無）"],
                                                command=self._on_provider_switch)
        self._provider_menu.pack(side="left", padx=(6, 10))

        ctk.CTkButton(switch_row, text="設為預設供應商", width=120, height=30, font=FONT_BODY,
                      command=self._set_default_provider).pack(side="left", padx=(0, 6))
        ctk.CTkButton(switch_row, text="刪除供應商", width=95, height=30, font=FONT_BODY,
                      fg_color="#b23b3b", hover_color="#8a2e2e",
                      command=self._delete_current_provider).pack(side="left", padx=(0, 6))
        ctk.CTkButton(switch_row, text="🎬 前往影片截圖視窗", width=155, height=30, font=FONT_SMALL,
                      command=lambda: self.tabs.set("  影片截圖視窗  ")).pack(side="right")

        # 供應商詳細資料表單
        form = ctk.CTkFrame(parent)
        form.pack(fill="both", expand=True, padx=14, pady=(0, 6))
        form.grid_columnconfigure(1, weight=1)

        self._p_name = ctk.StringVar()
        self._p_url = ctk.StringVar()
        self._p_key = ctk.StringVar()
        self._current_provider_models: list[str] = []
        self._p_selected_model = ctk.StringVar()

        # Row 0: 名稱
        ctk.CTkLabel(form, text="名稱：", font=FONT_BODY, anchor="e").grid(
            row=0, column=0, sticky="e", padx=(10, 8), pady=6
        )
        ctk.CTkEntry(form, textvariable=self._p_name, font=FONT_BODY).grid(
            row=0, column=1, sticky="ew", padx=(0, 10), pady=6
        )

        # Row 1: API 網址
        ctk.CTkLabel(form, text="API 網址：", font=FONT_BODY, anchor="e").grid(
            row=1, column=0, sticky="e", padx=(10, 8), pady=6
        )
        ctk.CTkEntry(form, textvariable=self._p_url, font=FONT_BODY).grid(
            row=1, column=1, sticky="ew", padx=(0, 10), pady=6
        )

        # Row 2: API 金鑰 + 檢視/編輯 .env 按鈕
        ctk.CTkLabel(form, text="API 金鑰：", font=FONT_BODY, anchor="e").grid(
            row=2, column=0, sticky="e", padx=(10, 8), pady=6
        )
        key_row = ctk.CTkFrame(form, fg_color="transparent")
        key_row.grid(row=2, column=1, sticky="ew", padx=(0, 10), pady=6)
        ctk.CTkEntry(key_row, textvariable=self._p_key, font=FONT_BODY, show="•").pack(
            side="left", fill="x", expand=True
        )
        ctk.CTkButton(
            key_row, text="檢視/編輯 .env (記事本)", width=175, height=28, font=FONT_SMALL,
            command=config.open_env_in_editor
        ).pack(side="left", padx=(8, 0))

        # Row 3: 金鑰提示
        self._key_hint = ctk.StringVar(value="")
        ctk.CTkLabel(
            form, textvariable=self._key_hint, font=FONT_SMALL,
            text_color=("gray40", "#9aa0a6"), anchor="w"
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=(10, 10), pady=(0, 4))

        # Row 4: 模型清單管理子區塊
        model_group = ctk.CTkFrame(form, fg_color=("gray92", "gray20"))
        model_group.grid(row=4, column=0, columnspan=2, sticky="ew", padx=10, pady=(6, 8))
        model_group.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(model_group, text="【模型清單管理】", font=FONT_BODY, anchor="w").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(6, 2)
        )

        ctk.CTkLabel(model_group, text="模型選擇：", font=FONT_BODY, anchor="e").grid(
            row=1, column=0, sticky="e", padx=(10, 8), pady=4
        )
        self._model_menu = ctk.CTkOptionMenu(
            model_group, width=280, height=28, font=FONT_BODY,
            values=["（無）"], variable=self._p_selected_model,
            command=self._on_model_menu_select
        )
        self._model_menu.grid(row=1, column=1, sticky="w", pady=4)

        model_btns = ctk.CTkFrame(model_group, fg_color="transparent")
        model_btns.grid(row=1, column=2, sticky="w", padx=(10, 10), pady=4)
        ctk.CTkButton(
            model_btns, text="設為目前使用", width=105, height=28, font=FONT_SMALL,
            command=self._set_current_model_active
        ).pack(side="left")
        ctk.CTkButton(
            model_btns, text="刪除此模型", width=90, height=28, font=FONT_SMALL,
            fg_color="#b23b3b", hover_color="#8a2e2e",
            command=self._delete_current_model
        ).pack(side="left", padx=(6, 0))

        ctk.CTkLabel(model_group, text="新增模型：", font=FONT_BODY, anchor="e").grid(
            row=2, column=0, sticky="e", padx=(10, 8), pady=(4, 8)
        )
        self._new_model_var = ctk.StringVar()
        new_model_entry = ctk.CTkEntry(
            model_group, textvariable=self._new_model_var, font=FONT_BODY,
            placeholder_text="輸入模型名稱，例如 deepseek/deepseek-chat"
        )
        new_model_entry.grid(row=2, column=1, sticky="ew", pady=(4, 8))
        ctk.CTkButton(
            model_group, text="＋ 加入模型", width=105, height=28, font=FONT_SMALL,
            command=self._add_model_to_current
        ).grid(row=2, column=2, sticky="w", padx=(10, 10), pady=(4, 8))

        # Bottom buttons
        btns = ctk.CTkFrame(parent, fg_color="transparent")
        btns.pack(fill="x", padx=14, pady=(0, 6))
        ctk.CTkButton(btns, text="新增空白供應商", width=120, height=30, font=FONT_BODY,
                      command=self._new_provider).pack(side="left")
        ctk.CTkButton(btns, text="儲存供應商與模型", width=140, height=30, font=FONT_BODY,
                      command=self._save_current_provider).pack(side="left", padx=(8, 0))
        ctk.CTkButton(btns, text="測試連線", width=110, height=30, font=FONT_BODY,
                      command=self._on_test_connection).pack(side="left", padx=(8, 0))

        self._test_status = ctk.StringVar(value="")
        self._test_label = ctk.CTkLabel(parent, textvariable=self._test_status,
                                        font=FONT_SMALL, anchor="w")
        self._test_label.pack(fill="x", padx=14, pady=(0, 6))

    def _provider_names(self) -> list[str]:
        return [p["name"] for p in self._settings.get("providers", [])]

    def _refresh_provider_menu(self, keep: str | None = None):
        names = self._provider_names() or ["（無）"]
        self._provider_menu.configure(values=names)
        sel = keep if keep in names else (names[0] if names != ["（無）"] else "（無）")
        self._provider_menu.set(sel)

    def _refresh_model_menu(self, keep: str | None = None):
        models = self._current_provider_models or ["（無）"]
        self._model_menu.configure(values=models)
        sel = keep if keep in models else (models[0] if models != ["（無）"] else "（無）")
        self._model_menu.set(sel)
        self._p_selected_model.set(sel)

    def _on_model_menu_select(self, val: str):
        self._p_selected_model.set(val)

    def _set_current_model_active(self):
        m = self._p_selected_model.get().strip()
        if not m or m == "（無）":
            return
        p_name = self._p_name.get().strip()
        if p_name:
            config.select_provider_model(p_name, m)
            self._settings = config.load_settings()
        self._test_status.set(f"已將「{m}」設為此供應商的使用模型。")
        self._test_label.configure(text_color=("#0a7d2c", "#5ec97a"))
        self._refresh_run_tab_summary()

    def _add_model_to_current(self):
        m = self._new_model_var.get().strip()
        if not m:
            return
        if m not in self._current_provider_models:
            self._current_provider_models.append(m)
        self._p_selected_model.set(m)
        self._refresh_model_menu(keep=m)
        self._new_model_var.set("")
        self._save_current_provider()

    def _delete_current_model(self):
        m = self._p_selected_model.get().strip()
        if not m or m == "（無）":
            return
        if len(self._current_provider_models) <= 1:
            messagebox.showwarning("無法刪除", "供應商至少需保留一個模型。")
            return
        self._current_provider_models = [x for x in self._current_provider_models if x != m]
        new_sel = self._current_provider_models[0]
        self._p_selected_model.set(new_sel)
        self._refresh_model_menu(keep=new_sel)
        self._save_current_provider()

    def _on_select_preset(self, choice: str):
        if choice.startswith("（"):
            return
        preset = next((p for p in config.PROVIDER_PRESETS if p["name"] == choice), None)
        if not preset:
            return
        self._p_name.set(preset["name"])
        self._p_url.set(preset["api_url"])
        env = config._env_var_name_for(preset["name"])
        existing_key = os.environ.get(env, "")
        self._p_key.set(existing_key)
        self._current_provider_models = list(preset.get("models", []))
        sel_m = preset.get("selected_model") or (self._current_provider_models[0] if self._current_provider_models else "")
        self._p_selected_model.set(sel_m)
        self._refresh_model_menu(keep=sel_m)
        self._key_hint.set(f"金鑰儲存於 .env：{env}")
        self._test_status.set(f"已帶入「{choice}」常用設定，請確認/填寫 API 金鑰後點擊儲存。")
        self._test_label.configure(text_color=("#0a7d2c", "#5ec97a"))
        self._preset_menu.set("（選擇常用範本…）")

    def _sync_api_form(self):
        if not hasattr(self, "_provider_menu"):
            return
        self._refresh_provider_menu()
        p = config.get_default_provider()
        if p:
            self._provider_menu.set(p["name"])
            self._load_provider_into_form(p)

    def _load_provider_into_form(self, p: dict):
        self._p_name.set(p.get("name", ""))
        self._p_url.set(p.get("api_url", ""))
        self._p_key.set(p.get("api_key", ""))
        models = list(p.get("models") or [p.get("model", "gpt-4o")])
        self._current_provider_models = models
        sel_m = p.get("selected_model") or (models[0] if models else "")
        self._p_selected_model.set(sel_m)
        self._refresh_model_menu(keep=sel_m)
        env = p.get("api_key_env") or ""
        self._key_hint.set(f"金鑰儲存於 .env：{env}" if env else "金鑰儲存於 .env")

    def _on_provider_switch(self, name: str):
        for p in self._settings.get("providers", []):
            if p.get("name") == name:
                self._load_provider_into_form(p)
                break

    def _form_to_provider(self) -> dict:
        models = list(self._current_provider_models)
        sel_m = self._p_selected_model.get().strip()
        if sel_m and sel_m not in models:
            models.append(sel_m)
        if not models:
            models = ["gpt-4o"]
        if not sel_m:
            sel_m = models[0]
        return {
            "name": self._p_name.get().strip(),
            "api_url": self._p_url.get().strip(),
            "api_key": self._p_key.get().strip(),
            "models": models,
            "selected_model": sel_m,
            "is_default": False,
        }

    def _new_provider(self):
        self._p_name.set("")
        self._p_url.set("https://api.openai.com/v1/chat/completions")
        self._p_key.set("")
        self._current_provider_models = ["gpt-4o"]
        self._p_selected_model.set("gpt-4o")
        self._refresh_model_menu(keep="gpt-4o")
        self._key_hint.set("填入名稱與金鑰後儲存，金鑰將寫入 .env（API_KEY_<名稱>）")

    def _save_current_provider(self):
        p = self._form_to_provider()
        if not p["name"]:
            messagebox.showwarning("缺少名稱", "請先填入供應商「名稱」。")
            return
        if not p["api_url"]:
            messagebox.showwarning("資料不完整", "API 網址為必填。")
            return
        for ex in self._settings.get("providers", []):
            if ex.get("name") == p["name"]:
                p["is_default"] = ex.get("is_default", False)
                break
        config.upsert_provider(p)
        self._settings = config.load_settings()
        self._refresh_provider_menu(keep=p["name"])
        self._refresh_prompt_tab_providers()
        self._refresh_run_tab_summary()
        self._test_status.set(f"已儲存供應商「{p['name']}」與模型設定。")
        self._test_label.configure(text_color=("#0a7d2c", "#5ec97a"))

    def _delete_current_provider(self):
        name = self._p_name.get().strip()
        if not name:
            return
        if not messagebox.askyesno("刪除供應商", f"確定刪除「{name}」？"):
            return
        config.delete_provider(name)
        self._settings = config.load_settings()
        self._sync_api_form()
        self._refresh_prompt_tab_providers()
        self._refresh_run_tab_summary()

    def _set_default_provider(self):
        name = self._p_name.get().strip()
        if not name:
            return
        p = self._form_to_provider()
        p["is_default"] = True
        config.upsert_provider(p)
        self._settings = config.load_settings()
        self._refresh_provider_menu(keep=name)
        self._refresh_run_tab_summary()

    def _on_test_connection(self):
        p = self._form_to_provider()
        if not p["api_url"] or not p["selected_model"]:
            messagebox.showwarning("資料不完整", "請先填入 API 網址並選擇模型。")
            return
        cfg = ProviderCfg.from_dict(p)
        self._test_status.set(f"測試連線至 {cfg.model}…")
        self._test_label.configure(text_color=("#555", "#9aa0a6"))

        def work():
            ok, msg = test_connection(cfg)
            self.after(0, lambda: self._show_test_result(ok, msg))

        threading.Thread(target=work, daemon=True).start()

    def _show_test_result(self, ok: bool, msg: str):
        self._test_status.set(("✅ " if ok else "❌ ") + msg)
        self._test_label.configure(text_color=("#0a7d2c", "#5ec97a") if ok else ("#b23b3b", "#ff8a80"))

    # ------------------------------------------------------------------ #
    #  Tab 3: Prompt Settings (v1.07)
    # ------------------------------------------------------------------ #
    def _build_prompt_tab(self, parent):
        ctk.CTkLabel(parent, text="Prompt 方案庫與翻譯設定", font=FONT_TITLE, anchor="w").pack(
            fill="x", padx=14, pady=(12, 4)
        )
        ctk.CTkLabel(
            parent,
            text="支援多組 Prompt 方案：可建立全域通用公版，或指定綁定某供應商模型的專用版本。",
            font=FONT_SMALL, text_color=("gray40", "#9aa0a6"), anchor="w"
        ).pack(fill="x", padx=14)

        # Profile selection & action buttons row
        top_row = ctk.CTkFrame(parent, fg_color="transparent")
        top_row.pack(fill="x", padx=14, pady=(8, 6))

        ctk.CTkLabel(top_row, text="方案選擇：", font=FONT_BODY).pack(side="left")
        self._prompt_menu_var = ctk.StringVar(value="（無）")
        self._prompt_display_map: dict[str, str] = {}
        self._prompt_menu = ctk.CTkOptionMenu(
            top_row, width=280, height=30, font=FONT_BODY,
            variable=self._prompt_menu_var,
            values=["（無）"],
            command=self._on_prompt_profile_switch
        )
        self._prompt_menu.pack(side="left", padx=(6, 10))

        ctk.CTkButton(top_row, text="新增方案", width=85, height=30, font=FONT_BODY,
                      command=self._new_prompt_profile).pack(side="left", padx=(0, 6))
        ctk.CTkButton(top_row, text="複製目前", width=85, height=30, font=FONT_BODY,
                      command=self._clone_prompt_profile).pack(side="left", padx=(0, 6))
        ctk.CTkButton(top_row, text="刪除方案", width=85, height=30, font=FONT_BODY,
                      fg_color="#b23b3b", hover_color="#8a2e2e",
                      command=self._delete_current_prompt_profile).pack(side="left", padx=(0, 6))
        ctk.CTkButton(top_row, text="設為預設公版", width=115, height=30, font=FONT_BODY,
                      command=self._set_current_as_active_global).pack(side="left", padx=(0, 6))

        # Profile details form
        form = ctk.CTkFrame(parent)
        form.pack(fill="both", expand=True, padx=14, pady=(0, 6))
        form.grid_columnconfigure(1, weight=1)

        self._current_prompt_id = ""
        self._prompt_name_var = ctk.StringVar()
        self._prompt_scope_var = ctk.StringVar(value="通用公版 (Global)")
        self._bound_provider_var = ctk.StringVar()
        self._bound_model_var = ctk.StringVar()

        # Row 0: 方案名稱
        ctk.CTkLabel(form, text="方案名稱：", font=FONT_BODY, anchor="e").grid(
            row=0, column=0, sticky="e", padx=(10, 8), pady=6
        )
        ctk.CTkEntry(form, textvariable=self._prompt_name_var, font=FONT_BODY).grid(
            row=0, column=1, sticky="ew", padx=(0, 10), pady=6
        )

        # Row 1: 適用類型
        ctk.CTkLabel(form, text="適用類型：", font=FONT_BODY, anchor="e").grid(
            row=1, column=0, sticky="e", padx=(10, 8), pady=6
        )
        self._prompt_scope_seg = ctk.CTkSegmentedButton(
            form, values=["通用公版 (Global)", "綁定特定模型 (Model-bound)"],
            variable=self._prompt_scope_var, command=self._on_prompt_scope_change,
            height=28, font=FONT_BODY
        )
        self._prompt_scope_seg.grid(row=1, column=1, sticky="w", padx=(0, 10), pady=6)

        # Row 2: 綁定設定 (Model-bound options)
        self._bound_frame = ctk.CTkFrame(form, fg_color=("gray92", "gray20"))
        self._bound_frame.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=4)
        ctk.CTkLabel(self._bound_frame, text="綁定供應商：", font=FONT_BODY).pack(side="left", padx=(10, 4), pady=4)
        self._bound_provider_menu = ctk.CTkOptionMenu(
            self._bound_frame, width=180, height=28, font=FONT_BODY,
            variable=self._bound_provider_var, command=self._on_bound_provider_change
        )
        self._bound_provider_menu.pack(side="left", padx=(0, 12), pady=4)

        ctk.CTkLabel(self._bound_frame, text="綁定模型：", font=FONT_BODY).pack(side="left", padx=(0, 4), pady=4)
        self._bound_model_menu = ctk.CTkOptionMenu(
            self._bound_frame, width=220, height=28, font=FONT_BODY,
            variable=self._bound_model_var
        )
        self._bound_model_menu.pack(side="left", padx=(0, 10), pady=4)
        self._bound_frame.grid_remove()  # hidden by default for global scope

        # Row 3: 目標語言
        lang_row = ctk.CTkFrame(form, fg_color="transparent")
        lang_row.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=6)
        ctk.CTkLabel(lang_row, text="目標語言：", font=FONT_BODY).pack(side="left")
        self._lang_var = ctk.StringVar(value=self._settings.get("target_lang", config.DEFAULT_TARGET_LANG))
        self._lang_menu = ctk.CTkOptionMenu(
            lang_row, width=160, height=28, font=FONT_BODY,
            values=config.TARGET_LANG_PRESETS, variable=self._lang_var
        )
        self._lang_menu.pack(side="left", padx=(6, 12))
        ctk.CTkLabel(lang_row, text="或自訂：", font=FONT_BODY).pack(side="left")
        self._lang_custom = ctk.StringVar(value="")
        entry = ctk.CTkEntry(lang_row, textvariable=self._lang_custom, width=160, font=FONT_BODY)
        entry.pack(side="left", padx=(6, 0))

        # Row 4: System Prompt
        ctk.CTkLabel(form, text="System Prompt", font=FONT_BODY, anchor="w").grid(
            row=4, column=0, columnspan=2, sticky="w", padx=10, pady=(6, 2)
        )
        self._sys_box = ctk.CTkTextbox(form, height=95, font=FONT_BODY)
        self._sys_box.grid(row=5, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 4))

        # Row 6: User Prompt
        ur = ctk.CTkFrame(form, fg_color="transparent")
        ur.grid(row=6, column=0, columnspan=2, sticky="ew", padx=10, pady=(6, 2))
        ctk.CTkLabel(ur, text="User Prompt（{lang} 會自動帶入目標語言）", font=FONT_BODY, anchor="w").pack(side="left")
        ctk.CTkButton(ur, text="恢復預設內容", width=100, height=24, font=FONT_SMALL,
                      command=self._reset_prompts).pack(side="right")

        self._usr_box = ctk.CTkTextbox(form, height=95, font=FONT_BODY)
        self._usr_box.grid(row=7, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 6))

        # Bottom row
        bottom_row = ctk.CTkFrame(parent, fg_color="transparent")
        bottom_row.pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkButton(bottom_row, text="儲存目前 Prompt 方案", width=180, height=32, font=FONT_BODY,
                      command=self._save_current_prompt_profile).pack(side="left")
        self._prompt_status_label = ctk.CTkLabel(bottom_row, text="", font=FONT_SMALL, text_color=("#0a7d2c", "#5ec97a"))
        self._prompt_status_label.pack(side="left", padx=(12, 0))

    def _format_prompt_display_name(self, p: dict) -> str:
        name = p.get("name", "未命名")
        if p.get("scope") == "model_bound":
            prov = p.get("bound_provider") or "?"
            mod = p.get("bound_model") or "?"
            return f"[專用: {prov}/{mod}] {name}"
        else:
            is_active = (p.get("id") == self._settings.get("active_global_prompt_id"))
            star = " (預設)" if is_active else ""
            return f"[公版] {name}{star}"

    def _refresh_prompt_tab_providers(self):
        if not hasattr(self, "_bound_provider_menu"):
            return
        providers = self._settings.get("providers", [])
        names = [p["name"] for p in providers] or ["（無供應商）"]
        self._bound_provider_menu.configure(values=names)
        cur_prov = self._bound_provider_var.get()
        if cur_prov not in names:
            cur_prov = names[0]
            self._bound_provider_var.set(cur_prov)
        self._on_bound_provider_change(cur_prov)

    def _on_bound_provider_change(self, prov_name: str):
        provider = next((p for p in self._settings.get("providers", []) if p.get("name") == prov_name), None)
        if provider:
            models = provider.get("models") or [provider.get("model", "gpt-4o")]
        else:
            models = ["（無模型）"]
        self._bound_model_menu.configure(values=models)
        cur_mod = self._bound_model_var.get()
        if cur_mod not in models:
            self._bound_model_var.set(models[0] if models else "")

    def _on_prompt_scope_change(self, val: str):
        if val.startswith("通用公版"):
            self._bound_frame.grid_remove()
        else:
            self._bound_frame.grid()
            self._refresh_prompt_tab_providers()

    def _sync_prompt_form(self, keep_id: str | None = None):
        if not hasattr(self, "_prompt_display_map"):
            return
        self._settings = config.load_settings()
        profiles = self._settings.get("prompt_profiles", [])
        if not profiles:
            return

        self._prompt_display_map.clear()
        display_names = []
        target_disp = None

        active_global_id = self._settings.get("active_global_prompt_id")
        for p in profiles:
            d = self._format_prompt_display_name(p)
            self._prompt_display_map[d] = p["id"]
            display_names.append(d)
            if keep_id and p["id"] == keep_id:
                target_disp = d
            elif not keep_id and not target_disp and p["id"] == active_global_id:
                target_disp = d

        if not target_disp and display_names:
            target_disp = display_names[0]

        self._prompt_menu.configure(values=display_names)
        if target_disp:
            self._prompt_menu.set(target_disp)
            pid = self._prompt_display_map.get(target_disp)
            prof = next((p for p in profiles if p["id"] == pid), profiles[0])
            self._load_prompt_profile_into_form(prof)

    def _on_prompt_profile_switch(self, disp_name: str):
        pid = self._prompt_display_map.get(disp_name)
        if not pid:
            return
        profiles = self._settings.get("prompt_profiles", [])
        p = next((x for x in profiles if x["id"] == pid), None)
        if p:
            self._load_prompt_profile_into_form(p)

    def _load_prompt_profile_into_form(self, p: dict):
        self._current_prompt_id = p.get("id", "")
        self._prompt_name_var.set(p.get("name", ""))
        scope = p.get("scope", "global")
        if scope == "model_bound":
            self._prompt_scope_seg.set("綁定特定模型 (Model-bound)")
            self._bound_frame.grid()
            self._refresh_prompt_tab_providers()
            b_prov = p.get("bound_provider", "")
            b_mod = p.get("bound_model", "")
            if b_prov:
                self._bound_provider_var.set(b_prov)
                self._on_bound_provider_change(b_prov)
            if b_mod:
                self._bound_model_var.set(b_mod)
        else:
            self._prompt_scope_seg.set("通用公版 (Global)")
            self._bound_frame.grid_remove()

        self._lang_var.set(p.get("target_lang", config.DEFAULT_TARGET_LANG))
        self._lang_custom.set(p.get("target_lang_custom", ""))
        self._sys_box.delete("1.0", "end")
        self._sys_box.insert("1.0", p.get("system_prompt", config.DEFAULT_SYSTEM_PROMPT))
        self._usr_box.delete("1.0", "end")
        self._usr_box.insert("1.0", p.get("user_prompt", config.DEFAULT_USER_PROMPT))
        self._prompt_status_label.configure(text="")

    def _reset_prompts(self):
        self._sys_box.delete("1.0", "end")
        self._sys_box.insert("1.0", config.DEFAULT_SYSTEM_PROMPT)
        self._usr_box.delete("1.0", "end")
        self._usr_box.insert("1.0", config.DEFAULT_USER_PROMPT)

    def _save_current_prompt_profile(self):
        name = self._prompt_name_var.get().strip()
        if not name:
            messagebox.showwarning("缺少名稱", "請先填寫方案名稱。")
            return

        is_bound = self._prompt_scope_seg.get().startswith("綁定特定模型")
        scope = "model_bound" if is_bound else "global"
        profile = {
            "id": self._current_prompt_id,
            "name": name,
            "scope": scope,
            "bound_provider": self._bound_provider_var.get().strip() if is_bound else "",
            "bound_model": self._bound_model_var.get().strip() if is_bound else "",
            "target_lang": self._lang_var.get(),
            "target_lang_custom": self._lang_custom.get().strip(),
            "system_prompt": self._sys_box.get("1.0", "end").strip("\n"),
            "user_prompt": self._usr_box.get("1.0", "end").strip("\n"),
        }
        config.upsert_prompt_profile(profile)
        self._settings = config.load_settings()
        self._sync_prompt_form(keep_id=profile["id"])
        self._refresh_run_tab_summary()
        self._prompt_status_label.configure(text=f"✅ 已儲存方案「{name}」")

    def _new_prompt_profile(self):
        import uuid
        new_id = f"prompt_{uuid.uuid4().hex[:8]}"
        new_prof = {
            "id": new_id,
            "name": "新 Prompt 方案",
            "scope": "global",
            "bound_provider": "",
            "bound_model": "",
            "target_lang": config.DEFAULT_TARGET_LANG,
            "target_lang_custom": "",
            "system_prompt": config.DEFAULT_SYSTEM_PROMPT,
            "user_prompt": config.DEFAULT_USER_PROMPT,
        }
        config.upsert_prompt_profile(new_prof)
        self._settings = config.load_settings()
        self._sync_prompt_form(keep_id=new_id)
        self._prompt_status_label.configure(text="✅ 已建立新方案，請修改後儲存。")

    def _clone_prompt_profile(self):
        import uuid
        old_name = self._prompt_name_var.get().strip() or "方案"
        new_id = f"prompt_{uuid.uuid4().hex[:8]}"
        is_bound = self._prompt_scope_seg.get().startswith("綁定特定模型")
        cloned_prof = {
            "id": new_id,
            "name": f"{old_name} (副本)",
            "scope": "model_bound" if is_bound else "global",
            "bound_provider": self._bound_provider_var.get().strip() if is_bound else "",
            "bound_model": self._bound_model_var.get().strip() if is_bound else "",
            "target_lang": self._lang_var.get(),
            "target_lang_custom": self._lang_custom.get().strip(),
            "system_prompt": self._sys_box.get("1.0", "end").strip("\n"),
            "user_prompt": self._usr_box.get("1.0", "end").strip("\n"),
        }
        config.upsert_prompt_profile(cloned_prof)
        self._settings = config.load_settings()
        self._sync_prompt_form(keep_id=new_id)
        self._prompt_status_label.configure(text=f"✅ 已複製方案為「{cloned_prof['name']}」")

    def _delete_current_prompt_profile(self):
        profiles = self._settings.get("prompt_profiles", [])
        if len(profiles) <= 1:
            messagebox.showwarning("無法刪除", "系統至少需保留一組 Prompt 方案。")
            return
        name = self._prompt_name_var.get().strip()
        if not messagebox.askyesno("刪除方案", f"確定刪除 Prompt 方案「{name}」？"):
            return
        config.delete_prompt_profile(self._current_prompt_id)
        self._settings = config.load_settings()
        self._sync_prompt_form()
        self._refresh_run_tab_summary()

    def _set_current_as_active_global(self):
        is_bound = self._prompt_scope_seg.get().startswith("綁定特定模型")
        if is_bound:
            messagebox.showwarning("無法設為預設公版", "此方案為「綁定特定模型」的專用版本，請先改為「通用公版」再設為全域預設。")
            return
        config.set_active_global_prompt(self._current_prompt_id)
        self._settings = config.load_settings()
        self._sync_prompt_form(keep_id=self._current_prompt_id)
        self._refresh_run_tab_summary()
        self._prompt_status_label.configure(text="⭐ 已將此方案設為全域預設公版。")

    # ------------------------------------------------------------------ #
    #  Tab 4: Execution & Batch LLM Translation (v1.07)
    # ------------------------------------------------------------------ #
    def _build_run_tab(self, parent):
        ctk.CTkLabel(parent, text="執行 OCR 翻譯與 SRT 匯出", font=FONT_TITLE, anchor="w").pack(
            fill="x", padx=14, pady=(12, 4))

        # 【目前執行配置摘要】
        summary_card = ctk.CTkFrame(parent)
        summary_card.pack(fill="x", padx=14, pady=(0, 8))
        summary_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(summary_card, text="【目前執行配置摘要】", font=FONT_BODY, anchor="w").grid(
            row=0, column=0, sticky="w", padx=12, pady=(8, 4)
        )
        self._run_prov_model_label = ctk.CTkLabel(
            summary_card, text="🔹 供應商：— ｜ 模型：—", font=FONT_BODY, anchor="w"
        )
        self._run_prov_model_label.grid(row=1, column=0, sticky="w", padx=12, pady=1)

        self._run_prompt_label = ctk.CTkLabel(
            summary_card, text="🔹 套用 Prompt：—", font=FONT_BODY, anchor="w"
        )
        self._run_prompt_label.grid(row=2, column=0, sticky="w", padx=12, pady=1)

        ctk.CTkLabel(
            summary_card,
            text="（💡 如需切換供應商或模型，請至「API 設定」頁籤調整）",
            font=FONT_SMALL, text_color=("gray40", "#9aa0a6"), anchor="w"
        ).grid(row=3, column=0, sticky="w", padx=12, pady=(2, 8))

        # SRT 插入方式 (由 Prompt 設定頁移入)
        mode_row = ctk.CTkFrame(parent, fg_color="transparent")
        mode_row.pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkLabel(mode_row, text="SRT 插入方式（當有指定目標 SRT 時）：", font=FONT_BODY).pack(side="left")
        self._mode_var = ctk.StringVar(value=self._settings.get("insertion_mode", "insert"))
        self._mode_seg = ctk.CTkSegmentedButton(
            mode_row, values=["insert", "append"],
            command=lambda v: [self._mode_var.set(v), config.patch_setting("insertion_mode", v)],
            variable=self._mode_var, height=30, font=FONT_BODY)
        self._mode_seg.pack(side="left", padx=(6, 12))
        ctk.CTkLabel(mode_row,
                     text="insert=依時間軸插入新字幕　append=附加至涵蓋時間軸的現有字幕",
                     font=FONT_SMALL, text_color=("gray40", "#9aa0a6")).pack(side="left")

        # 控制按鈕與任務統計
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

    def _refresh_run_tab_summary(self):
        provider = config.get_default_provider()
        prov_name = provider.get("name", "（未設定）") if provider else "（未設定）"
        if provider:
            models = provider.get("models") or [provider.get("model", "")]
            model_name = provider.get("selected_model") or (models[0] if models else "（未設定）")
        else:
            model_name = "（未設定）"
        prof, desc = config.resolve_prompt(prov_name, model_name, self._settings)
        if hasattr(self, "_run_prov_model_label"):
            self._run_prov_model_label.configure(text=f"🔹 供應商：{prov_name}  ｜  🔹 模型：{model_name}")
        if hasattr(self, "_run_prompt_label"):
            self._run_prompt_label.configure(text=f"🔹 套用 Prompt：{desc}")

    def _log_line(self, msg: str):
        if hasattr(self, "_log"):
            self.after(0, lambda: self._log.insert("end", msg + "\n"))
            self.after(0, lambda: self._log.see("end"))
        else:
            print(msg)

    def _set_busy(self, busy: bool):
        self._busy = busy
        state = "disabled" if busy else "normal"
        if hasattr(self, "_run_btn"):
            self.after(0, lambda: self._run_btn.configure(state=state))

    def _start_ocr(self):
        if self._busy:
            return
        if not self._tasks:
            messagebox.showwarning("無任務", "請先在「影片截圖視窗」設置至少一組畫面轉字幕任務。")
            return

        config.reload_env()
        self._settings = config.load_settings()

        # Resolve provider from dropdown or fallback
        selected_prov_name = self._ocr_exec_prov_var.get() if hasattr(self, "_ocr_exec_prov_var") else None
        provider = None
        if selected_prov_name:
            for p in config.get_providers():
                if p.get("name") == selected_prov_name:
                    provider = p
                    break
        if not provider or not provider.get("api_key"):
            provider = config.get_default_provider() or self._form_to_provider()
        if not provider or not provider.get("api_url"):
            messagebox.showwarning("API 未設定", "請先在「API 設定」填入網址與金鑰。")
            return

        # Resolve model from dropdown or provider default
        selected_model_name = self._ocr_exec_model_var.get() if hasattr(self, "_ocr_exec_model_var") else None
        if selected_model_name and selected_model_name not in ("", "default", "（無模型）"):
            model_name = selected_model_name
        else:
            model_name = provider.get("selected_model") or (provider.get("models")[0] if provider.get("models") else "")
        if not model_name:
            messagebox.showwarning("模型未設定", "請先在下拉選單或「API 設定」中選擇模型。")
            return

        # Resolve prompt profile from dropdown or fallback
        selected_prompt_name = self._ocr_exec_prompt_var.get() if hasattr(self, "_ocr_exec_prompt_var") else None
        prompt_prof = None
        prompt_desc = ""
        if selected_prompt_name:
            prompt_prof = config.get_prompt_profile_by_name(selected_prompt_name, self._settings)
            if prompt_prof:
                prompt_desc = f"[{prompt_prof.get('name')}]"
        if not prompt_prof:
            prompt_prof, prompt_desc = config.resolve_prompt(provider.get("name", ""), model_name, self._settings)

        # Persist user choices
        if selected_prov_name:
            config.patch_setting("selected_ocr_provider", selected_prov_name)
        if model_name:
            config.patch_setting("selected_ocr_model", model_name)
        if prompt_prof and prompt_prof.get("id"):
            config.patch_setting("selected_ocr_prompt_id", prompt_prof["id"])

        cfg = ProviderCfg.from_dict(provider)
        sys_prompt = prompt_prof.get("system_prompt") or config.DEFAULT_SYSTEM_PROMPT
        usr_prompt = prompt_prof.get("user_prompt") or config.DEFAULT_USER_PROMPT
        lang = prompt_prof.get("target_lang_custom") or prompt_prof.get("target_lang") or config.DEFAULT_TARGET_LANG
        max_side = int(self._settings.get("image_max_side", 1568))

        self._refresh_run_tab_summary()

        if hasattr(self, "_log"):
            self._log.delete("1.0", "end")
        if hasattr(self, "_preview_scroll"):
            for w in self._preview_scroll.winfo_children():
                w.destroy()
        self._progress.set(0)
        self._insert_btn.configure(state="disabled")
        self._set_busy(True)
        if hasattr(self, "_ocr_exec_status_label"):
            self._ocr_exec_status_label.configure(text=f"🔄 正在使用 {provider.get('name')} ｜ {model_name} 批次翻譯中...")

        def work():
            total = len(self._tasks)
            self._log_line(f"開始批次 OCR 翻譯，共 {total} 組任務...")
            self._log_line(f"使用供應商：{provider.get('name')} ｜ 模型：{model_name}")
            self._log_line(f"套用 Prompt：{prompt_desc}")
            for i, task in enumerate(self._tasks, 1):
                task.status = "翻譯中"
                self.after(0, self._render_tasks_list)
                scope = "框選區域" if task.crop_box else "全畫面"
                self._log_line(f"[{i}/{total}] {task.start_ts} → {task.end_ts} ({scope})")

                try:
                    text = translate_image(cfg, task.crop_img, sys_prompt, usr_prompt, lang, max_side)
                    task.translation = text
                    task.raw_translation = text
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
                self.after(0, self._render_ocr_output_textbox)
                self.after(0, lambda t=task, txt=text: self._add_preview(f"{t.start_ts} → {t.end_ts}", txt))
                self._progress.set(i / total)

            self._log_line("全部任務 OCR 翻譯完成。")
            self.after(0, lambda: self._insert_btn.configure(state="normal"))
            if hasattr(self, "_ocr_exec_status_label"):
                self.after(0, lambda: self._ocr_exec_status_label.configure(
                    text=f"✅ 翻譯完成！共 {total} 組任務。可在下方對話框修改文字後儲存為 SRT。"
                ))
            self._set_busy(False)

        threading.Thread(target=work, daemon=True).start()

    def _add_preview(self, ts_range: str, text: str):
        if hasattr(self, "_preview_scroll"):
            frame = ctk.CTkFrame(self._preview_scroll, fg_color=("gray86", "gray17"))
            frame.pack(fill="x", pady=2)
            ctk.CTkLabel(frame, text=ts_range, font=FONT_MONO, width=220, anchor="w").pack(side="left", padx=8, pady=4)
            ctk.CTkLabel(frame, text=text.replace("\n", " / "), font=FONT_BODY,
                         anchor="w", wraplength=640).pack(side="left", fill="x", expand=True)

    def _insert_and_save(self):
        if hasattr(self, "_sync_textbox_to_tasks"):
            self._sync_textbox_to_tasks()
        valid_tasks = [t for t in self._tasks if t.translation.strip()]
        if not valid_tasks:
            messagebox.showinfo("無翻譯內容", "尚無已完成翻譯的任務。請先執行翻譯或於對話框輸入文字。")
            return

        mode = self._mode_var.get() if hasattr(self, "_mode_var") else "insert"
        trans = [(t.start_sec, t.translation, t.duration) for t in valid_tasks]

        if self._srt_entries:
            entries = srt_utils.insert_translations(
                list(self._srt_entries), trans,
                mode=mode, duration_mode="per_subtitle"
            )
        else:
            entries = srt_utils.insert_translations(
                [], trans,
                mode="insert", duration_mode="per_subtitle"
            )

        # Check save mode: overwrite vs save_as
        save_mode = getattr(self, "_ocr_save_mode_var", None)
        mode_val = save_mode.get() if save_mode else self._settings.get("srt_save_mode", "save_as")

        out = None
        if mode_val == "overwrite":
            if self._srt_path and Path(self._srt_path).exists():
                out = self._srt_path
            else:
                messagebox.showinfo("提示", "目前尚未載入現有 SRT 檔案，將為您另存新檔。")

        if not out:
            suffix = getattr(self, "_ocr_suffix_var", None)
            s_val = suffix.get().strip() if suffix else "_translated"
            if s_val and not s_val.startswith(("_", "-", ".")):
                s_val = "_" + s_val
            if s_val.endswith(".srt"):
                s_val = s_val[:-4]
            config.patch_setting("custom_srt_suffix", s_val)

            if self._srt_path:
                orig = Path(self._srt_path)
                default_name = f"{orig.stem}{s_val}.srt"
                initialdir = str(orig.parent)
            elif self._video_path:
                orig = Path(self._video_path)
                default_name = f"{orig.stem}{s_val}.srt"
                initialdir = str(orig.parent)
            else:
                default_name = f"subtitles{s_val}.srt"
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

        # Update current SRT path and hot-reload into VLC
        self._srt_path = out
        self._settings["last_srt_path"] = out
        config.patch_setting("last_srt_path", out)
        self._srt_var.set(out)
        if hasattr(self, "_player_srt_var"):
            self._player_srt_var.set(out)
        self._srt_entries = entries
        if self._vlc_player:
            self._vlc_player.set_subtitle_file(out)
        self._populate_sub_treeview()
        self._update_sub_status_label()
        if hasattr(self, "_update_ocr_save_preview"):
            self._update_ocr_save_preview()

        msg = f"已成功儲存 SRT 字幕：\n{out}\n共包含 {len(entries)} 條字幕。"
        messagebox.showinfo("匯出完成", msg)
        self._log_line(f"已儲存：{out}（共 {len(entries)} 條字幕）")
        if hasattr(self, "_ocr_exec_status_label"):
            self._ocr_exec_status_label.configure(text=f"💾 已儲存：{Path(out).name}（共 {len(entries)} 條字幕）")

    # ================================================================== #
    #  v2.01 Pic-to-Pic (P2P) AI Image-to-Image Generation Subsystem
    # ================================================================== #

    # ------------------------------------------------------------------ #
    #  P2P Tab 1: Edit & Preview Window (_build_p2p_editor_tab)
    # ------------------------------------------------------------------ #
    def _build_p2p_editor_tab(self, parent):
        # v2.05 Top Utility Bar (Privacy Preview Switch)
        top_bar = ctk.CTkFrame(parent, fg_color="transparent")
        top_bar.pack(fill="x", padx=8, pady=(4, 2))

        ctk.CTkLabel(top_bar, text="🎨 AI 以圖生圖工作站", font=FONT_SUBTITLE, anchor="w").pack(side="left", padx=(4, 0))

        self._p2p_preview_switch = ctk.CTkSwitch(
            top_bar, text="👁️ 圖片即時預覽 (關閉即黑屏防窺)",
            variable=self._p2p_preview_visible_var,
            font=FONT_SMALL,
            command=self._on_p2p_preview_toggle
        )
        self._p2p_preview_switch.pack(side="right", padx=(0, 6))

        paned = tk.PanedWindow(
            parent, orient="horizontal", sashrelief="raised", sashwidth=6,
            bg="#252526", bd=0, opaqueresize=True
        )
        paned.pack(fill="both", expand=True, padx=6, pady=(2, 6))

        left_col = ctk.CTkFrame(paned)
        mid_col = ctk.CTkFrame(paned)
        right_col = ctk.CTkFrame(paned)

        paned.add(left_col, minsize=260, stretch="always")
        paned.add(mid_col, minsize=380, stretch="always")
        paned.add(right_col, minsize=380, stretch="always")

        # --- Left Column: Input Material Gallery ---
        ctk.CTkLabel(left_col, text="📁 輸入圖庫：", font=FONT_SUBTITLE, anchor="w").pack(
            fill="x", padx=10, pady=(8, 2))

        self._p2p_input_dir_var = ctk.StringVar(value="")
        self._p2p_input_dir_menu = ctk.CTkOptionMenu(
            left_col, values=["截圖素材庫 (預設)"], variable=self._p2p_input_dir_var, font=FONT_SMALL,
            command=self._on_p2p_input_dir_change
        )
        self._p2p_input_dir_menu.pack(fill="x", padx=10, pady=(0, 4))
        self._populate_p2p_input_dir_menu()

        ctk.CTkButton(
            left_col, text="🎬 前往影片截圖視窗 (ROI)", height=28, font=FONT_SMALL,
            fg_color="#1f6aa5", hover_color="#144d75",
            command=lambda: self.tabs.set("  影片截圖視窗  ")
        ).pack(fill="x", padx=10, pady=(0, 6))

        top_btns = ctk.CTkFrame(left_col, fg_color="transparent")
        top_btns.pack(fill="x", padx=10, pady=(0, 4))
        ctk.CTkButton(top_btns, text="🔄 重新整理", width=75, height=26, font=FONT_SMALL,
                      command=self._refresh_p2p_gallery).pack(side="left", padx=(0, 4))
        ctk.CTkButton(top_btns, text="📂 開啟資料夾", width=85, height=26, font=FONT_SMALL,
                      command=self._open_current_input_folder).pack(side="left", padx=(0, 4))
        ctk.CTkButton(top_btns, text="📋 拷貝至輸出庫", width=110, height=26, font=FONT_SMALL,
                      fg_color="#2e7d32", hover_color="#1b5e20",
                      command=self._copy_selected_input_to_output).pack(side="left", padx=(0, 4))
        self._p2p_history_toggle_btn = ctk.CTkButton(
            top_btns, text="📦 歷史素材", width=85, height=26, font=FONT_SMALL,
            command=self._toggle_p2p_history_mode
        )
        self._p2p_history_toggle_btn.pack(side="left")

        # Selection Toolbar Container
        self._p2p_sel_container = ctk.CTkFrame(left_col, fg_color="transparent")
        self._p2p_sel_container.pack(fill="x", padx=10, pady=(0, 4))

        # Normal Gallery Selection Toolbar
        self._p2p_normal_sel_btns = ctk.CTkFrame(self._p2p_sel_container, fg_color="transparent")
        self._p2p_normal_sel_btns.pack(fill="x")
        ctk.CTkButton(self._p2p_normal_sel_btns, text="☑ 全選", width=55, height=24, font=FONT_SMALL,
                      command=self._select_all_captured).pack(side="left", padx=(0, 4))
        ctk.CTkButton(self._p2p_normal_sel_btns, text="☐ 全不選", width=65, height=24, font=FONT_SMALL,
                      command=self._deselect_all_captured).pack(side="left", padx=(0, 4))
        ctk.CTkButton(self._p2p_normal_sel_btns, text="🗑️ 刪除", width=70, height=24, font=FONT_SMALL,
                      fg_color="#b23b3b", hover_color="#8a2e2e",
                      command=self._delete_selected_captured).pack(side="right")

        # History Gallery Selection Toolbar (hidden by default)
        self._p2p_history_sel_btns = ctk.CTkFrame(self._p2p_sel_container, fg_color="transparent")
        ctk.CTkButton(self._p2p_history_sel_btns, text="☑ 全選", width=55, height=24, font=FONT_SMALL,
                      command=self._select_all_p2p_drop).pack(side="left", padx=(0, 4))
        ctk.CTkButton(self._p2p_history_sel_btns, text="☐ 全不選", width=65, height=24, font=FONT_SMALL,
                      command=self._deselect_all_p2p_drop).pack(side="left", padx=(0, 4))
        ctk.CTkButton(self._p2p_history_sel_btns, text="↩️ 復原至素材庫", width=110, height=24, font=FONT_SMALL,
                      fg_color="#2e7d32", hover_color="#1b5e20",
                      command=self._restore_selected_p2p_drop).pack(side="left", padx=(0, 4))
        ctk.CTkButton(self._p2p_history_sel_btns, text="🧹 清空暫存", width=85, height=24, font=FONT_SMALL,
                      fg_color="#b23b3b", hover_color="#8a2e2e",
                      command=self._clear_p2p_drop).pack(side="right")

        self._p2p_gallery_count_label = ctk.CTkLabel(
            left_col, text="共 0 張 ｜ 已勾選 0 張", font=FONT_SMALL,
            text_color=("gray40", "#9aa0a6"), anchor="w"
        )
        self._p2p_gallery_count_label.pack(fill="x", padx=10, pady=(0, 4))

        self._p2p_gallery_scroll = ctk.CTkScrollableFrame(left_col)
        self._p2p_gallery_scroll.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        # --- Middle Column: Image Edit Canvas & Tools ---
        ctk.CTkLabel(mid_col, text="🖌️ 參考圖微調與旋轉", font=FONT_SUBTITLE, anchor="w").pack(
            fill="x", padx=10, pady=(8, 4))

        edit_bar1 = ctk.CTkFrame(mid_col, fg_color="transparent")
        edit_bar1.pack(fill="x", padx=10, pady=(0, 4))
        ctk.CTkButton(edit_bar1, text="⟲ 逆轉 90°", width=80, height=28, font=FONT_SMALL,
                      command=lambda: self._rotate_p2p_image(270)).pack(side="left", padx=(0, 4))
        ctk.CTkButton(edit_bar1, text="⟳ 順轉 90°", width=80, height=28, font=FONT_SMALL,
                      command=lambda: self._rotate_p2p_image(90)).pack(side="left", padx=(0, 4))
        ctk.CTkButton(edit_bar1, text="180° 旋轉", width=75, height=28, font=FONT_SMALL,
                      command=lambda: self._rotate_p2p_image(180)).pack(side="left", padx=(0, 4))
        ctk.CTkButton(edit_bar1, text="⇄ 水平翻轉", width=80, height=28, font=FONT_SMALL,
                      command=self._flip_p2p_image).pack(side="left")

        edit_bar2 = ctk.CTkFrame(mid_col, fg_color="transparent")
        edit_bar2.pack(fill="x", padx=10, pady=(0, 6))
        ctk.CTkButton(edit_bar2, text="↺ 重設原圖", width=80, height=26, font=FONT_SMALL,
                      command=self._reset_p2p_image).pack(side="left", padx=(0, 4))
        ctk.CTkButton(edit_bar2, text="💾 另存為新素材", width=110, height=26, font=FONT_SMALL,
                      fg_color="#2e7d32", hover_color="#1b5e20",
                      command=self._save_p2p_edited_as_new).pack(side="left")

        self._p2p_edit_info_label = ctk.CTkLabel(
            mid_col, text="請從左側點選一張圖片進入微調編輯", font=FONT_SMALL,
            text_color=("gray40", "#9aa0a6"), anchor="w"
        )
        self._p2p_edit_info_label.pack(fill="x", padx=10, pady=(0, 4))

        self._p2p_edit_canvas_box = ctk.CTkFrame(mid_col, fg_color="black")
        self._p2p_edit_canvas_box.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self._p2p_edit_canvas = tk.Canvas(self._p2p_edit_canvas_box, bg="#141414", highlightthickness=0)
        self._p2p_edit_canvas.pack(fill="both", expand=True)
        self._p2p_edit_canvas.bind("<Configure>", lambda _e: self._render_active_p2p_canvas())

        # --- Right Column: AI Generation Controls & Output ---
        ctk.CTkLabel(right_col, text="🚀 雲端聚合以圖生圖", font=FONT_SUBTITLE, anchor="w").pack(
            fill="x", padx=10, pady=(8, 4))

        right_scroll = ctk.CTkScrollableFrame(right_col)
        right_scroll.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        # Provider & Model selection row
        prov_row = ctk.CTkFrame(right_scroll, fg_color="transparent")
        prov_row.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(prov_row, text="服務商：", font=FONT_SMALL).pack(side="left")
        providers = [p.get("name", "") for p in self._p2p_settings.get("providers", [])] or ["OpenRouter"]
        self._p2p_prov_var = ctk.StringVar(value=self._p2p_settings.get("selected_provider") or providers[0])
        self._p2p_prov_menu = ctk.CTkOptionMenu(
            prov_row, values=providers, variable=self._p2p_prov_var, font=FONT_SMALL,
            width=130, height=26, command=self._on_p2p_provider_change
        )
        self._p2p_prov_menu.pack(side="left", padx=(2, 8))

        ctk.CTkLabel(prov_row, text="模型：", font=FONT_SMALL).pack(side="left")
        self._p2p_model_var = ctk.StringVar(value="")
        self._p2p_model_menu = ctk.CTkOptionMenu(
            prov_row, values=["default"], variable=self._p2p_model_var, font=FONT_SMALL,
            width=160, height=26, command=self._on_p2p_model_change
        )
        self._p2p_model_menu.pack(side="left", fill="x", expand=True, padx=(2, 0))

        # Prompt preset row
        preset_row = ctk.CTkFrame(right_scroll, fg_color="transparent")
        preset_row.pack(fill="x", pady=(4, 4))
        ctk.CTkLabel(preset_row, text="Prompt 範本：", font=FONT_SMALL).pack(side="left")
        presets = [p.get("name", "") for p in self._p2p_settings.get("drawing_prompts", [])] or ["未設定"]
        self._p2p_preset_var = ctk.StringVar(value=presets[0])
        self._p2p_preset_menu = ctk.CTkOptionMenu(
            preset_row, values=presets, variable=self._p2p_preset_var, font=FONT_SMALL,
            height=26, command=self._on_p2p_preset_change
        )
        self._p2p_preset_menu.pack(side="left", fill="x", expand=True, padx=(2, 0))

        # Positive Prompt
        ctk.CTkLabel(right_scroll, text="📝 正向提示詞 (Prompt)：", font=FONT_SMALL, anchor="w").pack(
            fill="x", pady=(4, 1))
        self._p2p_pos_prompt_box = ctk.CTkTextbox(right_scroll, height=65, font=FONT_BODY)
        self._p2p_pos_prompt_box.pack(fill="x", pady=(0, 4))

        # Negative Prompt
        self._p2p_neg_prompt_label = ctk.CTkLabel(right_scroll, text="🚫 負向提示詞 (Negative Prompt)：", font=FONT_SMALL, anchor="w")
        self._p2p_neg_prompt_label.pack(fill="x", pady=(2, 1))
        self._p2p_neg_prompt_box = ctk.CTkTextbox(right_scroll, height=45, font=FONT_SMALL)
        self._p2p_neg_prompt_box.pack(fill="x", pady=(0, 4))

        # Parameters Row 1: Count, Resolution, Aspect Ratio (v2.05 reorganization)
        param_row1 = ctk.CTkFrame(right_scroll, fg_color="transparent")
        param_row1.pack(fill="x", pady=(2, 3))

        self._p2p_count_label = ctk.CTkLabel(param_row1, text="張數：", font=FONT_SMALL)
        self._p2p_count_label.pack(side="left")
        self._p2p_count_var = ctk.StringVar(value="1")
        self._p2p_count_menu = ctk.CTkOptionMenu(
            param_row1, values=["1", "2", "3", "4"],
            variable=self._p2p_count_var, font=FONT_SMALL, height=24, width=58
        )
        self._p2p_count_menu.pack(side="left", padx=(2, 6))

        self._p2p_res_label = ctk.CTkLabel(param_row1, text="解析度：", font=FONT_SMALL)
        self._p2p_res_label.pack(side="left")
        self._p2p_res_var = ctk.StringVar(value="auto")
        self._p2p_res_menu = ctk.CTkOptionMenu(
            param_row1, values=["auto", "1K", "2K", "3K", "4K"],
            variable=self._p2p_res_var, font=FONT_SMALL, height=24, width=70
        )
        self._p2p_res_menu.pack(side="left", padx=(2, 6))

        self._p2p_aspect_label = ctk.CTkLabel(param_row1, text="比例：", font=FONT_SMALL)
        self._p2p_aspect_label.pack(side="left")
        init_aspect = self._p2p_settings.get("default_params", {}).get("aspect_ratio", "1:1")
        self._p2p_aspect_var = ctk.StringVar(value=init_aspect)
        self._p2p_aspect_menu = ctk.CTkOptionMenu(
            param_row1, values=["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "自定義"],
            variable=self._p2p_aspect_var, font=FONT_SMALL, height=24,
            command=self._on_p2p_aspect_ratio_change
        )
        self._p2p_aspect_menu.pack(side="left", fill="x", expand=True, padx=(2, 0))

        # Parameters Row 2: Dynamic Size Presets or Custom WxH entries (v2.05)
        self._p2p_size_container = ctk.CTkFrame(right_scroll, fg_color="transparent")
        self._p2p_size_container.pack(fill="x", pady=(2, 4))

        self._p2p_size_preset_frame = ctk.CTkFrame(self._p2p_size_container, fg_color="transparent")
        self._p2p_size_preset_frame.pack(fill="x")
        self._p2p_size_label = ctk.CTkLabel(self._p2p_size_preset_frame, text="尺寸規格：", font=FONT_SMALL)
        self._p2p_size_label.pack(side="left")
        self._p2p_size_var = ctk.StringVar(value="auto")
        init_sizes = image_gen_engine.ASPECT_RATIO_SIZE_PRESETS.get(init_aspect, ["auto", "1024x1024", "自定義..."])
        self._p2p_size_menu = ctk.CTkOptionMenu(
            self._p2p_size_preset_frame, values=init_sizes,
            variable=self._p2p_size_var, font=FONT_SMALL, height=24,
            command=self._on_p2p_size_preset_change
        )
        self._p2p_size_menu.pack(side="left", fill="x", expand=True, padx=(2, 0))

        self._p2p_custom_size_frame = ctk.CTkFrame(self._p2p_size_container, fg_color="transparent")
        ctk.CTkLabel(self._p2p_custom_size_frame, text="自訂尺寸：寬 W", font=FONT_SMALL).pack(side="left")
        self._p2p_custom_w_entry = ctk.CTkEntry(
            self._p2p_custom_size_frame, textvariable=self._p2p_custom_w_var, width=65, height=24, font=FONT_MONO, justify="center"
        )
        self._p2p_custom_w_entry.pack(side="left", padx=(2, 4))
        ctk.CTkLabel(self._p2p_custom_size_frame, text="× 高 H", font=FONT_SMALL).pack(side="left")
        self._p2p_custom_h_entry = ctk.CTkEntry(
            self._p2p_custom_size_frame, textvariable=self._p2p_custom_h_var, width=65, height=24, font=FONT_MONO, justify="center"
        )
        self._p2p_custom_h_entry.pack(side="left", padx=(2, 4))
        ctk.CTkLabel(self._p2p_custom_size_frame, text="px", font=FONT_SMALL).pack(side="left")

        # Advanced Drawing Parameters Collapsible Panel (v2.02 / v2.03)
        self._p2p_adv_open = False
        self._p2p_adv_toggle_btn = ctk.CTkButton(
            right_scroll, text="⚙️ 展開進階繪圖參數 (強度 / 畫質 / Seed / JSON...) ▼",
            height=22, font=FONT_SMALL, fg_color="transparent", text_color=("#1f6aa5", "#64b5f6"),
            hover_color=("gray85", "gray25"), anchor="w", command=self._toggle_p2p_advanced_params
        )
        self._p2p_adv_toggle_btn.pack(fill="x", pady=(2, 2))

        self._p2p_adv_frame = ctk.CTkFrame(right_scroll, fg_color=("gray88", "gray18"), corner_radius=6)

        # Adv Row 1: Strength (v2.03)
        adv_row1 = ctk.CTkFrame(self._p2p_adv_frame, fg_color="transparent")
        adv_row1.pack(fill="x", padx=8, pady=(6, 2))
        self._p2p_strength_label_title = ctk.CTkLabel(adv_row1, text="重繪強度：", font=FONT_SMALL, width=80, anchor="w")
        self._p2p_strength_label_title.pack(side="left")
        self._p2p_strength_var = ctk.DoubleVar(value=self._p2p_settings.get("default_params", {}).get("image_strength", 0.65))
        self._p2p_strength_slider = ctk.CTkSlider(
            adv_row1, from_=0.10, to=1.00, variable=self._p2p_strength_var,
            width=110, height=14, command=lambda v: self._p2p_strength_label.configure(text=f"{float(v):.2f}")
        )
        self._p2p_strength_slider.pack(side="left", padx=(2, 4))
        self._p2p_strength_label = ctk.CTkLabel(adv_row1, text=f"{self._p2p_strength_var.get():.2f}", font=FONT_SMALL, width=28)
        self._p2p_strength_label.pack(side="left", padx=(0, 6))

        # Adv Row 2: Quality & Background
        adv_row2 = ctk.CTkFrame(self._p2p_adv_frame, fg_color="transparent")
        adv_row2.pack(fill="x", padx=8, pady=2)
        self._p2p_quality_label = ctk.CTkLabel(adv_row2, text="畫質等級：", font=FONT_SMALL, width=80, anchor="w")
        self._p2p_quality_label.pack(side="left")
        self._p2p_quality_var = ctk.StringVar(value="auto")
        self._p2p_quality_menu = ctk.CTkOptionMenu(
            adv_row2, values=["auto", "low", "medium", "high", "xhigh", "max"],
            variable=self._p2p_quality_var, font=FONT_SMALL, height=24, width=95
        )
        self._p2p_quality_menu.pack(side="left", padx=(0, 6))

        self._p2p_bg_label = ctk.CTkLabel(adv_row2, text="背景：", font=FONT_SMALL)
        self._p2p_bg_label.pack(side="left")
        self._p2p_bg_var = ctk.StringVar(value="auto")
        self._p2p_bg_menu = ctk.CTkOptionMenu(
            adv_row2, values=["auto", "opaque", "transparent"],
            variable=self._p2p_bg_var, font=FONT_SMALL, height=24
        )
        self._p2p_bg_menu.pack(side="left", fill="x", expand=True)

        # Adv Row 3: Format & Seed (v2.03)
        adv_row3 = ctk.CTkFrame(self._p2p_adv_frame, fg_color="transparent")
        adv_row3.pack(fill="x", padx=8, pady=2)
        self._p2p_format_label = ctk.CTkLabel(adv_row3, text="輸出格式：", font=FONT_SMALL, width=80, anchor="w")
        self._p2p_format_label.pack(side="left")
        self._p2p_format_var = ctk.StringVar(value="auto")
        self._p2p_format_menu = ctk.CTkOptionMenu(
            adv_row3, values=["auto", "png", "jpeg", "webp"],
            variable=self._p2p_format_var, font=FONT_SMALL, height=24, width=95
        )
        self._p2p_format_menu.pack(side="left", padx=(0, 6))

        self._p2p_seed_label = ctk.CTkLabel(adv_row3, text="種子(Seed)：", font=FONT_SMALL)
        self._p2p_seed_label.pack(side="left")
        self._p2p_seed_var = ctk.StringVar(value="")
        self._p2p_seed_entry = ctk.CTkEntry(
            adv_row3, textvariable=self._p2p_seed_var, placeholder_text="隨機", font=FONT_SMALL, height=24
        )
        self._p2p_seed_entry.pack(side="left", fill="x", expand=True)

        # Adv Row 4: Custom Extra JSON
        adv_row4 = ctk.CTkFrame(self._p2p_adv_frame, fg_color="transparent")
        adv_row4.pack(fill="x", padx=8, pady=(2, 6))
        ctk.CTkLabel(adv_row4, text="自訂JSON：", font=FONT_SMALL, width=80, anchor="w").pack(side="left")
        self._p2p_extra_json_entry = ctk.CTkEntry(
            adv_row4, placeholder_text='如: {"steps": 30}', font=FONT_SMALL, height=24
        )
        self._p2p_extra_json_entry.pack(side="left", fill="x", expand=True)

        # Generate button
        self._p2p_gen_btn = ctk.CTkButton(
            right_scroll, text="🎨 開始 AI 以圖生圖", height=34, font=FONT_SUBTITLE,
            fg_color="#1f6aa5", hover_color="#144870", command=self._start_p2p_generate
        )
        self._p2p_gen_btn.pack(fill="x", pady=(4, 4))

        self._p2p_status_label = ctk.CTkLabel(
            right_scroll, text="準備就緒", font=FONT_SMALL, text_color=("gray40", "#9aa0a6")
        )
        self._p2p_status_label.pack(fill="x", pady=(0, 4))

        # Output Canvas & Gallery Section (v2.05)
        ctk.CTkLabel(right_scroll, text="🖼️ AI 生成成果預覽：", font=FONT_SMALL, anchor="w").pack(
            fill="x", pady=(4, 2))
        self._p2p_result_box = ctk.CTkFrame(right_scroll, fg_color="black")
        self._p2p_result_box.pack(fill="x", pady=(0, 4))

        self._p2p_result_canvas = tk.Canvas(self._p2p_result_box, bg="#141414", height=200, highlightthickness=0)
        self._p2p_result_canvas.pack(fill="both", expand=True)

        # Output Gallery Header & Dropdown (v2.05 replaces old bottom buttons)
        out_header = ctk.CTkFrame(right_scroll, fg_color="transparent")
        out_header.pack(fill="x", pady=(6, 2))
        ctk.CTkLabel(out_header, text="📁 輸出圖庫：", font=FONT_SUBTITLE, anchor="w").pack(side="left")

        self._p2p_output_dir_var = ctk.StringVar(value="")
        self._p2p_output_dir_menu = ctk.CTkOptionMenu(
            right_scroll, values=["AI生成資料夾 (預設)"], variable=self._p2p_output_dir_var, font=FONT_SMALL,
            command=self._on_p2p_output_dir_change
        )
        self._p2p_output_dir_menu.pack(fill="x", pady=(0, 4))
        self._populate_p2p_output_dir_menu()

        # Output Action Toolbar
        out_toolbar = ctk.CTkFrame(right_scroll, fg_color="transparent")
        out_toolbar.pack(fill="x", pady=(0, 4))
        ctk.CTkButton(out_toolbar, text="🔄 重新整理", width=75, height=26, font=FONT_SMALL,
                      command=self._refresh_p2p_output_gallery).pack(side="left", padx=(0, 4))
        ctk.CTkButton(out_toolbar, text="📂 開啟資料夾", width=85, height=26, font=FONT_SMALL,
                      command=self._open_current_output_folder).pack(side="left", padx=(0, 4))
        ctk.CTkButton(out_toolbar, text="📋 拷貝至輸入庫", width=110, height=26, font=FONT_SMALL,
                      fg_color="#2e7d32", hover_color="#1b5e20",
                      command=self._copy_selected_output_to_input).pack(side="left")

        out_sel_bar = ctk.CTkFrame(right_scroll, fg_color="transparent")
        out_sel_bar.pack(fill="x", pady=(0, 2))
        ctk.CTkButton(out_sel_bar, text="☑ 全選", width=55, height=24, font=FONT_SMALL,
                      command=self._select_all_output).pack(side="left", padx=(0, 4))
        ctk.CTkButton(out_sel_bar, text="☐ 全不選", width=65, height=24, font=FONT_SMALL,
                      command=self._deselect_all_output).pack(side="left", padx=(0, 4))
        ctk.CTkButton(out_sel_bar, text="🗑️ 刪除", width=70, height=24, font=FONT_SMALL,
                      fg_color="#b23b3b", hover_color="#8a2e2e",
                      command=self._delete_selected_output).pack(side="right")

        self._p2p_output_count_label = ctk.CTkLabel(
            right_scroll, text="共 0 張 ｜ 已勾選 0 張", font=FONT_SMALL,
            text_color=("gray40", "#9aa0a6"), anchor="w"
        )
        self._p2p_output_count_label.pack(fill="x", pady=(0, 2))

        # Output Thumbnails Scrollable Gallery (v2.05)
        self._p2p_output_gallery_scroll = ctk.CTkScrollableFrame(right_scroll, height=180)
        self._p2p_output_gallery_scroll.pack(fill="both", expand=True, pady=(0, 6))

    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    #  P2P Input & Output Directory Dropdown & History Management (v2.05)
    # ------------------------------------------------------------------ #
    def _populate_p2p_input_dir_menu(self):
        if not hasattr(self, "_p2p_input_dir_menu"):
            return
        default_dir = str(Path(config.resolve_captured_dir(self._settings)).resolve())
        items = [f"★ 預設截圖素材庫 ({Path(default_dir).name})"]
        for d in self._recent_input_dirs[:3]:
            try:
                dp = str(Path(d).resolve())
                if dp != default_dir and Path(dp).is_dir():
                    items.append(f"{Path(dp).name} ({dp})")
            except Exception:
                pass
        items.append("📂 瀏覽自訂資料夾...")
        self._p2p_input_dir_menu.configure(values=items)
        cur_p = str(Path(self._current_input_dir).resolve())
        if cur_p == default_dir:
            self._p2p_input_dir_var.set(items[0])
        else:
            for it in items[1:-1]:
                if cur_p in it:
                    self._p2p_input_dir_var.set(it)
                    break
            else:
                self._p2p_input_dir_var.set(items[0])

    def _on_p2p_input_dir_change(self, choice: str):
        if choice == "📂 瀏覽自訂資料夾...":
            chosen = filedialog.askdirectory(title="選擇輸入素材資料夾", initialdir=self._current_input_dir)
            if not chosen:
                self._populate_p2p_input_dir_menu()
                return
            self._set_current_input_dir(chosen)
        elif choice.startswith("★ 預設截圖素材庫"):
            self._set_current_input_dir(config.resolve_captured_dir(self._settings))
        else:
            m = re.search(r"\((.+)\)$", choice)
            if m and Path(m.group(1)).is_dir():
                self._set_current_input_dir(m.group(1))
            else:
                self._set_current_input_dir(config.resolve_captured_dir(self._settings))

    def _set_current_input_dir(self, target_dir: str):
        target_p = str(Path(target_dir).resolve())
        self._current_input_dir = target_p
        default_dir = str(Path(config.resolve_captured_dir(self._settings)).resolve())
        if target_p != default_dir:
            if target_p in self._recent_input_dirs:
                self._recent_input_dirs.remove(target_p)
            self._recent_input_dirs.insert(0, target_p)
            self._recent_input_dirs = self._recent_input_dirs[:3]
            config.update_recent_dirs(self._settings, recent_input=target_p)
        self._populate_p2p_input_dir_menu()
        self._selected_captured_paths.clear()
        self._refresh_p2p_gallery()

    def _populate_p2p_output_dir_menu(self):
        if not hasattr(self, "_p2p_output_dir_menu"):
            return
        default_dir = str(Path(config.resolve_generated_dir(self._settings)).resolve())
        items = [f"★ 預設 AI 生成庫 ({Path(default_dir).name})"]
        for d in self._recent_output_dirs[:3]:
            try:
                dp = str(Path(d).resolve())
                if dp != default_dir and Path(dp).is_dir():
                    items.append(f"{Path(dp).name} ({dp})")
            except Exception:
                pass
        items.append("📂 瀏覽自訂資料夾...")
        self._p2p_output_dir_menu.configure(values=items)
        cur_p = str(Path(self._current_output_dir).resolve())
        if cur_p == default_dir:
            self._p2p_output_dir_var.set(items[0])
        else:
            for it in items[1:-1]:
                if cur_p in it:
                    self._p2p_output_dir_var.set(it)
                    break
            else:
                self._p2p_output_dir_var.set(items[0])

    def _on_p2p_output_dir_change(self, choice: str):
        if choice == "📂 瀏覽自訂資料夾...":
            chosen = filedialog.askdirectory(title="選擇輸出圖片資料夾", initialdir=self._current_output_dir)
            if not chosen:
                self._populate_p2p_output_dir_menu()
                return
            self._set_current_output_dir(chosen)
        elif choice.startswith("★ 預設 AI 生成庫"):
            self._set_current_output_dir(config.resolve_generated_dir(self._settings))
        else:
            m = re.search(r"\((.+)\)$", choice)
            if m and Path(m.group(1)).is_dir():
                self._set_current_output_dir(m.group(1))
            else:
                self._set_current_output_dir(config.resolve_generated_dir(self._settings))

    def _set_current_output_dir(self, target_dir: str):
        target_p = str(Path(target_dir).resolve())
        self._current_output_dir = target_p
        default_dir = str(Path(config.resolve_generated_dir(self._settings)).resolve())
        if target_p != default_dir:
            if target_p in self._recent_output_dirs:
                self._recent_output_dirs.remove(target_p)
            self._recent_output_dirs.insert(0, target_p)
            self._recent_output_dirs = self._recent_output_dirs[:3]
            config.update_recent_dirs(self._settings, recent_output=target_p)
        self._populate_p2p_output_dir_menu()
        self._selected_output_paths.clear()
        self._refresh_p2p_output_gallery()

    def _open_current_ocr_folder(self):
        d = Path(self._current_ocr_dir)
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(d))
        except Exception as exc:
            messagebox.showerror("開啟失敗", f"無法開啟目錄：{exc}")

    def _open_captured_folder(self):
        self._open_current_input_folder()

    def _open_current_input_folder(self):
        d = Path(self._current_input_dir)
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(d))
        except Exception as exc:
            messagebox.showerror("開啟失敗", f"無法開啟目錄：{exc}")

    def _open_current_output_folder(self):
        d = Path(self._current_output_dir)
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(d))
        except Exception as exc:
            messagebox.showerror("開啟失敗", f"無法開啟目錄：{exc}")

    # ------------------------------------------------------------------ #
    #  P2P Input Gallery Operations
    # ------------------------------------------------------------------ #
    def _refresh_p2p_gallery(self):
        if not hasattr(self, "_p2p_gallery_scroll"):
            return
        self._captured_images = capture_manager.list_images_in_dir(self._current_input_dir)
        self._render_p2p_gallery()

    def _render_p2p_gallery(self):
        for w in self._p2p_gallery_scroll.winfo_children():
            w.destroy()
        self._p2p_gallery_checkboxes.clear()

        total = len(self._captured_images)
        sel_count = len(self._selected_captured_paths)
        if hasattr(self, "_p2p_gallery_count_label"):
            self._p2p_gallery_count_label.configure(text=f"共 {total} 張 ｜ 已勾選 {sel_count} 張")

        if not self._captured_images:
            cap_key = config.get_quick_capture_key_display(self._settings)
            folder_name = Path(self._current_input_dir).name
            empty_lbl = ctk.CTkLabel(
                self._p2p_gallery_scroll,
                text=f"[{folder_name}] 目前無圖片\n可於播放器按 [{cap_key}] 鍵或點擊截圖",
                font=FONT_SMALL, text_color=("gray40", "#9aa0a6")
            )
            empty_lbl.pack(pady=20)
            return

        self._thumb_cache = getattr(self, "_thumb_cache", {})

        for item in self._captured_images:
            p_str = str(item["path"])
            row_frame = ctk.CTkFrame(self._p2p_gallery_scroll, fg_color=("gray85", "gray20"))
            row_frame.pack(fill="x", pady=2, padx=2)

            cb_var = ctk.BooleanVar(value=(p_str in self._selected_captured_paths))
            self._p2p_gallery_checkboxes[p_str] = cb_var

            cb = ctk.CTkCheckBox(
                row_frame, text="", variable=cb_var, width=20,
                command=lambda p=p_str: self._toggle_captured_selection(p)
            )
            cb.pack(side="left", padx=(6, 4))

            # Thumbnail
            thumb_tk = self._thumb_cache.get(p_str)
            if not thumb_tk:
                try:
                    img = Image.open(item["path"])
                    img.thumbnail((64, 36), Image.Resampling.BILINEAR)
                    thumb_tk = ImageTk.PhotoImage(img)
                    self._thumb_cache[p_str] = thumb_tk
                except Exception:
                    thumb_tk = None

            if thumb_tk:
                thumb_lbl = tk.Label(row_frame, image=thumb_tk, bg="#202020", cursor="hand2")
                thumb_lbl.pack(side="left", padx=(0, 6), pady=3)
                thumb_lbl.bind("<Button-1>", lambda _e, p=p_str: self._set_active_p2p_image(p))

            info_col = ctk.CTkFrame(row_frame, fg_color="transparent")
            info_col.pack(side="left", fill="x", expand=True)

            name_lbl = ctk.CTkLabel(info_col, text=item["name"], font=FONT_SMALL, anchor="w", cursor="hand2")
            name_lbl.pack(fill="x")
            name_lbl.bind("<Button-1>", lambda _e, p=p_str: self._set_active_p2p_image(p))

            time_lbl = ctk.CTkLabel(info_col, text=item.get("date_str", ""), font=("Consolas", 10),
                                    text_color=("gray40", "#888"), anchor="w")
            time_lbl.pack(fill="x")

        # Automatically select first item if none active
        if not self._active_edit_path and self._captured_images:
            self._set_active_p2p_image(str(self._captured_images[0]["path"]))

    def _select_all_captured(self):
        for item in self._captured_images:
            self._selected_captured_paths.add(str(item["path"]))
        for var in self._p2p_gallery_checkboxes.values():
            var.set(True)
        if hasattr(self, "_p2p_gallery_count_label"):
            self._p2p_gallery_count_label.configure(
                text=f"共 {len(self._captured_images)} 張 ｜ 已勾選 {len(self._selected_captured_paths)} 張"
            )

    def _deselect_all_captured(self):
        self._selected_captured_paths.clear()
        for var in self._p2p_gallery_checkboxes.values():
            var.set(False)
        if hasattr(self, "_p2p_gallery_count_label"):
            self._p2p_gallery_count_label.configure(
                text=f"共 {len(self._captured_images)} 張 ｜ 已勾選 0 張"
            )

    def _toggle_captured_selection(self, p_str: str):
        var = self._p2p_gallery_checkboxes.get(p_str)
        if var and var.get():
            self._selected_captured_paths.add(p_str)
        else:
            self._selected_captured_paths.discard(p_str)
        if hasattr(self, "_p2p_gallery_count_label"):
            self._p2p_gallery_count_label.configure(
                text=f"共 {len(self._captured_images)} 張 ｜ 已勾選 {len(self._selected_captured_paths)} 張"
            )

    def _delete_selected_captured(self):
        if not self._selected_captured_paths:
            messagebox.showinfo("提示", "請先勾選欲刪除的截圖。")
            return
        count = len(self._selected_captured_paths)
        if not messagebox.askyesno("確認移至暫存", f"確定要將這 {count} 張截圖移至歷史暫存 (.drop) 嗎？"):
            return
        cap_drop_dir = config.resolve_captured_drop_dir(self._settings)
        for p_str in list(self._selected_captured_paths):
            try:
                capture_manager.safe_move_to_drop(p_str, cap_drop_dir)
            except Exception:
                pass
        self._selected_captured_paths.clear()
        self._active_edit_path = None
        self._active_edit_img = None
        self._original_active_img = None
        self._refresh_p2p_gallery()

    def _toggle_p2p_history_mode(self):
        self._p2p_history_mode = not self._p2p_history_mode
        self._selected_captured_paths.clear()
        self._selected_p2p_drop_paths.clear()
        if self._p2p_history_mode:
            self._p2p_history_toggle_btn.configure(text="📋 返回素材庫")
            if hasattr(self, "_p2p_normal_sel_btns"):
                self._p2p_normal_sel_btns.pack_forget()
            if hasattr(self, "_p2p_history_sel_btns"):
                self._p2p_history_sel_btns.pack(fill="x")
            self._refresh_p2p_drop_gallery()
        else:
            self._p2p_history_toggle_btn.configure(text="📦 歷史素材")
            if hasattr(self, "_p2p_history_sel_btns"):
                self._p2p_history_sel_btns.pack_forget()
            if hasattr(self, "_p2p_normal_sel_btns"):
                self._p2p_normal_sel_btns.pack(fill="x")
            self._refresh_p2p_gallery()

    def _refresh_p2p_drop_gallery(self):
        if not hasattr(self, "_p2p_gallery_scroll"):
            return
        for w in self._p2p_gallery_scroll.winfo_children():
            w.destroy()
        self._p2p_drop_checkboxes.clear()

        cap_drop_dir = config.resolve_captured_drop_dir(self._settings)
        drop_images = capture_manager.list_images_in_dir(cap_drop_dir)
        total = len(drop_images)
        sel_count = len(self._selected_p2p_drop_paths)

        if hasattr(self, "_p2p_gallery_count_label"):
            self._p2p_gallery_count_label.configure(
                text=f"歷史素材 (.drop) (共 {total} 張 ｜ 已勾選 {sel_count} 張)"
            )

        if not drop_images:
            empty_lbl = ctk.CTkLabel(
                self._p2p_gallery_scroll,
                text="歷史素材暫存資料夾 (.drop) 目前沒有截圖檔案。\n從素材庫刪除的截圖將自動保存在此。",
                font=FONT_SMALL, text_color=("gray40", "#9aa0a6")
            )
            empty_lbl.pack(pady=20)
            return

        for item in drop_images:
            p_str = str(item["path"])
            row_frame = ctk.CTkFrame(self._p2p_gallery_scroll, fg_color=("gray85", "gray20"))
            row_frame.pack(fill="x", pady=2, padx=2)

            cb = ctk.CTkCheckBox(
                row_frame, text="", width=24, checkbox_width=20, checkbox_height=20,
                command=lambda p=p_str: self._on_p2p_drop_cb_toggle(p)
            )
            cb.pack(side="left", padx=(6, 2))
            if p_str in self._selected_p2p_drop_paths:
                cb.set(True)
            self._p2p_drop_checkboxes[p_str] = cb

            c = tk.Canvas(row_frame, width=54, height=36, bg="#1a1a1a", highlightthickness=0)
            c.pack(side="left", padx=4, pady=2)
            try:
                im = Image.open(item["path"]).convert("RGB")
                im.thumbnail((54, 36), Image.Resampling.BILINEAR)
                th_tk = ImageTk.PhotoImage(im)
                c.create_image(27, 18, anchor="center", image=th_tk)
                c.image = th_tk
            except Exception:
                pass

            info_box = ctk.CTkFrame(row_frame, fg_color="transparent")
            info_box.pack(side="left", fill="both", expand=True, padx=4)

            name_lbl = ctk.CTkLabel(info_box, text=item["name"], font=FONT_SMALL, anchor="w")
            name_lbl.pack(fill="x")
            dim_lbl = ctk.CTkLabel(
                info_box, text=f"{item['size']//1024} KB | {item['date_str']}",
                font=("Microsoft JhengHei", 9), text_color=("gray40", "#9aa0a6"), anchor="w"
            )
            dim_lbl.pack(fill="x")

            ctk.CTkButton(
                row_frame, text="復原", width=50, height=24, font=FONT_SMALL,
                fg_color="#2e7d32", hover_color="#1b5e20",
                command=lambda p=p_str: self._restore_single_p2p_drop(p)
            ).pack(side="right", padx=6)

    def _on_p2p_drop_cb_toggle(self, p_str: str):
        if p_str in self._selected_p2p_drop_paths:
            self._selected_p2p_drop_paths.remove(p_str)
        else:
            self._selected_p2p_drop_paths.add(p_str)
        cap_drop_dir = config.resolve_captured_drop_dir(self._settings)
        items = capture_manager.list_images_in_dir(cap_drop_dir)
        if hasattr(self, "_p2p_gallery_count_label"):
            self._p2p_gallery_count_label.configure(
                text=f"歷史素材 (.drop) (共 {len(items)} 張 ｜ 已勾選 {len(self._selected_p2p_drop_paths)} 張)"
            )

    def _select_all_p2p_drop(self):
        cap_drop_dir = config.resolve_captured_drop_dir(self._settings)
        items = capture_manager.list_images_in_dir(cap_drop_dir)
        for it in items:
            self._selected_p2p_drop_paths.add(str(it["path"]))
        for cb in self._p2p_drop_checkboxes.values():
            cb.set(True)
        if hasattr(self, "_p2p_gallery_count_label"):
            self._p2p_gallery_count_label.configure(
                text=f"歷史素材 (.drop) (共 {len(items)} 張 ｜ 已勾選 {len(self._selected_p2p_drop_paths)} 張)"
            )

    def _deselect_all_p2p_drop(self):
        self._selected_p2p_drop_paths.clear()
        for cb in self._p2p_drop_checkboxes.values():
            cb.set(False)
        cap_drop_dir = config.resolve_captured_drop_dir(self._settings)
        items = capture_manager.list_images_in_dir(cap_drop_dir)
        if hasattr(self, "_p2p_gallery_count_label"):
            self._p2p_gallery_count_label.configure(
                text=f"歷史素材 (.drop) (共 {len(items)} 張 ｜ 已勾選 0 張)"
            )

    def _restore_single_p2p_drop(self, p_str: str):
        self._restore_p2p_drop_items([p_str])

    def _restore_selected_p2p_drop(self):
        if not self._selected_p2p_drop_paths:
            messagebox.showinfo("提示", "請先勾選欲復原的截圖。")
            return
        self._restore_p2p_drop_items(list(self._selected_p2p_drop_paths))

    def _restore_p2p_drop_items(self, paths: list[str]):
        count = 0
        for p_str in paths:
            restored = capture_manager.safe_restore_from_drop(p_str, self._current_input_dir)
            if restored:
                count += 1
        self._selected_p2p_drop_paths.clear()
        self._toggle_p2p_history_mode()
        messagebox.showinfo("復原成功", f"已成功復原 {count} 張截圖至截圖素材庫！")

    def _clear_p2p_drop(self):
        cap_drop_dir = config.resolve_captured_drop_dir(self._settings)
        drop_images = capture_manager.list_images_in_dir(cap_drop_dir)
        if not drop_images:
            messagebox.showinfo("提示", "目前沒有歷史素材暫存檔可清空。")
            return
        if not messagebox.askyesno("清空確認", f"確定要永久清空所有 {len(drop_images)} 張暫存截圖嗎？此動作無法復原。"):
            return
        count = capture_manager.clear_drop_dir(cap_drop_dir)
        self._selected_p2p_drop_paths.clear()
        self._refresh_p2p_drop_gallery()
        messagebox.showinfo("清空完成", f"已永久刪除 {count} 張暫存截圖。")

    def _copy_selected_input_to_output(self):
        if not self._selected_captured_paths:
            messagebox.showinfo("提示", "請先在輸入圖庫勾選至少一張圖片進行拷貝。")
            return
        copied = capture_manager.copy_images_to_directory(self._selected_captured_paths, self._current_output_dir)
        if copied:
            copied_str_set = {str(p) for p in copied}
            self._selected_output_paths.update(copied_str_set)
            self._refresh_p2p_output_gallery()
            self._set_active_output_image(str(copied[0]))
            for p_str, cb in self._p2p_output_checkboxes.items():
                if p_str in copied_str_set:
                    cb.set(True)
            if hasattr(self, "_p2p_output_count_label"):
                self._p2p_output_count_label.configure(
                    text=f"共 {len(self._output_images)} 張 ｜ 已勾選 {len(self._selected_output_paths)} 張"
                )
            messagebox.showinfo("拷貝成功", f"已成功拷貝 {len(copied)} 張圖片至輸出圖庫，並已自動勾選！")

    def _set_active_p2p_image(self, p_str: str):
        self._active_edit_path = p_str
        self._edit_rotation = 0
        self._edit_flip_h = False
        try:
            self._original_active_img = Image.open(p_str).convert("RGB")
            self._active_edit_img = self._original_active_img.copy()
        except Exception as exc:
            messagebox.showerror("讀取失敗", f"無法載入圖片：{exc}")
            return
        self._selected_captured_paths.add(p_str)
        if p_str in self._p2p_gallery_checkboxes:
            self._p2p_gallery_checkboxes[p_str].set(True)
        if hasattr(self, "_p2p_gallery_count_label"):
            self._p2p_gallery_count_label.configure(
                text=f"共 {len(self._captured_images)} 張 ｜ 已勾選 {len(self._selected_captured_paths)} 張"
            )
        self._render_active_p2p_canvas()

    # ------------------------------------------------------------------ #
    #  P2P Output Gallery Operations (v2.05)
    # ------------------------------------------------------------------ #
    def _refresh_p2p_output_gallery(self):
        if not hasattr(self, "_p2p_output_gallery_scroll"):
            return
        self._output_images = capture_manager.list_images_in_dir(self._current_output_dir)
        self._render_p2p_output_gallery()

    def _render_p2p_output_gallery(self):
        for w in self._p2p_output_gallery_scroll.winfo_children():
            w.destroy()
        self._p2p_output_checkboxes.clear()

        total = len(self._output_images)
        sel_count = len(self._selected_output_paths)
        if hasattr(self, "_p2p_output_count_label"):
            self._p2p_output_count_label.configure(text=f"共 {total} 張 ｜ 已勾選 {sel_count} 張")

        if not self._output_images:
            folder_name = Path(self._current_output_dir).name
            empty_lbl = ctk.CTkLabel(
                self._p2p_output_gallery_scroll,
                text=f"[{folder_name}] 目前無生成圖片\n點擊「開始 AI 以圖生圖」生成新成果",
                font=FONT_SMALL, text_color=("gray40", "#9aa0a6")
            )
            empty_lbl.pack(pady=20)
            return

        self._thumb_cache = getattr(self, "_thumb_cache", {})

        for item in self._output_images:
            p_str = str(item["path"])
            row_frame = ctk.CTkFrame(self._p2p_output_gallery_scroll, fg_color=("gray85", "gray20"))
            row_frame.pack(fill="x", pady=2, padx=2)

            cb_var = ctk.BooleanVar(value=(p_str in self._selected_output_paths))
            self._p2p_output_checkboxes[p_str] = cb_var

            cb = ctk.CTkCheckBox(
                row_frame, text="", variable=cb_var, width=20,
                command=lambda p=p_str: self._toggle_output_selection(p)
            )
            cb.pack(side="left", padx=(6, 4))

            # Thumbnail
            thumb_tk = self._thumb_cache.get(p_str)
            if not thumb_tk:
                try:
                    img = Image.open(item["path"])
                    img.thumbnail((64, 36), Image.Resampling.BILINEAR)
                    thumb_tk = ImageTk.PhotoImage(img)
                    self._thumb_cache[p_str] = thumb_tk
                except Exception:
                    thumb_tk = None

            if thumb_tk:
                thumb_lbl = tk.Label(row_frame, image=thumb_tk, bg="#202020", cursor="hand2")
                thumb_lbl.pack(side="left", padx=(0, 6), pady=3)
                thumb_lbl.bind("<Button-1>", lambda _e, p=p_str: self._set_active_output_image(p))

            info_col = ctk.CTkFrame(row_frame, fg_color="transparent")
            info_col.pack(side="left", fill="x", expand=True)

            name_lbl = ctk.CTkLabel(info_col, text=item["name"], font=FONT_SMALL, anchor="w", cursor="hand2")
            name_lbl.pack(fill="x")
            name_lbl.bind("<Button-1>", lambda _e, p=p_str: self._set_active_output_image(p))

            time_lbl = ctk.CTkLabel(info_col, text=item.get("date_str", ""), font=("Consolas", 10),
                                    text_color=("gray40", "#888"), anchor="w")
            time_lbl.pack(fill="x")

    def _set_active_output_image(self, p_str: str):
        try:
            self._current_gen_img = Image.open(p_str).convert("RGB")
            self._render_result_canvas()
        except Exception as exc:
            messagebox.showerror("讀取失敗", f"無法載入生成圖片：{exc}")

    def _select_all_output(self):
        for item in self._output_images:
            self._selected_output_paths.add(str(item["path"]))
        for var in self._p2p_output_checkboxes.values():
            var.set(True)
        if hasattr(self, "_p2p_output_count_label"):
            self._p2p_output_count_label.configure(
                text=f"共 {len(self._output_images)} 張 ｜ 已勾選 {len(self._selected_output_paths)} 張"
            )

    def _deselect_all_output(self):
        self._selected_output_paths.clear()
        for var in self._p2p_output_checkboxes.values():
            var.set(False)
        if hasattr(self, "_p2p_output_count_label"):
            self._p2p_output_count_label.configure(
                text=f"共 {len(self._output_images)} 張 ｜ 已勾選 0 張"
            )

    def _toggle_output_selection(self, p_str: str):
        var = self._p2p_output_checkboxes.get(p_str)
        if var and var.get():
            self._selected_output_paths.add(p_str)
        else:
            self._selected_output_paths.discard(p_str)
        if hasattr(self, "_p2p_output_count_label"):
            self._p2p_output_count_label.configure(
                text=f"共 {len(self._output_images)} 張 ｜ 已勾選 {len(self._selected_output_paths)} 張"
            )

    def _delete_selected_output(self):
        if not self._selected_output_paths:
            messagebox.showinfo("提示", "請先勾選欲刪除的生成圖片。")
            return
        count = len(self._selected_output_paths)
        if not messagebox.askyesno("確認刪除", f"確定要從硬碟刪除這 {count} 張生成圖片嗎？此操作無法復原。"):
            return
        for p_str in list(self._selected_output_paths):
            try:
                p = Path(p_str)
                if p.is_file():
                    p.unlink()
            except Exception:
                pass
        self._selected_output_paths.clear()
        self._refresh_p2p_output_gallery()

    def _copy_selected_output_to_input(self):
        if not self._selected_output_paths:
            messagebox.showinfo("提示", "請先在輸出圖庫勾選至少一張圖片進行拷貝。")
            return
        copied = capture_manager.copy_images_to_directory(self._selected_output_paths, self._current_input_dir)
        if copied:
            copied_str_set = {str(p) for p in copied}
            self._selected_captured_paths.update(copied_str_set)
            self._refresh_p2p_gallery()
            self._set_active_p2p_image(str(copied[0]))
            for p_str, cb in self._p2p_gallery_checkboxes.items():
                if p_str in copied_str_set:
                    cb.set(True)
            if hasattr(self, "_p2p_gallery_count_label"):
                self._p2p_gallery_count_label.configure(
                    text=f"共 {len(self._captured_images)} 張 ｜ 已勾選 {len(self._selected_captured_paths)} 張"
                )
            messagebox.showinfo("拷貝成功", f"已成功拷貝 {len(copied)} 張圖片至輸入圖庫，並已自動勾選！")

    # ------------------------------------------------------------------ #
    #  P2P Image Editing Pipeline (Rotate & Flip)
    # ------------------------------------------------------------------ #
    def _rotate_p2p_image(self, deg: int):
        if not self._original_active_img:
            return
        self._edit_rotation = (self._edit_rotation + deg) % 360
        self._apply_edits_and_render()

    def _flip_p2p_image(self):
        if not self._original_active_img:
            return
        self._edit_flip_h = not self._edit_flip_h
        self._apply_edits_and_render()

    def _reset_p2p_image(self):
        if not self._original_active_img:
            return
        self._edit_rotation = 0
        self._edit_flip_h = False
        self._apply_edits_and_render()

    def _apply_edits_and_render(self):
        if not self._original_active_img:
            return
        img = capture_manager.rotate_image(self._original_active_img, self._edit_rotation)
        if self._edit_flip_h:
            img = capture_manager.flip_image(img, horizontal=True)
        self._active_edit_img = img
        self._render_active_p2p_canvas()

    def _save_p2p_edited_as_new(self):
        if not self._active_edit_img:
            messagebox.showinfo("提示", "目前沒有正在編輯的圖片。")
            return
        orig_p = getattr(self, "_active_p2p_image_path", "")
        saved_p = capture_manager.save_edited_image(
            img=self._active_edit_img,
            original_path=orig_p,
            target_dir=self._current_input_dir,
        )
        self._refresh_p2p_gallery()
        self._set_active_p2p_image(str(saved_p))
        messagebox.showinfo("儲存成功", f"編輯後的圖片已另存為：\n{saved_p.name}")

    def _toggle_p2p_advanced_params(self):
        if not hasattr(self, "_p2p_adv_frame"):
            return
        if self._p2p_adv_open:
            self._p2p_adv_frame.pack_forget()
            self._p2p_adv_open = False
            self._p2p_adv_toggle_btn.configure(text="⚙️ 展開進階繪圖參數 (強度 / 畫質 / Seed / JSON...) ▼")
        else:
            self._p2p_adv_frame.pack(after=self._p2p_adv_toggle_btn, fill="x", pady=(0, 4))
            self._p2p_adv_open = True
            self._p2p_adv_toggle_btn.configure(text="⚙️ 收合進階繪圖參數 ▲")

    def _on_p2p_preview_toggle(self):
        """防窺模式切換：重新渲染畫布。"""
        self._render_active_p2p_canvas()
        self._render_result_canvas()

    def _render_active_p2p_canvas(self):
        if not hasattr(self, "_p2p_edit_canvas"):
            return
        cw = max(10, self._p2p_edit_canvas.winfo_width())
        ch = max(10, self._p2p_edit_canvas.winfo_height())
        self._p2p_edit_canvas.delete("all")

        # Privacy Mode: Pure Black Canvas (防窺黑屏模式)
        if not self._p2p_preview_visible_var.get():
            self._p2p_edit_canvas.create_rectangle(0, 0, cw, ch, fill="#050505", outline="")
            self._p2p_edit_canvas.create_text(
                cw // 2, ch // 2,
                text="[ 圖片預覽已關閉 (防窺黑屏模式) ]",
                fill="#555555", font=FONT_BODY
            )
            return

        if not self._active_edit_img:
            return

        iw, ih = self._active_edit_img.size
        scale = min(cw / iw, ch / ih)
        disp_w = max(1, int(iw * scale))
        disp_h = max(1, int(ih * scale))

        disp_img = self._active_edit_img.resize((disp_w, disp_h), Image.Resampling.BILINEAR)
        self._p2p_canvas_tk = ImageTk.PhotoImage(disp_img)
        self._p2p_edit_canvas.create_image(cw // 2, ch // 2, anchor="center", image=self._p2p_canvas_tk)

        if hasattr(self, "_p2p_edit_info_label"):
            flip_str = "是" if self._edit_flip_h else "否"
            self._p2p_edit_info_label.configure(
                text=f"原尺寸: {iw}x{ih} ｜ 旋轉: {self._edit_rotation}° ｜ 水平翻轉: {flip_str}"
            )

    def _render_result_canvas(self):
        if not hasattr(self, "_p2p_result_canvas"):
            return
        cw = max(10, self._p2p_result_canvas.winfo_width())
        ch = max(10, self._p2p_result_canvas.winfo_height())
        self._p2p_result_canvas.delete("all")

        # Privacy Mode: Pure Black Canvas (防窺黑屏模式)
        if not self._p2p_preview_visible_var.get():
            self._p2p_result_canvas.create_rectangle(0, 0, cw, ch, fill="#050505", outline="")
            self._p2p_result_canvas.create_text(
                cw // 2, ch // 2,
                text="[ 圖片預覽已關閉 (防窺黑屏模式) ]",
                fill="#555555", font=FONT_BODY
            )
            return

        if not self._current_gen_img:
            return

        iw, ih = self._current_gen_img.size
        scale = min(cw / iw, ch / ih)
        disp_w = max(1, int(iw * scale))
        disp_h = max(1, int(ih * scale))

        disp_img = self._current_gen_img.resize((disp_w, disp_h), Image.Resampling.BILINEAR)
        self._p2p_result_tk = ImageTk.PhotoImage(disp_img)
        self._p2p_result_canvas.create_image(cw // 2, ch // 2, anchor="center", image=self._p2p_result_tk)

    def _on_p2p_aspect_ratio_change(self, choice: str):
        if choice == "自定義":
            if hasattr(self, "_p2p_size_preset_frame"):
                self._p2p_size_preset_frame.pack_forget()
            if hasattr(self, "_p2p_custom_size_frame"):
                self._p2p_custom_size_frame.pack(fill="x")
        else:
            if hasattr(self, "_p2p_custom_size_frame"):
                self._p2p_custom_size_frame.pack_forget()
            if hasattr(self, "_p2p_size_preset_frame"):
                self._p2p_size_preset_frame.pack(fill="x")
            presets = image_gen_engine.ASPECT_RATIO_SIZE_PRESETS.get(choice, ["auto", "自定義..."])
            if hasattr(self, "_p2p_size_menu"):
                self._p2p_size_menu.configure(values=presets)
                cur_s = self._p2p_size_var.get()
                if cur_s not in presets:
                    self._p2p_size_var.set("auto")

    def _on_p2p_size_preset_change(self, choice: str):
        if choice == "自定義...":
            self._p2p_aspect_var.set("自定義")
            self._on_p2p_aspect_ratio_change("自定義")

    # ------------------------------------------------------------------ #
    #  P2P Generation & Result Management
    # ------------------------------------------------------------------ #
    def _sync_p2p_prompt_controls(self):
        if not hasattr(self, "_p2p_prov_menu"):
            return
        prov_list = [p.get("name", "") for p in self._p2p_settings.get("providers", [])]
        if prov_list:
            self._p2p_prov_menu.configure(values=prov_list)
            cur = self._p2p_prov_var.get()
            if cur not in prov_list:
                self._p2p_prov_var.set(prov_list[0])
            self._on_p2p_provider_change(self._p2p_prov_var.get())

        preset_list = [p.get("name", "") for p in self._p2p_settings.get("drawing_prompts", [])]
        if preset_list:
            self._p2p_preset_menu.configure(values=preset_list)
            cur_p = self._p2p_preset_var.get()
            if cur_p not in preset_list:
                self._p2p_preset_var.set(preset_list[0])
            self._on_p2p_preset_change(self._p2p_preset_var.get())

    def _on_p2p_provider_change(self, choice: str):
        for p in self._p2p_settings.get("providers", []):
            if p.get("name") == choice:
                models = p.get("models") or ["default"]
                if hasattr(self, "_p2p_model_menu"):
                    self._p2p_model_menu.configure(values=models)
                    sel_m = p.get("selected_model") or models[0]
                    self._p2p_model_var.set(sel_m)
                break
        self._sync_p2p_model_capabilities()

    def _on_p2p_model_change(self, choice: str):
        prov_name = self._p2p_prov_var.get()
        for p in self._p2p_settings.get("providers", []):
            if p.get("name") == prov_name:
                p["selected_model"] = choice
                break
        self._sync_p2p_model_capabilities()

    def _sync_p2p_model_capabilities(self):
        """根據當前選取的服務商與模型，動態啟用或反灰 (gray out) 控制項與更新標籤。"""
        if not hasattr(self, "_p2p_prov_var") or not hasattr(self, "_p2p_model_var"):
            return
        prov_name = self._p2p_prov_var.get()
        target_prov = None
        for p in self._p2p_settings.get("providers", []):
            if p.get("name") == prov_name:
                target_prov = dict(p)
                break
        if not target_prov:
            return

        target_prov["selected_model"] = self._p2p_model_var.get()
        cfg = P2PProviderCfg.from_dict(target_prov)
        caps = image_gen_engine.sniff_model_capabilities(cfg)

        # 1. Negative Prompt
        if hasattr(self, "_p2p_neg_prompt_box") and hasattr(self, "_p2p_neg_prompt_label"):
            sup_neg = caps.get("supports_negative_prompt", True)
            if sup_neg:
                self._p2p_neg_prompt_box.configure(state="normal")
                self._p2p_neg_prompt_label.configure(
                    text="🚫 負向提示詞 (Negative Prompt)：",
                    text_color=("gray10", "gray90")
                )
            else:
                self._p2p_neg_prompt_box.configure(state="disabled")
                self._p2p_neg_prompt_label.configure(
                    text="🚫 負向提示詞 [此模型不支援]：",
                    text_color=("gray50", "gray50")
                )

        # 2. Strength
        if hasattr(self, "_p2p_strength_slider") and hasattr(self, "_p2p_strength_label_title"):
            sup_str = caps.get("supports_strength", True)
            if sup_str:
                self._p2p_strength_slider.configure(state="normal")
                self._p2p_strength_label_title.configure(
                    text="重繪強度：",
                    text_color=("gray10", "gray90")
                )
            else:
                self._p2p_strength_slider.configure(state="disabled")
                self._p2p_strength_label_title.configure(
                    text="重繪強度 [不支援]：",
                    text_color=("gray50", "gray50")
                )

        # 3. Output Count
        if hasattr(self, "_p2p_count_menu"):
            sup_count = caps.get("supports_num_outputs", True)
            self._p2p_count_menu.configure(state="normal" if sup_count else "disabled")
            if not sup_count:
                self._p2p_count_var.set("1")

        # 4. Aspect Ratio
        if hasattr(self, "_p2p_aspect_menu"):
            sup_aspect = caps.get("supports_aspect_ratio", True)
            self._p2p_aspect_menu.configure(state="normal" if sup_aspect else "disabled")

        # 4b. Resolution (v2.05)
        if hasattr(self, "_p2p_res_menu") and hasattr(self, "_p2p_res_label"):
            sup_res = caps.get("supports_resolution", True)
            self._p2p_res_menu.configure(state="normal" if sup_res else "disabled")
            self._p2p_res_label.configure(
                text="解析度：" if sup_res else "解析度 [不支援]：",
                text_color=("gray10", "gray90") if sup_res else ("gray50", "gray50")
            )
            if not sup_res:
                self._p2p_res_var.set("auto")

        # 5. Size / Resolution
        if hasattr(self, "_p2p_size_menu") and hasattr(self, "_p2p_size_label"):
            sup_size = caps.get("supports_size", True)
            self._p2p_size_menu.configure(state="normal" if sup_size else "disabled")
            self._p2p_size_label.configure(
                text="尺寸規格：" if sup_size else "尺寸 [不支援]：",
                text_color=("gray10", "gray90") if sup_size else ("gray50", "gray50")
            )
            if not sup_size:
                self._p2p_size_var.set("auto")

        # 6. Quality
        if hasattr(self, "_p2p_quality_menu") and hasattr(self, "_p2p_quality_label"):
            sup_qual = caps.get("supports_quality", False)
            self._p2p_quality_menu.configure(state="normal" if sup_qual else "disabled")
            self._p2p_quality_label.configure(
                text="畫質等級：" if sup_qual else "畫質 [不支援]：",
                text_color=("gray10", "gray90") if sup_qual else ("gray50", "gray50")
            )
            if not sup_qual:
                self._p2p_quality_var.set("auto")

        # 7. Background
        if hasattr(self, "_p2p_bg_menu") and hasattr(self, "_p2p_bg_label"):
            sup_bg = caps.get("supports_background", False)
            self._p2p_bg_menu.configure(state="normal" if sup_bg else "disabled")
            self._p2p_bg_label.configure(
                text="背景：" if sup_bg else "背景 [不支援]：",
                text_color=("gray10", "gray90") if sup_bg else ("gray50", "gray50")
            )
            if not sup_bg:
                self._p2p_bg_var.set("auto")

        # 8. Seed
        if hasattr(self, "_p2p_seed_entry") and hasattr(self, "_p2p_seed_label"):
            sup_seed = caps.get("supports_seed", True)
            self._p2p_seed_entry.configure(state="normal" if sup_seed else "disabled")
            self._p2p_seed_label.configure(
                text="種子(Seed)：" if sup_seed else "種子 [不支援]：",
                text_color=("gray10", "gray90") if sup_seed else ("gray50", "gray50")
            )
            if not sup_seed:
                self._p2p_seed_var.set("")

        # 9. Output Format
        if hasattr(self, "_p2p_format_menu") and hasattr(self, "_p2p_format_label"):
            sup_fmt = caps.get("supports_output_format", True)
            self._p2p_format_menu.configure(state="normal" if sup_fmt else "disabled")
            self._p2p_format_label.configure(
                text="輸出格式：" if sup_fmt else "格式 [不支援]：",
                text_color=("gray10", "gray90") if sup_fmt else ("gray50", "gray50")
            )
            if not sup_fmt:
                self._p2p_format_var.set("auto")

        # 10. Update status hint if idle
        if hasattr(self, "_p2p_status_label") and not self._p2p_busy:
            max_refs = caps.get("max_input_references", 1)
            self._p2p_status_label.configure(
                text=f"模型：{cfg.model}（最多支援 {max_refs} 張參考圖）"
            )

    def _on_p2p_preset_change(self, choice: str):
        for p in self._p2p_settings.get("drawing_prompts", []):
            if p.get("name") == choice:
                if hasattr(self, "_p2p_pos_prompt_box"):
                    self._p2p_pos_prompt_box.delete("1.0", "end")
                    self._p2p_pos_prompt_box.insert("1.0", p.get("positive", ""))
                if hasattr(self, "_p2p_neg_prompt_box"):
                    prev_state = self._p2p_neg_prompt_box.cget("state")
                    self._p2p_neg_prompt_box.configure(state="normal")
                    self._p2p_neg_prompt_box.delete("1.0", "end")
                    self._p2p_neg_prompt_box.insert("1.0", p.get("negative", ""))
                    self._p2p_neg_prompt_box.configure(state=prev_state)
                break

    def _start_p2p_generate(self):
        if self._p2p_busy:
            return

        selected_imgs: list[Image.Image] = []
        for p_str in self._selected_captured_paths:
            if p_str == self._active_edit_path and self._active_edit_img:
                selected_imgs.append(self._active_edit_img.copy())
            else:
                try:
                    selected_imgs.append(Image.open(p_str).convert("RGB"))
                except Exception:
                    pass

        if not selected_imgs and self._active_edit_img:
            selected_imgs.append(self._active_edit_img.copy())

        if not selected_imgs:
            messagebox.showwarning("缺少參考圖", "請先在左側勾選至少一張截圖作為參考素材。")
            return

        prov_name = self._p2p_prov_var.get()
        target_prov = None
        for p in self._p2p_settings.get("providers", []):
            if p.get("name") == prov_name:
                target_prov = dict(p)
                break
        if not target_prov:
            messagebox.showwarning("未選定服務商", "請先選擇有效的圖生圖服務商。")
            return

        target_prov["selected_model"] = self._p2p_model_var.get()
        cfg = P2PProviderCfg.from_dict(target_prov)
        cfg.api_key = config.resolve_p2p_api_key(target_prov)

        pos_prompt = self._p2p_pos_prompt_box.get("1.0", "end").strip()
        neg_prompt = self._p2p_neg_prompt_box.get("1.0", "end").strip()

        # Collect parameters (v2.05)
        aspect = self._p2p_aspect_var.get()
        if aspect == "自定義":
            try:
                cw = int(self._p2p_custom_w_var.get().strip())
                ch = int(self._p2p_custom_h_var.get().strip())
                if cw <= 0 or ch <= 0:
                    raise ValueError
                adv_size = f"{cw}x{ch}"
                aspect = image_gen_engine.resolve_closest_aspect_ratio(adv_size)
            except Exception:
                messagebox.showerror("自訂尺寸格式錯誤", "自訂寬度 (W) 與高度 (H) 必須為正整數 px，例如 1024 與 768。")
                return
        else:
            adv_size = self._p2p_size_var.get().strip() if hasattr(self, "_p2p_size_var") else "auto"
            if adv_size == "自定義...":
                adv_size = "auto"

        res_val = self._p2p_res_var.get().strip() if hasattr(self, "_p2p_res_var") else "auto"
        strength = float(self._p2p_strength_var.get())
        adv_quality = self._p2p_quality_var.get().strip() if hasattr(self, "_p2p_quality_var") else "auto"
        adv_bg = self._p2p_bg_var.get().strip() if hasattr(self, "_p2p_bg_var") else "auto"
        adv_fmt = self._p2p_format_var.get().strip() if hasattr(self, "_p2p_format_var") else "auto"
        try:
            num_outputs = int(self._p2p_count_var.get()) if hasattr(self, "_p2p_count_var") else 1
        except Exception:
            num_outputs = 1

        seed_val = None
        if hasattr(self, "_p2p_seed_var"):
            seed_txt = self._p2p_seed_var.get().strip()
            if seed_txt:
                try:
                    seed_val = int(seed_txt)
                except ValueError:
                    messagebox.showerror("隨機種子格式錯誤", "隨機種子 (Seed) 必須為整數，例如 42。若要隨機請留空。")
                    return

        adv_extra = None
        if hasattr(self, "_p2p_extra_json_entry"):
            extra_txt = self._p2p_extra_json_entry.get().strip()
            if extra_txt:
                import json
                try:
                    adv_extra = json.loads(extra_txt)
                    if not isinstance(adv_extra, dict):
                        raise ValueError("自訂參數必須為 JSON 物件 (字典)，例如 {\"seed\": 42}")
                except Exception as je:
                    messagebox.showerror("自訂 JSON 格式錯誤", f"進階自訂 JSON 參數解析失敗：\n{je}")
                    return

        self._p2p_busy = True
        self._p2p_gen_btn.configure(state="disabled", text="⏳ AI 繪圖中...")
        self._p2p_status_label.configure(text=f"正在請求 {cfg.name} 生成新圖，請稍候...")

        def work():
            try:
                imgs = generate_image(
                    cfg=cfg,
                    images=selected_imgs,
                    prompt=pos_prompt,
                    negative_prompt=neg_prompt,
                    aspect_ratio=aspect,
                    strength=strength,
                    num_outputs=num_outputs,
                    size=adv_size,
                    resolution=res_val,
                    quality=adv_quality,
                    background=adv_bg,
                    output_format=adv_fmt,
                    seed=seed_val,
                    extra_params=adv_extra,
                )
                if imgs:
                    self._current_gen_img = imgs[0]
                    saved_p = capture_manager.save_generated_image(
                        self._current_gen_img,
                        prompt_name=self._p2p_preset_var.get(),
                        model_name=cfg.model,
                        target_dir=self._current_output_dir,
                    )
                    self.after(0, lambda: self._on_p2p_success(saved_p))
                else:
                    raise P2PError("服務端未回傳圖片資料。")
            except Exception as exc:
                self.after(0, lambda e=exc: self._on_p2p_error(str(e)))

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _on_p2p_success(self, saved_path: Path):
        self._p2p_busy = False
        self._p2p_gen_btn.configure(state="normal", text="🎨 開始 AI 以圖生圖")
        self._p2p_status_label.configure(text=f"✅ 生成成功！已存至：{saved_path.name}")
        self._refresh_p2p_output_gallery()
        self._selected_output_paths.add(str(saved_path))
        if str(saved_path) in self._p2p_output_checkboxes:
            self._p2p_output_checkboxes[str(saved_path)].set(True)
        if hasattr(self, "_p2p_output_count_label"):
            self._p2p_output_count_label.configure(
                text=f"共 {len(self._output_images)} 張 ｜ 已勾選 {len(self._selected_output_paths)} 張"
            )
        self._render_result_canvas()

    def _on_p2p_error(self, err_msg: str):
        self._p2p_busy = False
        self._p2p_gen_btn.configure(state="normal", text="🎨 開始 AI 以圖生圖")
        self._p2p_status_label.configure(text=f"❌ 生圖失敗：{err_msg[:60]}")
        messagebox.showerror("生成失敗", f"AI 繪圖發生錯誤：\n{err_msg}")

    def _save_p2p_result(self):
        if not self._current_gen_img:
            return
        prompt_name = self._p2p_preset_var.get() if hasattr(self, "_p2p_preset_var") else "custom"
        model_name = self._p2p_model_var.get() if hasattr(self, "_p2p_model_var") else "model"
        safe_prompt = capture_manager.sanitize_filename_part(prompt_name, max_len=30) if prompt_name else "custom"
        safe_model = capture_manager.simplify_model_name(model_name)
        now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M")
        suggested_name = f"{safe_prompt}_{safe_model}_{now_str}.png"

        target = filedialog.asksaveasfilename(
            defaultextension=".png",
            filetypes=[("PNG 圖片", "*.png"), ("JPEG 圖片", "*.jpg")],
            initialdir=str(self._current_output_dir),
            initialfile=suggested_name,
        )
        if target:
            self._current_gen_img.save(target)
            self._refresh_p2p_output_gallery()
            messagebox.showinfo("儲存完成", f"圖片已成功另存為：\n{target}")

    def _use_result_as_input(self):
        if not self._current_gen_img:
            return
        saved_p = capture_manager.save_captured_image(
            img=self._current_gen_img,
            custom_name="ai_ref",
            target_dir=self._current_input_dir,
        )
        self._refresh_p2p_gallery()
        self._set_active_p2p_image(str(saved_p))
        messagebox.showinfo("已設為參考圖", f"AI 生成圖已存入輸入圖庫並設為當前編輯參考圖：\n{saved_p.name}")

    # ------------------------------------------------------------------ #
    #  P2P Tab 2: API Settings (_build_p2p_api_tab)
    # ------------------------------------------------------------------ #
    def _build_p2p_api_tab(self, parent):
        ctk.CTkLabel(parent, text="圖生圖 API 設定 (settings_p2p.json)", font=FONT_TITLE, anchor="w").pack(
            fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(parent, text="管理支援圖生圖的聚合 API 服務商與繪圖模型，API 金鑰將自動同步儲存於 .env 檔案。",
                     font=FONT_SMALL, text_color=("gray40", "#9aa0a6"), anchor="w").pack(
            fill="x", padx=14, pady=(0, 6))

        card = ctk.CTkFrame(parent)
        card.pack(fill="x", padx=14, pady=(0, 8))

        row1 = ctk.CTkFrame(card, fg_color="transparent")
        row1.pack(fill="x", padx=12, pady=(10, 6))
        ctk.CTkLabel(row1, text="選擇服務商：", font=FONT_BODY).pack(side="left")

        prov_names = [p.get("name", "") for p in self._p2p_settings.get("providers", [])]
        self._p2p_api_prov_var = ctk.StringVar(value=prov_names[0] if prov_names else "")
        self._p2p_api_prov_menu = ctk.CTkOptionMenu(
            row1, values=prov_names or ["（無）"], variable=self._p2p_api_prov_var, font=FONT_BODY,
            command=self._on_p2p_api_provider_select
        )
        self._p2p_api_prov_menu.pack(side="left", padx=(4, 10))

        ctk.CTkButton(row1, text="➕ 新增服務商", width=110, height=28, font=FONT_SMALL,
                      command=self._add_p2p_provider).pack(side="left", padx=(0, 4))
        ctk.CTkButton(row1, text="🗑️ 刪除服務商", width=95, height=28, font=FONT_SMALL,
                      fg_color="#b23b3b", hover_color="#8a2e2e",
                      command=self._delete_p2p_provider).pack(side="left")

        # Form fields
        form = ctk.CTkFrame(parent)
        form.pack(fill="both", expand=True, padx=14, pady=(0, 10))
        form.grid_columnconfigure(1, weight=1)

        # Row 0: Name
        ctk.CTkLabel(form, text="服務商名稱：", font=FONT_BODY, anchor="e").grid(
            row=0, column=0, sticky="e", padx=(10, 8), pady=6
        )
        self._p2p_name_entry = ctk.CTkEntry(form, font=FONT_BODY)
        self._p2p_name_entry.grid(row=0, column=1, sticky="ew", padx=(0, 10), pady=6)
        self._p2p_name_entry.bind("<KeyRelease>", lambda e: self._update_p2p_endpoint_debug_display())

        # Row 1: API URL
        ctk.CTkLabel(form, text="API 網址 (Base URL)：", font=FONT_BODY, anchor="e").grid(
            row=1, column=0, sticky="e", padx=(10, 8), pady=6
        )
        self._p2p_url_entry = ctk.CTkEntry(form, font=FONT_BODY)
        self._p2p_url_entry.grid(row=1, column=1, sticky="ew", padx=(0, 10), pady=6)
        self._p2p_url_entry.bind("<KeyRelease>", lambda e: self._update_p2p_endpoint_debug_display())

        # Row 2: API Key + View .env
        ctk.CTkLabel(form, text="API 金鑰 (API Key)：", font=FONT_BODY, anchor="e").grid(
            row=2, column=0, sticky="e", padx=(10, 8), pady=6
        )
        key_row = ctk.CTkFrame(form, fg_color="transparent")
        key_row.grid(row=2, column=1, sticky="ew", padx=(0, 10), pady=6)
        self._p2p_key_entry = ctk.CTkEntry(key_row, show="●", font=FONT_BODY)
        self._p2p_key_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._p2p_show_key_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            key_row, text="顯示", variable=self._p2p_show_key_var, width=50,
            command=lambda: self._p2p_key_entry.configure(show="" if self._p2p_show_key_var.get() else "●")
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            key_row, text="檢視/編輯 .env (記事本)", width=175, height=28, font=FONT_SMALL,
            command=config.open_env_in_editor
        ).pack(side="left")

        # Row 3: Key Hint
        self._p2p_key_hint_var = ctk.StringVar(value="")
        ctk.CTkLabel(
            form, textvariable=self._p2p_key_hint_var, font=FONT_SMALL,
            text_color=("gray40", "#9aa0a6"), anchor="w"
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=(10, 10), pady=(0, 4))

        # Row 4: Dedicated Model Management Panel (Parity with Subtitle API)
        model_group = ctk.CTkFrame(form, fg_color=("gray92", "gray20"))
        model_group.grid(row=4, column=0, columnspan=2, sticky="ew", padx=10, pady=(6, 8))
        model_group.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(model_group, text="【繪圖模型清單管理】", font=FONT_BODY, anchor="w").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(6, 2)
        )

        ctk.CTkLabel(model_group, text="模型選擇：", font=FONT_BODY, anchor="e").grid(
            row=1, column=0, sticky="e", padx=(10, 8), pady=4
        )
        self._p2p_selected_model_var = ctk.StringVar(value="（無）")
        self._p2p_api_model_menu = ctk.CTkOptionMenu(
            model_group, width=280, height=28, font=FONT_BODY,
            values=["（無）"], variable=self._p2p_selected_model_var,
            command=self._on_p2p_model_menu_select
        )
        self._p2p_api_model_menu.grid(row=1, column=1, sticky="w", pady=4)

        model_btns = ctk.CTkFrame(model_group, fg_color="transparent")
        model_btns.grid(row=1, column=2, sticky="w", padx=(10, 10), pady=4)
        ctk.CTkButton(
            model_btns, text="設為目前使用", width=105, height=28, font=FONT_SMALL,
            command=self._set_current_p2p_model_active
        ).pack(side="left")
        ctk.CTkButton(
            model_btns, text="刪除此模型", width=90, height=28, font=FONT_SMALL,
            fg_color="#b23b3b", hover_color="#8a2e2e",
            command=self._delete_current_p2p_model
        ).pack(side="left", padx=(6, 0))

        ctk.CTkLabel(model_group, text="新增模型：", font=FONT_BODY, anchor="e").grid(
            row=2, column=0, sticky="e", padx=(10, 8), pady=(4, 6)
        )
        self._p2p_new_model_var = ctk.StringVar()
        self._p2p_new_model_var.trace_add("write", self._update_p2p_endpoint_debug_display)
        new_model_entry = ctk.CTkEntry(
            model_group, textvariable=self._p2p_new_model_var, font=FONT_BODY,
            placeholder_text="輸入繪圖模型名稱，例如 qwen-edit-uncensored 或 flux-1-schnell"
        )
        new_model_entry.grid(row=2, column=1, sticky="ew", pady=(4, 6))
        ctk.CTkButton(
            model_group, text="＋ 加入模型", width=105, height=28, font=FONT_SMALL,
            command=self._add_p2p_model_to_current
        ).grid(row=2, column=2, sticky="w", padx=(10, 10), pady=(4, 6))

        # Row 3: Sniffed Actual Endpoint & Debug Display
        ctk.CTkLabel(model_group, text="🎯 嗅探實際端點：", font=FONT_BODY, text_color=("#1f6aa5", "#4ea8de"), anchor="e").grid(
            row=3, column=0, sticky="e", padx=(10, 8), pady=(2, 6)
        )
        debug_box = ctk.CTkFrame(model_group, fg_color="transparent")
        debug_box.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(0, 10), pady=(2, 6))
        debug_box.grid_columnconfigure(0, weight=1)

        self._p2p_endpoint_debug_var = ctk.StringVar(value="[POST] （分析中…）")
        self._p2p_endpoint_notes_var = ctk.StringVar(value="")

        ctk.CTkLabel(
            debug_box, textvariable=self._p2p_endpoint_debug_var, font=("Microsoft JhengHei", 12, "bold"),
            text_color=("#0f5132", "#75b798"), anchor="w"
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            debug_box, textvariable=self._p2p_endpoint_notes_var, font=FONT_SMALL,
            text_color=("gray40", "#9aa0a6"), anchor="w"
        ).grid(row=1, column=0, sticky="w")

        # Bottom Buttons
        btn_row = ctk.CTkFrame(parent, fg_color="transparent")
        btn_row.pack(fill="x", padx=14, pady=(0, 6))
        ctk.CTkButton(btn_row, text="測試連線", width=120, height=32, font=FONT_BODY,
                      command=self._test_p2p_api_connection).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btn_row, text="儲存服務商與模型", width=160, height=32, font=FONT_BODY,
                      fg_color="#1f6aa5", command=self._save_p2p_api_settings).pack(side="left")
        self._p2p_api_status_label = ctk.CTkLabel(btn_row, text="", font=FONT_SMALL, anchor="w")
        self._p2p_api_status_label.pack(side="left", padx=10)

        # Guide Card: Step-by-Step Instructions for Universal Adapter
        guide_card = ctk.CTkFrame(parent, fg_color=("gray95", "gray17"))
        guide_card.pack(fill="x", padx=14, pady=(4, 10))

        ctk.CTkLabel(
            guide_card,
            text="💡 通用適配器使用與設定指引（新增供應商免繁瑣配置）：",
            font=("Microsoft JhengHei", 12, "bold"),
            anchor="w"
        ).pack(fill="x", padx=12, pady=(6, 2))

        guide_text = (
            "1. Venice AI：Base URL 建議填寫「https://api.venice.ai/api/v1」。通用適配器會自動識別模型：\n"
            "   • 圖片修改模型（如 qwen-edit-uncensored、帶 -edit 或 inpaint 之模型）自動導向 /image/edit (二進位串流模式)。\n"
            "   • 純文字生圖模型（如 qwen-image-2、venice-sd35）自動導向 /image/generate (JSON 模式)，免手動切換 URL。\n"
            "2. Atlas Cloud：Base URL 填寫「https://api.atlascloud.ai/api/v1」，自動啟用非同步任務排隊與輪詢取圖。\n"
            "3. OpenRouter / 聚合商：Base URL 填寫「https://openrouter.ai/api/v1」，自動透過多模態視覺通道生成。\n"
            "4. 其他服務商：填入官方 Base URL 即可，適配器將自動探測並於上方「🎯 嗅探實際端點」即時顯示除錯資訊。"
        )
        ctk.CTkLabel(
            guide_card,
            text=guide_text,
            font=FONT_SMALL,
            text_color=("gray30", "#adb5bd"),
            justify="left",
            anchor="w"
        ).pack(fill="x", padx=14, pady=(0, 6))

    def _update_p2p_endpoint_debug_display(self, *args):
        if not hasattr(self, "_p2p_endpoint_debug_var"):
            return
        url = self._p2p_url_entry.get().strip() if hasattr(self, "_p2p_url_entry") else ""
        name = self._p2p_name_entry.get().strip() if hasattr(self, "_p2p_name_entry") else ""

        new_typed = self._p2p_new_model_var.get().strip() if hasattr(self, "_p2p_new_model_var") else ""
        sel_m = self._p2p_selected_model_var.get().strip() if hasattr(self, "_p2p_selected_model_var") else ""
        if sel_m == "（無）":
            sel_m = ""

        target_model = new_typed or sel_m or "未選擇模型"
        cfg = P2PProviderCfg(name=name, api_url=url, api_key="", model=target_model)
        info = image_gen_engine.sniff_endpoint_info(cfg)

        self._p2p_endpoint_debug_var.set(
            f"[{info['method']}] {info['endpoint_url']}  |  模式：{info['mode_desc']}"
        )
        self._p2p_endpoint_notes_var.set(
            f"💡 適配說明：{info['notes']}"
        )

    def _refresh_p2p_model_menu(self, keep: str | None = None):
        models = self._p2p_current_models or ["（無）"]
        if hasattr(self, "_p2p_api_model_menu"):
            self._p2p_api_model_menu.configure(values=models)
        sel = keep if keep in models else (models[0] if models != ["（無）"] else "（無）")
        if hasattr(self, "_p2p_selected_model_var"):
            self._p2p_selected_model_var.set(sel)
        self._update_p2p_endpoint_debug_display()

    def _on_p2p_model_menu_select(self, val: str):
        self._p2p_selected_model_var.set(val)
        self._update_p2p_endpoint_debug_display()

    def _set_current_p2p_model_active(self):
        m = self._p2p_selected_model_var.get().strip()
        if not m or m == "（無）":
            return
        p_name = self._p2p_name_entry.get().strip()
        if p_name:
            config.select_p2p_provider_model(p_name, m)
            self._p2p_settings = config.load_p2p_settings()
            self._sync_p2p_prompt_controls()
        self._p2p_api_status_label.configure(
            text=f"✅ 已將「{m}」設為此服務商的目前使用模型。",
            text_color=("#0a7d2c", "#5ec97a")
        )

    def _add_p2p_model_to_current(self):
        m = self._p2p_new_model_var.get().strip()
        if not m:
            return
        if m not in self._p2p_current_models:
            self._p2p_current_models.append(m)
        self._refresh_p2p_model_menu(keep=m)
        self._p2p_new_model_var.set("")
        self._save_p2p_api_settings()

    def _delete_current_p2p_model(self):
        m = self._p2p_selected_model_var.get().strip()
        if not m or m == "（無）":
            return
        if len(self._p2p_current_models) <= 1:
            messagebox.showwarning("無法刪除", "服務商至少需保留一個模型。")
            return
        self._p2p_current_models = [x for x in self._p2p_current_models if x != m]
        new_sel = self._p2p_current_models[0]
        self._refresh_p2p_model_menu(keep=new_sel)
        self._save_p2p_api_settings()

    def _sync_p2p_api_form(self):
        prov_names = [p.get("name", "") for p in self._p2p_settings.get("providers", [])]
        if hasattr(self, "_p2p_api_prov_menu") and prov_names:
            self._p2p_api_prov_menu.configure(values=prov_names)
            cur = self._p2p_api_prov_var.get()
            if cur not in prov_names:
                cur = prov_names[0]
                self._p2p_api_prov_var.set(cur)
            self._on_p2p_api_provider_select(cur)

    def _on_p2p_api_provider_select(self, choice: str):
        for p in self._p2p_settings.get("providers", []):
            if p.get("name") == choice:
                if hasattr(self, "_p2p_name_entry"):
                    self._p2p_name_entry.delete(0, "end")
                    self._p2p_name_entry.insert(0, p.get("name", ""))
                    self._p2p_url_entry.delete(0, "end")
                    self._p2p_url_entry.insert(0, p.get("api_url", ""))
                    self._p2p_current_models = list(p.get("models") or [])
                    sel_m = p.get("selected_model") or (self._p2p_current_models[0] if self._p2p_current_models else "")
                    self._refresh_p2p_model_menu(keep=sel_m)
                    self._p2p_key_entry.delete(0, "end")
                    self._p2p_key_entry.insert(0, p.get("api_key", ""))
                    env = p.get("api_key_env") or config._env_var_name_for(p.get("name", ""))
                    if hasattr(self, "_p2p_key_hint_var"):
                        self._p2p_key_hint_var.set(f"金鑰儲存於 .env：{env}")
                    self._update_p2p_endpoint_debug_display()
                break

    def _save_p2p_api_settings(self):
        name = self._p2p_name_entry.get().strip()
        url = self._p2p_url_entry.get().strip()
        if not name:
            messagebox.showwarning("缺少名稱", "請先填入服務商「名稱」。")
            return
        if not url:
            messagebox.showwarning("資料不完整", "API 網址為必填。")
            return

        models = list(self._p2p_current_models)
        sel_m = self._p2p_selected_model_var.get().strip()
        if sel_m and sel_m not in models and sel_m != "（無）":
            models.append(sel_m)
        if not models:
            models = ["black-forest-labs/flux-1-dev"]
        if not sel_m or sel_m == "（無）":
            sel_m = models[0]

        key = self._p2p_key_entry.get().strip()

        provider = {
            "name": name,
            "api_url": url,
            "models": models,
            "selected_model": sel_m,
            "api_key": key,
        }

        cur_choice = self._p2p_api_prov_var.get()
        if self._p2p_settings.get("selected_provider") == cur_choice:
            provider["is_default"] = True

        config.upsert_p2p_provider(provider)
        self._p2p_settings = config.load_p2p_settings()
        self._p2p_api_prov_var.set(name)
        self._sync_p2p_api_form()
        self._sync_p2p_prompt_controls()
        self._p2p_api_status_label.configure(
            text=f"✅ 已儲存服務商「{name}」與模型設定（金鑰同步至 .env）",
            text_color=("#0a7d2c", "#5ec97a")
        )

    def _test_p2p_api_connection(self):
        url = self._p2p_url_entry.get().strip()
        key = self._p2p_key_entry.get().strip()
        name = self._p2p_name_entry.get().strip()
        models = list(self._p2p_current_models)
        sel_m = self._p2p_selected_model_var.get().strip() if hasattr(self, "_p2p_selected_model_var") else ""
        cfg = P2PProviderCfg(name=name, api_url=url, api_key=key, model=sel_m or (models[0] if models else ""))
        if not cfg.api_key:
            cfg.api_key = config.resolve_p2p_api_key({"name": name, "api_key": ""})

        self._p2p_api_status_label.configure(text="測試連線中...", text_color=("gray40", "#9aa0a6"))

        def work():
            ok, msg = test_p2p_connection(cfg)
            color = ("#0a7d2c", "#5ec97a") if ok else ("#b23b3b", "#ff6b6b")
            self.after(0, lambda: self._p2p_api_status_label.configure(text=f"{'✅' if ok else '❌'} {msg}", text_color=color))

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _add_p2p_provider(self):
        new_name = f"NewProvider_{len(self._p2p_settings.get('providers', [])) + 1}"
        new_p = {
            "name": new_name,
            "api_url": "https://openrouter.ai/api/v1",
            "models": ["black-forest-labs/flux-1-dev"],
            "selected_model": "black-forest-labs/flux-1-dev",
            "api_key": "",
        }
        config.upsert_p2p_provider(new_p)
        self._p2p_settings = config.load_p2p_settings()
        self._p2p_api_prov_var.set(new_name)
        self._sync_p2p_api_form()

    def _delete_p2p_provider(self):
        providers = self._p2p_settings.get("providers", [])
        if len(providers) <= 1:
            messagebox.showwarning("無法刪除", "至少需保留一個圖生圖服務商。")
            return
        cur = self._p2p_api_prov_var.get()
        if not messagebox.askyesno("刪除確認", f"確定要刪除服務商「{cur}」嗎？"):
            return
        config.delete_p2p_provider(cur)
        self._p2p_settings = config.load_p2p_settings()
        rem = self._p2p_settings.get("providers", [])
        if rem:
            self._p2p_api_prov_var.set(rem[0]["name"])
        self._sync_p2p_api_form()
        self._sync_p2p_prompt_controls()
        self._p2p_api_status_label.configure(
            text=f"🗑️ 已刪除服務商「{cur}」",
            text_color=("#b23b3b", "#ff6b6b")
        )

    # ------------------------------------------------------------------ #
    #  P2P Tab 3: Prompt Settings (_build_p2p_prompt_tab)
    # ------------------------------------------------------------------ #
    def _build_p2p_prompt_tab(self, parent):
        ctk.CTkLabel(parent, text="圖生圖 Prompt 設定 (繪圖提示詞庫)", font=FONT_TITLE, anchor="w").pack(
            fill="x", padx=14, pady=(12, 4))

        card = ctk.CTkFrame(parent)
        card.pack(fill="x", padx=14, pady=(0, 8))

        row1 = ctk.CTkFrame(card, fg_color="transparent")
        row1.pack(fill="x", padx=12, pady=(10, 6))
        ctk.CTkLabel(row1, text="選擇範本：", font=FONT_BODY).pack(side="left")

        presets = [p.get("name", "") for p in self._p2p_settings.get("drawing_prompts", [])]
        self._p2p_prompt_cfg_var = ctk.StringVar(value=presets[0] if presets else "")
        self._p2p_prompt_cfg_menu = ctk.CTkOptionMenu(
            row1, values=presets, variable=self._p2p_prompt_cfg_var, font=FONT_BODY,
            command=self._on_p2p_prompt_select
        )
        self._p2p_prompt_cfg_menu.pack(side="left", padx=(4, 10))

        ctk.CTkButton(row1, text="➕ 新增範本", width=100, height=28, font=FONT_SMALL,
                      command=self._add_p2p_prompt_preset).pack(side="left", padx=(0, 4))
        ctk.CTkButton(row1, text="🗑️ 刪除", width=70, height=28, font=FONT_SMALL,
                      fg_color="#b23b3b", hover_color="#8a2e2e",
                      command=self._delete_p2p_prompt_preset).pack(side="left")

        form = ctk.CTkFrame(parent)
        form.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        ctk.CTkLabel(form, text="範本名稱：", font=FONT_BODY, anchor="w").pack(fill="x", padx=12, pady=(10, 2))
        self._p2p_preset_name_entry = ctk.CTkEntry(form, font=FONT_BODY)
        self._p2p_preset_name_entry.pack(fill="x", padx=12, pady=(0, 6))

        ctk.CTkLabel(form, text="正向提示詞 (Positive Prompt)：", font=FONT_BODY, anchor="w").pack(fill="x", padx=12, pady=(4, 2))
        self._p2p_cfg_pos_box = ctk.CTkTextbox(form, height=100, font=FONT_BODY)
        self._p2p_cfg_pos_box.pack(fill="x", padx=12, pady=(0, 6))

        ctk.CTkLabel(form, text="負向提示詞 (Negative Prompt)：", font=FONT_BODY, anchor="w").pack(fill="x", padx=12, pady=(4, 2))
        self._p2p_cfg_neg_box = ctk.CTkTextbox(form, height=70, font=FONT_BODY)
        self._p2p_cfg_neg_box.pack(fill="x", padx=12, pady=(0, 10))

        btn_row = ctk.CTkFrame(form, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(4, 10))
        ctk.CTkButton(btn_row, text="儲存 Prompt 範本", width=160, height=32, font=FONT_BODY,
                      fg_color="#1f6aa5", command=self._save_p2p_prompt_preset).pack(side="left")
        self._p2p_prompt_status_label = ctk.CTkLabel(btn_row, text="", font=FONT_SMALL, anchor="w")
        self._p2p_prompt_status_label.pack(side="left", padx=10)

    def _sync_p2p_prompt_form(self):
        presets = [p.get("name", "") for p in self._p2p_settings.get("drawing_prompts", [])]
        if hasattr(self, "_p2p_prompt_cfg_menu") and presets:
            self._p2p_prompt_cfg_menu.configure(values=presets)
            cur = self._p2p_prompt_cfg_var.get()
            if cur not in presets:
                self._p2p_prompt_cfg_var.set(presets[0])
            self._on_p2p_prompt_select(self._p2p_prompt_cfg_var.get())

    def _on_p2p_prompt_select(self, choice: str):
        for p in self._p2p_settings.get("drawing_prompts", []):
            if p.get("name") == choice:
                if hasattr(self, "_p2p_preset_name_entry"):
                    self._p2p_preset_name_entry.delete(0, "end")
                    self._p2p_preset_name_entry.insert(0, p.get("name", ""))
                    self._p2p_cfg_pos_box.delete("1.0", "end")
                    self._p2p_cfg_pos_box.insert("1.0", p.get("positive", ""))
                    self._p2p_cfg_neg_box.delete("1.0", "end")
                    self._p2p_cfg_neg_box.insert("1.0", p.get("negative", ""))
                break

    def _save_p2p_prompt_preset(self):
        name = self._p2p_preset_name_entry.get().strip()
        pos = self._p2p_cfg_pos_box.get("1.0", "end").strip()
        neg = self._p2p_cfg_neg_box.get("1.0", "end").strip()

        cur_choice = self._p2p_prompt_cfg_var.get()
        for p in self._p2p_settings.get("drawing_prompts", []):
            if p.get("name") == cur_choice:
                p["name"] = name
                p["positive"] = pos
                p["negative"] = neg
                break
        config.save_p2p_settings(self._p2p_settings)
        self._p2p_settings = config.load_p2p_settings()
        self._sync_p2p_prompt_form()
        self._sync_p2p_prompt_controls()
        self._p2p_prompt_status_label.configure(text="✅ 提示詞範本已更新", text_color=("#0a7d2c", "#5ec97a"))

    def _add_p2p_prompt_preset(self):
        new_item = {
            "id": f"draw_custom_{len(self._p2p_settings.get('drawing_prompts', [])) + 1}",
            "name": f"新繪圖風格_{len(self._p2p_settings.get('drawing_prompts', [])) + 1}",
            "positive": "masterpiece, best quality, {subject}",
            "negative": "lowres, bad anatomy, bad hands, blurry",
        }
        self._p2p_settings.setdefault("drawing_prompts", []).append(new_item)
        config.save_p2p_settings(self._p2p_settings)
        self._p2p_settings = config.load_p2p_settings()
        self._p2p_prompt_cfg_var.set(new_item["name"])
        self._sync_p2p_prompt_form()

    def _delete_p2p_prompt_preset(self):
        presets = self._p2p_settings.get("drawing_prompts", [])
        if len(presets) <= 1:
            messagebox.showwarning("無法刪除", "至少需保留一組繪圖 Prompt 範本。")
            return
        cur = self._p2p_prompt_cfg_var.get()
        if not messagebox.askyesno("刪除確認", f"確定要刪除範本「{cur}」嗎？"):
            return
        self._p2p_settings["drawing_prompts"] = [p for p in presets if p.get("name") != cur]
        config.save_p2p_settings(self._p2p_settings)
        self._p2p_settings = config.load_p2p_settings()
        self._sync_p2p_prompt_form()

    # ------------------------------------------------------------------ #
    #  Window Cleanup
    # ------------------------------------------------------------------ #
    def _on_window_close(self):
        try:
            self._save_window_geometry()
        except Exception:
            pass
        try:
            if hasattr(self, "_volume_var"):
                vol = int(float(self._volume_var.get()))
                config.patch_setting("last_volume", vol)
        except Exception:
            pass
        self.destroy()

    def destroy(self):
        self._stop_play()
        if self._vlc_player:
            try:
                self._vlc_player.release()
            except Exception:
                pass
            self._vlc_player = None
        if self._video_engine:
            try:
                self._video_engine.close()
            except Exception:
                pass
            self._video_engine = None
        super().destroy()


def main():
    parser = argparse.ArgumentParser(
        prog="PicTrans2Srt",
        description="Video OCR, Subtitle Translation & Player (v1.11)",
    )
    parser.add_argument(
        "video_file",
        nargs="?",
        default=None,
        help="影片檔案路徑 (亦支援直接將影片拖曳至程式圖示開啟)",
    )
    parser.add_argument(
        "-v", "--video",
        dest="video_opt",
        default=None,
        help="指定影片檔案路徑",
    )
    parser.add_argument(
        "-s", "--srt",
        dest="srt_file",
        default=None,
        help="指定字幕檔案路徑 (若省略則自動搜尋同目錄下同名/相符的 .srt)",
    )
    parser.add_argument(
        "-p", "--play",
        dest="autoplay",
        action="store_true",
        default=None,
        help="載入後自動開始播放",
    )
    parser.add_argument(
        "--no-play",
        dest="no_play",
        action="store_true",
        help="載入後保持暫停狀態",
    )

    args = parser.parse_args()

    video_path = args.video_opt or args.video_file
    srt_path = args.srt_file
    autoplay = False
    if video_path:
        if args.no_play:
            autoplay = False
        elif args.autoplay is not None:
            autoplay = args.autoplay
        else:
            # 外部傳入影片預設自動播放
            autoplay = True

    app = App(
        initial_video=video_path,
        initial_srt=srt_path,
        autoplay=autoplay,
    )
    app.mainloop()


if __name__ == "__main__":
    main()
