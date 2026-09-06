"""Video decoding and frame extraction engine using OpenCV.

Provides frame-accurate seeking, sequential playback frame reading,
and conversion to PIL RGB Image.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image


class VideoEngineError(RuntimeError):
    """Raised when video cannot be opened or decoded."""


class VideoEngine:
    def __init__(self, video_path: str | Path):
        self.video_path = str(video_path)
        if not os.path.isfile(self.video_path):
            raise VideoEngineError(f"影片檔案不存在：{self.video_path}")

        self._cap: Optional[cv2.VideoCapture] = cv2.VideoCapture(self.video_path)
        if not self._cap or not self._cap.isOpened():
            raise VideoEngineError(f"無法開啟影片檔案：{self.video_path}")

        self.fps: float = float(self._cap.get(cv2.CAP_PROP_FPS) or 30.0)
        if self.fps <= 0:
            self.fps = 30.0
        self.frame_count: int = int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.width: int = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        self.height: int = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        self.duration_sec: float = self.frame_count / self.fps if self.fps > 0 else 0.0

        # Current playback position in seconds
        self.current_sec: float = 0.0

    @property
    def is_opened(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    def get_current_sec(self) -> float:
        if not self.is_opened:
            return self.current_sec
        pos_msec = self._cap.get(cv2.CAP_PROP_POS_MSEC)
        if pos_msec >= 0:
            self.current_sec = pos_msec / 1000.0
        return self.current_sec

    def seek_time(self, sec: float) -> tuple[bool, Optional[Image.Image]]:
        """Seek to a specific timestamp in seconds and return the decoded PIL Image."""
        if not self.is_opened:
            return False, None

        sec = max(0.0, min(sec, self.duration_sec))
        self.current_sec = sec

        # Set position by msec
        self._cap.set(cv2.CAP_PROP_POS_MSEC, sec * 1000.0)
        ret, frame = self._cap.read()
        if not ret or frame is None:
            # Fallback by frame index if POS_MSEC failed
            frame_idx = int(round(sec * self.fps))
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ret, frame = self._cap.read()

        if not ret or frame is None:
            return False, None

        return True, self._frame_to_image(frame)

    def seek_frame(self, frame_idx: int) -> tuple[bool, Optional[Image.Image]]:
        """Seek to a specific frame index and return the decoded PIL Image."""
        if not self.is_opened:
            return False, None
        frame_idx = max(0, min(frame_idx, max(0, self.frame_count - 1)))
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ret, frame = self._cap.read()
        if not ret or frame is None:
            return False, None
        self.current_sec = frame_idx / self.fps if self.fps > 0 else 0.0
        return True, self._frame_to_image(frame)

    def read_frame(self) -> tuple[bool, float, Optional[Image.Image]]:
        """Read the next sequential frame (used for playback loop)."""
        if not self.is_opened:
            return False, self.current_sec, None

        ret, frame = self._cap.read()
        if not ret or frame is None:
            return False, self.current_sec, None

        pos_msec = self._cap.get(cv2.CAP_PROP_POS_MSEC)
        if pos_msec >= 0:
            self.current_sec = pos_msec / 1000.0
        else:
            self.current_sec += 1.0 / self.fps

        return True, self.current_sec, self._frame_to_image(frame)

    def _frame_to_image(self, frame: np.ndarray) -> Image.Image:
        """Convert BGR OpenCV frame array to RGB PIL Image."""
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        return Image.fromarray(rgb)

    def close(self) -> None:
        """Release video capture resources."""
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None

