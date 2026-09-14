"""PyInstaller automated build script for PicTrans2Srt (v1.08).

Builds a standalone, portable Windows directory distribution (--onedir):
- PicTrans2Srt.exe
- Bundles CustomTkinter assets & themes
- Copies external vlc/ (LibVLC runtime & plugins)
- Copies settings.example.json & .env.example
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DIST_DIR = BASE_DIR / "dist"
BUILD_DIR = BASE_DIR / "build"
STAGE_DIR = BUILD_DIR / "dist_stage"
TARGET_DIR = DIST_DIR / "PicTrans2Srt"


def clean_previous_build():
    print(">>> 正在清理舊的建置資料夾...")
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR, ignore_errors=True)
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    for child in TARGET_DIR.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        except Exception:
            pass


def run_pyinstaller():
    print(">>> 正在執行 PyInstaller 打包流程...")

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "app.py",
        "--name=PicTrans2Srt",
        "--noconsole",
        "--onedir",
        "--clean",
        "--noconfirm",
        f"--distpath={STAGE_DIR}",
        "--collect-all=customtkinter",
        "--hidden-import=vlc",
        "--hidden-import=PIL",
        "--hidden-import=PIL.ImageTk",
        "--hidden-import=PIL._tkinter_finder",
        "--hidden-import=cv2",
        "--hidden-import=numpy",
        "--hidden-import=dotenv",
        "--hidden-import=requests",
    ]

    # Optional icon if exists
    icon_path = BASE_DIR / "icon.ico"
    if icon_path.is_file():
        cmd.append(f"--icon={str(icon_path)}")

    subprocess.check_call(cmd, cwd=str(BASE_DIR))


def sync_staged_output():
    staged_src = STAGE_DIR / "PicTrans2Srt"
    if not staged_src.is_dir():
        raise RuntimeError(f"PyInstaller 產出路徑不存在：{staged_src}")

    print(f">>> 正在同步打包檔案至發布目錄：{TARGET_DIR}...")
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    for child in staged_src.iterdir():
        dest = TARGET_DIR / child.name
        if child.is_dir():
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(child, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(child, dest)


def copy_runtime_assets():
    print(">>> 正在複製執行期外置資源 (LibVLC, 設定檔範本)...")

    # Copy VLC runtime if present
    vlc_src = BASE_DIR / "vlc"
    vlc_dest = TARGET_DIR / "vlc"
    if vlc_src.is_dir():
        print(f"    複製 VLC 目錄：{vlc_src} -> {vlc_dest}")
        if vlc_dest.exists():
            shutil.rmtree(vlc_dest, ignore_errors=True)
        shutil.copytree(vlc_src, vlc_dest, dirs_exist_ok=True)
    else:
        print("    [警告] 未在專案根目錄找到 vlc/ 資料夾，請確認 LibVLC runtime 是否存在！")

    # Copy settings.example.json
    settings_example = BASE_DIR / "settings.example.json"
    if settings_example.is_file():
        shutil.copy2(settings_example, TARGET_DIR / "settings.example.json")

    # Copy current settings.json if exists, or copy from example
    settings_json = BASE_DIR / "settings.json"
    if settings_json.is_file():
        shutil.copy2(settings_json, TARGET_DIR / "settings.json")
    elif settings_example.is_file():
        shutil.copy2(settings_example, TARGET_DIR / "settings.example.json")

    # Copy .env.example
    dotenv_example = BASE_DIR / ".env.example"
    if dotenv_example.is_file():
        shutil.copy2(dotenv_example, TARGET_DIR / ".env.example")


def print_summary():
    exe_file = TARGET_DIR / "PicTrans2Srt.exe"
    print("\n" + "=" * 60)
    if exe_file.is_file():
        print("  PicTrans2Srt (v1.08) 打包建置成功！")
        print(f"  執行檔位置: {exe_file}")
        print("=" * 60)
        print("發布目錄內容確認：")
        for item in TARGET_DIR.iterdir():
            t = "目錄" if item.is_dir() else "檔案"
            print(f"  - [{t}] {item.name}")
        print("=" * 60)
    else:
        print("  [錯誤] 未生成 PicTrans2Srt.exe，請檢查 PyInstaller 輸出日誌。")
        print("=" * 60)


def main():
    try:
        clean_previous_build()
        run_pyinstaller()
        sync_staged_output()
        copy_runtime_assets()
        print_summary()
    except Exception as exc:
        print(f"\n[建置失敗]: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
