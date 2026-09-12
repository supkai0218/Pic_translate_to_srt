"""Automated tests for v1.03 features: LibVLC audio/video engine, dual display, and subtitle integration."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import tkinter as tk
import customtkinter as ctk
import numpy as np
import cv2
from PIL import Image

import app
import config
import srt_utils
import vlc_engine


def _create_dummy_video_and_srt(tmp_dir: Path) -> tuple[Path, Path]:
    """Create a 2-second 30fps black video file and matching .srt subtitle file."""
    video_path = tmp_dir / "test_sample.mp4"
    srt_path = tmp_dir / "test_sample.srt"

    # Write dummy MP4 using OpenCV VideoWriter
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    fps = 30.0
    w, h = 320, 240
    out = cv2.VideoWriter(str(video_path), fourcc, fps, (w, h))
    for _ in range(60):
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        # Put small text
        cv2.putText(frame, "TEST", (50, 100), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
        out.write(frame)
    out.release()

    # Write matching SRT
    srt_content = (
        "1\n"
        "00:00:00,000 --> 00:00:01,000\n"
        "Hello World\n\n"
        "2\n"
        "00:00:01,000 --> 00:00:02,000\n"
        "Second Line\n"
    )
    srt_path.write_text(srt_content, encoding="utf-8")

    return video_path, srt_path


class TestV103Features(unittest.TestCase):
    def test_vlc_availability_and_version(self):
        """Verify local VLC core binaries are detected and version can be queried."""
        self.assertTrue(vlc_engine.is_vlc_available(), "VLC should be available from ./vlc directory")
        ver = vlc_engine.get_vlc_version()
        self.assertIsInstance(ver, str)
        self.assertTrue(len(ver) > 0)
        self.assertIn("3.", ver, f"Expected LibVLC 3.x, got: {ver}")

    def test_vlc_player_controls(self):
        """Verify VLCPlayer basic operations: volume, mute, rate, subtitle, window binding."""
        player = vlc_engine.VLCPlayer()
        try:
            # Volume
            player.set_volume(75)
            self.assertEqual(player.get_volume(), 75)
            self.assertFalse(player.is_muted)

            # Mute toggle
            muted = player.toggle_mute()
            self.assertTrue(muted)
            self.assertTrue(player.is_muted)
            self.assertEqual(player.get_volume(), 0)

            # Unmute
            unmuted = player.toggle_mute()
            self.assertFalse(unmuted)
            self.assertFalse(player.is_muted)
            self.assertEqual(player.get_volume(), 75)

            # Rate
            player.set_rate(1.5)
            self.assertAlmostEqual(player.get_rate(), 1.5, places=1)

            player.set_rate(1.0)
            self.assertAlmostEqual(player.get_rate(), 1.0, places=1)

            # Subtitle methods
            with tempfile.TemporaryDirectory() as td:
                dummy_srt = Path(td) / "dummy.srt"
                dummy_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")
                ok = player.set_subtitle_file(dummy_srt)
                # set_subtitle_file returns boolean without crashing
                self.assertIsInstance(ok, bool)
                player.clear_subtitle()

        finally:
            player.release()

    def test_app_vlc_initialization_and_audio_ui(self):
        """Test App initialized with VLC audio widgets and dual display frames."""
        a = app.App()
        try:
            a.update()
            self.assertIsNotNone(a._vlc_player)
            self.assertTrue(hasattr(a, "_vlc_frame"))
            self.assertTrue(hasattr(a, "_canvas"))
            self.assertTrue(hasattr(a, "_volume_slider"))
            self.assertTrue(hasattr(a, "_mute_btn"))
            self.assertTrue(hasattr(a, "_rate_menu"))
            self.assertTrue(hasattr(a, "_sub_status_label"))

            # Initial values
            self.assertEqual(a._active_display_mode, "canvas")
            self.assertEqual(a._volume_var.get(), 80.0)
            self.assertEqual(a._playback_rate_var.get(), "1.0x")
            self.assertIn("未掛載", a._sub_status_label.cget("text"))

            # Test volume slider callback
            a._on_volume_change(50.0)
            self.assertEqual(a._volume_label.cget("text"), "50%")
            self.assertEqual(a._vlc_player.get_volume(), 50)
            self.assertEqual(a._mute_btn.cget("text"), "🔊")

            # Mute toggle callback
            a._toggle_mute()
            self.assertEqual(a._mute_btn.cget("text"), "🔇")
            self.assertEqual(a._volume_label.cget("text"), "0%")

            a._toggle_mute()
            self.assertEqual(a._mute_btn.cget("text"), "🔊")
            self.assertEqual(a._volume_label.cget("text"), "50%")

            # Playback rate callback
            a._on_rate_change("1.5x")
            self.assertAlmostEqual(a._vlc_player.get_rate(), 1.5, places=1)

        finally:
            a.destroy()

    def test_dual_display_switching(self):
        """Test seamless switching between native VLC HWND display and Tkinter ROI canvas."""
        a = app.App()
        try:
            a.update()
            # Initial state is canvas
            self.assertEqual(a._active_display_mode, "canvas")

            # Switch to VLC
            a._show_vlc_display()
            self.assertEqual(a._active_display_mode, "vlc")

            # Switch back to canvas
            a._show_canvas_display()
            self.assertEqual(a._active_display_mode, "canvas")

        finally:
            a.destroy()

    def test_auto_srt_detection_and_subtitle_status(self):
        """Test automatic detection of same-named .srt file when loading a video."""
        with tempfile.TemporaryDirectory() as td:
            v_path, s_path = _create_dummy_video_and_srt(Path(td))

            a = app.App()
            try:
                a.update()
                a._load_video(str(v_path))
                a.update()

                # Verify same-named SRT auto-detected
                self.assertEqual(Path(a._srt_path).resolve(), s_path.resolve())
                self.assertEqual(len(a._srt_entries), 2)
                self.assertIn("已掛載", a._sub_status_label.cget("text"))
                self.assertIn("test_sample.srt", a._sub_status_label.cget("text"))

                # Clear SRT
                a._clear_srt()
                self.assertEqual(a._srt_path, "")
                self.assertEqual(len(a._srt_entries), 0)
                self.assertIn("未掛載", a._sub_status_label.cget("text"))

            finally:
                a.destroy()

    def test_playback_and_seek_synchronization(self):
        """Test play/pause toggle and seek synchronization between OpenCV canvas and VLC."""
        with tempfile.TemporaryDirectory() as td:
            v_path, s_path = _create_dummy_video_and_srt(Path(td))

            a = app.App()
            try:
                a.update()
                a._load_video(str(v_path))
                a.update()

                # Start playback -> switches to VLC mode
                a._start_play()
                self.assertTrue(a._is_playing)
                self.assertEqual(a._active_display_mode, "vlc")
                self.assertEqual(a._play_btn.cget("text"), "⏸ 暫停")

                # Stop playback -> pauses VLC, switches to canvas mode, syncs OpenCV frame
                a._stop_play()
                self.assertFalse(a._is_playing)
                self.assertEqual(a._active_display_mode, "canvas")
                self.assertEqual(a._play_btn.cget("text"), "▶ 播放")
                self.assertIsNotNone(a._current_frame_img)

                # Seek synchronization
                a._seek_to(1.0)
                self.assertAlmostEqual(a._current_sec, 1.0, places=2)

                # Step frame synchronization
                a._step_frame(5)
                self.assertGreater(a._current_sec, 1.0)

            finally:
                a.destroy()


if __name__ == "__main__":
    unittest.main()

