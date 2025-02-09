import os
import asyncio
import sys
import subprocess
from asyncio.subprocess import PIPE

OUTPUT_FOLDER = r"C:\Users\fbb92\OneDrive\Documents\Projects\Vidium\Output"


def get_ffmpeg_path() -> str:
    r"""
    Returns the absolute path to the bundled FFmpeg executable.
    Expects FFmpeg to be located in the 'ffmpeg' folder inside the project directory.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    ffmpeg_bin = os.path.join(script_dir, "ffmpeg", "ffmpeg.exe")
    if not os.path.exists(ffmpeg_bin):
        raise FileNotFoundError(
            f"FFmpeg executable not found at {ffmpeg_bin}. "
            "Please bundle FFmpeg in the 'ffmpeg' folder within your project directory."
        )
    return ffmpeg_bin


def get_ffprobe_path() -> str:
    r"""
    Returns the absolute path to the bundled FFprobe executable.
    Expects FFprobe to be located in the 'ffmpeg' folder inside the project directory.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    ffprobe_bin = os.path.join(script_dir, "ffmpeg", "ffprobe.exe")
    if not os.path.exists(ffprobe_bin):
        raise FileNotFoundError(
            f"FFprobe executable not found at {ffprobe_bin}. "
            "Please bundle FFprobe in the 'ffmpeg' folder within your project directory."
        )
    return ffprobe_bin


def get_input_bitrate(input_file: str) -> int:
    """
    Uses ffprobe to get the video bitrate of the input file.
    Returns the bitrate in bits per second.
    
    First it tries to retrieve the video stream’s bitrate; if that fails, it falls back
    to the overall format bitrate.
    """
    ffprobe_path = get_ffprobe_path()
    # Try video stream bitrate
    cmd = [
        ffprobe_path, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=bit_rate", "-of", "default=noprint_wrappers=1:nokey=1",
        input_file
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    bitrate_str = result.stdout.strip()
    if bitrate_str:
        try:
            bitrate = int(bitrate_str)
            if bitrate > 0:
                return bitrate
        except ValueError:
            pass
    # Fallback to overall container bitrate
    cmd = [
        ffprobe_path, "-v", "error",
        "-show_entries", "format=bit_rate", "-of", "default=noprint_wrappers=1:nokey=1",
        input_file
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    bitrate_str = result.stdout.strip()
    if bitrate_str:
        try:
            bitrate = int(bitrate_str)
            if bitrate > 0:
                return bitrate
        except ValueError:
            return None
    return None


async def run_ffmpeg(args: list) -> int:
    r"""
    Executes an FFmpeg command asynchronously.
    
    :param args: List of command-line arguments (excluding the ffmpeg executable)
    :return: The process return code
    """
    ffmpeg_path = get_ffmpeg_path()
    cmd = [ffmpeg_path] + args
    print(f"Running command: {' '.join(cmd)}")
    process = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
    stdout, stderr = await process.communicate()
    if stdout:
        print(f"[stdout]\n{stdout.decode()}")
    if stderr:
        print(f"[stderr]\n{stderr.decode()}", file=sys.stderr)
    return process.returncode


async def convert_file(input_file: str, output_file: str, extra_args: list = None) -> str:
    r"""
    Converts an input file to any desired format.
    
    This is a generic conversion function. If extra_args is provided,
    the FFmpeg command will be:
      ffmpeg -y -i input_file <extra_args> output_file
    Otherwise, a default conversion is applied (re-encoding with libx264).
    
    Returns the FFmpeg log output as a string.
    """
    if extra_args is None:
        extra_args = ["-c:v", "libx264", "-preset", "fast", "-crf", "23"]
    args = ["-y", "-i", input_file] + extra_args + [output_file]
    ffmpeg_path = get_ffmpeg_path()
    cmd = [ffmpeg_path] + args
    print(f"Running command: {' '.join(cmd)}")
    process = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
    stdout, stderr = await process.communicate()
    log = ""
    if stdout:
        log += f"[stdout]\n{stdout.decode()}\n"
    if stderr:
        log += f"[stderr]\n{stderr.decode()}\n"
    if process.returncode != 0:
        raise RuntimeError(
            f"Conversion failed for {input_file} (return code {process.returncode}). Log: {log}"
        )
    print(f"Conversion completed: {output_file}")
    return log

# For quick manual testing from the command line.
if __name__ == "__main__":
    async def main():
        # Replace this with an actual test video file.
        test_video = r"C:\Users\fbb92\OneDrive\Documents\Projects\Vidium\dang.webm"

        base = os.path.splitext(os.path.basename(test_video))[0]

        # Example: Convert to GIF (by specifying video filter parameters).
        gif_output = os.path.join(OUTPUT_FOLDER, base + ".gif")
        try:
            print("Starting conversion to GIF...")
            log = await convert_file(test_video, gif_output, extra_args=["-vf", "fps=10,scale=320:-1:flags=lanczos"])
            print(log)
        except Exception as e:
            print(f"Error during GIF conversion: {e}", file=sys.stderr)

        # Example: Generic video conversion to MP4 (using default extra_args).
        mp4_output = os.path.join(OUTPUT_FOLDER, base + ".mp4")
        try:
            print("Starting conversion to MP4...")
            log = await convert_file(test_video, mp4_output)
            print(log)
        except Exception as e:
            print(f"Error during MP4 conversion: {e}", file=sys.stderr)

        # Example: Audio extraction to MP3.
        mp3_output = os.path.join(OUTPUT_FOLDER, base + ".mp3")
        try:
            print("Starting audio extraction...")
            log = await convert_file(test_video, mp3_output, extra_args=["-q:a", "0", "-map", "a"])
            print(log)
        except Exception as e:
            print(f"Error during audio extraction: {e}", file=sys.stderr)

    asyncio.run(main())
