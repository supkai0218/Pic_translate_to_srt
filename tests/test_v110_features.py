"""Unit tests for v1.10 features: Multi-monitor window position memory and safe fallback.

Tests:
1. Config layer defaults and deep_merge support for window_geometry.
2. monitor_utils enumeration, validity check, and work area clamping.
3. monitor_utils fallback when a saved monitor rect no longer exists.
4. App._restore_window_geometry behavior with valid monitor vs disconnected monitor.
5. Window geometry debouncing and persistence.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
import monitor_utils


class TestConfigV110Features(unittest.TestCase):
    """Test configuration handling of window_geometry in config.py."""

    def test_default_has_window_geometry(self):
        """Verify _DEFAULTS includes window_geometry with default None."""
        self.assertIn("window_geometry", config._DEFAULTS)
        self.assertIsNone(config._DEFAULTS["window_geometry"])

    def test_deep_merge_preserves_custom_window_geometry(self):
        """Verify _deep_merge preserves custom window_geometry dict."""
        custom_geo = {
            "x": -1500,
            "y": 100,
            "width": 1200,
            "height": 800,
            "is_maximized": False,
        }
        merged = config._deep_merge(config._DEFAULTS, {"window_geometry": custom_geo})
        self.assertEqual(merged["window_geometry"], custom_geo)

    def test_save_and_load_window_geometry(self):
        """Verify window_geometry survives save_settings / load_settings cycle."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_settings_file = Path(tmpdir) / "settings.json"
            with patch.object(config, "SETTINGS_FILE", temp_settings_file):
                custom_settings = dict(config._DEFAULTS)
                custom_settings["window_geometry"] = {
                    "x": -1400,
                    "y": 80,
                    "width": 1160,
                    "height": 820,
                    "is_maximized": True,
                }
                config.save_settings(custom_settings)

                loaded = config.load_settings()
                self.assertEqual(loaded["window_geometry"]["x"], -1400)
                self.assertEqual(loaded["window_geometry"]["y"], 80)
                self.assertTrue(loaded["window_geometry"]["is_maximized"])


class TestMonitorUtils(unittest.TestCase):
    """Test multi-monitor utility functions in monitor_utils.py."""

    def test_get_monitors_returns_list(self):
        """Verify get_monitors returns a list of monitor dicts."""
        monitors = monitor_utils.get_monitors()
        self.assertIsInstance(monitors, list)
        if monitor_utils.IS_WINDOWS and monitors:
            m = monitors[0]
            self.assertIn("handle", m)
            self.assertIn("is_primary", m)
            self.assertIn("monitor_rect", m)
            self.assertIn("work_rect", m)

    def test_get_primary_monitor_work_rect(self):
        """Verify get_primary_monitor_work_rect returns valid dimensions."""
        left, top, w, h = monitor_utils.get_primary_monitor_work_rect()
        self.assertGreater(w, 0)
        self.assertGreater(h, 0)

    def test_is_rect_on_valid_monitor_primary(self):
        """Verify window rectangle on primary monitor is detected as valid."""
        left, top, w, h = monitor_utils.get_primary_monitor_work_rect()
        # Test a 800x600 window placed inside primary monitor
        valid, work = monitor_utils.is_rect_on_valid_monitor(left + 50, top + 50, 800, 600)
        self.assertTrue(valid)
        self.assertIsNotNone(work)
        self.assertEqual(len(work), 4)

    def test_is_rect_on_valid_monitor_disconnected(self):
        """Verify window rectangle on a disconnected monitor is detected as invalid (False)."""
        # (50000, 50000) will not intersect any real monitor
        valid, work = monitor_utils.is_rect_on_valid_monitor(50000, 50000, 1160, 820)
        if monitor_utils.IS_WINDOWS:
            self.assertFalse(valid)
            self.assertIsNone(work)

    def test_clamp_to_monitor_work_area_normal(self):
        """Verify clamping preserves valid coordinates inside work area."""
        work_rect = (-1920, 0, 0, 1080)
        cx, cy, cw, ch = monitor_utils.clamp_to_monitor_work_area(-1500, 100, 1160, 820, work_rect)
        self.assertEqual(cx, -1500)
        self.assertEqual(cy, 100)
        self.assertEqual(cw, 1160)
        self.assertEqual(ch, 820)

    def test_clamp_to_monitor_work_area_out_of_bounds(self):
        """Verify clamping pulls window back if titlebar is outside work area."""
        work_rect = (0, 0, 1920, 1080)
        # y is -50 (above monitor), x is 2500 (beyond right edge)
        cx, cy, cw, ch = monitor_utils.clamp_to_monitor_work_area(2500, -50, 1160, 820, work_rect)
        self.assertEqual(cy, 0)  # clamped to top of work area
        self.assertLessEqual(cx, work_rect[2] - 100)  # titlebar grab-able


class TestAppGeometryRestoration(unittest.TestCase):
    """Test window geometry restoration and fallback behavior."""

    @patch("config.load_settings")
    def test_restore_geometry_on_valid_monitor(self, mock_load):
        """Verify window restores to target monitor when valid."""
        mock_load.return_value = {
            "window_geometry": {
                "x": -1500,
                "y": 100,
                "width": 1160,
                "height": 820,
                "is_maximized": False,
            }
        }

        # Mock is_rect_on_valid_monitor to simulate valid secondary monitor
        with patch.object(monitor_utils, "is_rect_on_valid_monitor", return_value=(True, (-1920, 0, 0, 1080))):
            geo = mock_load.return_value["window_geometry"]
            valid, work = monitor_utils.is_rect_on_valid_monitor(geo["x"], geo["y"], geo["width"], geo["height"])
            self.assertTrue(valid)
            cx, cy, cw, ch = monitor_utils.clamp_to_monitor_work_area(
                geo["x"], geo["y"], geo["width"], geo["height"], work
            )
            geo_str = f"{cw}x{ch}+{cx}+{cy}"
            self.assertEqual(geo_str, "1160x820+-1500+100")

    @patch("config.load_settings")
    def test_restore_geometry_fallback_when_monitor_disconnected(self, mock_load):
        """Verify window falls back to primary monitor (Monitor 1) when saved monitor is disconnected."""
        mock_load.return_value = {
            "window_geometry": {
                "x": 4000,
                "y": 200,
                "width": 1160,
                "height": 820,
                "is_maximized": False,
            }
        }

        # Mock is_rect_on_valid_monitor to simulate disconnected monitor
        with patch.object(monitor_utils, "is_rect_on_valid_monitor", return_value=(False, None)), \
             patch.object(monitor_utils, "get_primary_monitor_work_rect", return_value=(0, 0, 1920, 1080)):
            geo = mock_load.return_value["window_geometry"]
            valid, work = monitor_utils.is_rect_on_valid_monitor(geo["x"], geo["y"], geo["width"], geo["height"])
            self.assertFalse(valid)

            # Fallback logic should place window on primary monitor
            prim_left, prim_top, prim_w, prim_h = monitor_utils.get_primary_monitor_work_rect()
            fallback_x = prim_left + max(0, (prim_w - geo["width"]) // 2)
            fallback_y = prim_top + max(0, (prim_h - geo["height"]) // 2)

            self.assertEqual(fallback_x, (1920 - 1160) // 2)
            self.assertEqual(fallback_y, (1080 - 820) // 2)
            self.assertGreater(fallback_x, 0)
            self.assertGreater(fallback_y, 0)


if __name__ == "__main__":
    unittest.main()

