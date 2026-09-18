"""Cloud aggregator and multimodal image generation engine for v2.01.

Supports OpenRouter, Venice AI, Atlas Cloud, and OpenAI-compatible image endpoints.
Handles multi-image reference inputs, prompt conditioning, aspect ratios,
and decodes output URLs/Base64 into PIL Images.
"""
from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from PIL import Image

if TYPE_CHECKING:
    import requests

requests = None


def _ensure_requests():
    global requests
    if requests is None:
        import requests as _requests_mod
        requests = _requests_mod


class P2PError(RuntimeError):
    """Raised when Image-to-Image generation API call fails."""


@dataclass
class P2PProviderCfg:
    name: str
    api_url: str
    api_key: str
    model: str
    provider_type: str = "openrouter"  # "openrouter", "venice", "atlas", "openai_images"

    def __post_init__(self):
        name_l = self.name.lower()
        url_l = self.api_url.lower()
        if "atlas" in name_l or "atlascloud" in url_l:
            self.provider_type = "atlas"
        elif "venice" in name_l or "venice" in url_l:
            self.provider_type = "venice"
        elif not self.provider_type:
            self.provider_type = "openrouter"

    @classmethod
    def from_dict(cls, d: dict) -> "P2PProviderCfg":
        model = d.get("selected_model") or d.get("model", "")
        if isinstance(model, list):
            model = model[0] if model else ""
        name = d.get("name", "")
        ptype = d.get("provider_type") or ""
        if not ptype:
            name_l = name.lower()
            url_l = (d.get("api_url") or "").lower()
            if "venice" in name_l or "venice" in url_l:
                ptype = "venice"
            elif "atlas" in name_l or "atlascloud" in url_l:
                ptype = "atlas"
            else:
                ptype = "openrouter"

        return cls(
            name=name,
            api_url=d.get("api_url", "").strip(),
            api_key=d.get("api_key", "").strip(),
            model=str(model).strip(),
            provider_type=ptype,
        )


def encode_image_b64(image_or_path: str | Path | Image.Image, max_side: int = 1536) -> tuple[str, str]:
    """Encode PIL Image or path to (base64_str, 'image/jpeg'). Downscales if needed."""
    if isinstance(image_or_path, (str, Path)):
        img = Image.open(image_or_path)
    elif isinstance(image_or_path, Image.Image):
        img = image_or_path.copy()
    else:
        raise ValueError(f"不支援的圖片格式：{type(image_or_path)}")

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    w, h = img.size
    longest = max(w, h)
    if longest > max_side:
        scale = max_side / longest
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return b64, "image/jpeg"


