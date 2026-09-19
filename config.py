"""Settings persistence for the screenshot OCR→translate→SRT app.

A single JSON file (settings.json next to this module) holds everything:
multiple LLM API providers (for quick switching), prompts, target language,
insertion mode, and the last-used directories. Adapted from the load/save/
patch pattern in QwenASR's app.py but with a provider list added.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv, set_key

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

SETTINGS_FILE = BASE_DIR / "settings.json"
SETTINGS_P2P_FILE = BASE_DIR / "settings_p2p.json"
CAPTURED_DIR = BASE_DIR / "captured"
GENERATED_DIR = BASE_DIR / "generated"
DOTENV_PATH = BASE_DIR / ".env"

# populate os.environ from .env (no-op if the file is absent)
load_dotenv(DOTENV_PATH)

API_KEY_PREFIX = "API_KEY_"


def _env_var_name_for(name: str) -> str:
    """Provider name → env-var name, e.g. 'Openrouter' → 'API_KEY_OPENROUTER'."""
    sanitized = re.sub(r"[^A-Z0-9]", "_", (name or "").upper()).strip("_")
    return f"{API_KEY_PREFIX}{sanitized}" if sanitized else f"{API_KEY_PREFIX}PROVIDER"


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

DEFAULT_STEP_SETTINGS: dict = {
    "step_back_large_sec": 5.0,
    "step_back_small_sec": 3.0,
    "step_back_frames": 1,
    "step_forward_frames": 1,
    "step_forward_small_sec": 3.0,
    "step_forward_large_sec": 5.0,
}

DEFAULT_KEYBINDINGS: dict = {
    "play_pause": "space",
    "step_back_large": "Left",
    "step_back_small": "j",
    "step_back_frame": "bracketleft",
    "step_forward_frame": "bracketright",
    "step_forward_small": "l",
    "step_forward_large": "Right",
    "set_start": "q",
    "set_end": "w",
    "jump_start": "a",
    "jump_end": "s",
    "reset_roi": "r",
    "apply_last_roi": "t",
    "add_task": "Return",
    "toggle_fullscreen": "F11",
    "hot_reload_sub": "F5",
    "quick_capture": "c",
}

# 內建常用供應商範本 (Presets)
PROVIDER_PRESETS: list[dict] = [
    {
        "name": "OpenAI",
        "api_url": "https://api.openai.com/v1/chat/completions",
        "models": ["gpt-4o", "gpt-4o-mini"],
        "selected_model": "gpt-4o",
    },
    {
        "name": "OpenRouter",
        "api_url": "https://openrouter.ai/api/v1/chat/completions",
        "models": ["google/gemini-2.0-flash-001", "deepseek/deepseek-chat"],
        "selected_model": "google/gemini-2.0-flash-001",
    },
    {
        "name": "DeepSeek",
        "api_url": "https://api.deepseek.com/chat/completions",
        "models": ["deepseek-chat"],
        "selected_model": "deepseek-chat",
    },
    {
        "name": "SiliconFlow (矽基流動)",
        "api_url": "https://api.siliconflow.cn/v1/chat/completions",
        "models": ["Qwen/Qwen2.5-VL-72B-Instruct"],
        "selected_model": "Qwen/Qwen2.5-VL-72B-Instruct",
    },
    {
        "name": "Groq",
        "api_url": "https://api.groq.com/openai/v1/chat/completions",
        "models": ["llama-3.2-11b-vision-preview"],
        "selected_model": "llama-3.2-11b-vision-preview",
    },
    {
        "name": "本地 Ollama",
        "api_url": "http://localhost:11434/v1/chat/completions",
        "models": ["qwen2.5-vl:7b"],
        "selected_model": "qwen2.5-vl:7b",
    },
    {
        "name": "本地 LM Studio",
        "api_url": "http://localhost:1234/v1/chat/completions",
        "models": ["local-model"],
        "selected_model": "local-model",
    },
]

DEFAULT_PROMPT_PROFILES: list[dict] = [
    {
        "id": "global_default",
        "name": "預設通用公版",
        "scope": "global",            # "global" | "model_bound"
        "bound_provider": "",
        "bound_model": "",
        "target_lang": DEFAULT_TARGET_LANG,
        "target_lang_custom": "",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": DEFAULT_USER_PROMPT,
        "is_builtin": True,
    }
]

_DEFAULTS: dict = {
    "providers": [
        {
            "name": "OpenAI",
            "api_url": "https://api.openai.com/v1/chat/completions",
            "api_key": "",
            "api_key_env": "API_KEY_OPENAI",
            "models": ["gpt-4o", "gpt-4o-mini"],
            "selected_model": "gpt-4o",
            "is_default": True,
        }
    ],
    "default_provider": "OpenAI",
    "prompt_profiles": DEFAULT_PROMPT_PROFILES,
    "active_global_prompt_id": "global_default",
    "target_lang": DEFAULT_TARGET_LANG,
    "target_lang_custom": "",
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "user_prompt": DEFAULT_USER_PROMPT,
    "insertion_mode": "insert",  # "insert" | "append"
    "duration_mode": "auto",     # "auto" | "unified" | "per_subtitle"
    "subtitle_duration": 2.0,    # 統一秒數模式的預設值（秒）
    "image_max_side": 1568,
    "last_image_dir": "",
    "last_video_dir": "",
    "last_video_path": "",
    "last_srt_path": "",
    "step_settings": DEFAULT_STEP_SETTINGS,
    "keybindings": DEFAULT_KEYBINDINGS,
    "appearance_mode": "dark",
    "sub_render_enabled": True,
    "last_volume": 80,
    "window_geometry": None,
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


def _resolve_keys(settings: dict) -> None:
    """In-memory only: fill each provider's api_key from os.environ using its
    api_key_env. Never persisted (save_settings strips api_key)."""
    for p in settings.get("providers", []):
        env = p.get("api_key_env") or _env_var_name_for(p.get("name", ""))
        p["api_key"] = os.environ.get(env, "")


def _migrate_secrets(settings: dict) -> bool:
    """Move any plaintext api_key found in settings into .env, leaving
    settings.json clean. Returns True if anything was migrated. Idempotent."""
    changed = False
    for p in settings.get("providers", []):
        env = p.get("api_key_env") or _env_var_name_for(p.get("name", ""))
        p.setdefault("api_key_env", env)
        if env != p.get("api_key_env"):
            p["api_key_env"] = env
            changed = True
        key = (p.get("api_key") or "").strip()
        if key:  # plaintext secret present → move it to .env
            set_key(str(DOTENV_PATH), env, key)
            os.environ[env] = key
            p["api_key"] = ""
            changed = True
    return changed


def _migrate_v107(settings: dict) -> bool:
    """Migrate older settings format (single model string, missing prompt_profiles) to v1.07.
    Returns True if changes were made and need saving."""
    changed = False

    # 1. Migrate providers: model -> models + selected_model
    for p in settings.get("providers", []):
        old_model = p.get("model")
        if "models" not in p or not isinstance(p["models"], list) or not p["models"]:
            p["models"] = [old_model] if old_model else ["gpt-4o"]
            changed = True
        if "selected_model" not in p or not p["selected_model"]:
            p["selected_model"] = old_model if (old_model and old_model in p["models"]) else p["models"][0]
            changed = True

    # 2. Migrate prompt_profiles
    if "prompt_profiles" not in settings or not settings["prompt_profiles"]:
        sys_p = settings.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
        usr_p = settings.get("user_prompt") or DEFAULT_USER_PROMPT
        lang = settings.get("target_lang") or DEFAULT_TARGET_LANG
        lang_cust = settings.get("target_lang_custom") or ""
        default_prof = {
            "id": "global_default",
            "name": "預設通用公版",
            "scope": "global",
            "bound_provider": "",
            "bound_model": "",
            "target_lang": lang,
            "target_lang_custom": lang_cust,
            "system_prompt": sys_p,
            "user_prompt": usr_p,
            "is_builtin": True,
        }
        settings["prompt_profiles"] = [default_prof]
        settings["active_global_prompt_id"] = "global_default"
        changed = True
    elif "active_global_prompt_id" not in settings or not settings["active_global_prompt_id"]:
        for p in settings["prompt_profiles"]:
            if p.get("scope") == "global":
                settings["active_global_prompt_id"] = p["id"]
                changed = True
                break
        else:
            settings["active_global_prompt_id"] = settings["prompt_profiles"][0]["id"]
            changed = True

    return changed


def reload_env() -> None:
    """Reload environment variables from .env with override=True."""
    if DOTENV_PATH.exists():
        load_dotenv(DOTENV_PATH, override=True)


def open_env_in_editor() -> None:
    """Open .env in Windows Notepad (creates the file if missing)."""
    if not DOTENV_PATH.exists():
        DOTENV_PATH.write_text("# API Keys\n", encoding="utf-8")
    try:
        import subprocess
        subprocess.Popen(["notepad.exe", str(DOTENV_PATH)])
    except Exception:
        try:
            os.startfile(str(DOTENV_PATH))
        except Exception:
            pass


def load_settings() -> dict:
    """Load settings, creating the file with defaults on first run and merging
    in any new default keys that an older file is missing. Migrates any
    plaintext api_key into .env and resolves keys in-memory."""
    if not SETTINGS_FILE.exists():
        save_settings(_DEFAULTS)
        data = dict(_DEFAULTS)
    else:
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}
    settings = _deep_merge(_DEFAULTS, data)
    migrated_sec = _migrate_secrets(settings)
    migrated_v107 = _migrate_v107(settings)
    if migrated_sec or migrated_v107:
        save_settings(settings)  # persist the now-cleaned and upgraded file
    _resolve_keys(settings)
    return settings


def save_settings(settings: dict) -> None:
    """Persist settings, always stripping api_key to '' (secrets live in .env)."""
    clean = copy.deepcopy(settings)
    for p in clean.get("providers", []):
        p["api_key"] = ""
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2, ensure_ascii=False)


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
    """Insert or update a provider keyed by name; persist. Any api_key is
    written to .env (never to settings.json)."""
    s = load_settings()
    providers = s.setdefault("providers", [])
    name = provider.get("name", "").strip()
    if not name:
        return
    env = provider.get("api_key_env") or _env_var_name_for(name)
    provider["api_key_env"] = env
    key = (provider.get("api_key") or "").strip()
    if key:  # a real key was supplied → store in .env only
        set_key(str(DOTENV_PATH), env, key)
        os.environ[env] = key
    provider["api_key"] = ""  # never persist the secret in settings.json

    # ensure models and selected_model
    models = provider.get("models")
    if not models:
        m = provider.get("model", "").strip()
        provider["models"] = [m] if m else ["gpt-4o"]
    if not provider.get("selected_model"):
        provider["selected_model"] = provider["models"][0]

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


def add_model_to_provider(provider_name: str, model_name: str) -> None:
    s = load_settings()
    for p in s.get("providers", []):
        if p.get("name") == provider_name:
            models = p.setdefault("models", [])
            if model_name not in models:
                models.append(model_name)
            p["selected_model"] = model_name
            break
    save_settings(s)


def delete_model_from_provider(provider_name: str, model_name: str) -> None:
    s = load_settings()
    for p in s.get("providers", []):
        if p.get("name") == provider_name:
            models = [m for m in p.get("models", []) if m != model_name]
            p["models"] = models
            if p.get("selected_model") == model_name:
                p["selected_model"] = models[0] if models else ""
            break
    save_settings(s)


def select_provider_model(provider_name: str, model_name: str) -> None:
    s = load_settings()
    for p in s.get("providers", []):
        if p.get("name") == provider_name:
            p["selected_model"] = model_name
            break
    save_settings(s)


# --- prompt profile helpers -----------------------------------------------

def get_prompt_profiles() -> list[dict]:
    return load_settings().get("prompt_profiles", [])


def get_active_global_prompt() -> dict:
    s = load_settings()
    active_id = s.get("active_global_prompt_id", "global_default")
    profiles = s.get("prompt_profiles", [])
    for p in profiles:
        if p.get("scope") == "global" and p.get("id") == active_id:
            return p
    for p in profiles:
        if p.get("scope") == "global":
            return p
    return DEFAULT_PROMPT_PROFILES[0]


def set_active_global_prompt(prompt_id: str) -> None:
    s = load_settings()
    s["active_global_prompt_id"] = prompt_id
    save_settings(s)


def upsert_prompt_profile(profile: dict) -> None:
    s = load_settings()
    profiles = s.setdefault("prompt_profiles", [])
    pid = profile.get("id", "").strip()
    if not pid:
        pid = f"prompt_{uuid.uuid4().hex[:8]}"
        profile["id"] = pid
    for i, p in enumerate(profiles):
        if p.get("id") == pid:
            profiles[i] = profile
            break
    else:
        profiles.append(profile)
    save_settings(s)


def delete_prompt_profile(profile_id: str) -> None:
    s = load_settings()
    profiles = [p for p in s.get("prompt_profiles", []) if p.get("id") != profile_id]
    s["prompt_profiles"] = profiles
    if s.get("active_global_prompt_id") == profile_id:
        globals_left = [p for p in profiles if p.get("scope") == "global"]
        s["active_global_prompt_id"] = globals_left[0]["id"] if globals_left else ""
    save_settings(s)


def resolve_prompt(provider_name: str, model_name: str, settings: dict | None = None) -> tuple[dict, str]:
    """Resolve prompt based on provider and model.
    Returns: (prompt_profile_dict, description_text)
    1. Priority: Model-bound prompt matching (provider_name, model_name).
    2. Fallback: Active global prompt.
    3. Final fallback: DEFAULT_PROMPT_PROFILES[0].
    """
    if settings is None:
        settings = load_settings()
    profiles = settings.get("prompt_profiles", [])

    # 1. Check model-bound prompt
    for p in profiles:
        if p.get("scope") == "model_bound":
            if p.get("bound_provider") == provider_name and p.get("bound_model") == model_name:
                return p, f"[專用] {p.get('name', '未命名')}"

    # 2. Check active global prompt
    active_id = settings.get("active_global_prompt_id")
    for p in profiles:
        if p.get("scope") == "global" and p.get("id") == active_id:
            return p, f"[公版] {p.get('name', '未命名')}"

    # 3. Fallback to any global
    for p in profiles:
        if p.get("scope") == "global":
            return p, f"[公版] {p.get('name', '未命名')}"

    return DEFAULT_PROMPT_PROFILES[0], "[公版] 預設通用公版"


# ====================================================================== #
#  v2.01 Pic-to-Pic (P2P) Image Generation Settings Management
# ====================================================================== #

DEFAULT_P2P_PROVIDERS: list[dict] = [
    {
        "name": "OpenRouter",
        "api_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "api_key": "",
        "models": [
            "openai/gpt-image-2.5-flare",
            "black-forest-labs/flux-1-dev",
            "black-forest-labs/flux-1-schnell",
            "recraft-ai/recraft-v20",
            "stabilityai/stable-diffusion-3.5-large",
        ],
        "selected_model": "openai/gpt-image-2.5-flare",
        "provider_type": "openrouter",
    },
    {
        "name": "Venice AI",
        "api_url": "https://api.venice.ai/api/v1",
        "api_key_env": "VENICE_API_KEY",
        "api_key": "",
        "models": [
            "qwen-edit-uncensored",
            "qwen-edit",
            "wan-2-7-pro-edit",
            "gpt-image-2-5-flare-edit",
            "flux-2-max-edit",
            "qwen-image-2",
            "venice-sd35",
            "flux-dev-uncensored",
        ],
        "selected_model": "qwen-edit-uncensored",
        "provider_type": "venice",
    },
    {
        "name": "Atlas Cloud",
        "api_url": "https://api.atlascloud.ai/v1",
        "api_key_env": "ATLAS_API_KEY",
        "api_key": "",
        "models": [
            "sdxl-turbo",
            "flux-realism",
        ],
        "selected_model": "sdxl-turbo",
        "provider_type": "atlas",
    },
]

DEFAULT_DRAWING_PROMPTS: list[dict] = [
    {
        "id": "draw_anime",
        "name": "動漫二次元風格",
        "positive": "masterpiece, best quality, anime style, highly detailed illustration of {subject}, vibrant colors, dynamic lighting",
        "negative": "lowres, bad anatomy, bad hands, text, error, missing fingers, extra digit, fewer digits, cropped, worst quality, low quality",
    },
    {
        "id": "draw_photo",
        "name": "寫實攝影風格",
        "positive": "photorealistic, 8k resolution, RAW photo, cinematic lighting, shot on 35mm lens, hyper-detailed texture of {subject}",
        "negative": "cartoon, 3d render, anime, drawing, painting, bad lighting, blurry, distorted",
    },
    {
        "id": "draw_cyberpunk",
        "name": "賽博龐克風格",
        "positive": "cyberpunk style, neon lights, night city, futuristic, highly detailed {subject}, cinematic atmosphere, 8k wallpaper",
        "negative": "rustic, historical, daytime, washed out, low quality, cartoonish",
    },
    {
        "id": "draw_sketch",
        "name": "概念插畫與線稿",
        "positive": "concept art, detailed sketch of {subject}, digital painting, trending on artstation, aesthetic composition",
        "negative": "ugly, blurry, lowres, watermark, oversaturated",
    },
]

DEFAULT_P2P_SETTINGS: dict = {
    "selected_provider": "OpenRouter",
    "providers": DEFAULT_P2P_PROVIDERS,
    "drawing_prompts": DEFAULT_DRAWING_PROMPTS,
    "active_prompt_id": "draw_anime",
    "default_params": {
        "image_strength": 0.65,
        "aspect_ratio": "1:1",
        "samples": 1,
    },
}


def _migrate_p2p_secrets(settings: dict) -> bool:
    """Migrate plaintext secrets from settings_p2p into .env."""
    changed = False
    for p in settings.get("providers", []):
        env = p.get("api_key_env") or _env_var_name_for(p.get("name", ""))
        p.setdefault("api_key_env", env)
        if env != p.get("api_key_env"):
            p["api_key_env"] = env
            changed = True
        key = (p.get("api_key") or "").strip()
        if key:
            set_key(str(DOTENV_PATH), env, key)
            os.environ[env] = key
            p["api_key"] = ""
            changed = True
    return changed


def resolve_p2p_api_key(provider: dict) -> str:
    """Resolve API key for a P2P provider from provider dict, .env, or os.environ."""
    key = (provider.get("api_key") or "").strip()
    if key:
        return key
    env = provider.get("api_key_env") or _env_var_name_for(provider.get("name", ""))
    val = os.environ.get(env, "").strip()
    if val:
        return val
    alt_env = f"API_KEY_{re.sub(r'[^A-Z0-9]', '_', provider.get('name', '').upper()).strip('_')}"
    val = os.environ.get(alt_env, "").strip()
    if val:
        return val
    name_l = provider.get("name", "").lower()
    if "atlas" in name_l:
        return (
            os.environ.get("API_KEY_ATLAS", "").strip()
            or os.environ.get("API_KEY_ATLAS_CLOUD", "").strip()
            or os.environ.get("ATLAS_API_KEY", "").strip()
        )
    return ""


def load_p2p_settings() -> dict:
    """Load settings_p2p.json, merging defaults and populating API keys."""
    if not SETTINGS_P2P_FILE.exists():
        save_p2p_settings(DEFAULT_P2P_SETTINGS)
        data = dict(DEFAULT_P2P_SETTINGS)
    else:
        try:
            with open(SETTINGS_P2P_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}

    settings = _deep_merge(DEFAULT_P2P_SETTINGS, data)
    migrated = _migrate_p2p_secrets(settings)
    if migrated:
        save_p2p_settings(settings)

    # In-memory resolve API keys
    for p in settings.get("providers", []):
        p["api_key"] = resolve_p2p_api_key(p)

    return settings


def save_p2p_settings(settings: dict) -> None:
    """Persist settings_p2p.json, stripping api_key to keep plaintext out of disk."""
    clean = copy.deepcopy(settings)
    for p in clean.get("providers", []):
        p["api_key"] = ""
    with open(SETTINGS_P2P_FILE, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2, ensure_ascii=False)


def upsert_drawing_prompt(prompt: dict) -> None:
    s = load_p2p_settings()
    prompts = s.setdefault("drawing_prompts", [])
    pid = prompt.get("id", "").strip()
    if not pid:
        pid = f"draw_{uuid.uuid4().hex[:8]}"
        prompt["id"] = pid
    for i, p in enumerate(prompts):
        if p.get("id") == pid:
            prompts[i] = prompt
            break
    else:
        prompts.append(prompt)
    save_p2p_settings(s)


def delete_drawing_prompt(prompt_id: str) -> None:
    s = load_p2p_settings()
    prompts = [p for p in s.get("drawing_prompts", []) if p.get("id") != prompt_id]
    s["drawing_prompts"] = prompts
    if s.get("active_prompt_id") == prompt_id:
        s["active_prompt_id"] = prompts[0]["id"] if prompts else ""
    save_p2p_settings(s)


def upsert_p2p_provider(provider: dict) -> None:
    """Insert or update a P2P provider keyed by name; persist. Any api_key is
    written to .env (never to settings_p2p.json)."""
    s = load_p2p_settings()
    providers = s.setdefault("providers", [])
    name = provider.get("name", "").strip()
    if not name:
        return
    env = provider.get("api_key_env") or _env_var_name_for(name)
    provider["api_key_env"] = env
    key = (provider.get("api_key") or "").strip()
    if key:  # a real key was supplied → store in .env only
        set_key(str(DOTENV_PATH), env, key)
        os.environ[env] = key
    provider["api_key"] = ""  # never persist the secret in settings_p2p.json

    # ensure models and selected_model
    models = provider.get("models")
    if not models:
        m = provider.get("model", "").strip()
        provider["models"] = [m] if m else ["black-forest-labs/flux-1-dev"]
    if not provider.get("selected_model"):
        provider["selected_model"] = provider["models"][0]

    for i, p in enumerate(providers):
        if p.get("name") == name:
            providers[i] = provider
            break
    else:
        providers.append(provider)

    if provider.get("is_default") or s.get("selected_provider") == name:
        s["selected_provider"] = name
    elif not s.get("selected_provider"):
        s["selected_provider"] = name

    save_p2p_settings(s)


def delete_p2p_provider(name: str) -> None:
    """Delete a P2P provider by name and persist."""
    s = load_p2p_settings()
    providers = [p for p in s.get("providers", []) if p.get("name") != name]
    s["providers"] = providers
    if s.get("selected_provider") == name:
        s["selected_provider"] = providers[0]["name"] if providers else ""
    save_p2p_settings(s)


def add_model_to_p2p_provider(provider_name: str, model_name: str) -> None:
    s = load_p2p_settings()
    for p in s.get("providers", []):
        if p.get("name") == provider_name:
            models = p.setdefault("models", [])
            if model_name not in models:
                models.append(model_name)
            p["selected_model"] = model_name
            break
    save_p2p_settings(s)


def delete_model_from_p2p_provider(provider_name: str, model_name: str) -> None:
    s = load_p2p_settings()
    for p in s.get("providers", []):
        if p.get("name") == provider_name:
            models = [m for m in p.get("models", []) if m != model_name]
            p["models"] = models
            if p.get("selected_model") == model_name:
                p["selected_model"] = models[0] if models else ""
            break
    save_p2p_settings(s)


def select_p2p_provider_model(provider_name: str, model_name: str) -> None:
    s = load_p2p_settings()
    for p in s.get("providers", []):
        if p.get("name") == provider_name:
            p["selected_model"] = model_name
            break
    save_p2p_settings(s)


