"""Automated tests for v1.01 video OCR and subtitle generation features."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

import app
import llm_engine
import srt_utils
from video_engine import VideoEngine


class TestV101Features(unittest.TestCase):
    def setUp(self):
        # Create a temporary synthetic 3-second video
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.video_path = os.path.join(self.tmp_dir.name, "test_synth.mp4")
        
        width, height = 640, 360
        fps = 30.0
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(self.video_path, fourcc, fps, (width, height))
        
        for i in range(90):  # 90 frames = 3.0s
            frame = np.zeros((height, width, 3), dtype=np.uint8)
            # draw a colored rectangle
            cv2.rectangle(frame, (100, 100), (300, 200), (0, 255, 0), -1)
            cv2.putText(frame, f"Frame {i}", (50, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            writer.write(frame)
        writer.release()

    def tearDown(self):
        import gc
        gc.collect()
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_video_engine(self):
        engine = VideoEngine(self.video_path)
        self.assertTrue(engine.is_opened)
        self.assertEqual(engine.width, 640)
        self.assertEqual(engine.height, 360)
        self.assertAlmostEqual(engine.fps, 30.0, delta=0.5)
        self.assertAlmostEqual(engine.duration_sec, 3.0, delta=0.1)

        # Seek to 1.5s
        ok, frame_img = engine.seek_time(1.5)
        self.assertTrue(ok)
        self.assertIsNotNone(frame_img)
        self.assertEqual(frame_img.size, (640, 360))

        # Sequential read
        ok2, sec, next_img = engine.read_frame()
        self.assertTrue(ok2)
        self.assertIsNotNone(next_img)
        self.assertGreater(sec, 0.0)

        engine.close()
        self.assertFalse(engine.is_opened)

    def test_roi_cropping_and_task(self):
        engine = VideoEngine(self.video_path)
        ok, frame_img = engine.seek_time(1.0)
        self.assertTrue(ok)
        engine.close()

        # Full frame task
        task_full = app.SubtitleTask(
            id=1,
            start_sec=1.0,
            end_sec=3.5,
            start_frame_img=frame_img,
            crop_box=None,
            crop_img=frame_img,
        )
        self.assertEqual(task_full.duration, 2.5)
        self.assertEqual(task_full.start_ts, "00:00:01,000")
        self.assertEqual(task_full.end_ts, "00:00:03,500")
        self.assertEqual(task_full.crop_img.size, (640, 360))

        # Cropped ROI task (x1=100, y1=100, x2=300, y2=200)
        crop_box = (100, 100, 300, 200)
        cropped = frame_img.crop(crop_box)
        task_crop = app.SubtitleTask(
            id=2,
            start_sec=4.0,
            end_sec=6.25,
            start_frame_img=frame_img,
            crop_box=crop_box,
            crop_img=cropped,
        )
        self.assertEqual(task_crop.crop_img.size, (200, 100))
        self.assertAlmostEqual(task_crop.duration, 2.25)
        self.assertEqual(task_crop.start_ts, "00:00:04,000")
        self.assertEqual(task_crop.end_ts, "00:00:06,250")

    def test_llm_engine_encode_pil_image(self):
        test_img = Image.new("RGB", (800, 600), color=(120, 200, 255))
        b64, mime = llm_engine.encode_image(test_img, max_side=500)
        self.assertEqual(mime, "image/jpeg")
        self.assertIsInstance(b64, str)
        self.assertGreater(len(b64), 100)

    def test_srt_generation_from_tasks(self):
        trans = [
            (1.5, "第一個翻譯字幕", 2.0),
            (5.0, "第二個翻譯字幕", 3.25),
        ]
        entries = srt_utils.insert_translations(
            [], trans, mode="insert", duration_mode="per_subtitle"
        )
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["start"], 1.5)
        self.assertEqual(entries[0]["end"], 3.5)
        self.assertEqual(entries[0]["text"], "第一個翻譯字幕")

        self.assertEqual(entries[1]["start"], 5.0)
        self.assertEqual(entries[1]["end"], 8.25)
        self.assertEqual(entries[1]["text"], "第二個翻譯字幕")

        # Write and parse back
        out_srt = os.path.join(self.tmp_dir.name, "out.srt")
        srt_utils.write_srt(out_srt, entries)
        parsed = srt_utils.parse_srt(out_srt)
        self.assertEqual(len(parsed), 2)
        self.assertAlmostEqual(parsed[0]["start"], 1.5, delta=0.001)
        self.assertAlmostEqual(parsed[0]["end"], 3.5, delta=0.001)
        self.assertEqual(parsed[0]["text"], "第一個翻譯字幕")

    def test_parse_time_input(self):
        self.assertAlmostEqual(app.parse_time_input("00:01:23,456"), 83.456)
        self.assertAlmostEqual(app.parse_time_input("01:23.456"), 83.456)
        self.assertAlmostEqual(app.parse_time_input("45.5"), 45.5)
        self.assertAlmostEqual(app.parse_time_input("invalid", default=10.0), 10.0)

    def test_app_gui_workflow(self):
        a = app.App()
        try:
            a.update()
            a._load_video(self.video_path)
            a.update()
            self.assertEqual(a._video_label.cget("text"), Path(self.video_path).name)
            self.assertEqual(len(a._tasks), 0)

            # Set start & end, crop box, add task
            a._seek_to(0.5)
            a._set_as_start()
            a._seek_to(2.0)
            a._set_as_end()
            a._crop_box = (50, 50, 350, 250)
            a._add_task()
            a.update()

            self.assertEqual(len(a._tasks), 1)
            t = a._tasks[0]
            self.assertAlmostEqual(t.duration, 1.5, delta=0.01)
            self.assertEqual(t.crop_box, (50, 50, 350, 250))
            self.assertEqual(t.crop_img.size, (300, 200))

            # Add second task (full frame)
            a._seek_to(2.1)
            a._set_as_start()
            a._seek_to(2.9)
            a._set_as_end()
            a._crop_box = None
            a._add_task()
            a.update()

            self.assertEqual(len(a._tasks), 2)
            self.assertIsNone(a._tasks[1].crop_box)
            self.assertEqual(a._tasks[1].crop_img.size, (640, 360))

            # Test preview task
            a._preview_task(a._tasks[0])
            a.update()
            self.assertEqual(a._crop_box, (50, 50, 350, 250))

            # Test delete task
            a._delete_task(a._tasks[0].id)
            self.assertEqual(len(a._tasks), 1)
        finally:
            a.destroy()


if __name__ == "__main__":
    unittest.main()

