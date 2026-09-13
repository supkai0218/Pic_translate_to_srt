"""Unit tests for v1.07 features: multi-model providers, prompt profiles, resolution logic, and migration."""
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
from llm_engine import ProviderCfg


class TestV107Features(unittest.TestCase):
    def test_provider_presets(self):
        """Verify default provider presets exist and have required keys."""
        self.assertTrue(len(config.PROVIDER_PRESETS) > 0)
        for p in config.PROVIDER_PRESETS:
            self.assertIn("name", p)
            self.assertIn("api_url", p)
            self.assertIn("models", p)
            self.assertIn("selected_model", p)
            self.assertIn(p["selected_model"], p["models"])

    def test_migrate_v107_legacy_provider(self):
        """Verify legacy single-model provider is converted to models list."""
        legacy_settings = {
            "providers": [
                {
                    "name": "LegacyOpenAI",
                    "api_url": "https://api.openai.com/v1/chat/completions",
                    "model": "gpt-4o",
                    "is_default": True,
                }
            ],
            "default_provider": "LegacyOpenAI",
        }
        changed = config._migrate_v107(legacy_settings)
        self.assertTrue(changed)
        p = legacy_settings["providers"][0]
        self.assertEqual(p["models"], ["gpt-4o"])
        self.assertEqual(p["selected_model"], "gpt-4o")

    def test_migrate_v107_prompt_profiles(self):
        """Verify legacy prompts are migrated into prompt_profiles list."""
        legacy_settings = {
            "system_prompt": "Custom Sys Prompt",
            "user_prompt": "Custom Usr Prompt",
            "target_lang": "日本語",
            "target_lang_custom": "",
        }
        changed = config._migrate_v107(legacy_settings)
        self.assertTrue(changed)
        self.assertIn("prompt_profiles", legacy_settings)
        profiles = legacy_settings["prompt_profiles"]
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["system_prompt"], "Custom Sys Prompt")
        self.assertEqual(profiles[0]["user_prompt"], "Custom Usr Prompt")
        self.assertEqual(profiles[0]["target_lang"], "日本語")
        self.assertEqual(legacy_settings["active_global_prompt_id"], profiles[0]["id"])

    def test_provider_cfg_from_dict(self):
        """Verify ProviderCfg handles selected_model as well as legacy model."""
        d_new = {
            "name": "TestP",
            "api_url": "https://example.com/api",
            "api_key": "sk-123",
            "models": ["m1", "m2"],
            "selected_model": "m2",
        }
        cfg_new = ProviderCfg.from_dict(d_new)
        self.assertEqual(cfg_new.model, "m2")

        d_old = {
            "name": "TestP",
            "api_url": "https://example.com/api",
            "api_key": "sk-123",
            "model": "m_old",
        }
        cfg_old = ProviderCfg.from_dict(d_old)
        self.assertEqual(cfg_old.model, "m_old")

    def test_prompt_resolution_logic(self):
        """Verify model-bound prompt priority and global fallback."""
        mock_settings = {
            "active_global_prompt_id": "p_global_1",
            "prompt_profiles": [
                {
                    "id": "p_global_1",
                    "name": "標準公版",
                    "scope": "global",
                    "bound_provider": "",
                    "bound_model": "",
                    "system_prompt": "Global Sys",
                    "user_prompt": "Global Usr",
                },
                {
                    "id": "p_bound_1",
                    "name": "Qwen專屬",
                    "scope": "model_bound",
                    "bound_provider": "Ollama",
                    "bound_model": "qwen2.5-vl:7b",
                    "system_prompt": "Bound Sys",
                    "user_prompt": "Bound Usr",
                },
            ],
        }

        # Case 1: Matching model-bound prompt
        resolved_prof, desc = config.resolve_prompt("Ollama", "qwen2.5-vl:7b", mock_settings)
        self.assertEqual(resolved_prof["id"], "p_bound_1")
        self.assertIn("[專用]", desc)
        self.assertEqual(resolved_prof["system_prompt"], "Bound Sys")

        # Case 2: No model-bound prompt, fallback to active global
        resolved_prof, desc = config.resolve_prompt("OpenRouter", "gemini-flash", mock_settings)
        self.assertEqual(resolved_prof["id"], "p_global_1")
        self.assertIn("[公版]", desc)
        self.assertEqual(resolved_prof["system_prompt"], "Global Sys")

        # Case 3: Ollama with different model, fallback to active global
        resolved_prof, desc = config.resolve_prompt("Ollama", "llama-vision", mock_settings)
        self.assertEqual(resolved_prof["id"], "p_global_1")
        self.assertIn("[公版]", desc)

    def test_prompt_profile_crud_in_memory(self):
        """Verify upsert and delete for prompt profiles."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test_settings.json"
            with patch.object(config, "SETTINGS_FILE", test_file):
                # Initial state
                prof1 = {
                    "id": "p1",
                    "name": "Test Global 1",
                    "scope": "global",
                    "system_prompt": "sys1",
                    "user_prompt": "usr1",
                }
                config.upsert_prompt_profile(prof1)
                profiles = config.get_prompt_profiles()
                self.assertTrue(any(p["id"] == "p1" for p in profiles))

                # Update state
                prof1["name"] = "Test Global Updated"
                config.upsert_prompt_profile(prof1)
                p1_loaded = next(p for p in config.get_prompt_profiles() if p["id"] == "p1")
                self.assertEqual(p1_loaded["name"], "Test Global Updated")

                # Set active
                config.set_active_global_prompt("p1")
                active = config.get_active_global_prompt()
                self.assertEqual(active["id"], "p1")

                # Delete
                config.delete_prompt_profile("p1")
                profiles_after = config.get_prompt_profiles()
                self.assertFalse(any(p["id"] == "p1" for p in profiles_after))

    def test_provider_model_operations(self):
        """Verify adding, selecting, and deleting models from a provider."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test_settings.json"
            with patch.object(config, "SETTINGS_FILE", test_file):
                p = {
                    "name": "TestProv",
                    "api_url": "https://api.test.com",
                    "models": ["model-a"],
                    "selected_model": "model-a",
                }
                config.upsert_provider(p)

                # Add model
                config.add_model_to_provider("TestProv", "model-b")
                prov = next(x for x in config.get_providers() if x["name"] == "TestProv")
                self.assertIn("model-b", prov["models"])
                self.assertEqual(prov["selected_model"], "model-b")

                # Select model
                config.select_provider_model("TestProv", "model-a")
                prov = next(x for x in config.get_providers() if x["name"] == "TestProv")
                self.assertEqual(prov["selected_model"], "model-a")

                # Delete model
                config.delete_model_from_provider("TestProv", "model-a")
                prov = next(x for x in config.get_providers() if x["name"] == "TestProv")
                self.assertNotIn("model-a", prov["models"])
                self.assertEqual(prov["selected_model"], "model-b")

    def test_open_env_in_editor(self):
        """Verify open_env_in_editor executes notepad without crashing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_env = Path(tmpdir) / ".env"
            with patch.object(config, "DOTENV_PATH", test_env):
                with patch("subprocess.Popen") as mock_popen:
                    config.open_env_in_editor()
                    self.assertTrue(test_env.exists())
                    mock_popen.assert_called_once_with(["notepad.exe", str(test_env)])

    def test_app_format_prompt_display_name(self):
        """Verify App._format_prompt_display_name formats labels accurately."""
        from app import App
        mock_self = MagicMock()
        mock_self._settings = {"active_global_prompt_id": "p1"}

        # Test global active
        disp1 = App._format_prompt_display_name(mock_self, {"id": "p1", "name": "通用版", "scope": "global"})
        self.assertEqual(disp1, "[公版] 通用版 (預設)")

        # Test global non-active
        disp2 = App._format_prompt_display_name(mock_self, {"id": "p2", "name": "漫畫版", "scope": "global"})
        self.assertEqual(disp2, "[公版] 漫畫版")

        # Test model-bound
        disp3 = App._format_prompt_display_name(mock_self, {
            "id": "p3", "name": "專屬", "scope": "model_bound",
            "bound_provider": "OpenRouter", "bound_model": "deepseek"
        })
        self.assertEqual(disp3, "[專用: OpenRouter/deepseek] 專屬")


if __name__ == "__main__":
    unittest.main()

