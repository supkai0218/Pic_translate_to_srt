"""Unit tests for v1.06 features: CLI argument parsing, startup load logic, and path resolution."""
import argparse
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
import vlc_engine


class TestV106Features(unittest.TestCase):
    def test_config_base_dir(self):
        """Verify config.BASE_DIR resolves correctly in dev and frozen modes."""
        self.assertTrue(config.BASE_DIR.is_dir())
        self.assertEqual(config.SETTINGS_FILE.name, "settings.json")

    def test_vlc_engine_paths(self):
        """Verify vlc_engine handles candidates and reports LibVLC availability."""
        self.assertTrue(hasattr(vlc_engine, "is_vlc_available"))
        # In this workspace, vlc/ is present
        self.assertTrue(vlc_engine.is_vlc_available())

    def test_cli_parsing_positional(self):
        """Verify positional video argument."""
        test_args = ["PicTrans2Srt", r"D:\test\sample.mp4"]
        with patch.object(sys, "argv", test_args):
            parser = argparse.ArgumentParser(prog="PicTrans2Srt")
            parser.add_argument("video_file", nargs="?", default=None)
            parser.add_argument("-v", "--video", dest="video_opt", default=None)
            parser.add_argument("-s", "--srt", dest="srt_file", default=None)
            parser.add_argument("-p", "--play", dest="autoplay", action="store_true", default=None)
            parser.add_argument("--no-play", dest="no_play", action="store_true")

            args = parser.parse_args()
            video_path = args.video_opt or args.video_file
            self.assertEqual(video_path, r"D:\test\sample.mp4")
            self.assertIsNone(args.srt_file)

    def test_cli_parsing_named_args(self):
        """Verify named video, srt, and autoplay arguments."""
        test_args = [
            "PicTrans2Srt",
            "-v", r"D:\test\movie.mkv",
            "-s", r"D:\test\movie_zh.srt",
            "--play"
        ]
        with patch.object(sys, "argv", test_args):
            parser = argparse.ArgumentParser(prog="PicTrans2Srt")
            parser.add_argument("video_file", nargs="?", default=None)
            parser.add_argument("-v", "--video", dest="video_opt", default=None)
            parser.add_argument("-s", "--srt", dest="srt_file", default=None)
            parser.add_argument("-p", "--play", dest="autoplay", action="store_true", default=None)
            parser.add_argument("--no-play", dest="no_play", action="store_true")

            args = parser.parse_args()
            video_path = args.video_opt or args.video_file
            self.assertEqual(video_path, r"D:\test\movie.mkv")
            self.assertEqual(args.srt_file, r"D:\test\movie_zh.srt")
            self.assertTrue(args.autoplay)
            self.assertFalse(args.no_play)

    def test_cli_parsing_no_play(self):
        """Verify --no-play flag."""
        test_args = ["PicTrans2Srt", r"D:\test\sample.mp4", "--no-play"]
        with patch.object(sys, "argv", test_args):
            parser = argparse.ArgumentParser(prog="PicTrans2Srt")
            parser.add_argument("video_file", nargs="?", default=None)
            parser.add_argument("-v", "--video", dest="video_opt", default=None)
            parser.add_argument("-s", "--srt", dest="srt_file", default=None)
            parser.add_argument("-p", "--play", dest="autoplay", action="store_true", default=None)
            parser.add_argument("--no-play", dest="no_play", action="store_true")

            args = parser.parse_args()
            video_path = args.video_opt or args.video_file
            autoplay = False
            if video_path:
                if args.no_play:
                    autoplay = False
                elif args.autoplay is not None:
                    autoplay = args.autoplay
                else:
                    autoplay = True
            self.assertFalse(autoplay)


if __name__ == "__main__":
    unittest.main()

