"""Unit tests for v2.03 Universal Image Adapter, Auto-Sniffer, and Debug Display."""
import io
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

import config
import image_gen_engine
from image_gen_engine import (
    P2PProviderCfg,
    is_venice_edit_model,
    sniff_endpoint_info,
    test_p2p_connection,
    generate_image,
)

image_gen_engine._ensure_requests()


class TestV203VeniceModelDetection(unittest.TestCase):
    """Test Venice AI model classification between Inpaint/Edit and Text-to-Image."""

    def test_is_venice_edit_model_positive(self):
        self.assertTrue(is_venice_edit_model("qwen-edit-uncensored"))
        self.assertTrue(is_venice_edit_model("qwen-edit"))
        self.assertTrue(is_venice_edit_model("firered-image-edit"))
        self.assertTrue(is_venice_edit_model("wan-2-7-pro-edit"))
        self.assertTrue(is_venice_edit_model("gpt-image-2-5-flare-edit"))
        self.assertTrue(is_venice_edit_model("flux-2-max-edit"))
        self.assertTrue(is_venice_edit_model("seedream-v5-pro-edit"))
        self.assertTrue(is_venice_edit_model("bria-bg-remover"))

    def test_is_venice_edit_model_negative(self):
        self.assertFalse(is_venice_edit_model("qwen-image-2"))
        self.assertFalse(is_venice_edit_model("qwen-image-2-pro"))
        self.assertFalse(is_venice_edit_model("venice-sd35"))
        self.assertFalse(is_venice_edit_model("flux-dev-uncensored"))
        self.assertFalse(is_venice_edit_model("flux-2-max"))
        self.assertFalse(is_venice_edit_model("gpt-image-2-5-flare"))


class TestV203EndpointSniffer(unittest.TestCase):
    """Test endpoint URL resolution and protocol identification."""

    def test_venice_edit_single_image(self):
        cfg = P2PProviderCfg(
            name="Venice AI",
            api_url="https://api.venice.ai/api/v1",
            api_key="test_key",
            model="qwen-edit-uncensored",
        )
        info = sniff_endpoint_info(cfg, has_images=True, num_images=1)
        self.assertEqual(info["endpoint_url"], "https://api.venice.ai/api/v1/image/edit")
        self.assertEqual(info["method"], "POST")
        self.assertIn("二進位串流", info["mode_desc"])
        self.assertIn("safe_mode: false", info["notes"])

    def test_venice_edit_multi_image(self):
        cfg = P2PProviderCfg(
            name="Venice AI",
            api_url="https://api.venice.ai/api/v1",
            api_key="test_key",
            model="wan-2-7-pro-edit",
        )
        info = sniff_endpoint_info(cfg, has_images=True, num_images=3)
        self.assertEqual(info["endpoint_url"], "https://api.venice.ai/api/v1/image/multi-edit")
        self.assertIn("多圖層合成修圖", info["mode_desc"])

    def test_venice_generate_model(self):
        cfg = P2PProviderCfg(
            name="Venice AI",
            api_url="https://api.venice.ai/api/v1",
            api_key="test_key",
            model="qwen-image-2",
        )
        info = sniff_endpoint_info(cfg, has_images=False, num_images=0)
        self.assertEqual(info["endpoint_url"], "https://api.venice.ai/api/v1/image/generate")
        self.assertIn("文字生圖", info["mode_desc"])

    def test_venice_url_with_redundant_action_normalized(self):
        # Even if user entered /image/edit as base URL, it normalizes correctly
        cfg = P2PProviderCfg(
            name="Venice AI",
            api_url="https://api.venice.ai/api/v1/image/edit",
            api_key="test_key",
            model="qwen-image-2",  # generate model
        )
        info = sniff_endpoint_info(cfg, has_images=False, num_images=0)
        self.assertEqual(info["endpoint_url"], "https://api.venice.ai/api/v1/image/generate")

    def test_atlas_cloud_sniffer(self):
        cfg = P2PProviderCfg(
            name="Atlas Cloud",
            api_url="https://api.atlascloud.ai/api/v1/model/generateImage",
            api_key="test_key",
            model="openai/gpt-image-2.5-flare/edit",
        )
        info = sniff_endpoint_info(cfg)
        self.assertEqual(info["endpoint_url"], "https://api.atlascloud.ai/api/v1/model/generateImage")
        self.assertIn("非同步佇列", info["mode_desc"])

    def test_openrouter_image_model_sniffer(self):
        # 1. Standard base URL with image model
        cfg = P2PProviderCfg(
            name="OpenRouter",
            api_url="https://openrouter.ai/api/v1",
            api_key="test_key",
            model="openai/gpt-image-2.5-flare",
        )
        info = sniff_endpoint_info(cfg)
        self.assertEqual(info["endpoint_url"], "https://openrouter.ai/api/v1/images/generations")
        self.assertIn("OpenRouter 專屬生圖", info["mode_desc"])

        # 2. URL containing /images should normalize cleanly without double /v1 or 404
        cfg_images_url = P2PProviderCfg(
            name="OpenRouter",
            api_url="https://openrouter.ai/api/v1/images",
            api_key="test_key",
            model="openai/gpt-image-2.5-flare",
        )
        info2 = sniff_endpoint_info(cfg_images_url)
        self.assertEqual(info2["endpoint_url"], "https://openrouter.ai/api/v1/images/generations")

    def test_openrouter_chat_model_sniffer(self):
        # Multimodal chat model on OpenRouter
        cfg = P2PProviderCfg(
            name="OpenRouter",
            api_url="https://openrouter.ai/api/v1",
            api_key="test_key",
            model="anthropic/claude-3.5-sonnet",
        )
        info = sniff_endpoint_info(cfg)
        self.assertEqual(info["endpoint_url"], "https://openrouter.ai/api/v1/chat/completions")
        self.assertIn("多模態視覺對話", info["mode_desc"])


