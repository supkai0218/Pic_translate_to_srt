"""Automated tests for v1.04 features: Pure Player Tab, Treeview Subtitle Editor, Window Scaling & Fullscreen."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import tkinter as tk
import numpy as np
import cv2

import app
import config
import srt_utils
import vlc_engine


def _create_dummy_video_and_srt(tmp_dir: Path) -> tuple[Path, Path]:
    """Create a 3-second 30fps video file and matching .srt subtitle file."""
    video_path = tmp_dir / "sample_v104.mp4"
    srt_path = tmp_dir / "sample_v104.srt"

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    fps = 30.0
    w, h = 640, 360
    out = cv2.VideoWriter(str(video_path), fourcc, fps, (w, h))
    for i in range(90):
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        cv2.putText(frame, f"Frame {i}", (50, 100), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
        out.write(frame)
    out.release()

    srt_content = (
        "1\n"
        "00:00:00,000 --> 00:00:01,000\n"
        "First Subtitle Line\n\n"
        "2\n"
        "00:00:01,200 --> 00:00:02,500\n"
        "Second Subtitle Line\n"
    )
    srt_path.write_text(srt_content, encoding="utf-8")

    return video_path, srt_path


class TestV104Features(unittest.TestCase):
    def test_config_new_keybindings(self):
        """Verify new keybindings for toggle_fullscreen and hot_reload_sub in config."""
        self.assertIn("toggle_fullscreen", config.DEFAULT_KEYBINDINGS)
        self.assertIn("hot_reload_sub", config.DEFAULT_KEYBINDINGS)
        self.assertEqual(config.DEFAULT_KEYBINDINGS["toggle_fullscreen"], "F11")
        self.assertEqual(config.DEFAULT_KEYBINDINGS["hot_reload_sub"], "F5")

        self.assertEqual(app.format_key_display("f11"), "F11 (全螢幕)")
        self.assertEqual(app.format_key_display("f5"), "F5 (熱重載)")

    def test_player_tab_initialization(self):
        """Verify App initializes with the pure player tab and all its widgets."""
        a = app.App()
        try:
            a.update()
            # Verify active tab
            self.assertEqual(a._active_tab_name, "  影音播放器  ")

            # Verify player widgets exist
            self.assertTrue(hasattr(a, "_player_vlc_frame"))
            self.assertTrue(hasattr(a, "_player_time_slider"))
            self.assertTrue(hasattr(a, "_player_time_label"))
            self.assertTrue(hasattr(a, "_player_play_btn"))
            self.assertTrue(hasattr(a, "_player_scale_menu"))
            self.assertTrue(hasattr(a, "_player_fullscreen_btn"))
            self.assertTrue(hasattr(a, "_player_sub_status_label"))
            self.assertTrue(hasattr(a, "_sub_tree"))
            self.assertTrue(hasattr(a, "_sub_count_label"))
            self.assertTrue(hasattr(a, "_sub_context_menu"))
        finally:
            a.destroy()

    def test_subtitle_treeview_population(self):
        """Verify Treeview correctly populates subtitle entries upon loading video and SRT."""
        with tempfile.TemporaryDirectory() as td:
            v_path, s_path = _create_dummy_video_and_srt(Path(td))

            a = app.App()
            try:
                a.update()
                a._load_video(str(v_path))
                a.update()

                # Verify treeview populated
                children = a._sub_tree.get_children()
                self.assertEqual(len(children), 2)

                item1 = a._sub_tree.item("1")
                self.assertIn("00:00:00,000", str(item1["values"][0]))
                self.assertIn("First Subtitle Line", item1["values"][1])

                item2 = a._sub_tree.item("2")
                self.assertIn("00:00:01,200", str(item2["values"][0]))
                self.assertIn("Second Subtitle Line", item2["values"][1])

                self.assertIn("共 2 條", a._sub_count_label.cget("text"))
            finally:
                a.destroy()

    def test_sub_left_click_and_double_click_jump(self):
        """Verify left clicking a subtitle line seeks playback to its start time."""
        with tempfile.TemporaryDirectory() as td:
            v_path, s_path = _create_dummy_video_and_srt(Path(td))

            a = app.App()
            try:
                a.update()
                a._load_video(str(v_path))
                a.update()

                # Simulate left click on item 2
                evt = MagicMock()
                evt.y = 35
                a._sub_tree.identify_row = MagicMock(return_value="2")
                a._on_sub_left_click(evt)

                # Expect current_sec jumped to 1.2s (start of line 2)
                self.assertAlmostEqual(a._current_sec, 1.2, places=2)

                # Simulate left double click on item 1
                a._sub_tree.identify_row = MagicMock(return_value="1")
                a._on_sub_left_double_click(evt)
                self.assertAlmostEqual(a._current_sec, 0.0, places=2)
            finally:
                a.destroy()

    def test_inline_edit_and_save(self):
        """Verify inline editing writes modified text to memory, treeview, disk, and hot-reloads VLC."""
        with tempfile.TemporaryDirectory() as td:
            v_path, s_path = _create_dummy_video_and_srt(Path(td))

            a = app.App()
            try:
                a.update()
                a._load_video(str(v_path))
                a.update()

                # Mock bbox for item 1
                a._sub_tree.bbox = MagicMock(return_value=(200, 10, 200, 24))
                a._start_inline_sub_edit("1")
                self.assertIsNotNone(a._inline_entry)

                # Edit text
                a._inline_entry.delete(0, "end")
                a._inline_entry.insert(0, "Updated First Line")

                # Commit edit
                a._close_inline_sub_edit(commit=True)
                self.assertIsNone(a._inline_entry)

                # Check in-memory update
                self.assertEqual(a._srt_entries[0]["text"], "Updated First Line")

                # Check Treeview update
                item1 = a._sub_tree.item("1")
                self.assertIn("Updated First Line", item1["values"][1])

                # Check disk file update
                file_text = s_path.read_text(encoding="utf-8")
                self.assertIn("Updated First Line", file_text)
            finally:
                a.destroy()

    def test_hot_reload_subtitle(self):
        """Verify clicking Hot Reload button re-reads modified SRT file from disk."""
        with tempfile.TemporaryDirectory() as td:
            v_path, s_path = _create_dummy_video_and_srt(Path(td))

            a = app.App()
            try:
                a.update()
                a._load_video(str(v_path))
                a.update()

                # Modify file on disk externally
                new_content = (
                    "1\n"
                    "00:00:00,000 --> 00:00:01,000\n"
                    "Externally Modified Line\n"
                )
                s_path.write_text(new_content, encoding="utf-8")

                # Trigger hot reload
                a._hot_reload_subtitle()

                # Verify treeview and entries updated
                self.assertEqual(len(a._srt_entries), 1)
                self.assertEqual(a._srt_entries[0]["text"], "Externally Modified Line")
                item1 = a._sub_tree.item("1")
                self.assertIn("Externally Modified Line", item1["values"][1])
                self.assertIn("已重載", a._player_sub_status_label.cget("text"))
            finally:
                a.destroy()

    def test_player_step_controls(self):
        """Verify player step forward/backward buttons seek and keep playback going."""
        with tempfile.TemporaryDirectory() as td:
            v_path, s_path = _create_dummy_video_and_srt(Path(td))

            a = app.App()
            try:
                a.update()
                a._load_video(str(v_path))
                a.update()

                # In player tab, step forward large (+5s clamped to duration 3s)
                a._on_player_step_forward_large()
                self.assertAlmostEqual(a._current_sec, 3.0, places=2)

                # Step back small (-3s)
                a._on_player_step_back_small()
                self.assertAlmostEqual(a._current_sec, 0.0, places=2)
            finally:
                a.destroy()

    def test_window_scale_and_fullscreen(self):
        """Verify window scale presets and fullscreen toggle."""
        a = app.App()
        try:
            a.update()
            # Window scale change
            a._on_window_scale_change("0.5x")
            geo_half = a.geometry()
            self.assertTrue(len(geo_half) > 0)

            # Fullscreen toggle
            a._toggle_fullscreen()
            self.assertTrue(a._is_fullscreen)
            self.assertIsNotNone(a._fullscreen_win)
            self.assertEqual(a._player_fullscreen_btn.cget("text"), "🗗 退出全螢幕")

            # Exit fullscreen
            a._toggle_fullscreen()
            self.assertFalse(a._is_fullscreen)
            self.assertIsNone(a._fullscreen_win)
            self.assertEqual(a._player_fullscreen_btn.cget("text"), "⛶ 全螢幕")
        finally:
            a.destroy()

    def test_dual_tab_state_synchronization(self):
        """Verify video, subtitle, and time state stays in sync between Player and Task tabs."""
        with tempfile.TemporaryDirectory() as td:
            v_path, s_path = _create_dummy_video_and_srt(Path(td))

            a = app.App()
            try:
                a.update()
                a._load_video(str(v_path))
                a.update()

                # Seek in player tab
                a._seek_to(1.5)

                # Switch to Task Tab
                a.tabs.set("  影片與字幕任務  ")
                a._on_tab_change()
                self.assertEqual(a._active_tab_name, "  影片與字幕任務  ")
                self.assertEqual(a._video_label.cget("text"), "sample_v104.mp4")
                self.assertEqual(a._srt_var.get(), str(s_path))
                self.assertAlmostEqual(a._current_sec, 1.5, places=2)

                # Switch back to Player Tab
                a.tabs.set("  影音播放器  ")
                a._on_tab_change()
                self.assertEqual(a._active_tab_name, "  影音播放器  ")
                self.assertEqual(a._player_video_label.cget("text"), "sample_v104.mp4")
                self.assertEqual(a._player_srt_var.get(), str(s_path))
                self.assertAlmostEqual(a._current_sec, 1.5, places=2)
            finally:
                a.destroy()


if __name__ == "__main__":
    unittest.main()

