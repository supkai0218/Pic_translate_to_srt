"""Unit tests for v2.05 features:
1. Folder expansion & custom directory configuration (resolve_captured_dir, resolve_generated_dir, update_recent_dirs)
2. Safe image copying with automatic sequential collision renaming ({stem}_{counter}{suffix})
3. Directory image listing (list_images_in_dir)
4. Dynamic quick capture shortcut key display
5. Aspect ratio & size preset resolution (resolve_closest_aspect_ratio, ASPECT_RATIO_SIZE_PRESETS)
6. Capability sniffing for resolution parameter
"""
import tempfile
import unittest
from pathlib import Path
from PIL import Image

import config
import capture_manager
import image_gen_engine


class TestV205Features(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.work_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    # ------------------------------------------------------------------ #
    #  1. Folder Resolution & Recent History
    # ------------------------------------------------------------------ #
    def test_folder_resolution_defaults(self):
        settings = {}
        cap_dir = config.resolve_captured_dir(settings)
        gen_dir = config.resolve_generated_dir(settings)
        self.assertTrue(Path(cap_dir).is_dir())
        self.assertTrue(Path(gen_dir).is_dir())
        self.assertEqual(cap_dir.name, "captured")
        self.assertEqual(gen_dir.name, "generated")

    def test_folder_resolution_custom(self):
        custom_cap = self.work_path / "my_custom_inputs"
        custom_gen = self.work_path / "my_custom_outputs"
        settings = {
            "storage_folders": {
                "default_captured_dir": str(custom_cap),
                "default_generated_dir": str(custom_gen),
            }
        }
        res_cap = config.resolve_captured_dir(settings)
        res_gen = config.resolve_generated_dir(settings)
        self.assertEqual(Path(res_cap), custom_cap.resolve())
        self.assertEqual(Path(res_gen), custom_gen.resolve())
        self.assertTrue(custom_cap.exists())
        self.assertTrue(custom_gen.exists())

    def test_recent_dirs_fifo_stacking(self):
        settings = {
            "storage_folders": {
                "recent_input_dirs": ["/path/a", "/path/b"],
                "recent_output_dirs": ["/out/1"],
            }
        }
        # Push existing "/path/b" -> should move to front without duplicate
        config.update_recent_dirs(settings, recent_input="/path/b")
        self.assertEqual(settings["storage_folders"]["recent_input_dirs"], ["/path/b", "/path/a"])

        # Push 2 more items -> should cap at max 3
        config.update_recent_dirs(settings, recent_input="/path/c")
        config.update_recent_dirs(settings, recent_input="/path/d")
        self.assertEqual(settings["storage_folders"]["recent_input_dirs"], ["/path/d", "/path/c", "/path/b"])

    # ------------------------------------------------------------------ #
    #  2. Dynamic Shortcut Key Resolution
    # ------------------------------------------------------------------ #
    def test_get_quick_capture_key_display(self):
        # Default 'c'
        s1 = {"keybindings": {"quick_capture": "c"}}
        self.assertEqual(config.get_quick_capture_key_display(s1), "C")

        # Custom 'f2'
        s2 = {"keybindings": {"quick_capture": "F2"}}
        self.assertEqual(config.get_quick_capture_key_display(s2), "F2")

        # Empty fallback
        s3 = {"keybindings": {}}
        self.assertEqual(config.get_quick_capture_key_display(s3), "C")

    # ------------------------------------------------------------------ #
    #  3. Safe Image Copying with Sequential Suffix Collision Renaming
    # ------------------------------------------------------------------ #
    def test_copy_images_collision_renaming(self):
        src_dir = self.work_path / "src"
        dst_dir = self.work_path / "dst"
        src_dir.mkdir()
        dst_dir.mkdir()

        # Create dummy image in source
        img_p = src_dir / "sample_pic.png"
        img = Image.new("RGB", (64, 64), color=(255, 0, 0))
        img.save(img_p)

        # First copy -> should be sample_pic.png
        copied1 = capture_manager.copy_images_to_directory([img_p], dst_dir)
        self.assertEqual(len(copied1), 1)
        self.assertEqual(copied1[0].name, "sample_pic.png")
        self.assertTrue(copied1[0].is_file())

        # Second copy of same file -> should be sample_pic_1.png
        copied2 = capture_manager.copy_images_to_directory([img_p], dst_dir)
        self.assertEqual(len(copied2), 1)
        self.assertEqual(copied2[0].name, "sample_pic_1.png")
        self.assertTrue(copied2[0].is_file())

        # Third copy -> should be sample_pic_2.png
        copied3 = capture_manager.copy_images_to_directory([img_p], dst_dir)
        self.assertEqual(len(copied3), 1)
        self.assertEqual(copied3[0].name, "sample_pic_2.png")
        self.assertTrue(copied3[0].is_file())

    # ------------------------------------------------------------------ #
    #  4. Directory Image Listing (list_images_in_dir)
    # ------------------------------------------------------------------ #
    def test_list_images_in_dir(self):
        test_dir = self.work_path / "gallery"
        test_dir.mkdir()

        # Create image and non-image files
        img = Image.new("RGB", (32, 32), color=(0, 255, 0))
        img.save(test_dir / "img1.jpg")
        img.save(test_dir / "img2.png")
        (test_dir / "notes.txt").write_text("not an image", encoding="utf-8")

        images = capture_manager.list_images_in_dir(test_dir)
        self.assertEqual(len(images), 2)
        names = {it["name"] for it in images}
        self.assertIn("img1.jpg", names)
        self.assertIn("img2.png", names)
        self.assertNotIn("notes.txt", names)

    # ------------------------------------------------------------------ #
    #  5. Target Directory Saving Support
    # ------------------------------------------------------------------ #
    def test_save_generated_image_to_target_dir(self):
        target_gen = self.work_path / "custom_gen"
        img = Image.new("RGB", (100, 100), color=(10, 20, 30))
        saved = capture_manager.save_generated_image(
            img=img,
            prompt_name="portrait",
            model_name="test-model",
            target_dir=target_gen,
        )
        self.assertTrue(saved.is_file())
        self.assertEqual(saved.parent.resolve(), target_gen.resolve())
        self.assertIn("portrait", saved.name)

    def test_save_captured_image_to_target_dir(self):
        target_cap = self.work_path / "custom_cap"
        img = Image.new("RGB", (100, 100), color=(50, 60, 70))
        saved = capture_manager.save_captured_image(
            img=img,
            custom_name="test_cap",
            target_dir=target_cap,
        )
        self.assertTrue(saved.is_file())
        self.assertEqual(saved.parent.resolve(), target_cap.resolve())

    # ------------------------------------------------------------------ #
    #  6. Aspect Ratio & Size Calculations
    # ------------------------------------------------------------------ #
    def test_resolve_closest_aspect_ratio(self):
        self.assertEqual(image_gen_engine.resolve_closest_aspect_ratio("1024x1024"), "1:1")
        self.assertEqual(image_gen_engine.resolve_closest_aspect_ratio("1920x1080"), "16:9")
        self.assertEqual(image_gen_engine.resolve_closest_aspect_ratio("1080x1920"), "9:16")
        self.assertEqual(image_gen_engine.resolve_closest_aspect_ratio("1024x768"), "4:3")
        self.assertEqual(image_gen_engine.resolve_closest_aspect_ratio("768x1024"), "3:4")
        self.assertEqual(image_gen_engine.resolve_closest_aspect_ratio("1536x1024"), "3:2")
        self.assertEqual(image_gen_engine.resolve_closest_aspect_ratio("1024x1536"), "2:3")
        self.assertEqual(image_gen_engine.resolve_closest_aspect_ratio("2560x1080"), "21:9")
        self.assertEqual(image_gen_engine.resolve_closest_aspect_ratio("auto"), "1:1")

    def test_aspect_ratio_size_presets_defined(self):
        presets = image_gen_engine.ASPECT_RATIO_SIZE_PRESETS
        self.assertIn("1:1", presets)
        self.assertIn("16:9", presets)
        self.assertIn("9:16", presets)
        self.assertIn("4:3", presets)
        self.assertIn("3:4", presets)
        self.assertIn("3:2", presets)
        self.assertIn("2:3", presets)
        self.assertIn("1024x1024", presets["1:1"])
        self.assertIn("2048x1152", presets["16:9"])

    # ------------------------------------------------------------------ #
    #  7. Capabilities Sniffing for Resolution
    # ------------------------------------------------------------------ #
    def test_capabilities_supports_resolution(self):
        cfg = image_gen_engine.P2PProviderCfg(
            name="OpenRouter",
            api_url="https://openrouter.ai/api/v1/chat/completions",
            api_key="test-key",
            model="openai/gpt-image-2.5-flare",
            provider_type="openrouter",
        )
        caps = image_gen_engine.sniff_model_capabilities(cfg)
        self.assertTrue(caps.get("supports_resolution", False))

    # ------------------------------------------------------------------ #
    #  8. P2P Input to Output Copy Logic Verification
    # ------------------------------------------------------------------ #
    def test_copy_selected_input_to_output_method(self):
        from unittest.mock import MagicMock, patch
        import app

        dummy_app = MagicMock(spec=app.App)
        dummy_app._selected_captured_paths = {str(self.work_path / "img1.png")}
        dummy_app._current_output_dir = self.work_path / "out"
        dummy_app._selected_output_paths = set()
        dummy_app._p2p_output_checkboxes = {}
        dummy_app._output_images = []

        with patch("capture_manager.copy_images_to_directory") as mock_copy, \
             patch("tkinter.messagebox.showinfo") as mock_info:
            target_file = self.work_path / "out" / "img1.png"
            mock_copy.return_value = [target_file]

            # Invoke method on dummy_app
            app.App._copy_selected_input_to_output(dummy_app)

            mock_copy.assert_called_once_with(dummy_app._selected_captured_paths, dummy_app._current_output_dir)
            dummy_app._refresh_p2p_output_gallery.assert_called_once()
            dummy_app._set_active_output_image.assert_called_once_with(str(target_file))
            self.assertIn(str(target_file), dummy_app._selected_output_paths)
            mock_info.assert_called_once()


if __name__ == "__main__":
    unittest.main()

