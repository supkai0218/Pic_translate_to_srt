"""Unit tests for v2.07 features:
1. save_edited_image naming: {原檔名}_edit_{流水號}
2. Multi-file batch editing with confirmation dialog
3. "另存新素材後刪除原檔" checkbox and .drop relocation logic (single & batch)
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

import capture_manager
import config


class TestV207Features(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.work_dir = Path(self.temp_dir.name)
        self.input_dir = self.work_dir / "captured"
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.drop_dir = self.input_dir / ".drop"
        self.drop_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    # ------------------------------------------------------------------ #
    #  1. save_edited_image Naming Rule: {原檔名}_edit_{流水號}
    # ------------------------------------------------------------------ #
    def test_save_edited_image_naming(self):
        img = Image.new("RGB", (100, 100), color="blue")
        orig_file = self.input_dir / "frame_001.jpg"
        img.save(orig_file)

        # First edit save -> frame_001_edit_1.jpg
        saved1 = capture_manager.save_edited_image(img, original_path=orig_file, target_dir=self.input_dir)
        self.assertEqual(saved1.name, "frame_001_edit_1.jpg")
        self.assertTrue(saved1.exists())

        # Second edit save -> frame_001_edit_2.jpg
        saved2 = capture_manager.save_edited_image(img, original_path=orig_file, target_dir=self.input_dir)
        self.assertEqual(saved2.name, "frame_001_edit_2.jpg")
        self.assertTrue(saved2.exists())

        # Third edit save referencing already edited image -> frame_001_edit_3.jpg (no nested _edit_1_edit_1)
        saved3 = capture_manager.save_edited_image(img, original_path=saved1, target_dir=self.input_dir)
        self.assertEqual(saved3.name, "frame_001_edit_3.jpg")
        self.assertTrue(saved3.exists())

    def test_save_edited_image_empty_original_path(self):
        img = Image.new("RGB", (50, 50), color="green")
        saved = capture_manager.save_edited_image(img, original_path="", target_dir=self.input_dir)
        self.assertEqual(saved.name, "captured_edit_1.jpg")
        self.assertTrue(saved.exists())

    # ------------------------------------------------------------------ #
    #  2. Multi-file Batch Edit Confirmation & Logic
    # ------------------------------------------------------------------ #
    def test_batch_edit_confirm_no(self):
        """When user clicks 'No' on confirmation dialog, no files should be modified or created."""
        from app import App

        # Create 3 test images
        img_paths = []
        for i in range(1, 4):
            p = self.input_dir / f"test_{i}.jpg"
            Image.new("RGB", (60, 60), color="red").save(p)
            img_paths.append(str(p))

        mock_app = MagicMock(spec=App)
        mock_app._selected_captured_paths = set(img_paths)
        mock_app._current_input_dir = str(self.input_dir)
        mock_app._settings = {}
        mock_app._p2p_delete_orig_on_save_var = MagicMock()
        mock_app._p2p_delete_orig_on_save_var.get.return_value = False

        # Bind methods to mock_app
        mock_app._batch_edit_p2p_images = App._batch_edit_p2p_images.__get__(mock_app, App)
        mock_app._rotate_p2p_image = App._rotate_p2p_image.__get__(mock_app, App)

        with patch("app.messagebox.askyesno", return_value=False) as mock_ask, \
             patch("app.messagebox.showinfo") as mock_info:
            mock_app._rotate_p2p_image(180)
            mock_ask.assert_called_once()
            # No files created
            created = list(self.input_dir.glob("*_edit_*.jpg"))
            self.assertEqual(len(created), 0)
            mock_info.assert_not_called()

    def test_batch_edit_confirm_yes_without_delete(self):
        """When user clicks 'Yes' without deleting originals, edited files are created and originals kept."""
        from app import App

        img_paths = []
        for i in range(1, 4):
            p = self.input_dir / f"item_{i}.jpg"
            Image.new("RGB", (60, 60), color="yellow").save(p)
            img_paths.append(str(p))

        mock_app = MagicMock(spec=App)
        mock_app._selected_captured_paths = set(img_paths)
        mock_app._current_input_dir = str(self.input_dir)
        mock_app._settings = {}
        mock_app._p2p_delete_orig_on_save_var = MagicMock()
        mock_app._p2p_delete_orig_on_save_var.get.return_value = False

        mock_app._batch_edit_p2p_images = App._batch_edit_p2p_images.__get__(mock_app, App)
        mock_app._rotate_p2p_image = App._rotate_p2p_image.__get__(mock_app, App)

        with patch("app.messagebox.askyesno", return_value=True) as mock_ask, \
             patch("app.messagebox.showinfo") as mock_info:
            mock_app._rotate_p2p_image(180)
            mock_ask.assert_called_once()
            mock_info.assert_called_once()

            # 3 edited images created
            created = list(self.input_dir.glob("item_*_edit_1.jpg"))
            self.assertEqual(len(created), 3)

            # Originals are NOT deleted
            for p_str in img_paths:
                self.assertTrue(Path(p_str).exists())

            # Selection updated to new items
            self.assertEqual(len(mock_app._selected_captured_paths), 3)
            for c in created:
                self.assertIn(str(c), mock_app._selected_captured_paths)

    # ------------------------------------------------------------------ #
    #  3. "另存新素材後刪除原檔" Checkbox Option
    # ------------------------------------------------------------------ #
    def test_batch_edit_with_delete_orig(self):
        """User checks '另存新素材後刪除原檔' -> batch 180 rotate -> confirm -> edits saved, originals moved to .drop."""
        from app import App

        img_paths = []
        for i in range(1, 4):
            p = self.input_dir / f"batch_{i}.jpg"
            Image.new("RGB", (80, 80), color="purple").save(p)
            img_paths.append(str(p))

        mock_app = MagicMock(spec=App)
        mock_app._selected_captured_paths = set(img_paths)
        mock_app._current_input_dir = str(self.input_dir)
        mock_app._settings = {}
        mock_app._p2p_delete_orig_on_save_var = MagicMock()
        mock_app._p2p_delete_orig_on_save_var.get.return_value = True

        mock_app._batch_edit_p2p_images = App._batch_edit_p2p_images.__get__(mock_app, App)
        mock_app._rotate_p2p_image = App._rotate_p2p_image.__get__(mock_app, App)

        with patch("app.config.resolve_captured_drop_dir", return_value=self.drop_dir), \
             patch("app.messagebox.askyesno", return_value=True) as mock_ask, \
             patch("app.messagebox.showinfo") as mock_info:
            mock_app._rotate_p2p_image(180)
            mock_ask.assert_called_once()
            mock_info.assert_called_once()

            # Check new files created
            created = list(self.input_dir.glob("batch_*_edit_1.jpg"))
            self.assertEqual(len(created), 3)

            # Check original files moved to .drop
            for p_str in img_paths:
                self.assertFalse(Path(p_str).exists())
                dropped = self.drop_dir / Path(p_str).name
                self.assertTrue(dropped.exists())

    def test_single_save_with_delete_orig(self):
        """Single image edit: when delete_orig is True, save as new and original moved to .drop."""
        from app import App

        orig_p = self.input_dir / "single_test.jpg"
        orig_img = Image.new("RGB", (50, 50), color="white")
        orig_img.save(orig_p)

        mock_app = MagicMock(spec=App)
        mock_app._active_edit_path = str(orig_p)
        mock_app._active_p2p_image_path = str(orig_p)
        mock_app._active_edit_img = orig_img
        mock_app._current_input_dir = str(self.input_dir)
        mock_app._settings = {}
        mock_app._selected_captured_paths = {str(orig_p)}
        mock_app._p2p_delete_orig_on_save_var = MagicMock()
        mock_app._p2p_delete_orig_on_save_var.get.return_value = True

        mock_app._save_p2p_edited_as_new = App._save_p2p_edited_as_new.__get__(mock_app, App)

        with patch("app.config.resolve_captured_drop_dir", return_value=self.drop_dir), \
             patch("app.messagebox.showinfo") as mock_info:
            mock_app._save_p2p_edited_as_new()
            mock_info.assert_called_once()

            # Original removed from input_dir and moved to .drop
            self.assertFalse(orig_p.exists())
            self.assertTrue((self.drop_dir / "single_test.jpg").exists())

            # New file created: single_test_edit_1.jpg
            new_file = self.input_dir / "single_test_edit_1.jpg"
            self.assertTrue(new_file.exists())
            self.assertIn(str(new_file), mock_app._selected_captured_paths)


if __name__ == "__main__":
    unittest.main()

