# converter.py
import os
import sys
import asyncio
import subprocess
from asyncio.subprocess import PIPE

# Use a user-writable folder for output when packaged
if getattr(sys, 'frozen', False):
    OUTPUT_FOLDER = os.path.join(
        os.environ["USERPROFILE"], "Documents", "Vidium Output")
else:
    OUTPUT_FOLDER = os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "Output")


def get_ffmpeg_path() -> str:
    import sys
    if getattr(sys, 'frozen', False):
        # When packaged, use the location of the executable and the _internal folder.
        base_path = os.path.dirname(sys.executable)
        ffmpeg_bin = os.path.join(
            base_path, "_internal", "ffmpeg", "ffmpeg.exe")
    else:
        # When running from source.
        script_dir = os.path.dirname(os.path.abspath(__file__))
        ffmpeg_bin = os.path.join(script_dir, "ffmpeg", "ffmpeg.exe")
    if not os.path.exists(ffmpeg_bin):
        raise FileNotFoundError(
            f"FFmpeg executable not found at {ffmpeg_bin}. Please bundle FFmpeg correctly."
        )
    return ffmpeg_bin


def get_ffprobe_path() -> str:
    import sys
    if getattr(sys, 'frozen', False):
        base_path = os.path.dirname(sys.executable)
        ffprobe_bin = os.path.join(
            base_path, "_internal", "ffmpeg", "ffprobe.exe")
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        ffprobe_bin = os.path.join(script_dir, "ffmpeg", "ffprobe.exe")
    if not os.path.exists(ffprobe_bin):
        raise FileNotFoundError(
            f"FFprobe executable not found at {ffprobe_bin}. Please bundle FFprobe correctly."
        )
    return ffprobe_bin


def get_input_bitrate(input_file: str) -> int:
    ffprobe_path = get_ffprobe_path()
    cmd = [ffprobe_path, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=bit_rate", "-of", "default=noprint_wrappers=1:nokey=1", input_file]
    result = subprocess.run(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    bitrate_str = result.stdout.strip()
    if bitrate_str:
        try:
            bitrate = int(bitrate_str)
            if bitrate > 0:
                return bitrate
        except ValueError:
            pass
    cmd = [ffprobe_path, "-v", "error", "-show_entries", "format=bit_rate",
           "-of", "default=noprint_wrappers=1:nokey=1", input_file]
    result = subprocess.run(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
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
    ffmpeg_path = get_ffmpeg_path()
    cmd = [ffmpeg_path] + args
    print(f"Running command: {' '.join(cmd)}")
    process = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE,
                                                   creationflags=subprocess.CREATE_NO_WINDOW)
    if stop_event is None:
        stdout, stderr = await process.communicate()
        if stdout:
            print(f"[stdout]\n{stdout.decode()}")
        if stderr:
            print(f"[stderr]\n{stderr.decode()}", file=sys.stderr)
        return process.returncode
    else:
        wait_tasks = [asyncio.create_task(process.communicate()),
                      asyncio.create_task(stop_event.wait())]
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
    if extra_args is None:
        extra_args = ["-c:v", "libx264", "-preset", "fast", "-crf", "23"]
    if use_gpu:
        pix_fmt = None
        if "-pix_fmt" in extra_args:
            idx = extra_args.index("-pix_fmt")
            if idx + 1 < len(extra_args):
                pix_fmt = extra_args[idx+1]
        hwaccel_format = "p010le" if pix_fmt == "p010le" else "nv12"
        gpu_flags = ["-hwaccel", "cuda",
                     "-hwaccel_output_format", hwaccel_format]
        args = ["-y"] + gpu_flags + ["-i", input_file] + \
            extra_args + [output_file]
    else:
        args = ["-y", "-i", input_file] + extra_args + [output_file]
    full_cmd = [get_ffmpeg_path()] + args
    print(f"Running command: {' '.join(full_cmd)}")
    process = await asyncio.create_subprocess_exec(*full_cmd, stdout=PIPE, stderr=PIPE,
                                                   creationflags=subprocess.CREATE_NO_WINDOW)
    if stop_event is None:
        stdout, stderr = await process.communicate()
        log = ""
        if stdout:
            log += f"[stdout]\n{stdout.decode()}\n"
        if stderr:
            log += f"[stderr]\n{stderr.decode()}\n"
        if process.returncode != 0:
            raise RuntimeError(
                f"Conversion failed for {input_file} (return code {process.returncode}). Log: {log}")
        print(f"Conversion completed: {output_file}")
        return log
    else:
        wait_tasks = [asyncio.create_task(process.communicate()),
                      asyncio.create_task(stop_event.wait())]
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
                    f"Conversion failed for {input_file} (return code {process.returncode}). Log: {log}")
            print(f"Conversion completed: {output_file}")
            return log


def is_video_10bit(input_file: str) -> bool:
    ffprobe_path = get_ffprobe_path()
    cmd = [ffprobe_path, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=pix_fmt", "-of", "default=noprint_wrappers=1:nokey=1", input_file]
    result = subprocess.run(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    pix_fmt = result.stdout.strip()
    return "10le" in pix_fmt