def _extract_images_from_response(body: dict | list) -> list[Image.Image]:
    """Extract PIL images from various API response formats (URLs, b64_json, markdown links, outputs)."""
    _ensure_requests()
    results: list[Image.Image] = []

    # 1. Standard OpenAI Images API: {"data": [{"b64_json": "..."}, {"url": "..."}]}
    if isinstance(body, dict) and "data" in body and isinstance(body["data"], list):
        for item in body["data"]:
            if isinstance(item, dict):
                if item.get("b64_json"):
                    raw = base64.b64decode(item["b64_json"])
                    results.append(Image.open(io.BytesIO(raw)))
                elif item.get("url"):
                    r = requests.get(item["url"], timeout=60)
                    if r.status_code == 200:
                        results.append(Image.open(io.BytesIO(r.content)))

    # 2. Venice AI format: {"images": ["base64_string...", ...]}
    if isinstance(body, dict) and "images" in body and isinstance(body["images"], list):
        for item in body["images"]:
            if isinstance(item, str):
                clean_b64 = re.sub(r"^data:image/[a-zA-Z0-9.+_-]+;base64,", "", item)
                raw = base64.b64decode(clean_b64)
                results.append(Image.open(io.BytesIO(raw)))

    # 3. Atlas Cloud / Task Polling format: {"data": {"outputs": ["https://..."]}} or {"outputs": ["https://..."]}
    outputs = None
    if isinstance(body, dict):
        data_field = body.get("data")
        if isinstance(data_field, dict) and "outputs" in data_field:
            outputs = data_field["outputs"]
        elif "outputs" in body:
            outputs = body["outputs"]

    if isinstance(outputs, list):
        for item in outputs:
            if isinstance(item, str):
                if item.startswith("http://") or item.startswith("https://"):
                    try:
                        r = requests.get(item, timeout=60)
                        if r.status_code == 200:
                            results.append(Image.open(io.BytesIO(r.content)))
                    except Exception:
                        pass
                elif "base64," in item:
                    clean_b64 = item.split("base64,")[-1]
                    try:
                        results.append(Image.open(io.BytesIO(base64.b64decode(clean_b64))))
                    except Exception:
                        pass
                else:
                    try:
                        raw = base64.b64decode(item)
                        results.append(Image.open(io.BytesIO(raw)))
                    except Exception:
                        pass
            elif isinstance(item, dict):
                if item.get("url"):
                    try:
                        r = requests.get(item["url"], timeout=60)
                        if r.status_code == 200:
                            results.append(Image.open(io.BytesIO(r.content)))
                    except Exception:
                        pass
                elif item.get("b64_json"):
                    try:
                        results.append(Image.open(io.BytesIO(base64.b64decode(item["b64_json"]))))
                    except Exception:
                        pass

    # 4. Chat completion format (e.g. OpenRouter multimodal image output):
    # {"choices": [{"message": {"content": "![image](data:image/png;base64,...)"}}]}
    if isinstance(body, dict) and "choices" in body and isinstance(body["choices"], list):
        for choice in body["choices"]:
            msg = choice.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        url_val = part.get("image_url", {}).get("url", "")
                        if url_val.startswith("data:"):
                            clean_b64 = re.sub(r"^data:image/[a-zA-Z0-9.+_-]+;base64,", "", url_val)
                            results.append(Image.open(io.BytesIO(base64.b64decode(clean_b64))))
                        elif url_val.startswith("http"):
                            r = requests.get(url_val, timeout=60)
                            if r.status_code == 200:
                                results.append(Image.open(io.BytesIO(r.content)))
            elif isinstance(content, str):
                data_uris = re.findall(r'data:image/[a-zA-Z0-9.+_-]+;base64,([A-Za-z0-9+/=]+)', content)
                for b64_str in data_uris:
                    try:
                        results.append(Image.open(io.BytesIO(base64.b64decode(b64_str))))
                    except Exception:
                        pass
                urls = re.findall(r'!\[.*?\]\((https?://[^\s\)]+)\)', content)
                for u in urls:
                    try:
                        r = requests.get(u, timeout=60)
                        if r.status_code == 200:
                            results.append(Image.open(io.BytesIO(r.content)))
                    except Exception:
                        pass

    return results


def test_p2p_connection(cfg: P2PProviderCfg) -> tuple[bool, str]:
    """Test API connection and authentication."""
    _ensure_requests()
    if not cfg.api_url:
        return False, "API 網址為空，請先填寫 API 網址。"

    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    url = cfg.api_url.rstrip("/")
    ptype = cfg.provider_type.lower()
    name_l = cfg.name.lower()

    if "atlascloud.ai" in url or ptype == "atlas" or "atlas" in name_l:
        test_url = "https://api.atlascloud.ai/public/v1/balance"
    elif url.endswith("/chat/completions"):
        test_url = url
    elif url.endswith("/v1"):
        test_url = f"{url}/models"
    elif url.endswith("/image/generate"):
        test_url = url.replace("/image/generate", "/models")
    elif url.endswith("/model/generateImage"):
        test_url = url.replace("/model/generateImage", "/models")
    else:
        test_url = f"{url}/models"

    try:
        resp = requests.get(test_url, headers=headers, timeout=15)
        if resp.status_code in (200, 201):
            return True, f"連線成功 (HTTP {resp.status_code})"
        if resp.status_code in (401, 403):
            return False, f"金鑰驗證失敗 (HTTP {resp.status_code})：請確認 API Key 是否正確"
        if resp.status_code == 404:
            return False, f"找不到端點 (HTTP 404)：請確認 API 網址是否正確"
        return False, f"伺服器回應錯誤 (HTTP {resp.status_code})"
    except Exception as exc:
        return False, f"連線失敗：{exc}"


