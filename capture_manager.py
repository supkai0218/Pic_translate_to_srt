"""Capture and image asset manager for v2.01.

Handles screenshot persistence into \\captured, naming sanitization,
gallery listing, and lightweight image operations (rotation, flipping).
"""
from __future__ import annotations

import datetime
import os
import re
import time
from pathlib import Path
from typing import Optional

from PIL import Image

import config


def get_captured_dir() -> Path:
    """Ensure and return the directory for captured video frames."""
    d = config.CAPTURED_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_generated_dir() -> Path:
    """Ensure and return the directory for AI-generated images."""
    d = config.GENERATED_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def sanitize_filename_part(name: str, max_len: int = 50) -> str:
    """Sanitize arbitrary string into safe Windows filename component."""
    if not name:
        return "clip"
    # Remove characters forbidden in Windows filesystems: \ / : * ? " < > |
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip(" ._")
    if not s:
        s = "clip"
    return s[:max_len]


def sec_to_safe_ts(sec: float) -> str:
    """Convert float seconds to filesystem-safe timestamp 'HH-MM-SS_mmm'."""
    sec = max(0.0, float(sec))
    total_ms = int(round(sec * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}-{m:02d}-{s:02d}_{ms:03d}"


def save_captured_image(
    img: Image.Image,
    video_path: str = "",
    sec: float = 0.0,
    is_crop: bool = False,
    crop_box: Optional[tuple[int, int, int, int]] = None,
    custom_name: str = "",
) -> Path:
    """Save captured image into \\captured directory with naming '{video}_{timestamp}.jpg'."""
    cap_dir = get_captured_dir()

    if custom_name:
        safe_stem = sanitize_filename_part(custom_name)
    elif video_path:
        video_stem = Path(video_path).stem
        safe_stem = sanitize_filename_part(video_stem)
    else:
        safe_stem = "capture"

    ts_str = sec_to_safe_ts(sec)
    crop_tag = "_crop" if is_crop else ""

    base_name = f"{safe_stem}_{ts_str}{crop_tag}"
    target = cap_dir / f"{base_name}.jpg"

    # Avoid collision by appending counter
    counter = 1
    while target.exists():
        target = cap_dir / f"{base_name}_{counter}.jpg"
        counter += 1

    # Convert to RGB before saving as JPEG if necessary
    if img.mode not in ("RGB", "L"):
        save_img = img.convert("RGB")
    else:
        save_img = img

    save_img.save(target, format="JPEG", quality=95)
    return target


def save_generated_image(
    img: Image.Image,
    prompt_name: str = "",
    index: int = 1,
) -> Path:
    """Save AI-generated image into \\generated directory."""
    gen_dir = get_generated_dir()
    now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_prompt = sanitize_filename_part(prompt_name, max_len=30) if prompt_name else "ai_gen"

    base_name = f"{now_str}_{safe_prompt}_{index}"
    target = gen_dir / f"{base_name}.png"

    counter = 1
    while target.exists():
        target = gen_dir / f"{base_name}_{counter}.png"
        counter += 1

    img.save(target, format="PNG")
    return target


def list_captured_images() -> list[dict]:
    """List all captured images in \\captured, newest first."""
    cap_dir = get_captured_dir()
    valid_exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

    items = []
    try:
        for entry in os.scandir(cap_dir):
            if entry.is_file():
                p = Path(entry.path)
                if p.suffix.lower() in valid_exts:
                    stat = entry.stat()
                    items.append({
                        "path": p,
                        "name": p.name,
                        "mtime": stat.st_mtime,
                        "size": stat.st_size,
                        "date_str": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    })
    except Exception:
        pass

    # Newest first
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def rotate_image(img: Image.Image, angle: int) -> Image.Image:
    """Rotate image clockwise by angle in [90, 180, 270].

    Note: PIL Image.rotate rotates counter-clockwise, so clockwise 90 is rotate(270).
    """
    angle = angle % 360
    if angle == 90:
        return img.transpose(Image.Transpose.ROTATE_270)
    elif angle == 180:
        return img.transpose(Image.Transpose.ROTATE_180)
    elif angle == 270:
        return img.transpose(Image.Transpose.ROTATE_90)
    return img.copy()


def flip_image(img: Image.Image, horizontal: bool = True) -> Image.Image:
    """Flip image horizontally (left-right) or vertically."""
    if horizontal:
        return img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    return img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
