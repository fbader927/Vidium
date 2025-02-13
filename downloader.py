import os
from PySide6.QtCore import QThread, Signal


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
            ydl_opts = {
                'outtmpl': os.path.join(self.output_folder, '%(title)s.%(ext)s'),
                'format': 'bestvideo+bestaudio/best',
                'noplaylist': True  # download only a single video, not a playlist
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([self.url])
            self.finished.emit("Download completed successfully.")
        except Exception as e:
            self.error.emit(str(e))
