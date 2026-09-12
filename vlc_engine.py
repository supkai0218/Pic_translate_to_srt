"""LibVLC player engine for smooth 1.0x audio/video playback and subtitle rendering.

Loads the local VLC runtime from ./vlc or ./vlc_core, avoids Python namespace
shadowing, and wraps media playback, audio volume, rate, and subtitle overlay.
"""
from __future__ import annotations

import os
import sys
import time
import threading
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent

# Check for local VLC binaries in vlc or vlc_core
_LOCAL_VLC_DIR = None
for candidate in (BASE_DIR / "vlc_core", BASE_DIR / "vlc"):
    if candidate.is_dir() and (candidate / "libvlc.dll").is_file():
        _LOCAL_VLC_DIR = candidate
        break

if _LOCAL_VLC_DIR is not None:
    os.environ["PYTHON_VLC_LIB_PATH"] = str(_LOCAL_VLC_DIR / "libvlc.dll")
    os.environ["PYTHON_VLC_MODULE_PATH"] = str(_LOCAL_VLC_DIR / "plugins")
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(str(_LOCAL_VLC_DIR))
        except Exception:
            pass

# Temporarily exclude current working directory from sys.path to avoid
# namespace package shadowing when the folder is named 'vlc'
_saved_sys_path = list(sys.path)
try:
    sys.path = [p for p in sys.path if p not in ("", ".", str(BASE_DIR), str(Path.cwd()))]
    import vlc
finally:
    sys.path = _saved_sys_path


def is_vlc_available() -> bool:
    """Check whether LibVLC library is loaded and usable."""
    try:
        ver = vlc.libvlc_get_version()
        return ver is not None
    except Exception:
        return False


def get_vlc_version() -> str:
    """Return LibVLC version string or empty if unavailable."""
    try:
        ver = vlc.libvlc_get_version()
        if isinstance(ver, bytes):
            return ver.decode("utf-8", errors="ignore")
        return str(ver)
    except Exception:
        return ""


class VLCPlayerError(RuntimeError):
    """Raised when VLC player operation fails."""


