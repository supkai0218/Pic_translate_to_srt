"""Unit tests for v1.09 features: volume persistence and mute defect fix.

Tests:
1. Config layer defaults and deep_merge support for last_volume.
2. Settings persistence with custom last_volume.
3. VLCPlayer initialization with custom volume (initial_volume).
4. VLCPlayer audio_set_mute synchronization when volume > 0 vs volume == 0.
5. VLCPlayer toggle_mute behavior and volume restoration.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
import vlc_engine


class TestConfigV109Features(unittest.TestCase):
    """Test configuration handling of last_volume in config.py."""

    def test_default_has_last_volume(self):
        """Verify _DEFAULTS includes last_volume with default 80."""
        self.assertIn("last_volume", config._DEFAULTS)
        self.assertEqual(config._DEFAULTS["last_volume"], 80)

    def test_deep_merge_preserves_or_supplies_last_volume(self):
        """Verify _deep_merge supplies 80 when absent, and preserves custom value when present."""
        # Case 1: absent in user settings
        merged1 = config._deep_merge(config._DEFAULTS, {})
        self.assertEqual(merged1["last_volume"], 80)

        # Case 2: present with custom value
        merged2 = config._deep_merge(config._DEFAULTS, {"last_volume": 42})
        self.assertEqual(merged2["last_volume"], 42)

    def test_save_and_load_last_volume(self):
        """Verify last_volume survives save_settings / load_settings cycle."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_settings_file = Path(tmpdir) / "settings.json"
            with patch.object(config, "SETTINGS_FILE", temp_settings_file):
                custom_settings = dict(config._DEFAULTS)
                custom_settings["last_volume"] = 35
                config.save_settings(custom_settings)

                loaded = config.load_settings()
                self.assertEqual(loaded["last_volume"], 35)


class TestVLCPlayerV109Features(unittest.TestCase):
    """Test VLCPlayer volume restoration and OS mute synchronization."""

    def setUp(self):
        # Mock libvlc so tests run deterministically in headless CI / unit test environments
        self.mock_vlc = MagicMock()
        self.mock_instance = MagicMock()
        self.mock_player = MagicMock()
        self.mock_vlc.Instance.return_value = self.mock_instance
        self.mock_instance.media_player_new.return_value = self.mock_player

    def test_vlc_player_init_custom_volume(self):
        """Verify VLCPlayer initializes with custom volume and syncs with player."""
        with patch.object(vlc_engine, "vlc", self.mock_vlc), \
             patch.object(vlc_engine, "is_vlc_available", return_value=True):
            player = vlc_engine.VLCPlayer(initial_volume=45)
            self.assertEqual(player.get_volume(), 45)
            self.assertFalse(player.is_muted)
            self.mock_player.audio_set_volume.assert_called_with(45)
            self.mock_player.audio_set_mute.assert_called_with(False)

    def test_vlc_player_init_zero_volume_is_muted(self):
        """Verify VLCPlayer initializes as muted when volume is 0."""
        with patch.object(vlc_engine, "vlc", self.mock_vlc), \
             patch.object(vlc_engine, "is_vlc_available", return_value=True):
            player = vlc_engine.VLCPlayer(initial_volume=0)
            self.assertEqual(player.get_volume(), 0)
            self.assertTrue(player.is_muted)
            self.mock_player.audio_set_volume.assert_called_with(0)
            self.mock_player.audio_set_mute.assert_called_with(True)

    def test_vlc_player_set_volume_unmutes_and_syncs_os(self):
        """Verify setting volume > 0 automatically unmutes and sends audio_set_mute(False)."""
        with patch.object(vlc_engine, "vlc", self.mock_vlc), \
             patch.object(vlc_engine, "is_vlc_available", return_value=True):
            player = vlc_engine.VLCPlayer(initial_volume=0)
            self.assertTrue(player.is_muted)

            # Change volume to 60
            player.set_volume(60)
            self.assertEqual(player.get_volume(), 60)
            self.assertFalse(player.is_muted)
            self.mock_player.audio_set_volume.assert_called_with(60)
            self.mock_player.audio_set_mute.assert_called_with(False)

    def test_vlc_player_set_volume_zero_mutes_and_syncs_os(self):
        """Verify setting volume to 0 automatically marks muted and sends audio_set_mute(True)."""
        with patch.object(vlc_engine, "vlc", self.mock_vlc), \
             patch.object(vlc_engine, "is_vlc_available", return_value=True):
            player = vlc_engine.VLCPlayer(initial_volume=80)
            self.assertFalse(player.is_muted)

            player.set_volume(0)
            self.assertEqual(player.get_volume(), 0)
            self.assertTrue(player.is_muted)
            self.mock_player.audio_set_volume.assert_called_with(0)
            self.mock_player.audio_set_mute.assert_called_with(True)

    def test_vlc_player_play_ensures_unmute_when_volume_positive(self):
        """Verify play() explicitly enforces audio_set_mute(False) if volume > 0."""
        with patch.object(vlc_engine, "vlc", self.mock_vlc), \
             patch.object(vlc_engine, "is_vlc_available", return_value=True):
            player = vlc_engine.VLCPlayer(initial_volume=70)
            self.mock_player.audio_set_mute.reset_mock()

            player.play()
            self.mock_player.audio_set_mute.assert_called_with(False)
            self.mock_player.play.assert_called_once()

    def test_vlc_player_toggle_mute(self):
        """Verify toggle_mute toggles between muted and unmuted and remembers last volume."""
        with patch.object(vlc_engine, "vlc", self.mock_vlc), \
             patch.object(vlc_engine, "is_vlc_available", return_value=True):
            player = vlc_engine.VLCPlayer(initial_volume=65)

            # Mute
            is_muted = player.toggle_mute()
            self.assertTrue(is_muted)
            self.assertEqual(player.get_volume(), 0)
            self.mock_player.audio_set_mute.assert_called_with(True)

            # Unmute
            is_muted = player.toggle_mute()
            self.assertFalse(is_muted)
            self.assertEqual(player.get_volume(), 65)
            self.mock_player.audio_set_mute.assert_called_with(False)


if __name__ == "__main__":
    unittest.main()

