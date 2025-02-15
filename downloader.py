import os
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
    progress = Signal(int)  # Signal for progress updates

    def __init__(self, url, output_folder):
        super().__init__()
        self.url = url
        self.output_folder = output_folder

    def run(self):
        try:
            import yt_dlp
            # Ensure the download folder exists
            os.makedirs(self.output_folder, exist_ok=True)

            source = detect_video_source(self.url)
            print(f"Detected video source: {source}")

            ydl_opts = {
                # Truncate the title to 100 characters to avoid overly long filenames
                'outtmpl': os.path.join(self.output_folder, '%(title).100s.%(ext)s'),
                'format': 'bestvideo+bestaudio/best',
                'noplaylist': True,  # download only a single video, not a playlist
                'restrictfilenames': True,  # ensure filenames contain only safe characters
                'progress_hooks': [self.download_hook]  # add progress hook
            }

            # Source-specific options:
            if source == "reddit":
                # For Reddit videos, ensure that video and audio are merged into an MP4 container.
                ydl_opts.update({
                    'merge_output_format': 'mp4'
                })
            elif source == "twitter":
                # For Twitter videos, ensure that video and audio are merged into an MP4 container.
                # Set a browser-like User-Agent to help avoid parsing issues.
                ydl_opts.update({
                    'merge_output_format': 'mp4',
                    'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
                })

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # extract_info with download=True both downloads and returns the info dict
                info = ydl.extract_info(self.url, download=True)
                downloaded_file = ydl.prepare_filename(info)
            self.finished.emit(
                "Download completed successfully.", downloaded_file)
        except Exception as e:
            error_msg = str(e)
            if "Failed to parse JSON" in error_msg:
                error_msg += "\nThis error is typically caused by changes in Twitter's API or a temporary issue with the service. Ensure you are using the latest version of yt-dlp (yt-dlp -U) and consider reporting this issue if it persists."
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


# --- New TrimWorker for trimming downloaded/converted files ---
class TrimWorker(QThread):
    finished = Signal(str, str)  # Emits a message and the (trimmed) file path
    error = Signal(str)
    progress = Signal(int)

    def __init__(self, input_file, start_time, end_time):
        super().__init__()
        self.input_file = input_file
        self.start_time = start_time
        self.end_time = end_time

    def run(self):
        try:
            # Convert time strings to seconds for validation
            start_seconds = self._time_to_seconds(self.start_time)
            end_seconds = self._time_to_seconds(self.end_time)
            if start_seconds is None or end_seconds is None:
                self.error.emit("Invalid time format. Please use HH:MM:SS:MS")
                return
            if start_seconds >= end_seconds:
                self.error.emit("Start time must be less than end time.")
                return

            # Get video duration for bounds checking
            duration = self._get_video_duration(self.input_file)
            if duration is None:
                self.error.emit(
                    "Could not retrieve video duration for validation.")
                return
            if end_seconds > duration:
                self.error.emit("End time is out of bounds.")
                return

            # Create a temporary output file name
            base, ext = os.path.splitext(self.input_file)
            temp_output = base + "_trimmed" + ext

            # Convert time strings to FFmpeg-friendly format (HH:MM:SS.MS)
            ffmpeg_start = self._format_time_for_ffmpeg(self.start_time)
            # Calculate duration for trimming (in seconds)
            trim_duration = end_seconds - start_seconds

            # Construct FFmpeg command for trimming (fast copy trim)
            # Updated command: place -ss before -i and use -t for duration
            import subprocess
            ffmpeg_path = self._get_ffmpeg_path()
            cmd = [
                ffmpeg_path, "-y",
                "-ss", ffmpeg_start,
                "-i", self.input_file,
                "-t", str(trim_duration),
                "-c", "copy",
                temp_output
            ]
            process = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if process.returncode != 0:
                err = process.stderr.decode()
                self.error.emit(f"Trimming failed: {err}")
                return

            # Replace the original file with the trimmed version
            try:
                os.remove(self.input_file)
            except Exception as e:
                self.error.emit(f"Failed to remove original file: {e}")
                return
            os.rename(temp_output, self.input_file)
            self.finished.emit(
                "Trimming completed successfully.", self.input_file)
        except Exception as e:
            self.error.emit(str(e))

    def _time_to_seconds(self, time_str):
        try:
            parts = time_str.split(":")
            if len(parts) == 3:
                # Support for HH:MM:SS format
                hours = float(parts[0])
                minutes = float(parts[1])
                seconds = float(parts[2])
                return hours * 3600 + minutes * 60 + seconds
            elif len(parts) == 4:
                # Support for HH:MM:SS:MS format (milliseconds)
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
        """
        Converts a time string from HH:MM:SS:MS format to HH:MM:SS.MS format.
        If the time string has three parts, returns it unchanged.
        """
        parts = time_str.split(":")
        if len(parts) == 4:
            return ":".join(parts[:3]) + "." + parts[3]
        return time_str

    def _get_video_duration(self, file_path):
        try:
            # Use FFprobe (bundled with FFmpeg) to get the video duration
            import subprocess
            ffprobe_path = self._get_ffprobe_path()
            cmd = [
                ffprobe_path, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                file_path
            ]
            result = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            duration_str = result.stdout.strip()
            if duration_str:
                return float(duration_str)
            return None
        except Exception:
            return None

    def _get_ffmpeg_path(self):
        # Reuse the bundled FFmpeg from converter.py
        from converter import get_ffmpeg_path
        return get_ffmpeg_path()

    def _get_ffprobe_path(self):
        from converter import get_ffprobe_path
        return get_ffprobe_path()
