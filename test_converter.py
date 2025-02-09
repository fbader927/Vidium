# File: test_converter.py
import os
import sys
import asyncio
import unittest
from converter import get_ffmpeg_path, run_ffmpeg


class TestFFmpegIntegration(unittest.TestCase):
    def test_ffmpeg_path_exists(self):
        """Test that the bundled FFmpeg executable is found."""
        ffmpeg_path = get_ffmpeg_path()
        self.assertTrue(os.path.exists(ffmpeg_path),
                        f"FFmpeg not found at {ffmpeg_path}")

    def test_ffmpeg_version(self):
        """
        Run 'ffmpeg -version' to verify that the bundled FFmpeg runs correctly.
        """
        async def run_version():
            ret = await run_ffmpeg(["-version"])
            return ret
        ret_code = asyncio.run(run_version())
        self.assertEqual(ret_code, 0, "ffmpeg -version did not return 0")


if __name__ == "__main__":
    unittest.main()
