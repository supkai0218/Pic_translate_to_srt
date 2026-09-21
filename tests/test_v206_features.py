"""Unit tests for v2.06 features:
1. config.py v2.06 settings & prompt helper functions (get_prompt_profiles, get_prompt_profile_by_id, get_prompt_profile_by_name)
2. Non-blocking capture in _capture_to_p2p_gallery (no messagebox.askyesno)
3. SubtitleTask raw_translation field and restoration logic
4. Structured output textbox parsing and synchronization (_sync_textbox_to_tasks)
5. SRT Save mode (overwrite vs save_as) & custom suffix handling
6. Tab routing and navigation for 翻譯字幕執行 and 編輯及預覽視窗
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

import config
import srt_utils
from app import SubtitleTask


class TestV206Features(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.work_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    # ------------------------------------------------------------------ #
    #  1. Config defaults & prompt profile helpers
    # ------------------------------------------------------------------ #
    def test_config_v206_defaults(self):
        settings = dict(config._DEFAULTS)
        self.assertEqual(settings.get("custom_srt_suffix"), "_translated")
        self.assertEqual(settings.get("srt_save_mode"), "save_as")
        self.assertIn("selected_ocr_provider", settings)
        self.assertIn("selected_ocr_model", settings)
        self.assertIn("selected_ocr_prompt_id", settings)

    def test_prompt_profile_helpers(self):
        profiles = config.get_prompt_profiles()
        self.assertTrue(len(profiles) >= 1)

        first_p = profiles[0]
        p_id = first_p.get("id")
        p_name = first_p.get("name")

        found_by_id = config.get_prompt_profile_by_id(p_id)
        self.assertIsNotNone(found_by_id)
        self.assertEqual(found_by_id["name"], p_name)

        found_by_name = config.get_prompt_profile_by_name(p_name)
        self.assertIsNotNone(found_by_name)
        self.assertEqual(found_by_name["id"], p_id)

        self.assertIsNone(config.get_prompt_profile_by_id("non_existent_id_999"))
        self.assertIsNone(config.get_prompt_profile_by_name("Non Existent Name 999"))

    # ------------------------------------------------------------------ #
    #  2. SubtitleTask raw_translation field
    # ------------------------------------------------------------------ #
    def test_subtitle_task_raw_translation(self):
        dummy_img = Image.new("RGB", (100, 100))
        task = SubtitleTask(
            id=1,
            start_sec=10.0,
            end_sec=13.5,
            start_frame_img=dummy_img,
            crop_box=(10, 20, 80, 90),
            crop_img=dummy_img,
            translation="修改後的字幕",
            raw_translation="原始AI翻譯文字",
        )
        self.assertEqual(task.translation, "修改後的字幕")
        self.assertEqual(task.raw_translation, "原始AI翻譯文字")
        self.assertEqual(task.duration, 3.5)

    # ------------------------------------------------------------------ #
    #  3. Structured Output Textbox Synchronization
    # ------------------------------------------------------------------ #
    def test_sync_textbox_to_tasks_structured(self):
        import app

        dummy_img = Image.new("RGB", (10, 10))
        t1 = SubtitleTask(1, 0.0, 2.0, dummy_img, None, dummy_img, translation="原句1", raw_translation="原句1")
        t2 = SubtitleTask(2, 2.5, 5.0, dummy_img, None, dummy_img, translation="原句2", raw_translation="原句2")

        mock_app = MagicMock()
        mock_app._tasks = [t1, t2]

        # Simulate user editing the textbox
        edited_text = (
            "[#1] 00:00:00,000 --> 00:00:02,000\n"
            "手動校對後的繁體第一句\n\n"
            "[#2] 00:00:02,500 --> 00:00:05,000\n"
            "手動校對後的繁體第二句\n"
        )
        mock_app._ocr_output_textbox.get.return_value = edited_text

        # Call the actual unbound method
        app.App._sync_textbox_to_tasks(mock_app)

        self.assertEqual(t1.translation, "手動校對後的繁體第一句")
        self.assertEqual(t2.translation, "手動校對後的繁體第二句")
        # raw_translation should remain untouched
        self.assertEqual(t1.raw_translation, "原句1")
        self.assertEqual(t2.raw_translation, "原句2")

    def test_restore_raw_ocr_translations(self):
        import app

        dummy_img = Image.new("RGB", (10, 10))
        t1 = SubtitleTask(1, 0.0, 2.0, dummy_img, None, dummy_img, translation="已被改動的文字", raw_translation="AI原始字")
        mock_app = MagicMock()
        mock_app._tasks = [t1]

        app.App._restore_raw_ocr_translations(mock_app)
        self.assertEqual(t1.translation, "AI原始字")

    # ------------------------------------------------------------------ #
    #  4. Non-blocking P2P Capture (No askyesno popup)
    # ------------------------------------------------------------------ #
    @patch("tkinter.messagebox.askyesno")
    def test_capture_to_p2p_gallery_no_popup(self, mock_askyesno):
        import app

        mock_app = MagicMock()
        mock_app._video_engine = MagicMock()
        mock_app._current_frame_img = Image.new("RGB", (200, 200))
        mock_app._crop_box = None
        mock_app._video_path = str(self.work_path / "test.mp4")
        mock_app._current_sec = 5.0
        mock_app._current_input_dir = str(self.work_path)

        app.App._capture_to_p2p_gallery(mock_app)

        # Confirm messagebox.askyesno was NOT called
        mock_askyesno.assert_not_called()

        # Confirm galleries were refreshed
        mock_app._refresh_p2p_gallery.assert_called_once()
        mock_app._refresh_capture_gallery.assert_called_once()

    # ------------------------------------------------------------------ #
    #  5. SRT Suffix & Custom Naming
    # ------------------------------------------------------------------ #
    def test_custom_suffix_assembly(self):
        video_stem = "sample_movie"
        for user_suffix, expected in [
            ("_translated", "sample_movie_translated.srt"),
            ("zh-TW", "sample_movie_zh-TW.srt"),
            ("_ocr.srt", "sample_movie_ocr.srt"),
            ("-tc", "sample_movie-tc.srt"),
        ]:
            s_val = user_suffix.strip()
            if s_val and not s_val.startswith(("_", "-", ".")):
                s_val = "_" + s_val
            if s_val.endswith(".srt"):
                s_val = s_val[:-4]
            result = f"{video_stem}{s_val}.srt"
            self.assertEqual(result, expected)

    # ------------------------------------------------------------------ #
    #  6. Navigation Methods
    # ------------------------------------------------------------------ #
    def test_navigation_methods(self):
        import app

        mock_app = MagicMock()
        mock_app.tabs = MagicMock()
        mock_app.ocr_tabs = MagicMock()
        mock_app.p2p_tabs = MagicMock()

        app.App._navigate_to_ocr_exec(mock_app)
        mock_app.tabs.set.assert_called_with("  截圖翻譯字幕任務  ")
        mock_app.ocr_tabs.set.assert_called_with("  翻譯字幕執行  ")

    def test_get_default_provider_name(self):
        name = config.get_default_provider_name()
        self.assertIsInstance(name, str)

    def test_build_ocr_exec_tab_widgets(self):
        import app
        import customtkinter as ctk

        root = ctk.CTk()
        root.withdraw()
        try:
            parent_frame = ctk.CTkFrame(root)
            dummy_app = object.__new__(app.App)
            dummy_app._settings = config.load_settings()
            dummy_app._tasks = []
            dummy_app._selected_ocr_exec_task = None
            dummy_app._srt_path = ""
            dummy_app._video_path = ""

            app.App._build_ocr_exec_tab(dummy_app, parent_frame)

            self.assertTrue(hasattr(dummy_app, "_ocr_exec_prov_menu"))
            self.assertTrue(hasattr(dummy_app, "_ocr_exec_model_menu"))
            self.assertTrue(hasattr(dummy_app, "_ocr_exec_prompt_menu"))
            self.assertTrue(hasattr(dummy_app, "_run_btn"))
            self.assertTrue(hasattr(dummy_app, "_progress"))
            self.assertTrue(hasattr(dummy_app, "_ocr_output_textbox"))
            self.assertTrue(hasattr(dummy_app, "_mode_seg"))
            self.assertTrue(hasattr(dummy_app, "_ocr_save_mode_seg"))
            self.assertTrue(hasattr(dummy_app, "_ocr_suffix_entry"))
            self.assertTrue(hasattr(dummy_app, "_insert_btn"))
            self.assertTrue(hasattr(dummy_app, "_ocr_history_toggle_btn"))
            self.assertTrue(hasattr(dummy_app, "_ocr_history_toolbar"))

            # Verify toggling OCR history mode without Tkinter errors
            dummy_app._ocr_history_mode = False
            dummy_app._selected_ocr_drop_paths = set()
            dummy_app._ocr_drop_checkboxes = {}
            dummy_app._toggle_ocr_history_mode()
            self.assertTrue(dummy_app._ocr_history_mode)
            self.assertEqual(dummy_app._ocr_history_toggle_btn.cget("text"), "📋 返回任務列表")

            dummy_app._toggle_ocr_history_mode()
            self.assertFalse(dummy_app._ocr_history_mode)
            self.assertEqual(dummy_app._ocr_history_toggle_btn.cget("text"), "📦 讀取歷史截圖")
        finally:
            root.destroy()

    # ------------------------------------------------------------------ #
    #  7. OCR Storage Directory & .drop Resolution
    # ------------------------------------------------------------------ #
    def test_resolve_ocr_dir_and_drop(self):
        custom_ocr = self.work_path / "custom_ocr"
        custom_cap = self.work_path / "custom_cap"
        settings = {
            "storage_folders": {
                "default_ocr_dir": str(custom_ocr),
                "default_captured_dir": str(custom_cap),
            }
        }
        res_ocr = config.resolve_ocr_dir(settings)
        self.assertEqual(res_ocr, custom_ocr)
        self.assertTrue(res_ocr.is_dir())

        res_ocr_drop = config.resolve_ocr_drop_dir(settings)
        self.assertEqual(res_ocr_drop, custom_ocr / ".drop")
        self.assertTrue(res_ocr_drop.is_dir())

        res_cap_drop = config.resolve_captured_drop_dir(settings)
        self.assertEqual(res_cap_drop, custom_cap / ".drop")
        self.assertTrue(res_cap_drop.is_dir())

    # ------------------------------------------------------------------ #
    #  8. Flexible Timestamp Parsing (flexible_ts_to_sec)
    # ------------------------------------------------------------------ #
    def test_flexible_ts_to_sec(self):
        # Standard format
        self.assertAlmostEqual(srt_utils.flexible_ts_to_sec("00:01:23,456"), 83.456, places=3)
        self.assertAlmostEqual(srt_utils.flexible_ts_to_sec("01:00:00.000"), 3600.0, places=3)
        # MM:SS.mmm
        self.assertAlmostEqual(srt_utils.flexible_ts_to_sec("02:15.500"), 135.5, places=3)
        # MM:SS
        self.assertAlmostEqual(srt_utils.flexible_ts_to_sec("01:30"), 90.0, places=3)
        # Float seconds
        self.assertAlmostEqual(srt_utils.flexible_ts_to_sec("45.2"), 45.2, places=3)
        # Invalid inputs
        self.assertIsNone(srt_utils.flexible_ts_to_sec("invalid_time"))
        self.assertIsNone(srt_utils.flexible_ts_to_sec(""))

    # ------------------------------------------------------------------ #
    #  9. OCR Filename Formatting & Timestamp Recovery
    # ------------------------------------------------------------------ #
    def test_ocr_save_and_parse_filename(self):
        import capture_manager

        img = Image.new("RGB", (50, 50), color="blue")
        ocr_dir = self.work_path / "ocr_test"
        saved = capture_manager.save_captured_image(
            img=img,
            video_path="my_video.mp4",
            sec=12.345,
            end_sec=16.789,
            is_crop=True,
            category="ocr",
            target_dir=ocr_dir,
        )
        self.assertTrue(saved.is_file())
        self.assertIn("my_video_ocr_00-00-12_345_00-00-16_789_crop", saved.name)

        parsed = capture_manager.parse_ocr_filename(saved.name)
        self.assertIsNotNone(parsed)
        start_sec, end_sec = parsed
        self.assertAlmostEqual(start_sec, 12.345, places=3)
        self.assertAlmostEqual(end_sec, 16.789, places=3)

    # ------------------------------------------------------------------ #
    #  10. Safe Move, Restore, and Clear .drop Operations
    # ------------------------------------------------------------------ #
    def test_safe_drop_operations(self):
        import capture_manager

        work_dir = self.work_path / "work"
        drop_dir = self.work_path / "work" / ".drop"
        work_dir.mkdir(parents=True, exist_ok=True)

        # Create 2 dummy files
        f1 = work_dir / "test1.jpg"
        f2 = work_dir / "test2.jpg"
        f1.write_text("dummy image 1")
        f2.write_text("dummy image 2")

        # 1. Move to drop
        moved1 = capture_manager.safe_move_to_drop(f1, drop_dir)
        moved2 = capture_manager.safe_move_to_drop(f2, drop_dir)

        self.assertFalse(f1.exists())
        self.assertFalse(f2.exists())
        self.assertTrue(moved1.exists())
        self.assertTrue(moved2.exists())
        self.assertEqual(moved1.parent, drop_dir)

        # 2. Restore 1 file back
        restored1 = capture_manager.safe_restore_from_drop(moved1, work_dir)
        self.assertTrue(restored1.exists())
        self.assertEqual(restored1.parent, work_dir)
        self.assertFalse(moved1.exists())

        # 3. Clear drop dir
        del_count = capture_manager.clear_drop_dir(drop_dir)
        self.assertEqual(del_count, 1)  # Only moved2 was left in drop
        self.assertFalse(moved2.exists())

    # ------------------------------------------------------------------ #
    #  11. Inline Task Timestamp Editing (_on_task_time_edit)
    # ------------------------------------------------------------------ #
    def test_on_task_time_edit(self):
        import app

        dummy_img = Image.new("RGB", (10, 10))
        task = SubtitleTask(1, 5.0, 10.0, dummy_img, None, dummy_img)
        mock_app = MagicMock()

        start_e = MagicMock()
        end_e = MagicMock()
        dur_lbl = MagicMock()

        # Valid edit
        app.App._on_task_time_edit(mock_app, task, "00:00:06,000", "00:00:12,500", start_e, end_e, dur_lbl)
        self.assertEqual(task.start_sec, 6.0)
        self.assertEqual(task.end_sec, 12.5)
        self.assertEqual(task.duration, 6.5)
        dur_lbl.configure.assert_called_with(text="(6.50s)")

        # Invalid edit (end <= start): should revert without changing task
        app.App._on_task_time_edit(mock_app, task, "00:00:20,000", "00:00:10,000", start_e, end_e, dur_lbl)
        self.assertEqual(task.start_sec, 6.0)
        self.assertEqual(task.end_sec, 12.5)
        start_e.insert.assert_called_with(0, task.start_ts)
        end_e.insert.assert_called_with(0, task.end_ts)


if __name__ == "__main__":
    unittest.main()

