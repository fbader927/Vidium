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
    finished = Signal(str)
    error = Signal(str)

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
            }

            # Source-specific options:
            if source == "reddit":
                # For Reddit videos, ensure that video and audio are merged into an MP4 container.
                ydl_opts.update({
                    'merge_output_format': 'mp4'
                })
            elif source == "twitter":
                # For Twitter videos, ensure that video and audio are merged into an MP4 container.
                ydl_opts.update({
                    'merge_output_format': 'mp4'
                })

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([self.url])
            self.finished.emit("Download completed successfully.")
        except Exception as e:
            self.error.emit(str(e))