def generate_image(
    cfg: P2PProviderCfg,
    images: list[Image.Image],
    prompt: str,
    negative_prompt: str = "",
    aspect_ratio: str = "1:1",
    strength: float = 0.65,
    num_outputs: int = 1,
    timeout: int = 180,
) -> list[Image.Image]:
    """Generate new image(s) using Cloud Aggregator API from reference images and prompt."""
    _ensure_requests()
    if not cfg.api_url:
        raise P2PError("請先設定圖生圖 API 網址。")

    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    base_url = cfg.api_url.rstrip("/")
    ptype = cfg.provider_type.lower()
    name_l = cfg.name.lower()

    # ------------------------------------------------------------- #
    #  Atlas Cloud Endpoint: /api/v1/model/generateImage (Async Polling)
    # ------------------------------------------------------------- #
    if ptype == "atlas" or "atlascloud" in base_url or "atlas" in name_l or "/model/generateImage" in base_url:
        if "atlascloud.ai" in base_url:
            gen_url = "https://api.atlascloud.ai/api/v1/model/generateImage"
            pred_base = "https://api.atlascloud.ai/api/v1/model/prediction"
        elif "/model/generateImage" in base_url:
            gen_url = base_url
            pred_base = base_url.replace("/model/generateImage", "/model/prediction")
        else:
            gen_url = f"{base_url}/model/generateImage"
            pred_base = f"{base_url}/model/prediction"

        payload: dict = {
            "model": cfg.model,
            "prompt": prompt,
        }
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        if aspect_ratio:
            payload["aspect_ratio"] = aspect_ratio

        if images:
            img_list = []
            for img in images:
                b64, mime = encode_image_b64(img)
                img_list.append(f"data:{mime};base64,{b64}")
            payload["images"] = img_list

        try:
            resp = requests.post(gen_url, headers=headers, json=payload, timeout=60)
        except Exception as exc:
            raise P2PError(f"連線至 Atlas Cloud 失敗：{exc}") from exc

        if resp.status_code != 200:
            raise P2PError(f"Atlas Cloud 請求失敗 (HTTP {resp.status_code}): {resp.text[:300]}")

        res_json = resp.json()
        prediction_id = ""
        if isinstance(res_json.get("data"), dict):
            prediction_id = res_json["data"].get("id", "")
        if not prediction_id:
            prediction_id = res_json.get("prediction_id") or res_json.get("id") or ""

        if not prediction_id:
            immediate_imgs = _extract_images_from_response(res_json)
            if immediate_imgs:
                return immediate_imgs
            raise P2PError(f"Atlas Cloud 未回傳任務 ID (prediction_id)：{resp.text[:300]}")

        # Async polling loop
        poll_url = f"{pred_base}/{prediction_id}"
        poll_interval = 3
        max_attempts = max(10, timeout // poll_interval)
        import time

        for attempt in range(max_attempts):
            time.sleep(poll_interval)
            try:
                poll_resp = requests.get(poll_url, headers=headers, timeout=30)
            except Exception:
                continue

            if poll_resp.status_code != 200:
                continue

            poll_data = poll_resp.json()
            data_block = poll_data.get("data") if isinstance(poll_data.get("data"), dict) else poll_data
            status = str(data_block.get("status", "")).lower()

            if status == "completed":
                imgs = _extract_images_from_response(poll_data)
                if not imgs:
                    raise P2PError("Atlas Cloud 任務完成，但未解析出圖片。")
                return imgs
            elif status == "failed":
                err_msg = data_block.get("error") or poll_resp.text[:300]
                raise P2PError(f"Atlas Cloud 繪圖生成失敗：{err_msg}")

        raise P2PError(f"Atlas Cloud 繪圖逾時（已等待超過 {timeout} 秒）。")

    # ------------------------------------------------------------- #
    #  Venice AI Endpoint: /api/v1/image/generate
    # ------------------------------------------------------------- #
    if ptype == "venice" or "/image/generate" in base_url:
        target_url = base_url if "/image/generate" in base_url else f"{base_url}/image/generate"
        payload = {
            "model": cfg.model,
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "return_binary": False,
            "aspect_ratio": aspect_ratio,
        }
        if images:
            # Venice image-to-image uses first reference image
            b64, _ = encode_image_b64(images[0])
            payload["image"] = b64
            payload["strength"] = strength

        try:
            resp = requests.post(target_url, headers=headers, json=payload, timeout=timeout)
        except Exception as exc:
            raise P2PError(f"連線至 Venice 失敗：{exc}") from exc

        if resp.status_code != 200:
            raise P2PError(f"Venice 回應錯誤 (HTTP {resp.status_code}): {resp.text[:300]}")

        imgs = _extract_images_from_response(resp.json())
        if not imgs:
            raise P2PError("Venice 回應中未包含圖片資料。")
        return imgs

    # ------------------------------------------------------------- #
    #  OpenRouter / Chat Multimodal Endpoint
    # ------------------------------------------------------------- #
    # If endpoint doesn't specify /chat/completions or /images/generations, default to chat
    if ptype == "openrouter" or base_url.endswith("/v1") or "/chat/completions" in base_url:
        target_url = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"

        # Build prompt instructions with negative prompt and aspect ratio
        prompt_instruction = (
            f"Generate a new high-quality image based on the provided reference image(s).\n"
            f"Prompt: {prompt}\n"
        )
        if negative_prompt:
            prompt_instruction += f"Negative prompt: {negative_prompt}\n"
        prompt_instruction += f"Aspect ratio: {aspect_ratio}\n"

        user_content = [{"type": "text", "text": prompt_instruction}]

        # Inject all selected reference images
        for idx, img in enumerate(images, 1):
            b64, mime = encode_image_b64(img)
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"}
            })

        payload = {
            "model": cfg.model,
            "messages": [
                {
                    "role": "user",
                    "content": user_content,
                }
            ],
            "modalities": ["image", "text"],
        }

        try:
            resp = requests.post(target_url, headers=headers, json=payload, timeout=timeout)
        except Exception as exc:
            raise P2PError(f"連線至繪圖服務失敗：{exc}") from exc

        if resp.status_code != 200:
            raise P2PError(f"繪圖服務回應錯誤 (HTTP {resp.status_code}): {resp.text[:400]}")

        imgs = _extract_images_from_response(resp.json())
        if imgs:
            return imgs

        # If chat format didn't return image, check if response text contains message
        body = resp.json()
        try:
            reply_text = body["choices"][0]["message"]["content"]
            raise P2PError(f"模型未產生圖片，回傳文字訊息：{reply_text[:200]}")
        except KeyError:
            raise P2PError(f"回應格式不符預期：{str(body)[:300]}")

    # ------------------------------------------------------------- #
    #  Fallback: Standard OpenAI /images/generations
    # ------------------------------------------------------------- #
    target_url = f"{base_url}/images/generations" if not base_url.endswith("/images/generations") else base_url
    payload = {
        "model": cfg.model,
        "prompt": prompt,
        "n": num_outputs,
        "response_format": "b64_json",
    }
    try:
        resp = requests.post(target_url, headers=headers, json=payload, timeout=timeout)
    except Exception as exc:
        raise P2PError(f"無法發送請求至 {target_url}：{exc}") from exc

    if resp.status_code != 200:
        raise P2PError(f"HTTP {resp.status_code}: {resp.text[:300]}")

    imgs = _extract_images_from_response(resp.json())
    if not imgs:
        raise P2PError("API 回應未包含圖片。")
    return imgs
