import sys
import os
import asyncio
import tempfile
import subprocess
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLineEdit, QFileDialog, QLabel, QMenu, QComboBox, QPlainTextEdit,
    QCheckBox, QSlider, QListWidget, QSizePolicy, QProgressBar, QGroupBox, QStyle, QTabWidget, QStatusBar
)
from PySide6.QtGui import QStandardItemModel, QStandardItem, QFont, QTextOption, QPainter
from PySide6.QtCore import Qt, QThread, Signal, Slot, QTimer, QSettings, QPoint, QUrl, QSize, QEvent
from converter import convert_file, OUTPUT_FOLDER, get_input_bitrate, run_ffmpeg, get_ffmpeg_path
from downloader import DownloadWorker  # Import the downloader functionality
# Imports for video preview
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget


# --- New ClickableSlider subclass to enable seeking on click ---
class ClickableSlider(QSlider):
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            # Calculate new slider value based on click position
            new_value = QStyle.sliderValueFromPosition(
                self.minimum(), self.maximum(), event.x(), self.width())
            self.setValue(new_value)
            self.sliderMoved.emit(new_value)
            event.accept()
        super().mousePressEvent(event)


# --- Custom QListWidget with placeholder text when empty ---
class PlaceholderListWidget(QListWidget):
    def __init__(self, placeholder, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.placeholder = placeholder

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.count() == 0:
            painter = QPainter(self.viewport())
            painter.setPen(Qt.gray)
            painter.drawText(self.viewport().rect(),
                             Qt.AlignCenter, self.placeholder)


def convert_file_with_full_args(args: list) -> str:
    import asyncio
    ffmpeg_path = get_ffmpeg_path()
    cmd = [ffmpeg_path] + args
    print(f"Running command: {' '.join(cmd)}")

    async def run_cmd():
        from asyncio.subprocess import PIPE
        process = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
        stdout, stderr = await process.communicate()
        log = ""
        if stdout:
            log += f"[stdout]\n{stdout.decode()}\n"
        if stderr:
            log += f"[stderr]\n{stderr.decode()}\n"
        if process.returncode != 0:
            raise RuntimeError(
                f"Command failed with code {process.returncode}. Log: {log}")
        return log
    return asyncio.run(run_cmd())


# --- New worker for converting mkv to webm preview with optional GPU acceleration ---
class PreviewConversionWorker(QThread):
    # Emits the path of the converted preview file
    conversionFinished = Signal(str)

    def __init__(self, input_file, output_file, use_gpu=False):
        super().__init__()
        self.input_file = input_file
        self.output_file = output_file
        self.use_gpu = use_gpu

    def run(self):
        # Use FFmpeg (the bundled executable) to convert a short segment of the mkv to webm.
        # This conversion is intended only for preview purposes (limited to 30 seconds).
        ffmpeg_path = get_ffmpeg_path()
        cmd = [ffmpeg_path, "-y"]
        if self.use_gpu:
            # Add GPU flags if GPU is enabled.
            gpu_flags = ["-hwaccel", "cuda", "-hwaccel_output_format", "nv12"]
            cmd.extend(gpu_flags)
        cmd.extend([
            "-i", self.input_file,
            "-t", "30",  # convert only the first 30 seconds
            "-c:v", "libvpx", "-crf", "30", "-b:v", "500k",
            "-c:a", "libvorbis",
            "-f", "webm",
            self.output_file
        ])
        # Run the conversion and ignore output; errors will show up in the log if any.
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.conversionFinished.emit(self.output_file)


class ConversionWorker(QThread):
    conversionFinished = Signal(str, str)  # output_file, status message
    conversionError = Signal(str)          # error message
    logMessage = Signal(str)               # log output from FFmpeg

    def __init__(self, input_file, output_file, extra_args=None, use_gpu=False, quality=100):
        super().__init__()
        self.input_file = input_file
        self.output_file = output_file
        self.extra_args = extra_args
        self.use_gpu = use_gpu
        self.quality = quality
        self._stop_event = None  # Will be created in run()
        self._loop = None        # Will hold the worker's asyncio loop

    def stop(self):
        # Use the worker's event loop to safely set the stop event from another thread.
        if self._stop_event and self._loop:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    async def run_command_with_args(self, args: list) -> str:
        ffmpeg_path = get_ffmpeg_path()
        cmd = [ffmpeg_path] + args
        print(f"Running command: {' '.join(cmd)}")
        from asyncio.subprocess import PIPE
        process = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
        wait_tasks = [
            asyncio.create_task(process.communicate()),
            asyncio.create_task(self._stop_event.wait())
        ]
        done, pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
        if wait_tasks[1] in done:
            process.kill()
            await process.wait()
            for t in pending:
                t.cancel()
            if os.path.exists(self.output_file):
                os.remove(self.output_file)
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
                    f"Conversion failed for {self.input_file} (return code {process.returncode}). Log: {log}")
            return log

    async def do_conversion(self):
        import os
        from converter import run_ffmpeg
        ext = os.path.splitext(self.output_file)[1].lower()
        log = ""
        if ext == '.gif':
            desired_fps = 30 if self.quality >= 80 else 10
            palette_file = os.path.join(OUTPUT_FOLDER, "palette_temp.png")
            try:
                palette_args = [
                    "-y", "-i", self.input_file,
                    "-vf", f"fps={desired_fps},scale=320:-1:flags=lanczos,palettegen",
                    palette_file
                ]
                ret = await run_ffmpeg(palette_args, self._stop_event)
                if ret != 0:
                    raise RuntimeError("Palette generation for GIF failed.")
                gif_args = [
                    "-y", "-i", self.input_file, "-i", palette_file,
                    "-filter_complex", f"fps={desired_fps},scale=320:-1:flags=lanczos[x];[x][1:v]paletteuse",
                    self.output_file
                ]
                log = await self.run_command_with_args(gif_args)
            finally:
                if os.path.exists(palette_file):
                    try:
                        os.remove(palette_file)
                    except Exception:
                        pass
        elif ext in ['.mp4', '.webm', '.mkv']:
            input_bitrate = get_input_bitrate(self.input_file)
            bitrate_arg = None
            if input_bitrate:
                target_bitrate = int(input_bitrate * self.quality / 100)
                target_bitrate_k = target_bitrate // 1000
                bitrate_arg = f"{target_bitrate_k}k"
            if self.extra_args is None:
                base_extra_args = ["-pix_fmt", "yuv420p", "-r", "60",
                                   "-c:v", "libx264", "-preset", "fast", "-crf", "23"]
            else:
                base_extra_args = self.extra_args.copy()
            if self.use_gpu:
                if "-pix_fmt" in base_extra_args:
                    idx = base_extra_args.index("-pix_fmt")
                    del base_extra_args[idx:idx+2]
                if "-crf" in base_extra_args:
                    idx = base_extra_args.index("-crf")
                    del base_extra_args[idx:idx+2]
                if "-c:v" in base_extra_args:
                    idx = base_extra_args.index("-c:v")
                    base_extra_args[idx+1] = "h264_nvenc"
                else:
                    base_extra_args = ["-c:v", "h264_nvenc",
                                       "-preset", "fast"] + base_extra_args
                if self.input_file.lower().endswith(".webm"):
                    base_extra_args += ["-tile-columns",
                                        "6", "-frame-parallel", "1"]
                if bitrate_arg:
                    base_extra_args += ["-b:v", bitrate_arg, "-maxrate", bitrate_arg,
                                        "-bufsize", f"{(target_bitrate_k * 2)}k"]
            else:
                if bitrate_arg:
                    base_extra_args += ["-b:v", bitrate_arg]
            if "-r" not in base_extra_args:
                base_extra_args += ["-r", "60"]
            if "-pix_fmt" not in base_extra_args:
                base_extra_args += ["-pix_fmt", "yuv420p"]
            from converter import convert_file
            log = await convert_file(self.input_file, self.output_file, base_extra_args, use_gpu=self.use_gpu, stop_event=self._stop_event)
        else:
            from converter import convert_file
            log = await convert_file(self.input_file, self.output_file, self.extra_args, stop_event=self._stop_event)
        return log

    def run(self):
        import asyncio
        self._stop_event = asyncio.Event()
        loop = asyncio.new_event_loop()
        # Assign the new loop to self._loop so stop() can call it safely
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            log = loop.run_until_complete(self.do_conversion())
            self.conversionFinished.emit(
                self.output_file, "Conversion completed successfully.")
            self.logMessage.emit(log)
        except asyncio.CancelledError as ce:
            self.conversionError.emit("Conversion stopped by user.")
            self.logMessage.emit(str(ce))
        except Exception as e:
            self.conversionError.emit(str(e))
            self.logMessage.emit(str(e))
        finally:
            loop.close()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Vidium Video Converter")
        self.resize(800, 600)
        self.setStatusBar(QStatusBar())
        self.current_index = 0
        self.overall_progress = 0.0
        self.settings = QSettings("MyCompany", "VidiumConverter")
        self.conversion_aborted = False
        self.conversion_active = False  # Flag indicating conversion in progress
        self.worker = None
        self.download_worker = None  # For download functionality
        self.preview_conversion_worker = None  # For mkv preview conversion
        self.initUI()
        self.setAcceptDrops(True)
        self.init_drop_overlay()
        # Install event filter on video widget to block its mouse events when conversion is active
        self.video_widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj == self.video_widget and self.conversion_active:
            if event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick):
                return True
        return super().eventFilter(obj, event)

    def initUI(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)

        self.tab_widget = QTabWidget()

        self.convert_tab = QWidget()
        convert_layout = QVBoxLayout(self.convert_tab)

        # Top area: input files and preview
        top_layout = QHBoxLayout()
        input_group = QGroupBox("Add input file(s)")
        input_layout = QVBoxLayout(input_group)
        self.input_list = PlaceholderListWidget(
            "Add, or Drag and Drop in Files")
        self.input_list.setMinimumHeight(200)
        self.input_list.setStyleSheet(
            "QListWidget { margin-left: 0px; margin-right: 0px; }")
        self.input_list.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.input_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.input_list.customContextMenuRequested.connect(
            self.input_list_context_menu)
        # If conversion is active, ignore selection changes.
        self.input_list.currentItemChanged.connect(self.preview_selected_video)
        input_layout.addWidget(self.input_list)
        self.input_browse_button = QPushButton("Browse")
        self.input_browse_button.setFixedWidth(87)
        self.input_browse_button.clicked.connect(self.browse_input_files)
        input_layout.addWidget(self.input_browse_button)
        top_layout.addWidget(input_group)

        preview_group = QGroupBox("Preview")
        preview_layout = QVBoxLayout(preview_group)
        self.video_widget = QVideoWidget()
        preview_layout.addWidget(self.video_widget)
        self.media_player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.audio_output.setVolume(0.5)
        self.media_player.setAudioOutput(self.audio_output)
        self.media_player.setVideoOutput(self.video_widget)
        self.media_player.pause()
        # Use ClickableSlider instead of QSlider for proper click-based seeking.
        self.video_slider = ClickableSlider(Qt.Horizontal)
        preview_layout.addWidget(self.video_slider)
        self.media_player.positionChanged.connect(self.video_slider.setValue)
        self.media_player.durationChanged.connect(
            lambda d: self.video_slider.setRange(0, d))
        self.video_slider.sliderMoved.connect(self.media_player.setPosition)
        video_controls_layout = QHBoxLayout()
        self.toggle_button = QPushButton()
        self.toggle_button.setFixedSize(40, 40)
        self.play_icon = self.style().standardIcon(QStyle.SP_MediaPlay)
        self.pause_icon = self.style().standardIcon(QStyle.SP_MediaPause)
        self.toggle_button.setIcon(self.play_icon)
        self.toggle_button.setIconSize(QSize(32, 32))
        self.toggle_button.clicked.connect(self.toggle_play_pause)
        video_controls_layout.addWidget(self.toggle_button)
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(50)
        self.volume_slider.setFixedWidth(100)
        self.volume_slider.valueChanged.connect(
            lambda val: self.audio_output.setVolume(val/100.0))
        video_controls_layout.addWidget(self.volume_slider)
        preview_layout.addLayout(video_controls_layout)
        top_layout.addWidget(preview_group)
        convert_layout.addLayout(top_layout)

        # Output Folder area
        out_addr_layout = QHBoxLayout()
        out_addr_layout.setSpacing(0)
        out_addr_layout.setContentsMargins(0, 0, 0, 0)
        out_addr_layout.setAlignment(Qt.AlignLeft)
        out_folder_label = QLabel("Output Folder:")
        out_folder_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        out_addr_layout.addWidget(out_folder_label)
        out_addr_layout.addSpacing(10)
        self.output_folder_edit = QLineEdit()
        default_folder = self.settings.value(
            "default_output_folder", OUTPUT_FOLDER)
        self.output_folder_edit.setText(default_folder)
        self.output_folder_edit.setFixedWidth(300)
        out_addr_layout.addWidget(self.output_folder_edit)
        convert_layout.addLayout(out_addr_layout)

        # Buttons below folder
        out_buttons_layout = QHBoxLayout()
        out_buttons_layout.setAlignment(Qt.AlignLeft)
        self.output_browse_button = QPushButton("Browse")
        out_buttons_layout.addWidget(self.output_browse_button)
        self.goto_folder_button = QPushButton("Go To Folder")
        out_buttons_layout.addWidget(self.goto_folder_button)
        self.default_checkbox = QCheckBox("Default")
        default_checked = self.settings.value(
            "default_checked", True, type=bool)
        self.default_checkbox.setChecked(default_checked)
        out_buttons_layout.addWidget(self.default_checkbox)
        out_buttons_layout.addStretch()
        convert_layout.addLayout(out_buttons_layout)
        self.output_browse_button.clicked.connect(self.browse_output_folder)
        self.goto_folder_button.clicked.connect(self.goto_output_folder)
        self.default_checkbox.stateChanged.connect(
            self.default_checkbox_changed)

        # Output Format Dropdown
        format_layout = QHBoxLayout()
        format_layout.setSpacing(0)
        format_layout.setContentsMargins(0, 0, 0, 0)
        format_layout.setAlignment(Qt.AlignLeft)
        format_label = QLabel("Output Format:")
        format_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        format_layout.addWidget(format_label)
        format_layout.addSpacing(10)
        self.output_format_combo = QComboBox()
        self.populate_output_format_combo()
        format_layout.addWidget(self.output_format_combo)
        convert_layout.addLayout(format_layout)

        # Options: GPU and Quality
        options_layout = QVBoxLayout()
        self.gpu_checkbox = QCheckBox("Use GPU (Very Fast)")
        options_layout.addWidget(self.gpu_checkbox)
        quality_layout = QHBoxLayout()
        quality_layout.setSpacing(0)
        quality_layout.setContentsMargins(0, 0, 0, 0)
        quality_layout.setAlignment(Qt.AlignLeft)
        quality_label = QLabel("Quality:")
        quality_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        quality_layout.addWidget(quality_label)
        quality_layout.addSpacing(10)
        self.quality_slider = QSlider(Qt.Horizontal)
        self.quality_slider.setMinimum(10)
        self.quality_slider.setMaximum(100)
        self.quality_slider.setTickInterval(10)
        self.quality_slider.setTickPosition(QSlider.TicksBelow)
        self.quality_slider.setValue(100)
        self.quality_slider.setFixedWidth(150)
        self.quality_slider.valueChanged.connect(self.update_quality_label)
        quality_layout.addWidget(self.quality_slider)
        self.quality_value_label = QLabel("100%")
        self.quality_value_label.setSizePolicy(
            QSizePolicy.Fixed, QSizePolicy.Fixed)
        quality_layout.addWidget(self.quality_value_label)
        options_layout.addLayout(quality_layout)
        convert_layout.addLayout(options_layout)

        # Convert and Stop Buttons
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(0)
        button_layout.setAlignment(Qt.AlignLeft)
        self.convert_button = QPushButton("Convert")
        self.convert_button.setFixedSize(120, 60)
        font = self.convert_button.font()
        font.setPointSize(font.pointSize() * 2)
        self.convert_button.setFont(font)
        self.convert_button.setStyleSheet("margin: 0px; padding: 0px;")
        button_layout.addWidget(self.convert_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setFixedSize(120, 60)
        self.stop_button.setFont(font)
        self.stop_button.setStyleSheet("margin: 0px; padding: 0px;")
        self.stop_button.clicked.connect(self.stop_conversion)
        self.stop_button.setEnabled(False)
        button_layout.addWidget(self.stop_button)
        convert_layout.addLayout(button_layout)
        self.convert_button.clicked.connect(self.start_conversion_queue)

        # Progress Section
        self.current_progress_label = QLabel("Current File Progress: 0%")
        convert_layout.addWidget(self.current_progress_label)
        self.current_progress_bar = QProgressBar()
        self.current_progress_bar.setMinimum(0)
        self.current_progress_bar.setMaximum(100)
        self.current_progress_bar.setValue(0)
        self.current_progress_bar.setFixedWidth(203)
        convert_layout.addWidget(self.current_progress_bar)
        self.overall_progress_label = QLabel("Overall Progress: 0%")
        convert_layout.addWidget(self.overall_progress_label)
        self.overall_progress_bar = QProgressBar()
        self.overall_progress_bar.setMinimum(0)
        self.overall_progress_bar.setMaximum(100)
        self.overall_progress_bar.setValue(0)
        self.overall_progress_bar.setFixedWidth(203)
        convert_layout.addWidget(self.overall_progress_bar)

        self.tab_widget.addTab(self.convert_tab, "Convert")

        # Download Tab
        self.download_tab = QWidget()
        download_layout = QVBoxLayout(self.download_tab)

        # YouTube URL
        youtube_url_layout = QHBoxLayout()
        youtube_url_label = QLabel("YouTube URL:")
        youtube_url_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.youtube_url_edit = QLineEdit()
        self.youtube_url_edit.setPlaceholderText("Enter YouTube URL here...")
        youtube_url_layout.addWidget(youtube_url_label)
        youtube_url_layout.addWidget(self.youtube_url_edit)
        download_layout.addLayout(youtube_url_layout)

        # Download Folder
        download_folder_layout = QHBoxLayout()
        download_folder_label = QLabel("Download Folder:")
        download_folder_label.setSizePolicy(
            QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.download_folder_edit = QLineEdit()
        DEFAULT_DOWNLOAD_FOLDER = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "Downloads")
        default_download_folder = self.settings.value(
            "default_download_folder", DEFAULT_DOWNLOAD_FOLDER)
        self.download_folder_edit.setText(default_download_folder)
        self.download_folder_edit.setFixedWidth(300)
        download_folder_layout.addWidget(download_folder_label)
        download_folder_layout.addWidget(self.download_folder_edit)
        download_layout.addLayout(download_folder_layout)

        # Browse and Default for Download Folder
        download_folder_buttons_layout = QHBoxLayout()
        self.download_browse_button = QPushButton("Browse")
        self.download_default_checkbox = QCheckBox("Default")
        default_download_checked = self.settings.value(
            "default_download_checked", True, type=bool)
        self.download_default_checkbox.setChecked(default_download_checked)
        download_folder_buttons_layout.addWidget(self.download_browse_button)
        download_folder_buttons_layout.addWidget(
            self.download_default_checkbox)
        download_folder_buttons_layout.addStretch()
        download_layout.addLayout(download_folder_buttons_layout)

        # Download Button
        self.download_button = QPushButton("Download")
        self.download_button.setFixedSize(120, 60)
        download_layout.addWidget(
            self.download_button, alignment=Qt.AlignCenter)

        self.tab_widget.addTab(self.download_tab, "Download")

        main_layout.addWidget(self.tab_widget)

        # Console Log
        self.log_text_edit = QPlainTextEdit()
        self.log_text_edit.setReadOnly(True)
        self.log_text_edit.setPlaceholderText(
            "Conversion log output will appear here...")
        self.log_text_edit.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.log_text_edit.setFixedHeight(150)
        self.log_text_edit.setFixedWidth(406)
        main_layout.addWidget(self.log_text_edit)

        # Conversion Queue and Progress Simulation
        self.conversion_queue = []
        self.total_files = 0
        self.current_index = 0
        self.file_timer = QTimer(self)
        self.file_timer.timeout.connect(self.update_current_progress)
        self.current_file_progress = 0

        # Connect download tab buttons
        self.download_browse_button.clicked.connect(
            self.browse_download_folder)
        self.download_default_checkbox.stateChanged.connect(
            self.download_default_checkbox_changed)
        self.download_button.clicked.connect(self.start_download)

    def init_drop_overlay(self):
        self.drop_overlay = QWidget(self)
        self.drop_overlay.setGeometry(self.rect())
        self.drop_overlay.setStyleSheet(
            "background-color: rgba(0, 0, 0, 0.2);")
        self.drop_overlay.hide()
        self.overlay_label = QLabel("Drop Files Here", self.drop_overlay)
        self.overlay_label.setStyleSheet("color: white; font-size: 24pt;")
        self.overlay_label.setAlignment(Qt.AlignCenter)
        layout = QVBoxLayout(self.drop_overlay)
        layout.addWidget(self.overlay_label, alignment=Qt.AlignCenter)

    def populate_output_format_combo(self):
        from PySide6.QtGui import QStandardItemModel, QStandardItem, QFont
        model = QStandardItemModel()
        bold_font = QFont()
        bold_font.setBold(True)
        header_video = QStandardItem("Video:")
        header_video.setFlags(Qt.NoItemFlags)
        header_video.setFont(bold_font)
        model.appendRow(header_video)
        for fmt in ["mp4", "webm", "mkv"]:
            item = QStandardItem("   " + fmt)
            model.appendRow(item)
        header_audio = QStandardItem("Audio:")
        header_audio.setFlags(Qt.NoItemFlags)
        header_audio.setFont(bold_font)
        model.appendRow(header_audio)
        for fmt in ["mp3", "wav"]:
            item = QStandardItem("   " + fmt)
            model.appendRow(item)
        header_gif = QStandardItem("GIF:")
        header_gif.setFlags(Qt.NoItemFlags)
        header_gif.setFont(bold_font)
        model.appendRow(header_gif)
        item = QStandardItem("   gif")
        model.appendRow(item)
        self.output_format_combo.setModel(model)
        self.output_format_combo.setCurrentIndex(1)

    def update_quality_label(self, value):
        self.quality_value_label.setText(f"{value}%")

    def browse_input_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select Input File(s)", "",
            "Video Files (*.mp4 *.mkv *.webm *.avi *.mov *.flv *.wmv *.mpeg *.mpg)"
        )
        if files:
            for f in files:
                if not self.file_already_added(f):
                    self.input_list.addItem(f)
            if self.input_list.currentItem() is None and self.input_list.count() > 0:
                self.input_list.setCurrentRow(0)

    def input_list_context_menu(self, point: QPoint):
        item = self.input_list.itemAt(point)
        if item is not None:
            menu = QMenu()
            remove_action = menu.addAction("Remove")
            action = menu.exec(self.input_list.mapToGlobal(point))
            if action == remove_action:
                self.input_list.takeItem(self.input_list.row(item))

    def browse_output_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Output Folder", OUTPUT_FOLDER)
        if folder:
            self.output_folder_edit.setText(folder)
            self.default_checkbox.setChecked(False)
            self.settings.setValue("default_output_folder", folder)
            self.settings.setValue("default_checked", False)

    def goto_output_folder(self):
        folder = self.output_folder_edit.text().strip()
        if folder and os.path.isdir(folder):
            os.startfile(folder)
        else:
            self.statusBar().showMessage("Output folder not found.")

    def default_checkbox_changed(self, state):
        if state == Qt.Checked:
            self.output_folder_edit.setText(OUTPUT_FOLDER)
            self.output_folder_edit.setReadOnly(True)
            self.settings.setValue("default_output_folder", OUTPUT_FOLDER)
            self.settings.setValue("default_checked", True)
        else:
            self.output_folder_edit.setReadOnly(False)
            self.settings.setValue("default_checked", False)

    def preview_selected_video(self, current, previous):
        if self.conversion_active:
            return
        if current:
            file_path = current.text()
            ext = os.path.splitext(file_path)[1].lower()
            if ext == ".mkv":
                # For mkv files, convert to a temporary webm for preview to enable proper seeking.
                base = os.path.splitext(os.path.basename(file_path))[0]
                temp_dir = tempfile.gettempdir()
                preview_file = os.path.join(temp_dir, base + "_preview.webm")
                if os.path.exists(preview_file):
                    self.media_player.setSource(
                        QUrl.fromLocalFile(preview_file))
                    self.media_player.pause()
                    self.toggle_button.setIcon(self.play_icon)
                else:
                    self.append_log(
                        "Converting mkv to webm for preview, please wait...")
                    # Pass the GPU flag based on the GPU checkbox state.
                    self.preview_conversion_worker = PreviewConversionWorker(
                        file_path, preview_file, self.gpu_checkbox.isChecked())
                    self.preview_conversion_worker.conversionFinished.connect(
                        self.on_preview_conversion_finished)
                    self.preview_conversion_worker.start()
            else:
                self.media_player.setSource(QUrl.fromLocalFile(file_path))
                self.media_player.pause()
                self.toggle_button.setIcon(self.play_icon)

    @Slot(str)
    def on_preview_conversion_finished(self, preview_file):
        self.append_log("Preview conversion finished.")
        self.media_player.setSource(QUrl.fromLocalFile(preview_file))
        self.media_player.pause()
        self.toggle_button.setIcon(self.play_icon)

    def toggle_play_pause(self):
        if self.conversion_active:
            return
        if self.media_player.playbackState() == QMediaPlayer.PlayingState:
            self.media_player.pause()
            self.toggle_button.setIcon(self.play_icon)
        else:
            self.media_player.play()
            self.toggle_button.setIcon(self.pause_icon)

    def disable_preview(self):
        self.media_player.pause()
        self.video_widget.setEnabled(False)
        self.toggle_button.setEnabled(False)
        self.volume_slider.setEnabled(False)
        self.input_list.setEnabled(False)
        self.conversion_active = True

    def enable_preview(self):
        self.video_widget.setEnabled(True)
        self.toggle_button.setEnabled(True)
        self.volume_slider.setEnabled(True)
        self.input_list.setEnabled(True)
        self.conversion_active = False

    def start_conversion_queue(self):
        if self.conversion_active:
            return
        count = self.input_list.count()
        if count == 0:
            self.statusBar().showMessage("No input files selected.")
            return
        if self.default_checkbox.isChecked():
            out_folder = OUTPUT_FOLDER
        else:
            out_folder = self.output_folder_edit.text().strip()
            if not out_folder:
                self.statusBar().showMessage("Please select an output folder.")
                return
        self.disable_preview()
        out_format = self.get_selected_format()
        self.conversion_queue = []
        for i in range(count):
            input_path = self.input_list.item(i).text()
            base = os.path.splitext(os.path.basename(input_path))[0]
            output_path = os.path.join(out_folder, base + "." + out_format)
            self.conversion_queue.append((input_path, output_path))
        self.total_files = len(self.conversion_queue)
        self.current_index = 0
        self.overall_progress_bar.setValue(0)
        self.progress_label_update()
        self.convert_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.conversion_aborted = False
        self.start_next_conversion()

    def progress_label_update(self):
        if self.current_index < self.total_files:
            current_file = os.path.basename(
                self.conversion_queue[self.current_index][0])
            self.progress_label = f"Converting file \"{current_file}\" ({self.current_index+1}/{self.total_files}) to {self.get_selected_format()}"
            self.overall_progress_bar.setValue(
                int((self.current_index / self.total_files)*100))
        else:
            self.progress_label = "All conversions complete."
        self.update_progress_labels()

    def update_progress_labels(self):
        self.current_progress_label.setText(
            f"Current File Progress: {self.current_file_progress}%")
        overall_percent = int(
            (self.current_index / self.total_files)*100) if self.total_files > 0 else 100
        self.overall_progress_label.setText(
            f"Overall Progress: {overall_percent}%")

    def start_next_conversion(self):
        if self.conversion_aborted:
            self.convert_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self.append_log("Conversion aborted.")
            self.enable_preview()
            return
        if self.current_index < self.total_files:
            input_file, output_file = self.conversion_queue[self.current_index]
            self.progress_label_update()
            extra_args = None
            if self.get_selected_format().lower() == "gif":
                extra_args = ["-vf", "fps=10,scale=320:-1:flags=lanczos"]
            self.worker = ConversionWorker(
                input_file, output_file, extra_args, self.gpu_checkbox.isChecked(), self.quality_slider.value())
            # Set the parent so the thread is owned by the MainWindow
            self.worker.setParent(self)
            # Connect finished signal to simply clear our worker reference.
            self.worker.finished.connect(lambda: setattr(self, "worker", None))
            self.worker.conversionFinished.connect(
                self.file_conversion_finished)
            self.worker.conversionError.connect(self.file_conversion_error)
            self.worker.logMessage.connect(self.append_log)
            self.current_file_progress = 0
            self.current_progress_bar.setValue(self.current_file_progress)
            self.file_timer.start(500)
            self.worker.start()
        else:
            self.overall_progress_bar.setValue(100)
            self.progress_label_update()
            self.convert_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self.enable_preview()

    def update_current_progress(self):
        if self.current_file_progress < 100:
            self.current_file_progress += 2
            if self.current_file_progress > 100:
                self.current_file_progress = 100
            self.current_progress_bar.setValue(self.current_file_progress)
            self.update_progress_labels()
        else:
            self.file_timer.stop()

    @Slot(str, str)
    def file_conversion_finished(self, output_file, message):
        self.file_timer.stop()
        self.current_file_progress = 100
        self.current_progress_bar.setValue(self.current_file_progress)
        self.append_log(f"{output_file}: {message}")
        # Only remove the item if conversion was not aborted.
        if not self.conversion_aborted:
            if self.input_list.count() > 0:
                self.input_list.takeItem(0)
            self.current_index += 1
            overall = int((self.current_index / self.total_files)*100)
            self.overall_progress_bar.setValue(overall)
            self.start_next_conversion()
        else:
            self.convert_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self.append_log("Conversion aborted.")
            self.enable_preview()

    @Slot(str)
    def file_conversion_error(self, error_message):
        self.file_timer.stop()
        self.append_log(f"Error: {error_message}")
        # Only remove the item if conversion was not aborted.
        if not self.conversion_aborted:
            if self.input_list.count() > 0:
                self.input_list.takeItem(0)
            self.current_index += 1
            overall = int((self.current_index / self.total_files)*100)
            self.overall_progress_bar.setValue(overall)
            self.start_next_conversion()
        else:
            self.convert_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self.append_log("Conversion aborted.")
            self.enable_preview()

    @Slot(str)
    def append_log(self, text):
        self.log_text_edit.appendPlainText(text)

    def get_selected_format(self):
        text = self.output_format_combo.currentText().strip()
        if text.endswith(":"):
            return "mp4"
        return text

    def output_format_changed(self):
        pass

    def show_about(self):
        self.statusBar().showMessage(
            "Vidium Video Converter v1.0\nBuilt with PySide6 and bundled FFmpeg.")

    # --- Drag and Drop Event Handlers ---
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.show_drop_overlay()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.hide_drop_overlay()
        event.accept()

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.hide_drop_overlay()
            if self.tab_widget.currentIndex() != 0:
                self.tab_widget.setCurrentIndex(0)
            urls = event.mimeData().urls()
            for url in urls:
                file_path = url.toLocalFile()
                if self.is_supported_file(file_path) and not self.file_already_added(file_path):
                    self.input_list.addItem(file_path)
            if self.input_list.currentItem() is None and self.input_list.count() > 0:
                self.input_list.setCurrentRow(0)
        else:
            event.ignore()

    def is_supported_file(self, file_path):
        supported_exts = {'.mp4', '.mkv', '.webm', '.avi',
                          '.mov', '.flv', '.wmv', '.mpeg', '.mpg'}
        ext = os.path.splitext(file_path)[1].lower()
        return ext in supported_exts

    def file_already_added(self, file_path):
        for i in range(self.input_list.count()):
            if self.input_list.item(i).text() == file_path:
                return True
        return False

    def show_drop_overlay(self):
        self.drop_overlay.setGeometry(self.rect())
        self.drop_overlay.show()

    def hide_drop_overlay(self):
        self.drop_overlay.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'drop_overlay'):
            self.drop_overlay.setGeometry(self.rect())

    def stop_conversion(self):
        # Set the abort flag and stop the current worker if running.
        self.conversion_aborted = True
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait()  # Wait for the worker to finish.
        # Do not clear the conversion queue or remove the current item;
        # this way the processed video remains in the list.
        self.convert_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.enable_preview()
        self.append_log("Stop requested. Conversion aborted.")

    def browse_download_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Download Folder", self.download_folder_edit.text().strip())
        if folder:
            self.download_folder_edit.setText(folder)
            self.download_default_checkbox.setChecked(False)
            self.settings.setValue("default_download_folder", folder)
            self.settings.setValue("default_download_checked", False)

    def download_default_checkbox_changed(self, state):
        DEFAULT_DOWNLOAD_FOLDER = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "Downloads")
        if state == Qt.Checked:
            self.download_folder_edit.setText(DEFAULT_DOWNLOAD_FOLDER)
            self.download_folder_edit.setReadOnly(True)
            self.settings.setValue(
                "default_download_folder", DEFAULT_DOWNLOAD_FOLDER)
            self.settings.setValue("default_download_checked", True)
        else:
            self.download_folder_edit.setReadOnly(False)
            self.settings.setValue("default_download_checked", False)

    def start_download(self):
        url = self.youtube_url_edit.text().strip()
        if not url:
            self.statusBar().showMessage("Please enter a YouTube URL.")
            return
        download_folder = self.download_folder_edit.text().strip()
        if not download_folder:
            self.statusBar().showMessage("Please select a download folder.")
            return
        self.download_button.setEnabled(False)
        self.download_browse_button.setEnabled(False)
        self.youtube_url_edit.setEnabled(False)
        self.download_folder_edit.setEnabled(False)
        self.download_default_checkbox.setEnabled(False)
        self.append_log("Starting download...")
        self.download_worker = DownloadWorker(url, download_folder)
        self.download_worker.finished.connect(self.download_finished)
        self.download_worker.error.connect(self.download_error)
        self.download_worker.start()

    @Slot(str)
    def download_finished(self, message):
        self.append_log(message)
        self.download_button.setEnabled(True)
        self.download_browse_button.setEnabled(True)
        self.youtube_url_edit.setEnabled(True)
        self.download_folder_edit.setEnabled(True)
        self.download_default_checkbox.setEnabled(True)

    @Slot(str)
    def download_error(self, error_message):
        self.append_log("Download error: " + error_message)
        self.download_button.setEnabled(True)
        self.download_browse_button.setEnabled(True)
        self.youtube_url_edit.setEnabled(True)
        self.download_folder_edit.setEnabled(True)
        self.download_default_checkbox.setEnabled(True)

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
