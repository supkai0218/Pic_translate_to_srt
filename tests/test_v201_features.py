"""Unit tests for v2.01 features:
1. Capture Manager (safe filenames, timestamp formatting, save to \\captured, list gallery, 90°/180° rotation, flip)
2. P2P Settings persistence (settings_p2p.json, .env sharing, prompt presets upsert/delete)
3. Image Gen Engine (OpenRouter/Venice/Atlas provider config, multi-image encoding, response extraction)
4. App UI Three-Pillar Hierarchical Structure & Lazy Tab Building for v2.01
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

import capture_manager
import config
import image_gen_engine


class TestV201CaptureManager(unittest.TestCase):
    """Test capture_manager functionalities."""

    def test_sanitize_filename_part(self):
        self.assertEqual(capture_manager.sanitize_filename_part("Episode 01: The Beginning?"), "Episode 01_ The Beginning")
        self.assertEqual(capture_manager.sanitize_filename_part("bad/slash\\test*name?"), "bad_slash_test_name")
        self.assertEqual(capture_manager.sanitize_filename_part(""), "clip")

    def test_sec_to_safe_ts(self):
        self.assertEqual(capture_manager.sec_to_safe_ts(0.0), "00-00-00_000")
        self.assertEqual(capture_manager.sec_to_safe_ts(65.123), "00-01-05_123")
        self.assertEqual(capture_manager.sec_to_safe_ts(3661.05), "01-01-01_050")

    def test_save_captured_image_and_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cap = config.CAPTURED_DIR
            config.CAPTURED_DIR = Path(tmpdir) / "captured"
            try:
                img = Image.new("RGB", (100, 100), color=(255, 0, 0))
                saved_path = capture_manager.save_captured_image(
                    img, video_path="C:/videos/my_anime.mp4", sec=72.5, is_crop=True
                )
                self.assertTrue(saved_path.exists())
                self.assertIn("my_anime_00-01-12_500_crop", saved_path.name)

                # Test listing
                items = capture_manager.list_captured_images()
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["name"], saved_path.name)
            finally:
                config.CAPTURED_DIR = old_cap

    def test_rotate_and_flip_image(self):
        # 10x20 image
        img = Image.new("RGB", (10, 20), color=(100, 150, 200))
        # Clockwise 90 -> 20x10
        r90 = capture_manager.rotate_image(img, 90)
        self.assertEqual(r90.size, (20, 10))

        # Clockwise 180 -> 10x20
        r180 = capture_manager.rotate_image(img, 180)
        self.assertEqual(r180.size, (10, 20))

        # Clockwise 270 -> 20x10
        r270 = capture_manager.rotate_image(img, 270)
        self.assertEqual(r270.size, (20, 10))

        # Flip horizontal
        flipped = capture_manager.flip_image(img, horizontal=True)
        self.assertEqual(flipped.size, (10, 20))

    def test_save_generated_image(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_gen = config.GENERATED_DIR
            config.GENERATED_DIR = Path(tmpdir) / "generated"
            try:
                img = Image.new("RGB", (120, 120), color=(0, 255, 0))
                saved_p = capture_manager.save_generated_image(img, prompt_name="Anime_Style")
                self.assertTrue(saved_p.exists())
                self.assertIn("Anime_Style", saved_p.name)
                self.assertTrue(saved_p.name.endswith(".png"))
            finally:
                config.GENERATED_DIR = old_gen


class TestV201P2PSettings(unittest.TestCase):
    """Test settings_p2p.json configuration."""

    def test_load_and_save_p2p_settings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_file = config.SETTINGS_P2P_FILE
            config.SETTINGS_P2P_FILE = Path(tmpdir) / "settings_p2p.json"
            try:
                s = config.load_p2p_settings()
                self.assertIn("providers", s)
                self.assertIn("drawing_prompts", s)
                prov_names = [p["name"] for p in s["providers"]]
                self.assertIn("OpenRouter", prov_names)
                self.assertIn("Venice AI", prov_names)

                # Save with secret
                s["providers"][0]["api_key"] = "secret_p2p_key"
                config.save_p2p_settings(s)

                # Verify clean on disk
                import json
                with open(config.SETTINGS_P2P_FILE, "r", encoding="utf-8") as f:
                    disk_data = json.load(f)
                self.assertEqual(disk_data["providers"][0]["api_key"], "")
            finally:
                config.SETTINGS_P2P_FILE = old_file

    def test_p2p_drawing_prompt_crud(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_file = config.SETTINGS_P2P_FILE
            config.SETTINGS_P2P_FILE = Path(tmpdir) / "settings_p2p.json"
            try:
                config.upsert_drawing_prompt({
                    "id": "custom_1",
                    "name": "我的自訂風格",
                    "positive": "vibrant, 4k",
                    "negative": "blurry",
                })
                s = config.load_p2p_settings()
                found = any(p["id"] == "custom_1" for p in s["drawing_prompts"])
                self.assertTrue(found)

                config.delete_drawing_prompt("custom_1")
                s2 = config.load_p2p_settings()
                found2 = any(p["id"] == "custom_1" for p in s2["drawing_prompts"])
                self.assertFalse(found2)
            finally:
                config.SETTINGS_P2P_FILE = old_file

    def test_p2p_provider_crud_and_models(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_file = config.SETTINGS_P2P_FILE
            old_dotenv = config.DOTENV_PATH
            config.SETTINGS_P2P_FILE = Path(tmpdir) / "settings_p2p.json"
            config.DOTENV_PATH = Path(tmpdir) / ".env"
            config.DOTENV_PATH.touch()
            try:
                # 1. Upsert new provider with key
                config.upsert_p2p_provider({
                    "name": "CustomP2P",
                    "api_url": "https://api.custom.ai/v1",
                    "models": ["model-a", "model-b"],
                    "selected_model": "model-a",
                    "api_key": "test_secret_key_123",
                })
                s = config.load_p2p_settings()
                p = next(x for x in s["providers"] if x["name"] == "CustomP2P")
                self.assertEqual(p["api_url"], "https://api.custom.ai/v1")
                self.assertEqual(p["selected_model"], "model-a")

                # Verify key was written to .env
                env_key = p.get("api_key_env") or "API_KEY_CUSTOMP2P"
                self.assertEqual(os.environ.get(env_key), "test_secret_key_123")

                # 2. Add model to provider
                config.add_model_to_p2p_provider("CustomP2P", "model-c")
                s2 = config.load_p2p_settings()
                p2 = next(x for x in s2["providers"] if x["name"] == "CustomP2P")
                self.assertIn("model-c", p2["models"])
                self.assertEqual(p2["selected_model"], "model-c")

                # 3. Select model
                config.select_p2p_provider_model("CustomP2P", "model-b")
                s3 = config.load_p2p_settings()
                p3 = next(x for x in s3["providers"] if x["name"] == "CustomP2P")
                self.assertEqual(p3["selected_model"], "model-b")

                # 4. Delete model
                config.delete_model_from_p2p_provider("CustomP2P", "model-b")
                s4 = config.load_p2p_settings()
                p4 = next(x for x in s4["providers"] if x["name"] == "CustomP2P")
                self.assertNotIn("model-b", p4["models"])

                # 5. Delete provider
                config.delete_p2p_provider("CustomP2P")
                s5 = config.load_p2p_settings()
                self.assertFalse(any(x["name"] == "CustomP2P" for x in s5["providers"]))
            finally:
                config.SETTINGS_P2P_FILE = old_file
                config.DOTENV_PATH = old_dotenv


class TestV201ImageGenEngine(unittest.TestCase):
    """Test image_gen_engine request formatting and response decoding."""

    def test_encode_image_b64(self):
        img = Image.new("RGB", (200, 100), color=(10, 20, 30))
        b64_str, mime = image_gen_engine.encode_image_b64(img)
        self.assertEqual(mime, "image/jpeg")
        self.assertTrue(len(b64_str) > 50)

    def test_extract_images_from_b64_response(self):
        # Create a small test image base64
        import io
        import base64
        test_img = Image.new("RGB", (32, 32), color=(255, 128, 0))
        buf = io.BytesIO()
        test_img.save(buf, format="PNG")
        b64_val = base64.b64encode(buf.getvalue()).decode("ascii")

        # OpenAI format
        body_openai = {"data": [{"b64_json": b64_val}]}
        res1 = image_gen_engine._extract_images_from_response(body_openai)
        self.assertEqual(len(res1), 1)
        self.assertEqual(res1[0].size, (32, 32))

        # Venice format
        body_venice = {"images": [f"data:image/png;base64,{b64_val}"]}
        res2 = image_gen_engine._extract_images_from_response(body_venice)
        self.assertEqual(len(res2), 1)
        self.assertEqual(res2[0].size, (32, 32))

        # Chat completions markdown data URL format
        body_chat = {
            "choices": [
                {"message": {"content": f"Here is the generated image: ![art](data:image/png;base64,{b64_val})"}}
            ]
        }
        res3 = image_gen_engine._extract_images_from_response(body_chat)
        self.assertEqual(len(res3), 1)
        self.assertEqual(res3[0].size, (32, 32))

        # Atlas Cloud / task polling outputs format
        body_atlas = {
            "code": 200,
            "data": {
                "id": "pred_123",
                "status": "completed",
                "outputs": [f"data:image/png;base64,{b64_val}"],
            }
        }
        res4 = image_gen_engine._extract_images_from_response(body_atlas)
        self.assertEqual(len(res4), 1)
        self.assertEqual(res4[0].size, (32, 32))

    def test_connection_test_404_rejected(self):
        """Verify that an HTTP 404 response is treated as a failure, not success."""
        from unittest.mock import MagicMock, patch
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "Not Found"

        cfg = image_gen_engine.P2PProviderCfg(
            name="UnknownProvider",
            api_url="https://api.example.com/notfound",
            api_key="sk-test",
            model="test-model"
        )
        with patch.object(image_gen_engine, "requests") as mock_req:
            mock_req.get.return_value = mock_resp
            ok, msg = image_gen_engine.test_p2p_connection(cfg)
            self.assertFalse(ok)
            self.assertIn("404", msg)

    def test_atlas_cloud_async_polling(self):
        """Verify Atlas Cloud image generation submits async task and polls until completed."""
        from unittest.mock import MagicMock, patch
        import io, base64

        test_img = Image.new("RGB", (16, 16), color=(0, 255, 0))
        buf = io.BytesIO()
        test_img.save(buf, format="PNG")
        b64_out = base64.b64encode(buf.getvalue()).decode("ascii")

        post_resp = MagicMock()
        post_resp.status_code = 200
        post_resp.json.return_value = {
            "code": 200,
            "data": {"id": "pred_test_abc", "status": "processing"}
        }

        poll_resp_processing = MagicMock()
        poll_resp_processing.status_code = 200
        poll_resp_processing.json.return_value = {
            "code": 200,
            "data": {"id": "pred_test_abc", "status": "processing"}
        }

        poll_resp_completed = MagicMock()
        poll_resp_completed.status_code = 200
        poll_resp_completed.json.return_value = {
            "code": 200,
            "data": {
                "id": "pred_test_abc",
                "status": "completed",
                "outputs": [f"data:image/png;base64,{b64_out}"]
            }
        }

        cfg = image_gen_engine.P2PProviderCfg(
            name="Atlas Cloud",
            api_url="https://api.atlascloud.ai/api/v1/model/generateImage",
            api_key="apikey-mock",
            model="google/nano-banana-2-lite/reference-to-image"
        )

        with patch.object(image_gen_engine, "requests") as mock_req:
            mock_req.post.return_value = post_resp
            mock_req.get.side_effect = [poll_resp_processing, poll_resp_completed]
            with patch("time.sleep"):  # skip actual waiting
                imgs = image_gen_engine.generate_image(cfg=cfg, images=[test_img], prompt="test")
                self.assertEqual(len(imgs), 1)
                self.assertEqual(imgs[0].size, (16, 16))


class TestV201AppHierarchicalTabs(unittest.TestCase):
    """Test v2.01 App UI three-pillar tabs and lazy building."""

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

    def test_crop_box_and_start_frame_reset_on_load_video(self):
        """Verify Bug 1 fix: _load_video resets _crop_box, _start_frame_img, and canvas rect."""
        self.app._switch_to_tab("  影片截圖視窗  ")
        self.app._start_frame_img = Image.new("RGB", (100, 100), color="blue")
        self.app._crop_box = (10, 10, 50, 50)
        self.app._last_crop_box = (10, 10, 50, 50)

        # Mock video engine load
        from unittest.mock import MagicMock
        with unittest.mock.patch("app.VideoEngine") as mock_ve_cls:
            mock_ve = MagicMock()
            mock_ve.duration_sec = 10.0
            mock_ve.fps = 30.0
            mock_ve.width = 640
            mock_ve.height = 360
            mock_ve.seek_time.return_value = (True, Image.new("RGB", (640, 360), color="green"))
            mock_ve_cls.return_value = mock_ve

            self.app._load_video("dummy_video_b.mp4")

            self.assertIsNone(self.app._crop_box)
            self.assertIsNone(self.app._start_frame_img)
            self.assertIsNone(self.app._rect_id)

    def test_get_crop_image_source_override(self):
        """Verify Bug 1 fix: _get_crop_image prioritizes source_override over start_frame_img."""
        red_img = Image.new("RGB", (100, 100), color=(255, 0, 0))
        blue_img = Image.new("RGB", (100, 100), color=(0, 0, 255))
        self.app._start_frame_img = red_img
        self.app._current_frame_img = red_img
        self.app._crop_box = (0, 0, 10, 10)

        # Without override, returns current/start
        crop1 = self.app._get_crop_image()
        self.assertEqual(crop1.getpixel((0, 0)), (255, 0, 0))

        # With source_override, returns crop of override (blue)
        crop2 = self.app._get_crop_image(source_override=blue_img)
        self.assertEqual(crop2.getpixel((0, 0)), (0, 0, 255))

    def test_top_level_tabs_and_subtabs_exist(self):
        """Verify the 5 main tabs exist, along with ocr_tabs and p2p_tabs."""
        # Top-level tabs
        self.assertIn("  影音播放器  ", self.app.tabs._tab_dict)
        self.assertIn("  影片截圖視窗  ", self.app.tabs._tab_dict)
        self.assertIn("  截圖翻譯字幕任務  ", self.app.tabs._tab_dict)
        self.assertIn("  圖生圖任務  ", self.app.tabs._tab_dict)
        self.assertIn("  按鈕與快捷鍵設定  ", self.app.tabs._tab_dict)

        # OCR Sub-tabs (API & Prompt settings)
        self.assertIn("  翻譯字幕 API 設定  ", self.app.ocr_tabs._tab_dict)
        self.assertIn("  翻譯字幕 Prompt 設定  ", self.app.ocr_tabs._tab_dict)

        # P2P Sub-tabs
        self.assertIn("  編輯及預覽視窗  ", self.app.p2p_tabs._tab_dict)
        self.assertIn("  圖生圖 API 設定  ", self.app.p2p_tabs._tab_dict)
        self.assertIn("  圖生圖 Prompt 設定  ", self.app.p2p_tabs._tab_dict)

    def test_player_navigation_and_capture_buttons(self):
        """Verify quick capture and ROI navigation buttons on player tab."""
        self.assertTrue(hasattr(self.app, "_player_btn_capture"))
        self.assertEqual(self.app._player_btn_capture.cget("text"), "📷 截圖至素材庫")
        self.assertTrue(hasattr(self.app, "_player_btn_roi"))
        self.assertEqual(self.app._player_btn_roi.cget("text"), "✂️ 前往影片截圖(ROI)")

    def test_video_capture_tab_builds_dual_action_buttons(self):
        """Verify switching to 影片截圖視窗 builds dual action buttons."""
        self.app._switch_to_tab("  影片截圖視窗  ")
        self.assertIn("  影片截圖視窗  ", self.app._built_tabs)
        self.assertTrue(hasattr(self.app, "_add_task_btn"))
        self.assertEqual(self.app._add_task_btn.cget("text"), "➕ 新增至字幕翻譯任務")
        self.assertTrue(hasattr(self.app, "_capture_p2p_btn"))
        self.assertEqual(self.app._capture_p2p_btn.cget("text"), "🎨 截取範圍至圖生圖素材庫")

    def test_p2p_editor_tab_builds_and_loads(self):
        """Verify switching to P2P tab lazily builds editor controls."""
        self.app._switch_to_tab("  圖生圖任務  ")
        self.assertIn("  圖生圖任務  ", self.app._built_tabs)
        self.assertTrue(hasattr(self.app, "_p2p_edit_canvas"))
        self.assertTrue(hasattr(self.app, "_p2p_gen_btn"))
        self.assertTrue(hasattr(self.app, "_p2p_gallery_scroll"))

    def test_p2p_api_tab_model_management(self):
        """Verify P2P API tab has dedicated model list management controls."""
        self.app._switch_to_tab("  圖生圖 API 設定  ")
        self.assertTrue(hasattr(self.app, "_p2p_api_model_menu"))
        self.assertTrue(hasattr(self.app, "_p2p_selected_model_var"))
        self.assertTrue(hasattr(self.app, "_p2p_new_model_var"))
        self.assertTrue(hasattr(self.app, "_p2p_name_entry"))
        self.assertTrue(hasattr(self.app, "_p2p_url_entry"))


if __name__ == "__main__":
    unittest.main()
