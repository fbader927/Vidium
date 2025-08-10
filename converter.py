import os
import sys
import asyncio
import subprocess
from asyncio.subprocess import PIPE
from typing import Optional, Callable

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

# get media duration in seconds using ffprobe (format duration)
def get_duration_seconds(input_file: str) -> Optional[float]:
    try:
        ffprobe_path = get_ffprobe_path()
        cmd = [ffprobe_path, "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", input_file]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        out = result.stdout.strip()
        if out:
            return float(out)
    except Exception:
        pass
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


async def convert_file(input_file: str, output_file: str, extra_args: list = None, use_gpu: bool = False, stop_event: asyncio.Event = None, progress_callback: Optional[Callable[[int], None]] = None, log_callback: Optional[Callable[[str], None]] = None) -> str:
    if extra_args is None: # set default ffmpeg args if none are given
        extra_args = ["-c:v", "libx264", "-preset", "fast", "-crf", "23"]
    progress_args = ["-progress", "pipe:1", "-nostats"]
    if use_gpu:
        pix_fmt = None # check for pixel format to set hardware accel
        if "-pix_fmt" in extra_args:
            idx = extra_args.index("-pix_fmt")
            if idx + 1 < len(extra_args):
                pix_fmt = extra_args[idx+1]
        hwaccel_format = "p010le" if pix_fmt == "p010le" else "nv12"
        gpu_flags = ["-hwaccel", "cuda",
                     "-hwaccel_output_format", hwaccel_format]
        args = ["-y"] + gpu_flags + progress_args + ["-i", input_file] + \
            extra_args + [output_file]
    else:
        args = ["-y"] + progress_args + ["-i", input_file] + extra_args + [output_file]
    full_cmd = [get_ffmpeg_path()] + args                  # build and run ffmpeg command
    print(f"Running command: {' '.join(full_cmd)}")

    # Prepare duration for progress estimation
    total_duration = get_duration_seconds(input_file)

    process = await asyncio.create_subprocess_exec(
        *full_cmd, stdout=PIPE, stderr=PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW
    )

    # Read stderr incrementally to extract time= and emit progress
    log_chunks: list[str] = []

    async def _read_stderr():
        import re
        time_re = re.compile(r"time=(\d{2}):(\d{2}):(\d{2})(?:[\.:](\d+))?")
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            s = line.decode(errors="ignore")
            log_chunks.append(s)
            if log_callback is not None:
                try:
                    log_callback(s)
                except Exception:
                    pass
            if progress_callback is not None and total_duration:
                m = time_re.search(s)
                if m:
                    hh, mm, ss, frac = m.groups()
                    secs = int(hh) * 3600 + int(mm) * 60 + int(ss)
                    if frac:
                        # Handle either .ms or :frames formatting by best effort
                        try:
                            secs += float(f"0.{frac}")
                        except Exception:
                            pass
                    pct = max(0, min(99, int((secs / float(total_duration)) * 100)))
                    try:
                        progress_callback(pct)
                    except Exception:
                        pass

    async def _read_stdout():
        import re
        # ffmpeg -progress keys: out_time_ms (microseconds, integer), out_time (HH:MM:SS.micro), progress=continue|end
        out_time_ms_re = re.compile(r"^out_time_ms=(\d+)")
        out_time_us_re = re.compile(r"^out_time_us=(\d+)")
        out_time_re = re.compile(r"^out_time=(\d{2}):(\d{2}):(\d{2})(?:[\.:](\d+))?")
        progress_re = re.compile(r"^progress=(\w+)")
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            s = line.decode(errors="ignore").strip()
            log_chunks.append(s)
            if log_callback is not None:
                try:
                    log_callback(s + "\n")
                except Exception:
                    pass
            if progress_callback is not None and total_duration:
                pct: Optional[int] = None
                m0 = out_time_ms_re.match(s) or out_time_us_re.match(s)
                if m0:
                    micros = int(m0.group(1))
                    secs = micros / 1_000_000.0
                    pct = int(min(99.0, max(0.0, (secs / float(total_duration)) * 100.0)))
                else:
                    m1 = out_time_re.match(s)
                    if m1:
                        hh, mm, ss, frac = m1.groups()
                        secs = int(hh) * 3600 + int(mm) * 60 + int(ss)
                        if frac:
                            try:
                                secs += float(f"0.{frac}")
                            except Exception:
                                pass
                        pct = int(min(99.0, max(0.0, (secs / float(total_duration)) * 100.0)))
                    else:
                        m2 = progress_re.match(s)
                        if m2 and m2.group(1) == "end":
                            pct = 100
                if pct is not None:
                    try:
                        progress_callback(pct)
                    except Exception:
                        pass

    if stop_event is None:
        await asyncio.gather(_read_stderr(), _read_stdout())
        await process.wait()
        log = "".join(log_chunks)
        if process.returncode != 0:
            raise RuntimeError(
                f"Conversion failed for {input_file} (return code {process.returncode}). Log: {log}")
        print(f"Conversion completed: {output_file}")
        # force 100% on completion
        if progress_callback is not None:
            try:
                progress_callback(100)
            except Exception:
                pass
        return log
    else:
        # With cancellation support
        reader = asyncio.create_task(_read_stderr())
        reader_out = asyncio.create_task(_read_stdout())
        stopper = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait(
            {reader, reader_out, stopper, asyncio.create_task(process.wait())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stopper in done:
            process.kill()
            await process.wait()
            for t in pending:
                t.cancel()
            if os.path.exists(output_file):
                os.remove(output_file)
            raise asyncio.CancelledError("Conversion was stopped.")
        # Ensure readers are finished
        await asyncio.gather(reader, reader_out, return_exceptions=True)
        log = "".join(log_chunks)
        if process.returncode != 0:
            raise RuntimeError(
                f"Conversion failed for {input_file} (return code {process.returncode}). Log: {log}")
        print(f"Conversion completed: {output_file}")
        if progress_callback is not None:
            try:
                progress_callback(100)
            except Exception:
                pass
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
