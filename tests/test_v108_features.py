"""Automated unit tests for v1.08 features:
1. TS / MPEG-TS container playback support via avformat demuxer in LibVLC.
2. Subtitle rendering toggle switch on media player and video task screens.
3. Configuration persistence and file dialog filters.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import app
import config
import vlc_engine


class TestV108Features(unittest.TestCase):
    def test_config_sub_render_default(self):
        """Verify sub_render_enabled setting is in config._DEFAULTS."""
        self.assertIn("sub_render_enabled", config._DEFAULTS)
        self.assertTrue(config._DEFAULTS["sub_render_enabled"])

    def test_vlc_ts_demux_option(self):
        """Verify .ts, .m2ts, and .mts media receive :demux=avformat option."""
        if not vlc_engine.is_vlc_available():
            self.skipTest("LibVLC not available in test environment")

        player = vlc_engine.VLCPlayer()

        with tempfile.TemporaryDirectory() as tmpdir:
            for ext in (".ts", ".m2ts", ".mts"):
                video_file = Path(tmpdir) / f"test{ext}"
                video_file.write_bytes(b"dummy")

                mock_media = MagicMock()
                with patch.object(player._instance, "media_new", return_value=mock_media):
                    with patch.object(player._player, "set_media"):
                        player.load_media(str(video_file))
                        mock_media.add_option.assert_any_call(":demux=avformat")

            # Non-ts video should not receive demux=avformat
            mp4_file = Path(tmpdir) / "test.mp4"
            mp4_file.write_bytes(b"dummy")
            mock_media_mp4 = MagicMock()
            with patch.object(player._instance, "media_new", return_value=mock_media_mp4):
                with patch.object(player._player, "set_media"):
                    player.load_media(str(mp4_file))
                    demux_calls = [
                        call for call in mock_media_mp4.add_option.call_args_list
                        if call[0] and ":demux=avformat" in call[0]
                    ]
                    self.assertEqual(len(demux_calls), 0)

    def test_vlc_subtitle_visibility_controls(self):
        """Verify subtitle visibility getter, setter, and toggle on VLCPlayer."""
        if not vlc_engine.is_vlc_available():
            self.skipTest("LibVLC not available in test environment")

        player = vlc_engine.VLCPlayer()
        self.assertTrue(player.is_subtitle_visible())

        # Toggle to false
        new_state = player.toggle_subtitle_visible()
        self.assertFalse(new_state)
        self.assertFalse(player.is_subtitle_visible())

        # Set explicitly to True
        player.set_subtitle_visible(True)
        self.assertTrue(player.is_subtitle_visible())

        # Set explicitly to False
        player.set_subtitle_visible(False)
        self.assertFalse(player.is_subtitle_visible())

    def test_vlc_load_media_respects_subtitle_visible(self):
        """Verify load_media does not add subtitle slave when subtitle rendering is disabled."""
        if not vlc_engine.is_vlc_available():
            self.skipTest("LibVLC not available in test environment")

        player = vlc_engine.VLCPlayer()
        with tempfile.TemporaryDirectory() as tmpdir:
            video_file = Path(tmpdir) / "video.mp4"
            srt_file = Path(tmpdir) / "video.srt"
            video_file.write_bytes(b"dummy")
            srt_file.write_text("1\n00:00:00,000 --> 00:00:01,000\nSub\n", encoding="utf-8")

            # Case 1: Subtitle visible = False
            player.set_subtitle_visible(False)
            mock_media = MagicMock()
            with patch.object(player._instance, "media_new", return_value=mock_media):
                with patch.object(player._player, "set_media"):
                    player.load_media(str(video_file), srt_path=str(srt_file))
                    mock_media.slaves_add.assert_not_called()

            # Case 2: Subtitle visible = True
            player.set_subtitle_visible(True)
            mock_media_on = MagicMock()
            with patch.object(player._instance, "media_new", return_value=mock_media_on):
                with patch.object(player._player, "set_media"):
                    player.load_media(str(video_file), srt_path=str(srt_file))
                    mock_media_on.slaves_add.assert_called_once()

    def test_app_sub_render_ui_components(self):
        """Verify App initializes subtitle rendering switch and variables."""
        a = app.App()
        try:
            a.update()
            self.assertTrue(hasattr(a, "_sub_render_var"))
            self.assertTrue(hasattr(a, "_sub_render_switch"))
            self.assertTrue(a._sub_render_var.get())

            # Test toggle function
            with patch.object(config, "patch_setting") as mock_patch:
                a._sub_render_var.set(False)
                a._on_sub_render_toggle()
                self.assertFalse(a._sub_render_var.get())
                mock_patch.assert_called_with("sub_render_enabled", False)

                a._sub_render_var.set(True)
                a._on_sub_render_toggle()
                self.assertTrue(a._sub_render_var.get())
                mock_patch.assert_called_with("sub_render_enabled", True)
        finally:
            a.destroy()

    def test_update_sub_status_label_reflects_render_state(self):
        """Verify status label reflects [渲染已關閉] when sub_render_var is False."""
        a = app.App()
        try:
            a.update()
            a._sub_render_var.set(False)
            a._update_sub_status_label()
            if hasattr(a, "_player_sub_status_label"):
                text = a._player_sub_status_label.cget("text")
                self.assertIn("渲染已關閉", text)

            a._sub_render_var.set(True)
            a._update_sub_status_label()
            if hasattr(a, "_player_sub_status_label"):
                text = a._player_sub_status_label.cget("text")
                self.assertNotIn("渲染已關閉", text)
        finally:
            a.destroy()


if __name__ == "__main__":
    unittest.main()

