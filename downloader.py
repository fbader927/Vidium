import os
import time
from PySide6.QtCore import QThread, Signal


def detect_video_source(url: str) -> str:
    """
    Detects the video source based on the URL.
    Returns 'youtube', 'reddit', 'twitter', or 'unknown'.
    """
    url_lower = url.lower()
    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        return "youtube"
    elif "reddit.com" in url_lower or "redd.it" in url_lower or "v.redd.it" in url_lower:
        return "reddit"
    elif "twitter.com" in url_lower or "t.co" in url_lower or "twimg" in url_lower or "x.com" in url_lower:
        return "twitter"
    else:
        return "unknown"


class DownloadWorker(QThread):
    finished = Signal(str, str)  # Emits a message and the downloaded file path
    error = Signal(str)
    progress = Signal(int)       # Signal for progress updates

    def __init__(self, url, output_folder):
        super().__init__()
        self.url = url
        self.output_folder = output_folder

    def run(self):
        try:
            import yt_dlp
            os.makedirs(self.output_folder, exist_ok=True)
            source = detect_video_source(self.url)
            print(f"Detected video source: {source}")
            ydl_opts = {
                'outtmpl': os.path.join(self.output_folder, '%(title).100s.%(ext)s'),
                'format': 'bestvideo+bestaudio/best',
                'noplaylist': True,
                'restrictfilenames': True,
                'progress_hooks': [self.download_hook]
            }
            if source == "reddit":
                ydl_opts.update({'merge_output_format': 'mp4'})
            elif source == "twitter":
                ydl_opts.update({
                    'merge_output_format': 'mp4',
                    'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
                })
            if source == "twitter":
                max_retries = 3
                attempt = 0
                while attempt < max_retries:
                    try:
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            info = ydl.extract_info(self.url, download=True)
                            downloaded_file = ydl.prepare_filename(info)
                        break
                    except Exception as e:
                        if "Failed to parse JSON" in str(e) and attempt < max_retries - 1:
                            attempt += 1
                            time.sleep(1)
                            continue
                        else:
                            raise e
            else:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(self.url, download=True)
                    downloaded_file = ydl.prepare_filename(info)
            self.finished.emit(
                "Download completed successfully.", downloaded_file)
        except Exception as e:
            error_msg = str(e)
            if "Failed to parse JSON" in error_msg:
                error_msg += ("\nThis error is typically caused by changes in Twitter's API or a temporary issue with the service. "
                              "Ensure you are using the latest version of yt-dlp (yt-dlp -U) and consider reporting this issue if it persists.")
            self.error.emit(error_msg)

    def download_hook(self, d):
        if d.get('status') == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            downloaded = d.get('downloaded_bytes', 0)
            if total:
                progress_percent = int(downloaded / total * 100)
                self.progress.emit(progress_percent)
        elif d.get('status') == 'finished':
            self.progress.emit(100)


def format_timestamp(time_str):
    """
    Converts a time string in HH:MM:SS:MS format to a compact timestamp.
    If hours and minutes are zero, returns "SS_MS" (e.g. "40_00").
    Otherwise, if hours are zero, returns "MM_SS_MS" (e.g. "01_40_00").
    If hours are nonzero, returns "HH_MM_SS".
    """
    parts = time_str.split(":")
    if len(parts) == 4:
        hh, mm, ss, ms = parts
        if hh == "00" and mm == "00":
            return f"{ss}_{ms}"
        elif hh == "00":
            return f"{mm}_{ss}_{ms}"
        else:
            return f"{hh}_{mm}_{ss}"
    elif len(parts) == 3:
        hh, mm, ss = parts
        if hh == "00":
            return f"{mm}_{ss}"
        else:
            return f"{hh}_{mm}_{ss}"
    else:
        return time_str.replace(":", "_")


