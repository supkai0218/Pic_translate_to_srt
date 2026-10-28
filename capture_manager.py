"""Capture and image asset manager for v2.01.

Handles screenshot persistence into \\captured, naming sanitization,
gallery listing, and lightweight image operations (rotation, flipping).
"""
from __future__ import annotations

import datetime
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from PIL import Image

import config


def get_captured_dir() -> Path:
    """Ensure and return the directory for captured video frames."""
    return config.resolve_captured_dir()


def get_captured_drop_dir() -> Path:
    """Ensure and return the .drop directory for captured video frames."""
    return config.resolve_captured_drop_dir()


def get_ocr_dir() -> Path:
    """Ensure and return the directory for OCR subtitle crop images."""
    return config.resolve_ocr_dir()


def get_ocr_drop_dir() -> Path:
    """Ensure and return the .drop directory for OCR subtitle crop images."""
    return config.resolve_ocr_drop_dir()


def get_generated_dir() -> Path:
    """Ensure and return the directory for AI-generated images."""
    return config.resolve_generated_dir()


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


def safe_ts_to_sec(ts_str: str) -> float:
    """Convert filesystem-safe timestamp 'HH-MM-SS_mmm' back to float seconds."""
    try:
        parts = ts_str.split("_")
        h_m_s = parts[0].split("-")
        h = int(h_m_s[0])
        m = int(h_m_s[1])
        s = int(h_m_s[2])
        ms = int(parts[1]) if len(parts) > 1 else 0
        return round(h * 3600.0 + m * 60.0 + s + ms / 1000.0, 3)
    except Exception:
        return 0.0


def parse_ocr_filename(filename: str) -> Optional[tuple[float, float]]:
    """Parse start and end seconds from OCR capture filename.

    Pattern: '{video_stem}_ocr_{start_ts}_{end_ts}{crop_tag}.jpg'
    Returns (start_sec, end_sec) or None if pattern not found.
    """
    pattern = r"_ocr_(\d{2}-\d{2}-\d{2}_\d{3})_(\d{2}-\d{2}-\d{2}_\d{3})"
    m = re.search(pattern, filename)
    if m:
        start_sec = safe_ts_to_sec(m.group(1))
        end_sec = safe_ts_to_sec(m.group(2))
        return (start_sec, end_sec)

    pattern_single = r"_ocr_(\d{2}-\d{2}-\d{2}_\d{3})"
    m2 = re.search(pattern_single, filename)
    if m2:
        start_sec = safe_ts_to_sec(m2.group(1))
        return (start_sec, start_sec + 2.0)
    return None


def save_captured_image(
    img: Image.Image,
    video_path: str = "",
    sec: float = 0.0,
    end_sec: Optional[float] = None,
    is_crop: bool = False,
    crop_box: Optional[tuple[int, int, int, int]] = None,
    custom_name: str = "",
    category: str = "captured",
    target_dir: Optional[Path | str] = None,
) -> Path:
    """Save captured image into target directory.

    If category == 'ocr' and target_dir is None, defaults to get_ocr_dir().
    Otherwise defaults to get_captured_dir().
    """
    if target_dir:
        cap_dir = Path(target_dir)
        cap_dir.mkdir(parents=True, exist_ok=True)
    elif category == "ocr":
        cap_dir = get_ocr_dir()
    else:
        cap_dir = get_captured_dir()

    if custom_name:
        safe_stem = sanitize_filename_part(custom_name)
    elif video_path:
        video_stem = Path(video_path).stem
        safe_stem = sanitize_filename_part(video_stem)
    else:
        safe_stem = "capture"

    start_ts = sec_to_safe_ts(sec)
    crop_tag = "_crop" if is_crop else ""

    if category == "ocr":
        if end_sec is not None:
            end_ts = sec_to_safe_ts(end_sec)
            base_name = f"{safe_stem}_ocr_{start_ts}_{end_ts}{crop_tag}"
        else:
            base_name = f"{safe_stem}_ocr_{start_ts}{crop_tag}"
    else:
        base_name = f"{safe_stem}_{start_ts}{crop_tag}"

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


def simplify_model_name(model_name: str) -> str:
    """Simplify model ID string into concise, safe filename token.

    Examples:
        'openai/gpt-image-2.5-flare/edit' -> 'gpt-image-2.5-flare'
        'google/nano-banana-2-lite/reference-to-image' -> 'nano-banana-2-lite'
        'black-forest-labs/flux-1-dev' -> 'flux-1-dev'
    """
    if not model_name:
        return "ai_model"
    parts = [p.strip() for p in model_name.split("/") if p.strip()]
    if not parts:
        return sanitize_filename_part(model_name, max_len=30)

    # Known task-specific keywords to strip from endpoint paths
    task_keywords = ("edit", "generate", "to-image", "to-video", "sequential")

    candidate = parts[-1]
    if len(parts) >= 2 and any(k in candidate.lower() for k in task_keywords):
        candidate = parts[-2]
    elif len(parts) >= 2:
        candidate = parts[-1]

    return sanitize_filename_part(candidate, max_len=30)


