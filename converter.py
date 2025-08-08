import os
import sys
import asyncio
import subprocess
from asyncio.subprocess import PIPE
from typing import Optional

# set output folder: uses user 'Documents' folder for packaged release, or local 'Output' folder for development
if getattr(sys, 'frozen', False):
    OUTPUT_FOLDER = os.path.join(
        os.environ["USERPROFILE"], "Documents", "Vidium Output")
else:
    OUTPUT_FOLDER = os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "Output")

# get ffmpeg path: points to bundled ffmpeg in packaged release or local ffmpeg folder if in development
def get_ffmpeg_path() -> str:
    if getattr(sys, 'frozen', False):
        base_path = os.path.dirname(sys.executable)
        ffmpeg_bin = os.path.join(
            base_path, "_internal", "ffmpeg", "ffmpeg.exe")
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        ffmpeg_bin = os.path.join(script_dir, "ffmpeg", "ffmpeg.exe")
    if not os.path.exists(ffmpeg_bin):
        raise FileNotFoundError(
            f"FFmpeg executable not found at {ffmpeg_bin}. Please bundle FFmpeg correctly."
        )
    return ffmpeg_bin

# get ffprobe path: uses bundled ffprobe in packaged release or local ffmpeg folder in development
def get_ffprobe_path() -> str:
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

# get input bitrate using ffprobe: tries stream bitrate first, fallsback to format bitrate 
def get_input_bitrate(input_file: str) -> Optional[int]:
    ffprobe_path = get_ffprobe_path()
    # attempt to get video stream bitrate
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
    #fallback to get overall format bitrate if stream bitrate fails
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

# run ffmpeg processes (required for conversions/trimming) ran in async threads for multi-batch processing
async def run_ffmpeg(args: list, stop_event: asyncio.Event = None) -> int:
    ffmpeg_path = get_ffmpeg_path()
    cmd = [ffmpeg_path] + args
    print(f"Running command: {' '.join(cmd)}")
    # start ffmpeg process
    process = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE,
                                                   creationflags=subprocess.CREATE_NO_WINDOW) 
    if stop_event is None:  # run normally if no stop event
        stdout, stderr = await process.communicate()
        if stdout:  
            print(f"[stdout]\n{stdout.decode()}")
        if stderr:
            print(f"[stderr]\n{stderr.decode()}", file=sys.stderr)
        return process.returncode
    else:  # allow process to be stopped using stop_event
        wait_tasks = [asyncio.create_task(process.communicate()),
                      asyncio.create_task(stop_event.wait())]
        done, pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
        if wait_tasks[1] in done:
            process.kill()  # stop_event triggered, kill process
            await process.wait()
            for t in pending:
                t.cancel()
            return -1  # signal stop
        else:
            for t in pending:  # process finished first
                t.cancel()
            stdout, stderr = wait_tasks[0].result()
            if stdout:
                print(f"[stdout]\n{stdout.decode()}")
            if stderr:
                print(f"[stderr]\n{stderr.decode()}", file=sys.stderr)
            return process.returncode


async def convert_file(input_file: str, output_file: str, extra_args: list = None, use_gpu: bool = False, stop_event: asyncio.Event = None) -> str:
    if extra_args is None: # set default ffmpeg args if none are given
        extra_args = ["-c:v", "libx264", "-preset", "fast", "-crf", "23"]
    if use_gpu:
        pix_fmt = None # check for pixel format to set hardware accel
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
    full_cmd = [get_ffmpeg_path()] + args                  # build and run ffmpeg command 
    print(f"Running command: {' '.join(full_cmd)}")
    process = await asyncio.create_subprocess_exec(*full_cmd, stdout=PIPE, stderr=PIPE,
                                                   creationflags=subprocess.CREATE_NO_WINDOW)
    if stop_event is None: # run conversion normally
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
    else: # handle stop_event for cancellation
        wait_tasks = [asyncio.create_task(process.communicate()),
                      asyncio.create_task(stop_event.wait())]
        done, pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
        if wait_tasks[1] in done: # stop_event triggered, kill process and cleanup
            process.kill()
            await process.wait()
            for t in pending:
                t.cancel()
            if os.path.exists(output_file):
                os.remove(output_file)
            raise asyncio.CancelledError("Conversion was stopped.")
        else: # process finished before stop_event
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
    # get pixel format of first video stream
    cmd = [ffprobe_path, "-v", "error", "-select_streams", "v:0", 
           "-show_entries", "stream=pix_fmt", "-of", "default=noprint_wrappers=1:nokey=1", input_file]
    # run ffprobe and get output
    result = subprocess.run(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    pix_fmt = result.stdout.strip() # check if pixel format contains '10le' (used for 10-bit vids)
    return "10le" in pix_fmt