class VLCPlayer:
    def __init__(self):
        if not is_vlc_available():
            raise VLCPlayerError("LibVLC 核心未正確載入，請確認 vlc 目錄內包含 libvlc.dll 與 plugins。")

        # Create VLC instance with optimized flags for embedded playback
        self._instance: vlc.Instance = vlc.Instance(
            "--no-xlib",
            "--quiet",
            "--no-video-title-show",
            "--sub-source=marq",
        )
        self._player: vlc.MediaPlayer = self._instance.media_player_new()
        self._hwnd: Optional[int] = None
        self._media_path: str = ""
        self._srt_path: str = ""
        self._is_muted: bool = False
        self._last_volume: int = 80
        self._volume: int = 80

    def bind_window(self, hwnd: int, start_sec: Optional[float] = None, is_playing: Optional[bool] = None) -> None:
        """Bind native Windows HWND to VLC player with vout re-creation if switching."""
        if self._hwnd != hwnd:
            self._hwnd = hwnd
            if sys.platform.startswith("win"):
                was_playing = self.is_playing() if is_playing is None else bool(is_playing)
                cur_t = self.get_time() if start_sec is None else float(start_sec)
                if cur_t <= 0 and start_sec is not None:
                    cur_t = float(start_sec)
                cur_rate = self.get_rate()
                try:
                    self._player.stop()
                    self._player.release()
                except Exception:
                    pass
                self._player = self._instance.media_player_new()
                self._player.set_hwnd(hwnd)
                try:
                    self._player.video_set_mouse_input(False)
                    self._player.video_set_key_input(False)
                    self._player.audio_set_volume(0 if self._is_muted else self._volume)
                    if cur_rate > 0:
                        self._player.set_rate(cur_rate)
                except Exception:
                    pass
                if self._media_path and os.path.isfile(self._media_path):
                    self.load_media(self._media_path, self._srt_path)
                    self._player.play()
                    target_pl = self._player

                    def _sync_vout_state(pl, t, playing):
                        for _ in range(100):
                            time.sleep(0.01)
                            if self._player is not pl:
                                return
                            try:
                                has_vo = False
                                try:
                                    has_vo = (pl.has_vout() > 0)
                                except Exception:
                                    pass
                                if has_vo:
                                    if t > 0:
                                        pl.set_time(int(round(t * 1000.0)))
                                    if not playing:
                                        pl.set_pause(1)
                                    return
                            except Exception:
                                return

                    threading.Thread(target=_sync_vout_state, args=(target_pl, cur_t, was_playing), daemon=True).start()

    def load_media(self, video_path: str | Path, srt_path: str = "", start_time: float = 0.0) -> None:
        """Load video media into player and optionally attach external subtitle."""
        self._media_path = str(video_path)
        self._srt_path = str(srt_path) if srt_path else ""

        media = self._instance.media_new(self._media_path)
        if start_time > 0:
            media.add_option(f"start-time={start_time:.3f}")
        if self._srt_path and os.path.isfile(self._srt_path):
            try:
                mrl = self._srt_path.replace("\\", "/")
                if not mrl.startswith("file:///"):
                    mrl = "file:///" + mrl
                media.slaves_add(vlc.MediaSlaveType.subtitle, 4, mrl)
            except Exception:
                pass
        self._player.set_media(media)

        try:
            self._player.video_set_mouse_input(False)
            self._player.video_set_key_input(False)
        except Exception:
            pass

        # Re-bind HWND if previously set
        if self._hwnd and sys.platform.startswith("win"):
            self._player.set_hwnd(self._hwnd)

    def set_subtitle_file(self, srt_path: str | Path) -> bool:
        """Set or hot-reload an external SRT subtitle file."""
        p = str(srt_path)
        if not os.path.isfile(p):
            return False
        self._srt_path = p
        if self._player.is_playing():
            try:
                ok = self._player.video_set_subtitle_file(p)
                return bool(ok)
            except Exception:
                return False
        else:
            t_sec = self.get_time()
            if self._media_path:
                self.load_media(self._media_path, self._srt_path)
                if t_sec > 0:
                    self.set_time(t_sec)
                return True
            return False

    def reload_subtitle(self) -> bool:
        """Reload currently assigned subtitle file into VLC."""
        if self._srt_path and os.path.isfile(self._srt_path):
            return self.set_subtitle_file(self._srt_path)
        return False

    def clear_subtitle(self) -> None:
        """Disable or clear subtitles."""
        try:
            self._player.video_set_spu(-1)
            self._srt_path = ""
        except Exception:
            pass

    def play(self) -> bool:
        """Start or resume playback."""
        res = self._player.play()
        return res == 0

    def pause(self) -> None:
        """Pause playback."""
        if self._player.is_playing():
            self._player.pause()

    def toggle_play(self) -> bool:
        """Toggle play/pause state. Returns True if now playing."""
        if self._player.is_playing():
            self._player.pause()
            return False
        else:
            self._player.play()
            return True

    def stop(self) -> None:
        """Stop playback."""
        try:
            self._player.stop()
        except Exception:
            pass

    def is_playing(self) -> bool:
        """Return True if media is actively playing."""
        try:
            return bool(self._player.is_playing())
        except Exception:
            return False

    def get_time(self) -> float:
        """Return current playback time in seconds (float)."""
        try:
            t_ms = self._player.get_time()
            return max(0.0, t_ms / 1000.0) if t_ms >= 0 else 0.0
        except Exception:
            return 0.0

    def set_time(self, sec: float) -> None:
        """Seek to specified position in seconds."""
        try:
            ms = max(0, int(round(sec * 1000.0)))
            self._player.set_time(ms)
        except Exception:
            pass

    def get_duration(self) -> float:
        """Return media duration in seconds (float)."""
        try:
            dur_ms = self._player.get_length()
            return max(0.0, dur_ms / 1000.0) if dur_ms >= 0 else 0.0
        except Exception:
            return 0.0

    def get_volume(self) -> int:
        """Return current audio volume (0-100)."""
        if self._is_muted:
            return 0
        return self._volume

    def set_volume(self, volume: int) -> None:
        """Set audio volume (0-100)."""
        vol = max(0, min(100, int(volume)))
        self._volume = vol
        try:
            self._player.audio_set_volume(vol)
        except Exception:
            pass
        if vol > 0:
            self._last_volume = vol
            self._is_muted = False
        else:
            self._is_muted = True

    def toggle_mute(self) -> bool:
        """Toggle mute state. Returns True if now muted."""
        if not self._is_muted:
            self._last_volume = self._volume if self._volume > 0 else (self._last_volume or 80)
            self._is_muted = True
            self._volume = 0
            try:
                self._player.audio_set_volume(0)
                self._player.audio_set_mute(True)
            except Exception:
                pass
        else:
            self._is_muted = False
            self._volume = self._last_volume or 80
            try:
                self._player.audio_set_volume(self._volume)
                self._player.audio_set_mute(False)
            except Exception:
                pass
        return self._is_muted

    @property
    def is_muted(self) -> bool:
        return self._is_muted or (self._volume == 0)

    def get_rate(self) -> float:
        """Return playback rate (e.g. 1.0)."""
        try:
            return float(self._player.get_rate() or 1.0)
        except Exception:
            return 1.0

    def set_rate(self, rate: float) -> None:
        """Set playback rate (e.g. 0.5, 1.0, 1.5, 2.0)."""
        try:
            r = max(0.25, min(4.0, float(rate)))
            self._player.set_rate(r)
        except Exception:
            pass

    def release(self) -> None:
        """Release player and VLC instance."""
        try:
            self._player.stop()
            self._player.release()
            self._instance.release()
        except Exception:
            pass