def save_edited_image(
    img: Image.Image,
    original_path: str | Path = "",
    target_dir: Optional[Path | str] = None,
) -> Path:
    """Save rotated or flipped image into target directory (default get_captured_dir()) with '{original_stem}_edit_{counter}.jpg'.

    Prevents repetitive chain suffixes like '_edit_edit_edit' by normalizing to clean stem.
    """
    if target_dir:
        cap_dir = Path(target_dir)
        cap_dir.mkdir(parents=True, exist_ok=True)
    else:
        cap_dir = get_captured_dir()

    if original_path:
        orig_stem = Path(original_path).stem
        # Clean off existing _edit or _edit_X suffix
        clean_stem = re.sub(r"_edit(_\d+)?$", "", orig_stem)
        safe_stem = sanitize_filename_part(clean_stem, max_len=50)
    else:
        safe_stem = "captured"

    counter = 1
    target = cap_dir / f"{safe_stem}_edit_{counter}.jpg"
    while target.exists():
        counter += 1
        target = cap_dir / f"{safe_stem}_edit_{counter}.jpg"

    if img.mode not in ("RGB", "L"):
        save_img = img.convert("RGB")
    else:
        save_img = img

    save_img.save(target, format="JPEG", quality=95)
    return target


def save_generated_image(
    img: Image.Image,
    prompt_name: str = "",
    model_name: str = "",
    index: int = 1,
    target_dir: Optional[Path | str] = None,
) -> Path:
    """Save AI-generated image into target directory (default get_generated_dir())
    with naming '{prompt範本名稱}_{模型名稱}_YYYYMMDD_hhmm.png'.
    """
    if target_dir:
        gen_dir = Path(target_dir)
        gen_dir.mkdir(parents=True, exist_ok=True)
    else:
        gen_dir = get_generated_dir()

    now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    safe_prompt = sanitize_filename_part(prompt_name, max_len=30) if prompt_name else "custom"
    safe_model = simplify_model_name(model_name)

    base_name = f"{safe_prompt}_{safe_model}_{now_str}"
    target = gen_dir / f"{base_name}.png"

    counter = 1
    while target.exists():
        target = gen_dir / f"{base_name}_{counter}.png"
        counter += 1

    img.save(target, format="PNG")
    return target


def list_images_in_dir(target_dir: Path | str) -> list[dict]:
    """List all supported images in specified directory, newest first."""
    p_dir = Path(target_dir)
    if not p_dir.is_dir():
        return []
    valid_exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

    items = []
    try:
        for entry in os.scandir(p_dir):
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

    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def list_captured_images(custom_dir: Optional[Path | str] = None) -> list[dict]:
    """List all captured images in captured directory (or custom directory), newest first."""
    d = Path(custom_dir) if custom_dir else get_captured_dir()
    return list_images_in_dir(d)


def list_generated_images(custom_dir: Optional[Path | str] = None) -> list[dict]:
    """List all generated images in generated directory (or custom directory), newest first."""
    d = Path(custom_dir) if custom_dir else get_generated_dir()
    return list_images_in_dir(d)


def copy_images_to_directory(source_paths: list[Path | str], target_dir: Path | str) -> list[Path]:
    """Copy selected image files into target directory.

    If target file already exists, automatically appends sequential suffix
    (e.g., '{stem}_1{suffix}', '{stem}_2{suffix}') to avoid overwriting.
    Returns list of destination Paths.
    """
    dest_dir = Path(target_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []

    for src_raw in source_paths:
        src = Path(src_raw)
        if not src.is_file():
            continue

        target_file = dest_dir / src.name
        stem = src.stem
        suffix = src.suffix
        counter = 1
        while target_file.exists():
            target_file = dest_dir / f"{stem}_{counter}{suffix}"
            counter += 1

        shutil.copy2(src, target_file)
        copied.append(target_file)

    return copied


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


def safe_move_to_drop(filepath: str | Path, drop_dir: Path | str) -> Optional[Path]:
    """Move a file to the .drop directory, avoiding collisions by appending counter if needed."""
    src = Path(filepath)
    if not src.is_file():
        return None
    d_dir = Path(drop_dir)
    d_dir.mkdir(parents=True, exist_ok=True)

    dest = d_dir / src.name
    stem = src.stem
    suffix = src.suffix
    counter = 1
    while dest.exists():
        dest = d_dir / f"{stem}_{counter}{suffix}"
        counter += 1

    try:
        shutil.move(str(src), str(dest))
        return dest
    except Exception as e:
        print(f"[safe_move_to_drop] Error moving {src} to {dest}: {e}")
        return None


def safe_restore_from_drop(filepath: str | Path, target_dir: Path | str) -> Optional[Path]:
    """Move a file from .drop back to target working directory, avoiding collisions."""
    src = Path(filepath)
    if not src.is_file():
        return None
    t_dir = Path(target_dir)
    t_dir.mkdir(parents=True, exist_ok=True)

    dest = t_dir / src.name
    stem = src.stem
    suffix = src.suffix
    counter = 1
    while dest.exists():
        dest = t_dir / f"{stem}_{counter}{suffix}"
        counter += 1

    try:
        shutil.move(str(src), str(dest))
        return dest
    except Exception as e:
        print(f"[safe_restore_from_drop] Error moving {src} to {dest}: {e}")
        return None


def clear_drop_dir(drop_dir: Path | str) -> int:
    """Permanently delete all image files inside drop_dir. Returns count of deleted files."""
    d_dir = Path(drop_dir)
    if not d_dir.is_dir():
        return 0
    deleted = 0
    for entry in os.scandir(d_dir):
        if entry.is_file():
            p = Path(entry.path)
            try:
                p.unlink()
                deleted += 1
            except Exception:
                pass
    return deleted
