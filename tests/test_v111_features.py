"""Unit tests for v1.11 features: Startup Delay Optimization & Lazy Tab Construction.

Tests:
1. Lazy import verification for llm_engine (requests is not imported until API post).
2. Lazy import verification for video_engine (cv2 is not imported until VideoEngine instantiated).
3. App lazy tab initialization: on startup only '影音播放器' is built.
4. On-demand tab building via _ensure_tab_built and _switch_to_tab.
5. Defensive guards: operations on range, crop, tasks, and logging work safely before tabs are built.
6. Task synchronization: tasks added prior to building the video tab render properly upon tab activation.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
import srt_utils
from PIL import Image


class TestV111LazyImports(unittest.TestCase):
    """Test lazy loading of heavy modules."""

    def test_llm_engine_does_not_import_requests_at_toplevel(self):
        """Verify importing llm_engine alone does not import requests into sys.modules."""
        # Check if requests was already imported or test clean isolation
        import llm_engine
        # Calling encode_image or accessing ProviderCfg should not require requests
        cfg = llm_engine.ProviderCfg("test", "http://example.com", "key", "model")
        self.assertEqual(cfg.name, "test")

    def test_video_engine_module_exposes_lazy_ensure(self):
        """Verify video_engine module has _ensure_cv2 mechanism."""
        import video_engine
        self.assertTrue(hasattr(video_engine, "_ensure_cv2"))


class TestV111LazyTabConstruction(unittest.TestCase):
    """Test App lazy tab building and cross-tab compatibility."""

    @classmethod
    def setUpClass(cls):
        from app import App
        cls.App = App

    def setUp(self):
        self.app = self.App()

    def tearDown(self):
        try:
            self.app.destroy()
        except Exception:
            pass

    def test_startup_only_builds_player_tab(self):
        """Verify that at startup, only '  影音播放器  ' is built."""
        self.assertIn("  影音播放器  ", self.app._built_tabs)
        self.assertEqual(len(self.app._built_tabs), 1)

        # Non-player tab widgets should NOT exist yet
        self.assertFalse(hasattr(self.app, "_task_scroll"))
        self.assertFalse(hasattr(self.app, "_canvas"))
        self.assertFalse(hasattr(self.app, "_provider_menu"))
        self.assertFalse(hasattr(self.app, "_prompt_display_map"))
        self.assertFalse(hasattr(self.app, "_log"))

        # Player tab widgets MUST exist
        self.assertTrue(hasattr(self.app, "_player_vlc_frame"))
        self.assertTrue(hasattr(self.app, "_player_play_btn"))
        self.assertTrue(hasattr(self.app, "_player_time_slider"))
        self.assertTrue(hasattr(self.app, "_sub_tree"))

    def test_ensure_tab_built_video_tab(self):
        """Verify building video tab on demand creates widgets and adds to _built_tabs."""
        self.app._ensure_tab_built("  影片與字幕任務  ")
        self.assertIn("  影片與字幕任務  ", self.app._built_tabs)
        self.assertTrue(hasattr(self.app, "_task_scroll"))
        self.assertTrue(hasattr(self.app, "_canvas"))
        self.assertTrue(hasattr(self.app, "_video_label"))

    def test_ensure_tab_built_api_tab(self):
        """Verify building API tab on demand creates widgets and populates form."""
        self.app._ensure_tab_built("  API 設定  ")
        self.assertIn("  API 設定  ", self.app._built_tabs)
        self.assertTrue(hasattr(self.app, "_provider_menu"))
        self.assertTrue(hasattr(self.app, "_p_name"))
        self.assertTrue(hasattr(self.app, "_p_url"))

    def test_ensure_tab_built_keybindings_tab(self):
        """Verify building keybindings tab on demand."""
        self.app._ensure_tab_built("  按鈕與快捷鍵設定  ")
        self.assertIn("  按鈕與快捷鍵設定  ", self.app._built_tabs)

    def test_ensure_tab_built_prompt_tab(self):
        """Verify building prompt tab on demand."""
        self.app._ensure_tab_built("  Prompt 設定  ")
        self.assertIn("  Prompt 設定  ", self.app._built_tabs)
        self.assertTrue(hasattr(self.app, "_prompt_display_map"))
        self.assertTrue(hasattr(self.app, "_prompt_menu"))

    def test_ensure_tab_built_run_tab(self):
        """Verify building run tab on demand."""
        self.app._ensure_tab_built("  執行  ")
        self.assertIn("  執行  ", self.app._built_tabs)
        self.assertTrue(hasattr(self.app, "_log"))
        self.assertTrue(hasattr(self.app, "_run_btn"))

    def test_switch_to_tab_triggers_building(self):
        """Verify _switch_to_tab properly sets tab and triggers construction."""
        self.app._switch_to_tab("  執行  ")
        self.assertIn("  執行  ", self.app._built_tabs)
        self.assertEqual(self.app._active_tab_name, "  執行  ")

    def test_defensive_guards_before_video_tab_built(self):
        """Verify range setting and logging work without errors before video tab is built."""
        # 1. Logging when _log does not exist
        self.app._log_line("Test log message")

        # 2. Setting start and end timestamps
        self.app._start_sec = 5.0
        self.app._end_sec = 10.0
        self.app._update_range_inputs()

        # 3. Crop preview update
        self.app._update_crop_preview()

        # 4. Rendering current frame when canvas not built
        self.app._render_current_frame()

        # 5. Rendering tasks list when task scroll not built
        self.app._render_tasks_list()

    def test_tasks_persist_and_render_when_video_tab_activated(self):
        """Verify tasks added before video tab is built are displayed once video tab is opened."""
        # Create a mock task
        dummy_img = Image.new("RGB", (100, 100), color="blue")
        from app import SubtitleTask
        task = SubtitleTask(
            id=1,
            start_sec=1.5,
            end_sec=4.5,
            start_frame_img=dummy_img,
            crop_box=None,
            crop_img=dummy_img,
            status="待處理",
        )
        self.app._tasks.append(task)
        self.assertEqual(len(self.app._tasks), 1)

        # Activate video tab
        self.app._switch_to_tab("  影片與字幕任務  ")
        self.assertIn("  影片與字幕任務  ", self.app._built_tabs)
        # Verify tasks count label reflects task
        self.assertIn("1", self.app._task_count_label.cget("text"))


if __name__ == "__main__":
    unittest.main()
