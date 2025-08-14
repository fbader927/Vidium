# downloader.py
import os
import time
from PySide6.QtCore import QThread, Signal
# For bitrate and 10-bit check
from converter import get_input_bitrate, is_video_10bit, get_ffmpeg_path


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
    logMessage = Signal(str)

    def __init__(self, url, output_folder):
        super().__init__()
        self.url = url
        self.output_folder = output_folder
        # Internal state for robust progress across multi-part (video+audio) and HLS downloads
        self._last_progress = 0
        self._current_filename = None
        self._parts_seen = set()
        self._parts_done = set()
        self._total_parts_est = 1
        self._observed_progress = False
        self._last_total_parts_est = 1

    def run(self):
        try:
            import yt_dlp
            os.makedirs(self.output_folder, exist_ok=True)
            source = detect_video_source(self.url)
            print(f"Detected video source: {source}")
            # Include ffmpeg_location so yt_dlp can merge formats
            ydl_opts = {
                'outtmpl': os.path.join(self.output_folder, '%(title).100s.%(ext)s'),
                'format': 'bestvideo*+bestaudio/best',
                'noplaylist': True,
                'restrictfilenames': True,
                'progress_hooks': [self.download_hook],
                'ffmpeg_location': os.path.dirname(get_ffmpeg_path()),
                # Force progress updates to be emitted; some environments suppress frequent prints
                'noprogress': False,
                'progress_with_newlines': True,
            }
            # Preflight: determine number of parts to download (video+audio) to avoid early 100% scaling
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl_probe:
                    info0 = ydl_probe.extract_info(self.url, download=False)
                    rf = info0.get('requested_formats')
                    fmt_str = ydl_opts.get('format', '') or ''
                    if isinstance(rf, (list, tuple)) and len(rf) > 1:
                        self._total_parts_est = len(rf)
                    else:
                        # Even if probe didn't split, we asked for bestvideo+bestaudio; be conservative
                        self._total_parts_est = 2 if ('+' in fmt_str) else 1
            except Exception:
                # Fallback if probe fails
                fmt_str = ydl_opts.get('format', '') or ''
                self._total_parts_est = 2 if ('+' in fmt_str) else 1
            self._last_total_parts_est = self._total_parts_est

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
                            try:
                                ydl.add_progress_hook(self.download_hook)
                            except Exception:
                                pass
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
                    try:
                        ydl.add_progress_hook(self.download_hook)
                    except Exception:
                        pass
                    info = ydl.extract_info(self.url, download=True)
                    downloaded_file = ydl.prepare_filename(info)
            # Ensure final progress is at least 99 before emitting finished
            try:
                if not self._observed_progress:
                    # Force a minimal non-zero update so UI doesn't jump from 0->end
                    self._last_progress = max(self._last_progress, 1)
                    self.progress.emit(self._last_progress)
                self._last_progress = 99
                self.progress.emit(self._last_progress)
            except Exception:
                pass
            self.finished.emit("Download completed successfully.", downloaded_file)
        except Exception as e:
            error_msg = str(e)
            if "Failed to parse JSON" in error_msg:
                error_msg += ("\nThis error is typically caused by changes in Twitter's API or a temporary issue with the service. "
                              "Ensure you are using the latest version of yt-dlp (yt-dlp -U) and consider reporting this issue if it persists.")
            self.error.emit(error_msg)

    def download_hook(self, d):
        # Robust progress for single or multi-part downloads (video+audio) and HLS fragments
        try:
            status = d.get('status')
            prev_filename = self._current_filename
            prev_total_parts_est = self._total_parts_est
            filename = d.get('filename') or d.get('tmpfilename') or None
            info = d.get('info_dict') or {}
            # Anticipate multi-part (video+audio) early to avoid premature 100% on first part
            try:
                requested_formats = info.get('requested_formats')
                if isinstance(requested_formats, (list, tuple)) and len(requested_formats) > 1:
                    self._total_parts_est = max(self._total_parts_est, len(requested_formats))
            except Exception:
                pass
            if filename and filename != self._current_filename:
                self._current_filename = filename
                if filename not in self._parts_seen:
                    self._parts_seen.add(filename)
                    # Update parts estimate to reflect newly detected part(s)
                    self._total_parts_est = max(self._total_parts_est, len(self._parts_seen))
            parts_est_increased = self._total_parts_est > prev_total_parts_est
            part_changed = (filename is not None and prev_filename is not None and filename != prev_filename)

            if status == 'downloading':
                # Per-part percentage
                per_part_pct = None
                total = d.get('total_bytes') or d.get('total_bytes_estimate')
                downloaded = d.get('downloaded_bytes', 0)
                if total and total > 0 and downloaded is not None:
                    per_part_pct = max(0, min(100, int(downloaded / total * 100)))
                else:
                    # HLS/native fragment-based fallback (support multiple possible key names)
                    frag_idx = (d.get('fragment_index') or d.get('frag_index') or d.get('fragment_number') or d.get('frag_no'))
                    frag_cnt = (d.get('fragment_count') or d.get('total_fragments') or d.get('total_frags') or d.get('fragments_total') or d.get('fragments'))
                    if isinstance(frag_cnt, list):
                        frag_cnt = len(frag_cnt)
                    if frag_idx is not None and frag_cnt:
                        try:
                            frag_idx_i = int(frag_idx)
                            frag_cnt_i = int(frag_cnt)
                            if frag_cnt_i > 0:
                                per_part_pct = max(0, min(100, int((frag_idx_i / float(frag_cnt_i)) * 100)))
                        except Exception:
                            per_part_pct = None
                # As a last resort, parse yt-dlp's formatted percent string when numeric fields are missing
                if per_part_pct is None:
                    pct_str = d.get('_percent_str')
                    if isinstance(pct_str, str) and '%' in pct_str:
                        try:
                            # Accept forms like ' 23.4%'
                            val = pct_str.strip().replace('%', '').strip()
                            per_part_pct = max(0, min(100, int(float(val))))
                        except Exception:
                            per_part_pct = None
                if per_part_pct is None:
                    # Time-based rough estimate if ETA is present
                    elapsed = d.get('elapsed')
                    eta = d.get('eta')
                    try:
                        if elapsed is not None and eta is not None and (elapsed + eta) > 0:
                            per_part_pct = int(max(0.0, min(100.0, (float(elapsed) / float(elapsed + eta)) * 100.0)))
                    except Exception:
                        per_part_pct = None

                # Overall across parts: completed parts + current part fraction
                completed_parts = len(self._parts_done)
                if per_part_pct is not None:
                    # Ensure denominator accounts for the current active part at minimum
                    effective_total_parts = max(self._total_parts_est, completed_parts + 1)
                    denom = float(max(1, effective_total_parts))
                    overall = (completed_parts + (per_part_pct / 100.0)) / denom
                    base_percent = int(max(0.0, min(99.0, overall * 100.0)))
                    # Allow downward recalibration when part changes or parts estimate increases
                    if part_changed or parts_est_increased:
                        percent = base_percent
                    else:
                        percent = max(self._last_progress, base_percent)
                    self._last_progress = percent
                    if percent > 0:
                        self._observed_progress = True
                    try:
                        if percent == 0 or percent % 10 == 0:
                            self.logMessage.emit(f"Download progress: part={completed_parts+1}/{effective_total_parts} per={per_part_pct}% overall={percent}%")
                    except Exception:
                        pass
                    self.progress.emit(percent)

            elif status == 'finished':
                # If this 'finished' refers to an HLS fragment, update based on fragment_index/count and return
                frag_idx = (d.get('fragment_index') or d.get('frag_index') or d.get('fragment_number') or d.get('frag_no'))
                frag_cnt = (d.get('fragment_count') or d.get('total_fragments') or d.get('total_frags') or d.get('fragments_total') or d.get('fragments'))
                if isinstance(frag_cnt, list):
                    frag_cnt = len(frag_cnt)
                if frag_idx is not None and frag_cnt:
                    try:
                        frag_idx_i = int(frag_idx)
                        frag_cnt_i = int(frag_cnt)
                        if frag_cnt_i > 0:
                            frac = int(min(99.0, max(0.0, (frag_idx_i / float(frag_cnt_i)) * 100.0)))
                        else:
                            frac = 0
                        effective_total_parts = max(self._total_parts_est, len(self._parts_done) + 1)
                        overall = (len(self._parts_done) + (frac / 100.0)) / float(max(1, effective_total_parts))
                        base_percent = int(max(0.0, min(99.0, overall * 100.0)))
                        if part_changed or parts_est_increased:
                            percent = base_percent
                        else:
                            percent = max(self._last_progress, base_percent)
                        self._last_progress = percent
                        try:
                            if percent % 10 == 0:
                                self.logMessage.emit(f"Download frag finished {frag_idx_i}/{frag_cnt_i} overall={percent}%")
                        except Exception:
                            pass
                        self.progress.emit(percent)
                    except Exception:
                        pass
                else:
                    # End of a full part (e.g., video or audio). Mark complete.
                    if filename:
                        self._parts_done.add(filename)
                        self._total_parts_est = max(self._total_parts_est, len(self._parts_seen), len(self._parts_done))
                    completed_parts = len(self._parts_done)
                    effective_total_parts = max(self._total_parts_est, completed_parts if completed_parts > 0 else 1)
                    overall = completed_parts / float(max(1, effective_total_parts))
                    base_percent = int(max(0.0, min(99.0, overall * 100.0)))
                    if part_changed or parts_est_increased:
                        percent = base_percent
                    else:
                        percent = max(self._last_progress, base_percent)
                    self._last_progress = percent
                    if percent > 0:
                        self._observed_progress = True
                    try:
                        self.logMessage.emit(f"Download part finished {completed_parts}/{effective_total_parts} overall={percent}%")
                    except Exception:
                        pass
                    self.progress.emit(percent)
        except Exception:
            # Never break the download on hook errors
            pass


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

    def __init__(self, input_file, start_time, end_time, use_gpu=False, delete_original=True, output_folder=None, copy_mode=False):
        """
        If copy_mode is True, we use stream copy (lossless) for trimming.
        Otherwise, we re-encode using high-quality parameters that preserve the original bitrate.
        """
        super().__init__()
        self.input_file = input_file
        self.start_time = start_time
        self.end_time = end_time
        self.use_gpu = use_gpu
        self.delete_original = delete_original
        self.output_folder = output_folder
        self.copy_mode = copy_mode

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
            # For accurate trimming (avoiding stutter) use -ss after the input
            ffmpeg_start = self._format_time_for_ffmpeg(self.start_time)
            trim_duration = end_seconds - start_seconds
            base, ext = os.path.splitext(os.path.basename(self.input_file))
            # For "Trim & Convert" mode (non-copy mode) force the temporary output to be .mp4 for compatibility
            if not self.copy_mode:
                ext = ".mp4"
            # Build a temporary output path in the same folder as the input file
            temp_output = os.path.join(os.path.dirname(
                self.input_file), base + "_temp" + ext)
            import subprocess
            ffmpeg_path = self._get_ffmpeg_path()

            audio_args = ["-c:a", "copy"]

            if self.copy_mode:
                # For stream copy, using fast seek (-ss before -i) is acceptable.
                cmd = [ffmpeg_path, "-y", "-ss", ffmpeg_start, "-i", self.input_file,
                       "-t", str(trim_duration), "-c", "copy", temp_output]
            else:
                # For re-encoding, use accurate trimming by placing -ss after -i.
                from converter import get_input_bitrate
                input_bitrate = get_input_bitrate(self.input_file)
                if input_bitrate:
                    target_bitrate = input_bitrate  # use original bitrate
                    target_bitrate_k = target_bitrate // 1000
                else:
                    target_bitrate_k = 5000  # fallback value

                if self.use_gpu:
                    if is_video_10bit(self.input_file):
                        gpu_flags = ["-hwaccel", "cuda",
                                     "-hwaccel_output_format", "p010le"]
                        encoder_args = ["-c:v", "hevc_nvenc", "-qp", "18", "-profile:v", "main10", "-pix_fmt", "p010le",
                                        "-b:v", f"{target_bitrate_k}k", "-maxrate", f"{target_bitrate_k}k",
                                        "-bufsize", f"{target_bitrate_k * 2}k"]
                    else:
                        gpu_flags = ["-hwaccel", "cuda",
                                     "-hwaccel_output_format", "nv12"]
                        encoder_args = ["-c:v", "h264_nvenc", "-qp", "18",
                                        "-b:v", f"{target_bitrate_k}k", "-maxrate", f"{target_bitrate_k}k",
                                        "-bufsize", f"{target_bitrate_k * 2}k"]
                    cmd = [ffmpeg_path, "-y"] + gpu_flags + ["-i", self.input_file, "-ss", ffmpeg_start,
                                                             "-t", str(trim_duration)] + encoder_args + audio_args + [temp_output]
                else:
                    if ext.lower() == ".webm":
                        encoder_args = ["-c:v", "libvpx-vp9", "-b:v", f"{target_bitrate_k}k",
                                        "-maxrate", f"{target_bitrate_k}k", "-bufsize", f"{target_bitrate_k * 2}k"]
                    else:
                        encoder_args = ["-c:v", "libx264", "-preset", "veryslow", "-b:v", f"{target_bitrate_k}k",
                                        "-maxrate", f"{target_bitrate_k}k", "-bufsize", f"{target_bitrate_k * 2}k"]
                    cmd = [ffmpeg_path, "-y", "-i", self.input_file, "-ss", ffmpeg_start,
                           "-t", str(trim_duration)] + encoder_args + audio_args + [temp_output]

            print("Running trim command:", " ".join(cmd))
            # Run ffmpeg and stream stderr to compute progress when possible
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       creationflags=subprocess.CREATE_NO_WINDOW, text=True, universal_newlines=True)
            import re
            time_re = re.compile(r"time=(\d{2}):(\d{2}):(\d{2})(?:[\.:](\d+))?")
            # Determine total duration of the trim segment for progress
            total_seconds = float(trim_duration) if isinstance(trim_duration, (int, float)) else float(trim_duration)
            last_pct = -1
            while True:
                line = process.stderr.readline()
                if not line:
                    break
                m = time_re.search(line)
                if m and total_seconds:
                    hh, mm, ss, frac = m.groups()
                    secs = int(hh) * 3600 + int(mm) * 60 + int(ss)
                    if frac:
                        try:
                            secs += float(f"0.{frac}")
                        except Exception:
                            pass
                    pct = int(min(99.0, max(0.0, (secs / float(total_seconds)) * 100.0)))
                    if pct != last_pct:
                        last_pct = pct
                        try:
                            self.progress.emit(pct)
                        except Exception:
                            pass
            process.wait()
            if process.returncode != 0:
                err = process.stderr.read() if process.stderr else ""
                self.error.emit(f"Trimming failed: {err}")
                return

            ts_start = format_timestamp(self.start_time)
            ts_end = format_timestamp(self.end_time)
            suffix = f"_{ts_start}_to_{ts_end}"
            max_base_length = 100
            if len(base) + len(suffix) > max_base_length:
                base = base[:max_base_length - len(suffix)]
            new_filename = base + suffix + ext
            dest_dir = self.output_folder if self.output_folder is not None else os.path.dirname(
                self.input_file)
            new_filepath = os.path.join(dest_dir, new_filename)
            if self.delete_original:
                try:
                    os.remove(self.input_file)
                except Exception as e:
                    self.error.emit(f"Failed to remove original file: {e}")
                    return
            import shutil
            shutil.move(temp_output, new_filepath)
            try:
                self.progress.emit(100)
            except Exception:
                pass
            self.finished.emit("Trimming completed successfully.", new_filepath)
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