class TestV203ConnectionTestNormalization(unittest.TestCase):
    """Test URL normalization in test_p2p_connection."""

    @patch("image_gen_engine.requests.get")
    def test_openrouter_images_url_normalization(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        # User inputted /api/v1/images
        cfg = P2PProviderCfg(
            name="OpenRouter",
            api_url="https://openrouter.ai/api/v1/images",
            api_key="valid_openrouter_key",
            model="openai/gpt-image-2.5-flare",
        )
        ok, msg = test_p2p_connection(cfg)
        self.assertTrue(ok)
        self.assertIn("OpenRouter", msg)
        # Verify requested test URL was normalized to /v1/models
        mock_get.assert_called_with(
            "https://openrouter.ai/api/v1/models",
            headers={"Content-Type": "application/json", "Authorization": "Bearer valid_openrouter_key"},
            timeout=15,
        )

    @patch("image_gen_engine.requests.get")
    def test_venice_edit_url_does_not_404(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        # User inputted /image/edit
        cfg = P2PProviderCfg(
            name="Venice AI",
            api_url="https://api.venice.ai/api/v1/image/edit",
            api_key="valid_key",
            model="qwen-edit-uncensored",
        )
        ok, msg = test_p2p_connection(cfg)
        self.assertTrue(ok)
        self.assertIn("Venice AI", msg)
        # Verify requested test URL was normalized to /models rather than /image/edit/models
        mock_get.assert_called_with("https://api.venice.ai/api/v1/models", headers={"Content-Type": "application/json", "Authorization": "Bearer valid_key"}, timeout=15)


class TestV203VeniceExecutionAndDecoding(unittest.TestCase):
    """Test Venice AI payload construction and dual-mode decoding in generate_image."""

    @patch("image_gen_engine.requests.post")
    def test_venice_edit_payload_sanitization_and_binary_decoding(self, mock_post):
        # Create a real small PNG image bytes
        test_img = Image.new("RGB", (64, 64), color=(255, 120, 80))
        buf = io.BytesIO()
        test_img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "image/png"}
        mock_resp.content = png_bytes
        mock_resp.text = ""
        mock_post.return_value = mock_resp

        cfg = P2PProviderCfg(
            name="Venice AI",
            api_url="https://api.venice.ai/api/v1",
            api_key="test_venice_key",
            model="qwen-edit-uncensored",
        )
        ref_img = Image.new("RGB", (64, 64), color=(0, 0, 255))

        out_imgs = generate_image(
            cfg=cfg,
            images=[ref_img],
            prompt="Make background white",
            negative_prompt="blurry, ugly",  # Should be filtered out
            strength=0.65,                    # Should be filtered out
            aspect_ratio="16:9",
            output_format="png",
        )

        self.assertEqual(len(out_imgs), 1)
        self.assertEqual(out_imgs[0].size, (64, 64))

        # Check call arguments to requests.post
        call_args = mock_post.call_args
        target_url = call_args[0][0]
        payload = call_args[1]["json"]

        self.assertEqual(target_url, "https://api.venice.ai/api/v1/image/edit")
        self.assertEqual(payload["model"], "qwen-edit-uncensored")
        self.assertEqual(payload["prompt"], "Make background white")
        self.assertEqual(payload["safe_mode"], False)
        self.assertEqual(payload["aspect_ratio"], "16:9")
        self.assertEqual(payload["output_format"], "png")
        self.assertIn("image", payload)
        # Verify forbidden keys are NOT in payload
        self.assertNotIn("negative_prompt", payload)
        self.assertNotIn("strength", payload)
        self.assertNotIn("size", payload)
        self.assertNotIn("quality", payload)


class TestV203OpenRouterExecutionAndDecoding(unittest.TestCase):
    """Test OpenRouter Image Generations payload construction and base64 decoding."""

    @patch("image_gen_engine.requests.post")
    def test_openrouter_image_to_image_execution(self, mock_post):
        test_img = Image.new("RGB", (64, 64), color=(100, 200, 50))
        buf = io.BytesIO()
        test_img.save(buf, format="PNG")
        import base64
        png_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {
            "created": 12345678,
            "data": [{"b64_json": png_b64}]
        }
        mock_post.return_value = mock_resp

        cfg = P2PProviderCfg(
            name="OpenRouter",
            api_url="https://openrouter.ai/api/v1/images",
            api_key="test_key",
            model="openai/gpt-image-2.5-flare",
        )
        ref_img = Image.new("RGB", (64, 64), color=(0, 0, 255))

        out_imgs = generate_image(
            cfg=cfg,
            images=[ref_img],
            prompt="Turn blue into green",
            aspect_ratio="16:9",
        )

        self.assertEqual(len(out_imgs), 1)
        self.assertEqual(out_imgs[0].size, (64, 64))

        call_args = mock_post.call_args
        target_url = call_args[0][0]
        payload = call_args[1]["json"]

        self.assertEqual(target_url, "https://openrouter.ai/api/v1/images/generations")
        self.assertEqual(payload["model"], "openai/gpt-image-2.5-flare")
        self.assertEqual(payload["prompt"], "Turn blue into green")
        self.assertEqual(payload["aspect_ratio"], "16:9")
        self.assertIn("image", payload)
        self.assertEqual(payload["response_format"], "b64_json")


class TestV203UIDebugDisplay(unittest.TestCase):
    """Test dynamic UI debug display updating."""

    def test_app_debug_display_update(self):
        import app
        root = app.App()
        root.withdraw()
        try:
            # Ensure P2P API tab is constructed
            root._build_p2p_api_tab(root.p2p_tabs.tab("  圖生圖 API 設定  "))

            # Set Venice URL and edit model
            root._p2p_url_entry.delete(0, "end")
            root._p2p_url_entry.insert(0, "https://api.venice.ai/api/v1")
            root._p2p_selected_model_var.set("qwen-edit-uncensored")
            root._update_p2p_endpoint_debug_display()

            debug_text = root._p2p_endpoint_debug_var.get()
            self.assertIn("https://api.venice.ai/api/v1/image/edit", debug_text)
            self.assertIn("以圖修圖", debug_text)

            # Switch to generate model
            root._p2p_selected_model_var.set("qwen-image-2")
            root._update_p2p_endpoint_debug_display()
            debug_text_gen = root._p2p_endpoint_debug_var.get()
            self.assertIn("https://api.venice.ai/api/v1/image/generate", debug_text_gen)
            self.assertIn("文字生圖", debug_text_gen)

            # Type a new model in entry
            root._p2p_new_model_var.set("wan-2-7-pro-edit")
            debug_text_new = root._p2p_endpoint_debug_var.get()
            self.assertIn("https://api.venice.ai/api/v1/image/edit", debug_text_new)

        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
