"""Settings persistence for the screenshot OCR→translate→SRT app.

A single JSON file (settings.json next to this module) holds everything:
multiple LLM API providers (for quick switching), prompts, target language,
insertion mode, and the last-used directories. Adapted from the load/save/
patch pattern in QwenASR's app.py but with a provider list added.
"""
from __future__ import annotations

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BASE_DIR / "settings.json"

DEFAULT_SYSTEM_PROMPT = (
    "你是一個專業的字幕 OCR 與翻譯助手。請辨識圖片畫面中所有可見的文字，"
    "將其翻譯為指定語言，保留原有的換行。只輸出翻譯結果，"
    "不要加任何說明、引號或註解。若圖片中沒有可辨識的文字，請回覆空字串。"
)

DEFAULT_USER_PROMPT = (
    "請辨識這張截圖中的所有文字，翻譯成{lang}。"
    "直接輸出翻譯內容，保留換行，不要任何說明。"
)

DEFAULT_TARGET_LANG = "繁體中文"

TARGET_LANG_PRESETS = ["繁體中文", "簡體中文", "English", "日本語", "한국어"]

_DEFAULTS: dict = {
    "providers": [
        {
            "name": "OpenAI",
            "api_url": "https://api.openai.com/v1/chat/completions",
            "api_key": "",
            "model": "gpt-4o",
            "is_default": True,
        }
    ],
    "default_provider": "OpenAI",
    "target_lang": DEFAULT_TARGET_LANG,
    "target_lang_custom": "",
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "user_prompt": DEFAULT_USER_PROMPT,
    "insertion_mode": "insert",  # "insert" | "append"
    "image_max_side": 1568,
    "last_image_dir": "",
    "last_srt_path": "",
    "appearance_mode": "dark",
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base so newly-added defaults appear for
    old settings files without clobbering user values."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings() -> dict:
    """Load settings, creating the file with defaults on first run and merging
    in any new default keys that an older file is missing."""
    if not SETTINGS_FILE.exists():
        save_settings(_DEFAULTS)
        return dict(_DEFAULTS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        data = {}
    return _deep_merge(_DEFAULTS, data)


def save_settings(settings: dict) -> None:
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)


def patch_setting(key: str, value) -> None:
    """Update a single top-level key and persist."""
    s = load_settings()
    s[key] = value
    save_settings(s)


# --- provider list helpers ------------------------------------------------

def get_providers() -> list[dict]:
    return load_settings().get("providers", [])


def get_default_provider() -> dict | None:
    s = load_settings()
    name = s.get("default_provider")
    for p in s.get("providers", []):
        if p.get("name") == name:
            return p
    # fall back to the first provider marked is_default, else first
    for p in s.get("providers", []):
        if p.get("is_default"):
            return p
    providers = s.get("providers", [])
    return providers[0] if providers else None


def upsert_provider(provider: dict) -> None:
    """Insert or update a provider keyed by name; persist."""
    s = load_settings()
    providers = s.setdefault("providers", [])
    name = provider.get("name", "").strip()
    if not name:
        return
    for i, p in enumerate(providers):
        if p.get("name") == name:
            providers[i] = provider
            break
    else:
        providers.append(provider)
    # keep at most one is_default True; if this one is default, clear others
    if provider.get("is_default"):
        for p in providers:
            if p.get("name") != name:
                p["is_default"] = False
        s["default_provider"] = name
    save_settings(s)


def delete_provider(name: str) -> None:
    s = load_settings()
    providers = [p for p in s.get("providers", []) if p.get("name") != name]
    s["providers"] = providers
    if s.get("default_provider") == name:
        s["default_provider"] = providers[0]["name"] if providers else ""
        if providers:
            providers[0]["is_default"] = True
    save_settings(s)