class TrimWorker(QThread):
    finished = Signal(str, str)  # Emits a message and the (trimmed) file path
    error = Signal(str)
    progress = Signal(int)

    def __init__(self, input_file, start_time, end_time, use_gpu=False, delete_original=True, output_folder=None):
        super().__init__()
        self.input_file = input_file
        self.start_time = start_time
        self.end_time = end_time
        self.use_gpu = use_gpu
        self.delete_original = delete_original
        # If provided, trimmed file will be placed here.
        self.output_folder = output_folder

    def run(self):
        try:
            start_seconds = self._time_to_seconds(self.start_time)
            end_seconds = self._time_to_seconds(self.end_time)
            if start_seconds is None or end_seconds is None:
                self.error.emit("Invalid time format. Please use HH:MM:SS:MS")
                return
            if start_seconds >= end_seconds:
                self.error.emit("Start time must be less than end time.")
                return
            duration = self._get_video_duration(self.input_file)
            if duration is None:
                self.error.emit(
                    "Could not retrieve video duration for validation.")
                return
            if end_seconds > duration:
                self.error.emit("End time is out of bounds.")
                return
            ffmpeg_start = self._format_time_for_ffmpeg(self.start_time)
            trim_duration = end_seconds - start_seconds
            # Use only the basename (filename without directory) for output naming
            base, ext = os.path.splitext(os.path.basename(self.input_file))
            if self.use_gpu:
                if ext.lower() == ".webm":
                    out_ext = ".mp4"
                else:
                    out_ext = ext
                encoder_args = ["-c:v", "h264_nvenc", "-preset", "fast"]
            else:
                if ext.lower() == ".webm":
                    out_ext = ext
                    encoder_args = ["-c:v", "libvpx-vp9",
                                    "-crf", "30", "-b:v", "0"]
                else:
                    out_ext = ext
                    encoder_args = ["-c:v", "libx264",
                                    "-crf", "18", "-preset", "veryfast"]
            if out_ext in [".mp4", ".mkv"]:
                audio_args = ["-c:a", "aac", "-b:a", "192k"]
            elif out_ext == ".webm":
                audio_args = ["-c:a", "libopus", "-b:a", "128k"]
            else:
                audio_args = ["-c:a", "copy"]
            # Build a temporary output path in the same folder as the input file
            temp_output = os.path.join(os.path.dirname(
                self.input_file), base + "_temp" + out_ext)
            import subprocess
            ffmpeg_path = self._get_ffmpeg_path()
            if self.use_gpu:
                gpu_flags = ["-hwaccel", "cuda",
                             "-hwaccel_output_format", "nv12"]
                cmd = [ffmpeg_path, "-y"] + gpu_flags + ["-i", self.input_file, "-ss", ffmpeg_start, "-t", str(
                    trim_duration)] + encoder_args + audio_args + ["-avoid_negative_ts", "make_zero", temp_output]
            else:
                cmd = [ffmpeg_path, "-y", "-i", self.input_file, "-ss", ffmpeg_start, "-t", str(
                    trim_duration)] + encoder_args + audio_args + ["-avoid_negative_ts", "make_zero", temp_output]
            process = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if process.returncode != 0:
                err = process.stderr.decode()
                self.error.emit(f"Trimming failed: {err}")
                return
            ts_start = format_timestamp(self.start_time)
            ts_end = format_timestamp(self.end_time)
            suffix = f"_{ts_start}_to_{ts_end}"
            max_base_length = 100
            if len(base) + len(suffix) > max_base_length:
                base = base[:max_base_length - len(suffix)]
            new_filename = base + suffix + out_ext
            # If an output folder is specified, use it; otherwise use the input file's directory.
            dest_dir = self.output_folder if self.output_folder is not None else os.path.dirname(
                self.input_file)
            new_filepath = os.path.join(dest_dir, new_filename)
            if self.delete_original:
                try:
                    os.remove(self.input_file)
                except Exception as e:
                    self.error.emit(f"Failed to remove original file: {e}")
                    return
            os.rename(temp_output, new_filepath)
            self.finished.emit(
                "Trimming completed successfully.", new_filepath)
        except Exception as e:
            self.error.emit(str(e))

    def _time_to_seconds(self, time_str):
        try:
            parts = time_str.split(":")
            if len(parts) == 3:
                hours = float(parts[0])
                minutes = float(parts[1])
                seconds = float(parts[2])
                return hours * 3600 + minutes * 60 + seconds
            elif len(parts) == 4:
                hours = float(parts[0])
                minutes = float(parts[1])
                seconds = float(parts[2])
                millis = float(parts[3])
                return hours * 3600 + minutes * 60 + seconds + millis / 1000.0
            else:
                return None
        except:
            return None

    def _format_time_for_ffmpeg(self, time_str):
        parts = time_str.split(":")
        if len(parts) == 4:
            return ":".join(parts[:3]) + "." + parts[3]
        return time_str

    def _get_video_duration(self, file_path):
        try:
            import subprocess
            ffprobe_path = self._get_ffprobe_path()
            cmd = [ffprobe_path, "-v", "error", "-select_streams", "v:0",
                   "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file_path]
            result = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            duration_str = result.stdout.strip()
            if duration_str:
                return float(duration_str)
            return None
        except Exception:
            return None

    def _get_ffmpeg_path(self):
        from converter import get_ffmpeg_path
        return get_ffmpeg_path()

    def _get_ffprobe_path(self):
        from converter import get_ffprobe_path
        return get_ffprobe_path()
