# converter.py
import os
import asyncio
import sys
import subprocess
from asyncio.subprocess import PIPE

OUTPUT_FOLDER = r"C:\Users\fbb92\OneDrive\Documents\Projects\Vidium\Output"


def get_ffmpeg_path() -> str:
    r"""
    Returns the absolute path to the bundled FFmpeg executable.
    Expects FFmpeg to be located in the 'ffmpeg' folder inside your project directory.
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
    Expects FFprobe to be located in the 'ffmpeg' folder inside your project directory.
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


async def run_ffmpeg(args: list, stop_event: asyncio.Event = None) -> int:
    r"""
    Executes an FFmpeg command asynchronously with an optional stop event.
    
    :param args: List of command-line arguments (excluding the ffmpeg executable).
    :param stop_event: An asyncio.Event that when set will abort the conversion.
    :return: The process return code, or -1 if cancelled.
    """
    ffmpeg_path = get_ffmpeg_path()
    cmd = [ffmpeg_path] + args
    print(f"Running command: {' '.join(cmd)}")
    process = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
    if stop_event is None:
        stdout, stderr = await process.communicate()
        if stdout:
            print(f"[stdout]\n{stdout.decode()}")
        if stderr:
            print(f"[stderr]\n{stderr.decode()}", file=sys.stderr)
        return process.returncode
    else:
        wait_tasks = [
            asyncio.create_task(process.communicate()),
            asyncio.create_task(stop_event.wait())
        ]
        done, pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
        if wait_tasks[1] in done:
            process.kill()
            await process.wait()
            for t in pending:
                t.cancel()
            return -1
        else:
            for t in pending:
                t.cancel()
            stdout, stderr = wait_tasks[0].result()
            if stdout:
                print(f"[stdout]\n{stdout.decode()}")
            if stderr:
                print(f"[stderr]\n{stderr.decode()}", file=sys.stderr)
            return process.returncode


async def convert_file(input_file: str, output_file: str, extra_args: list = None, use_gpu: bool = False, stop_event: asyncio.Event = None) -> str:
    r"""
    Converts an input file to any desired format asynchronously, with support for cancellation.
    
    Returns the FFmpeg log output as a string.
    
    When use_gpu is True, GPU acceleration is enabled:
      - Decoding uses CUDA with output format "nv12"
      - If a video filter (-vf) is provided in extra_args, it is processed so that any "scale=" calls are replaced with "scale_npp=" and ",format=nv12" is appended.
    """
    if extra_args is None:
        extra_args = ["-c:v", "libx264", "-preset", "fast", "-crf", "23"]
    if use_gpu:
        gpu_flags = ["-hwaccel", "cuda", "-hwaccel_output_format", "nv12"]
        args = ["-y"] + gpu_flags + ["-i", input_file] + \
            extra_args + [output_file]
    else:
        args = ["-y", "-i", input_file] + extra_args + [output_file]
    ffmpeg_path = get_ffmpeg_path()
    cmd = [ffmpeg_path] + args
    print(f"Running command: {' '.join(cmd)}")
    process = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
    if stop_event is None:
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
    else:
        wait_tasks = [
            asyncio.create_task(process.communicate()),
            asyncio.create_task(stop_event.wait())
        ]
        done, pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
        if wait_tasks[1] in done:
            process.kill()
            await process.wait()
            for t in pending:
                t.cancel()
            if os.path.exists(output_file):
                os.remove(output_file)
            raise asyncio.CancelledError("Conversion was stopped.")
        else:
            for t in pending:
                t.cancel()
            stdout, stderr = wait_tasks[0].result()
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

        # Example: Convert to GIF (GPU enabled for maximum throughput/quality)
        gif_output = os.path.join(OUTPUT_FOLDER, base + ".gif")
        try:
            print("Starting conversion to GIF with GPU...")
            # If GPU is enabled, add GPU flags and use the CUDA-accelerated scaling filter.
            # Note: There is no native GPU encoder for GIF, but we can use GPU for decoding and scaling.
            # The palette generation and gif conversion commands below use "-hwaccel cuda" and "scale_npp".
            if True:  # This branch is taken when GPU is enabled for GIF conversion.
                palette_args = [
                    "-y",
                    "-hwaccel", "cuda",
                    "-hwaccel_output_format", "nv12",
                    "-i", test_video,
                    "-vf", "fps=10,scale_npp=320:-1,palettegen",
                    os.path.join(OUTPUT_FOLDER, "palette_temp.png")
                ]
                ret = await run_ffmpeg(palette_args)
                if ret != 0:
                    raise RuntimeError(
                        "Palette generation for GIF failed (GPU).")
                gif_args = [
                    "-y",
                    "-hwaccel", "cuda",
                    "-hwaccel_output_format", "nv12",
                    "-i", test_video,
                    "-i", os.path.join(OUTPUT_FOLDER, "palette_temp.png"),
                    "-filter_complex", "fps=10,scale_npp=320:-1[p];[p][1:v]paletteuse",
                    gif_output
                ]
                log = await run_ffmpeg(gif_args)
                # Clean up temporary palette file
                palette_file = os.path.join(OUTPUT_FOLDER, "palette_temp.png")
                if os.path.exists(palette_file):
                    os.remove(palette_file)
            else:
                # CPU-based conversion (fallback)
                log = await convert_file(
                    test_video,
                    gif_output,
                    extra_args=["-vf", "fps=10,scale=320:-1:flags=lanczos"],
                    use_gpu=False
                )
            print(log)
        except Exception as e:
            print(f"Error during GIF conversion: {e}", file=sys.stderr)

        # Example: Generic video conversion to MP4 using GPU acceleration.
        mp4_output = os.path.join(OUTPUT_FOLDER, base + ".mp4")
        try:
            print("Starting conversion to MP4 with GPU...")
            gpu_extra_args = ["-r", "60", "-c:v",
                              "h264_nvenc", "-preset", "fast"]
            if test_video.lower().endswith(".webm"):
                gpu_extra_args += ["-tile-columns",
                                   "6", "-frame-parallel", "1"]
            log = await convert_file(
                test_video,
                mp4_output,
                extra_args=gpu_extra_args,
                use_gpu=True
            )
            print(log)
        except Exception as e:
            print(f"Error during MP4 conversion: {e}", file=sys.stderr)

        # Example: Audio extraction to MP3.
        mp3_output = os.path.join(OUTPUT_FOLDER, base + ".mp3")
        try:
            print("Starting audio extraction...")
            log = await convert_file(
                test_video,
                mp3_output,
                extra_args=["-q:a", "0", "-map", "a"],
                use_gpu=False  # Audio extraction does not use GPU.
            )
            print(log)
        except Exception as e:
            print(f"Error during audio extraction: {e}", file=sys.stderr)

    asyncio.run(main())
