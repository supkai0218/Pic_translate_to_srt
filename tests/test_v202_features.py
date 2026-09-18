"""Unit and regression test suite for v2.02 features.

Covers:
1. Advanced Model Parameters and Schema-Aware Parameter Mapper (image_gen_engine.py).
2. Edited Image Naming ({original_stem}_edit.jpg) and anti-chaining (capture_manager.py).
3. Generated Image Naming ({prompt}_{model}_{timestamp}.png) (capture_manager.py).
4. P2P Advanced Parameters UI and toggle logic (app.py).
"""
import datetime
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

import capture_manager
import config
import image_gen_engine


class TestV202CaptureManagerNaming(unittest.TestCase):
    """Test v2.02 image naming conventions in capture_manager."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_captured = config.CAPTURED_DIR
        self.old_generated = config.GENERATED_DIR
        config.CAPTURED_DIR = Path(self.temp_dir.name) / "captured"
        config.GENERATED_DIR = Path(self.temp_dir.name) / "generated"

    def tearDown(self):
        config.CAPTURED_DIR = self.old_captured
        config.GENERATED_DIR = self.old_generated
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_simplify_model_name(self):
        """Test extraction of clean, safe model tokens from model identifiers."""
        self.assertEqual(
            capture_manager.simplify_model_name("openai/gpt-image-2.5-flare/edit"),
            "gpt-image-2.5-flare"
        )
        self.assertEqual(
            capture_manager.simplify_model_name("google/nano-banana-2-lite/reference-to-image"),
            "nano-banana-2-lite"
        )
        self.assertEqual(
            capture_manager.simplify_model_name("black-forest-labs/flux-1-dev"),
            "flux-1-dev"
        )
        self.assertEqual(
            capture_manager.simplify_model_name("alibaba/wan-2.7-pro/image-edit"),
            "wan-2.7-pro"
        )
        self.assertEqual(
            capture_manager.simplify_model_name("flux-dev-uncensored"),
            "flux-dev-uncensored"
        )
        self.assertEqual(
            capture_manager.simplify_model_name(""),
            "ai_model"
        )

    def test_save_edited_image_naming(self):
        """Verify edited image is saved as '{orig_stem}_edit.jpg' without chain suffixes."""
        img = Image.new("RGB", (50, 50), color=(100, 150, 200))
        orig_file = config.CAPTURED_DIR / "Ep01_00-05-12_340_roi.jpg"
        config.CAPTURED_DIR.mkdir(parents=True, exist_ok=True)
        orig_file.touch()

        # First edit
        p1 = capture_manager.save_edited_image(img, original_path=orig_file)
        self.assertEqual(p1.name, "Ep01_00-05-12_340_roi_edit.jpg")
        self.assertTrue(p1.exists())

        # Second edit of original
        p2 = capture_manager.save_edited_image(img, original_path=orig_file)
        self.assertEqual(p2.name, "Ep01_00-05-12_340_roi_edit_1.jpg")
        self.assertTrue(p2.exists())

        # Edit of an already edited image (should NOT become _edit_edit)
        p3 = capture_manager.save_edited_image(img, original_path=p1)
        self.assertEqual(p3.name, "Ep01_00-05-12_340_roi_edit_2.jpg")
        self.assertTrue(p3.exists())

    def test_save_generated_image_naming(self):
        """Verify generated image is saved as '{prompt}_{model}_YYYYMMDD_hhmm.png'."""
        img = Image.new("RGB", (50, 50), color=(200, 100, 50))
        now_prefix = datetime.datetime.now().strftime("%Y%m%d_%H%M")

        p = capture_manager.save_generated_image(
            img=img,
            prompt_name="人物五視角",
            model_name="openai/gpt-image-2.5-flare/edit"
        )
        expected_base = f"人物五視角_gpt-image-2.5-flare_{now_prefix}"
        self.assertTrue(p.name.startswith(expected_base))
        self.assertTrue(p.name.endswith(".png"))
        self.assertTrue(p.exists())

        # Subsequent generation in same minute
        p2 = capture_manager.save_generated_image(
            img=img,
            prompt_name="人物五視角",
            model_name="openai/gpt-image-2.5-flare/edit"
        )
        self.assertEqual(p2.name, f"{expected_base}_1.png")
        self.assertTrue(p2.exists())


class TestV202SchemaAwareMapper(unittest.TestCase):
    """Test Schema-Aware Parameter Mapper in image_gen_engine."""

    def test_gpt_image_model_detection(self):
        self.assertTrue(image_gen_engine.is_gpt_image_model("openai/gpt-image-2.5-flare/edit"))
        self.assertTrue(image_gen_engine.is_gpt_image_model("openai/gpt-image-2.5-sunburst/edit"))
        self.assertTrue(image_gen_engine.is_gpt_image_model("dall-e-3"))
        self.assertFalse(image_gen_engine.is_gpt_image_model("google/nano-banana-2-lite/reference-to-image"))
        self.assertFalse(image_gen_engine.is_gpt_image_model("black-forest-labs/flux-1-dev"))

    def test_atlas_payload_gpt_image_mapping(self):
        """Verify GPT Image model receives size, quality, background, format and omits aspect_ratio."""
        cfg = image_gen_engine.P2PProviderCfg(
            name="Atlas Cloud",
            api_url="https://api.atlascloud.ai/api/v1/model/generateImage",
            api_key="k",
            model="openai/gpt-image-2.5-flare/edit"
        )
        payload = image_gen_engine.build_atlas_payload(
            cfg=cfg,
            images=[],
            prompt="Turn into cyberpunk city",
            negative_prompt="blurry",
            aspect_ratio="16:9",
            strength=0.7,
            num_outputs=2,
            quality="high",
            background="transparent",
            output_format="png",
            extra_params={"seed": 1234}
        )

        self.assertEqual(payload["model"], "openai/gpt-image-2.5-flare/edit")
        self.assertEqual(payload["prompt"], "Turn into cyberpunk city")
        self.assertEqual(payload["size"], "2048x1152")
        self.assertEqual(payload["quality"], "high")
        self.assertEqual(payload["background"], "transparent")
        self.assertEqual(payload["output_format"], "png")
        self.assertEqual(payload["n"], 2)
        self.assertEqual(payload["seed"], 1234)
        # Verify forbidden fields for GPT-image are omitted
        self.assertNotIn("aspect_ratio", payload)
        self.assertNotIn("strength", payload)

    def test_atlas_payload_general_model_mapping(self):
        """Verify general models (Nano Banana, Flux, Wan) receive aspect_ratio, strength and omit quality."""
        cfg = image_gen_engine.P2PProviderCfg(
            name="Atlas Cloud",
            api_url="https://api.atlascloud.ai/api/v1/model/generateImage",
            api_key="k",
            model="google/nano-banana-2-lite/reference-to-image"
        )
        payload = image_gen_engine.build_atlas_payload(
            cfg=cfg,
            images=[],
            prompt="A cute cat",
            negative_prompt="ugly",
            aspect_ratio="16:9",
            strength=0.65,
            quality="high",
            background="transparent"
        )

        self.assertEqual(payload["model"], "google/nano-banana-2-lite/reference-to-image")
        self.assertEqual(payload["aspect_ratio"], "16:9")
        self.assertEqual(payload["negative_prompt"], "ugly")
        self.assertEqual(payload["strength"], 0.65)
        # Verify GPT-specific fields are omitted for general models
        self.assertNotIn("quality", payload)
        self.assertNotIn("background", payload)

    def test_atlas_payload_auto_and_none_sanitization(self):
        """Verify 'auto' and None values are safely cleaned out."""
        cfg = image_gen_engine.P2PProviderCfg(
            name="Atlas Cloud",
            api_url="https://api.atlascloud.ai/api/v1/model/generateImage",
            api_key="k",
            model="openai/gpt-image-2.5-flare/edit"
        )
        payload = image_gen_engine.build_atlas_payload(
            cfg=cfg,
            images=[],
            prompt="Test clean",
            size="auto",
            quality="auto",
            background="auto",
            output_format="auto"
        )
        self.assertEqual(payload["size"], "1024x1024")  # Default mapped from 1:1
        self.assertNotIn("quality", payload)
        self.assertNotIn("background", payload)
        self.assertNotIn("output_format", payload)


class TestV202AppAdvancedUI(unittest.TestCase):
    """Test v2.02 App UI advanced parameters controls and toggle mechanism."""

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

    def test_p2p_advanced_controls_exist(self):
        """Verify advanced parameter variables and toggle button are created."""
        self.app._switch_to_tab("  圖生圖任務  ")
        self.assertTrue(hasattr(self.app, "_p2p_adv_toggle_btn"))
        self.assertTrue(hasattr(self.app, "_p2p_adv_frame"))
        self.assertTrue(hasattr(self.app, "_p2p_size_var"))
        self.assertTrue(hasattr(self.app, "_p2p_quality_var"))
        self.assertTrue(hasattr(self.app, "_p2p_bg_var"))
        self.assertTrue(hasattr(self.app, "_p2p_format_var"))
        self.assertTrue(hasattr(self.app, "_p2p_count_var"))
        self.assertTrue(hasattr(self.app, "_p2p_extra_json_entry"))

    def test_p2p_advanced_toggle(self):
        """Verify toggle expands and collapses advanced parameter frame."""
        self.app._switch_to_tab("  圖生圖任務  ")
        self.assertFalse(self.app._p2p_adv_open)

        # Expand
        self.app._toggle_p2p_advanced_params()
        self.assertTrue(self.app._p2p_adv_open)
        self.assertIn("收合", self.app._p2p_adv_toggle_btn.cget("text"))

        # Collapse
        self.app._toggle_p2p_advanced_params()
        self.assertFalse(self.app._p2p_adv_open)
        self.assertIn("展開", self.app._p2p_adv_toggle_btn.cget("text"))


if __name__ == "__main__":
    unittest.main()

