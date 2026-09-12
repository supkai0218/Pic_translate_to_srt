"""Automated tests for v1.02 features: custom step buttons, keybindings settings, and event handling."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock

import tkinter as tk
import customtkinter as ctk

import app
import config


class TestV102Features(unittest.TestCase):
    def test_config_defaults(self):
        self.assertIn("step_settings", config._DEFAULTS)
        self.assertIn("keybindings", config._DEFAULTS)

        step_s = config._DEFAULTS["step_settings"]
        self.assertEqual(step_s["step_back_large_sec"], 5.0)
        self.assertEqual(step_s["step_back_small_sec"], 3.0)
        self.assertEqual(step_s["step_back_frames"], 1)
        self.assertEqual(step_s["step_forward_frames"], 1)
        self.assertEqual(step_s["step_forward_small_sec"], 3.0)
        self.assertEqual(step_s["step_forward_large_sec"], 5.0)

        kb = config._DEFAULTS["keybindings"]
        self.assertEqual(kb["play_pause"], "space")
        self.assertEqual(kb["step_back_large"], "Left")
        self.assertEqual(kb["step_back_small"], "j")
        self.assertEqual(kb["step_forward_small"], "l")
        self.assertEqual(kb["step_forward_large"], "Right")
        self.assertEqual(kb["set_start"], "q")
        self.assertEqual(kb["set_end"], "w")
        self.assertEqual(kb["add_task"], "Return")

    def test_format_key_display(self):
        self.assertEqual(app.format_key_display("space"), "Space (空白鍵)")
        self.assertEqual(app.format_key_display("return"), "Enter (回車鍵)")
        self.assertEqual(app.format_key_display("bracketleft"), "[")
        self.assertEqual(app.format_key_display("bracketright"), "]")
        self.assertEqual(app.format_key_display("left"), "← Left (左方向鍵)")
        self.assertEqual(app.format_key_display("q"), "Q")
        self.assertEqual(app.format_key_display(""), "（未設定）")

    def test_step_buttons_customization(self):
        a = app.App()
        try:
            a.update()
            # Verify initial button texts
            self.assertEqual(a._btn_step_back_large.cget("text"), "⏪ -5s")
            self.assertEqual(a._btn_step_back_small.cget("text"), "⏪ -3s")
            self.assertEqual(a._btn_step_back_frame.cget("text"), "◀ -1幀")
            self.assertEqual(a._btn_step_forward_frame.cget("text"), "▶ +1幀")
            self.assertEqual(a._btn_step_forward_small.cget("text"), "⏩ +3s")
            self.assertEqual(a._btn_step_forward_large.cget("text"), "⏩ +5s")

            # Custom step values
            a._step_settings["step_back_large_sec"] = 10.0
            a._step_settings["step_back_small_sec"] = 2.0
            a._step_settings["step_back_frames"] = 3
            a._step_settings["step_forward_frames"] = 4
            a._step_settings["step_forward_small_sec"] = 2.5
            a._step_settings["step_forward_large_sec"] = 12.0
            a._refresh_step_buttons()

            self.assertEqual(a._btn_step_back_large.cget("text"), "⏪ -10s")
            self.assertEqual(a._btn_step_back_small.cget("text"), "⏪ -2s")
            self.assertEqual(a._btn_step_back_frame.cget("text"), "◀ -3幀")
            self.assertEqual(a._btn_step_forward_frame.cget("text"), "▶ +4幀")
            self.assertEqual(a._btn_step_forward_small.cget("text"), "⏩ +2.5s")
            self.assertEqual(a._btn_step_forward_large.cget("text"), "⏩ +12s")
        finally:
            a.destroy()

    def test_step_settings_save_and_reset(self):
        a = app.App()
        try:
            a.update()
            # Set values in stringvars
            a._s_back_large_var.set("8.5")
            a._s_back_small_var.set("1.5")
            a._s_back_frame_var.set("2")
            a._s_fwd_frame_var.set("3")
            a._s_fwd_small_var.set("1.8")
            a._s_fwd_large_var.set("9.0")

            a._save_step_settings()
            self.assertEqual(a._step_settings["step_back_large_sec"], 8.5)
            self.assertEqual(a._step_settings["step_back_small_sec"], 1.5)
            self.assertEqual(a._btn_step_back_large.cget("text"), "⏪ -8.5s")
            self.assertEqual(a._btn_step_back_small.cget("text"), "⏪ -1.5s")

            # Reset to defaults
            a._reset_step_settings()
            self.assertEqual(a._step_settings["step_back_large_sec"], 5.0)
            self.assertEqual(a._step_settings["step_back_small_sec"], 3.0)
            self.assertEqual(a._btn_step_back_large.cget("text"), "⏪ -5s")
        finally:
            a.destroy()

    def test_keybinding_recording(self):
        a = app.App()
        try:
            a.update()
            # Set recording action
            a._recording_action = "play_pause"

            # Create synthetic event for keypress 'p'
            evt = tk.Event()
            evt.keysym = "p"

            res = a._on_global_key_press(evt)
            self.assertEqual(res, "break")
            self.assertIsNone(a._recording_action)
            self.assertEqual(a._keybindings["play_pause"], "p")

            # Cancel recording with Escape
            a._recording_action = "set_start"
            esc_evt = tk.Event()
            esc_evt.keysym = "Escape"
            res_esc = a._on_global_key_press(esc_evt)
            self.assertEqual(res_esc, "break")
            self.assertIsNone(a._recording_action)
            self.assertEqual(a._keybindings["set_start"], "q")  # Unchanged
        finally:
            a.destroy()

    def test_keybinding_dispatch(self):
        a = app.App()
        try:
            a.update()
            a.tabs.set("  影片與字幕任務  ")
            a._on_tab_change()
            a._keybindings = dict(config.DEFAULT_KEYBINDINGS)
            a._toggle_play = MagicMock()
            a._on_step_back_large = MagicMock()
            a._set_as_start = MagicMock()
            a._set_end = MagicMock() if hasattr(a, "_set_end") else MagicMock()
            a._set_as_end = MagicMock()
            a._add_task = MagicMock()

            # Space triggers play_pause
            evt = tk.Event()
            evt.keysym = "space"
            res = a._on_global_key_press(evt)
            self.assertEqual(res, "break")
            a._toggle_play.assert_called_once()

            # Left arrow triggers step_back_large
            evt_left = tk.Event()
            evt_left.keysym = "Left"
            a._on_global_key_press(evt_left)
            a._on_step_back_large.assert_called_once()

            # 'q' triggers set_start
            evt_q = tk.Event()
            evt_q.keysym = "q"
            a._on_global_key_press(evt_q)
            a._set_as_start.assert_called_once()

            # 'Return' triggers add_task
            evt_ret = tk.Event()
            evt_ret.keysym = "Return"
            a._on_global_key_press(evt_ret)
            a._add_task.assert_called_once()
        finally:
            a.destroy()

    def test_keybinding_entry_focus_exclusion(self):
        a = app.App()
        try:
            a.update()
            a._keybindings = dict(config.DEFAULT_KEYBINDINGS)
            a._toggle_play = MagicMock()

            # Mock focus_get to return an Entry widget
            mock_entry = tk.Entry(a)
            a.focus_get = MagicMock(return_value=mock_entry)

            evt = tk.Event()
            evt.keysym = "space"
            res = a._on_global_key_press(evt)

            # Should return None and NOT call toggle_play
            self.assertIsNone(res)
            a._toggle_play.assert_not_called()
        finally:
            a.destroy()


if __name__ == "__main__":
    unittest.main()

