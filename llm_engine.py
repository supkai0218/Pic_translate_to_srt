"""OpenAI-compatible vision LLM client.

One code path: POST to {api_url} with an OpenAI chat-completions payload,
base64 image inline as a data URL. Works for OpenAI, Gemini's OpenAI endpoint,
Groq, OpenRouter, LM Studio, Ollama (OpenAI mode), etc.
"""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from pathlib import Path

import requests

TEST_MESSAGE = 'hi! this is a API test, just respond "ok!"'


@dataclass
class ProviderCfg:
    name: str
    api_url: str
    api_key: str
    model: str

    @classmethod
    def from_dict(cls, d: dict) -> "ProviderCfg":
        return cls(
            name=d.get("name", ""),
            api_url=d.get("api_url", "").strip(),
            api_key=d.get("api_key", "").strip(),
            model=d.get("model", "").strip(),
        )


class LLMError(RuntimeError):
    """Raised when the API call fails or returns an unusable payload."""


def encode_image(image_or_path: str | Path | Image.Image, max_side: int = 1568) -> tuple[str, str]:
    """Load or accept an image, downscale longest side to max_side, return (b64, mime).

    Re-encodes as JPEG to keep payload size down. Returns mime 'image/jpeg'.
    """
    from PIL import Image  # imported lazily so config errors surface first

    if isinstance(image_or_path, (str, Path)):
        img = Image.open(image_or_path)
    elif isinstance(image_or_path, Image.Image):
        img = image_or_path.copy()
    else:
        raise ValueError(f"不支援的圖片類型：{type(image_or_path)}")

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    # downscale if needed (preserve aspect)
    w, h = img.size
    longest = max(w, h)
    if longest > max_side:
        scale = max_side / longest
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return b64, "image/jpeg"


def _post(cfg: ProviderCfg, payload: dict, timeout: int = 120) -> dict:
    if not cfg.api_url:
        raise LLMError("API 網址為空，請先在「API 設定」填入。")
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    try:
        resp = requests.post(cfg.api_url, headers=headers, json=payload, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise LLMError(f"無法連線到 API：{exc}") from exc
    excerpt = ""
    try:
        body = resp.json()
    except ValueError:
        body = None
        excerpt = resp.text[:400]
    if resp.status_code != 200:
        if body is not None:
            excerpt = str(body)[:400]
        raise LLMError(f"HTTP {resp.status_code}: {excerpt}")
    if body is None:
        raise LLMError(f"非 JSON 回應：{excerpt}")
    return body


def translate_image(
    cfg: ProviderCfg,
    image_or_path: str | Path | Image.Image,
    system_prompt: str,
    user_prompt: str,
    lang: str,
    max_side: int = 1568,
) -> str:
    """OCR+translate one image via the vision LLM; return the translated text."""
    b64, mime = encode_image(image_or_path, max_side)
    try:
        user_text = user_prompt.format(lang=lang)
    except (KeyError, IndexError):
        # user prompt without a placeholder
        user_text = user_prompt

    payload = {
        "model": cfg.model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    },
                ],
            },
        ],
    }
    body = _post(cfg, payload)
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"回應格式不符預期：{str(body)[:400]}")
    if isinstance(content, list):  # some providers return content blocks
        content = "".join(
            blk.get("text", "") for blk in content if isinstance(blk, dict)
        )
    return (content or "").strip()


def test_connection(cfg: ProviderCfg) -> tuple[bool, str]:
    """Send a trivial text round-trip; success if reply mentions 'ok'."""
    payload = {
        "model": cfg.model,
        "temperature": 0,
        "max_tokens": 20,
        "messages": [{"role": "user", "content": TEST_MESSAGE}],
    }
    try:
        body = _post(cfg, payload, timeout=30)
    except LLMError as exc:
        return False, str(exc)
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return False, f"回應格式不符預期：{str(body)[:300]}"
    if isinstance(content, list):
        content = "".join(
            blk.get("text", "") for blk in content if isinstance(blk, dict)
        )
    text = (content or "").strip()
    if "ok" in text.lower():
        return True, f"OK – {text}"
    return False, f"連線成功但未回覆 ok：{text!r}"
