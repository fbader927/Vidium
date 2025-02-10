import sys
import os
import asyncio
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLineEdit, QFileDialog, QLabel, QMenuBar, QMenu, QComboBox, QPlainTextEdit,
    QCheckBox, QSlider, QSizePolicy
)
from PySide6.QtGui import QAction, QStandardItemModel, QStandardItem, QFont, QTextOption
from PySide6.QtCore import Qt, QThread, Signal, Slot
from converter import convert_file, OUTPUT_FOLDER, get_input_bitrate, run_ffmpeg, get_ffmpeg_path

# Helper function to run a full ffmpeg command for GIF conversion.


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
                f"Command failed with code {process.returncode}. Log: {log}"
            )
        return log
    return asyncio.run(run_cmd())


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

    def run(self):
        import os
        try:
            ext = os.path.splitext(self.output_file)[1].lower()
            # --- GIF Conversion Branch ---
            if ext == '.gif':
                desired_fps = 30 if self.quality >= 80 else 10
                palette_file = os.path.join(OUTPUT_FOLDER, "palette_temp.png")
                try:
                    # First pass: generate the palette.
                    palette_args = [
                        "-y", "-i", self.input_file,
                        "-vf", f"fps={desired_fps},scale=320:-1:flags=lanczos,palettegen",
                        palette_file
                    ]
                    ret = asyncio.run(run_ffmpeg(palette_args))
                    if ret != 0:
                        raise RuntimeError(
                            "Palette generation for GIF failed.")
                    # Second pass: create the GIF using the palette.
                    gif_args = [
                        "-y", "-i", self.input_file, "-i", palette_file,
                        "-filter_complex", f"fps={desired_fps},scale=320:-1:flags=lanczos[x];[x][1:v]paletteuse",
                        self.output_file
                    ]
                    log = convert_file_with_full_args(gif_args)
                finally:
                    if os.path.exists(palette_file):
                        try:
                            os.remove(palette_file)
                        except Exception:
                            pass
            # --- Video Conversions (MP4, WEBM, MKV) ---
            elif ext in ['.mp4', '.webm', '.mkv']:
                input_bitrate = get_input_bitrate(self.input_file)
                bitrate_arg = None
                if input_bitrate:
                    target_bitrate = int(input_bitrate * self.quality / 100)
                    target_bitrate_k = target_bitrate // 1000
                    bitrate_arg = f"{target_bitrate_k}k"
                if self.extra_args is None:
                    base_extra_args = ["-pix_fmt", "yuv420p", "-r", "60"]
                    base_extra_args += ["-c:v", "libx264",
                                        "-preset", "fast", "-crf", "23"]
                else:
                    base_extra_args = self.extra_args.copy()
                if self.use_gpu:
                    if "-c:v" in base_extra_args:
                        idx = base_extra_args.index("-c:v")
                        base_extra_args[idx+1] = "h264_nvenc"
                    else:
                        base_extra_args = ["-c:v", "h264_nvenc",
                                           "-preset", "fast"] + base_extra_args
                    if "-crf" in base_extra_args:
                        idx = base_extra_args.index("-crf")
                        del base_extra_args[idx:idx+2]
                    if bitrate_arg:
                        base_extra_args += [
                            "-b:v", bitrate_arg,
                            "-maxrate", bitrate_arg,
                            "-bufsize", f"{(target_bitrate_k * 2)}k"
                        ]
                else:
                    if bitrate_arg:
                        base_extra_args += ["-b:v", bitrate_arg]
                if "-r" not in base_extra_args:
                    base_extra_args += ["-r", "60"]
                if "-pix_fmt" not in base_extra_args:
                    base_extra_args += ["-pix_fmt", "yuv420p"]
                log = asyncio.run(convert_file(
                    self.input_file, self.output_file, base_extra_args))
            # --- Other Conversions (e.g. Audio) ---
            else:
                log = asyncio.run(convert_file(
                    self.input_file, self.output_file, self.extra_args))
            self.conversionFinished.emit(
                self.output_file, "Conversion completed successfully.")
            self.logMessage.emit(log)
        except Exception as e:
            self.conversionError.emit(str(e))
            self.logMessage.emit(str(e))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Vidium Video Converter")
        self.resize(600, 500)
        self.initUI()

    def initUI(self):
        menu_bar = QMenuBar(self)
        self.setMenuBar(menu_bar)
        file_menu = QMenu("File", self)
        menu_bar.addMenu(file_menu)
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        edit_menu = QMenu("Edit", self)
        menu_bar.addMenu(edit_menu)
        help_menu = QMenu("Help", self)
        menu_bar.addMenu(help_menu)
        about_action = QAction("About", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)
        settings_menu = QMenu("Settings", self)
        menu_bar.addMenu(settings_menu)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        file_layout = QHBoxLayout()
        self.input_line_edit = QLineEdit()
        self.input_line_edit.setPlaceholderText("Select input file...")
        browse_button = QPushButton("Browse")
        browse_button.clicked.connect(self.browse_file)
        file_layout.addWidget(self.input_line_edit)
        file_layout.addWidget(browse_button)
        main_layout.addLayout(file_layout)

        output_layout = QHBoxLayout()
        self.output_line_edit = QLineEdit()
        self.output_line_edit.setPlaceholderText("Select output file...")
        output_browse_button = QPushButton("Browse")
        output_browse_button.clicked.connect(self.browse_output_file)
        output_layout.addWidget(self.output_line_edit)
        output_layout.addWidget(output_browse_button)
        main_layout.addLayout(output_layout)

        format_layout = QHBoxLayout()
        format_label = QLabel("Output Format:")
        self.output_format_combo = QComboBox()
        self.populate_output_format_combo()
        self.output_format_combo.currentIndexChanged.connect(
            self.output_format_changed)
        format_layout.addWidget(format_label)
        format_layout.addWidget(self.output_format_combo)
        main_layout.addLayout(format_layout)

        gpu_layout = QHBoxLayout()
        self.gpu_checkbox = QCheckBox("Use GPU")
        gpu_layout.addWidget(self.gpu_checkbox)
        main_layout.addLayout(gpu_layout)

        quality_layout = QVBoxLayout()
        quality_label = QLabel("Quality:")
        quality_layout.addWidget(quality_label)
        slider_layout = QHBoxLayout()
        self.quality_slider = QSlider(Qt.Horizontal)
        self.quality_slider.setMinimum(10)
        self.quality_slider.setMaximum(100)
        self.quality_slider.setTickInterval(10)
        self.quality_slider.setTickPosition(QSlider.TicksBelow)
        self.quality_slider.setValue(100)
        self.quality_slider.setFixedWidth(150)
        self.quality_slider.valueChanged.connect(self.update_quality_label)
        slider_layout.addWidget(self.quality_slider)
        self.quality_value_label = QLabel("100%")
        slider_layout.addWidget(self.quality_value_label)
        quality_layout.addLayout(slider_layout)
        main_layout.addLayout(quality_layout)

        self.convert_button = QPushButton("Convert")
        self.convert_button.clicked.connect(self.start_conversion)
        main_layout.addWidget(self.convert_button)

        self.status_label = QLabel("")
        main_layout.addWidget(self.status_label)

        self.log_text_edit = QPlainTextEdit()
        self.log_text_edit.setReadOnly(True)
        self.log_text_edit.setPlaceholderText(
            "Conversion log output will appear here...")
        self.log_text_edit.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.log_text_edit.setFixedHeight(150)
        self.log_text_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        text_option = QTextOption()
        text_option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.log_text_edit.document().setDefaultTextOption(text_option)
        self.log_text_edit.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.log_text_edit.setFixedWidth(580)
        main_layout.addWidget(self.log_text_edit)

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

    def get_selected_format(self):
        text = self.output_format_combo.currentText().strip()
        if text.endswith(":"):
            return "mp4"
        return text

    def output_format_changed(self):
        selected_format = self.get_selected_format().lower()
        if self.input_line_edit.text():
            base = os.path.splitext(os.path.basename(
                self.input_line_edit.text()))[0]
            self.output_line_edit.setText(os.path.join(
                OUTPUT_FOLDER, base + "." + selected_format))

    def browse_file(self):
        from PySide6.QtWidgets import QFileDialog
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Video File", "",
            "Video Files (*.mp4 *.mkv *.webm *.avi *.mov *.flv *.wmv *.mpeg *.mpg)"
        )
        if file_path:
            self.input_line_edit.setText(file_path)
            base = os.path.splitext(os.path.basename(file_path))[0]
            fmt = self.get_selected_format().lower()
            self.output_line_edit.setText(
                os.path.join(OUTPUT_FOLDER, base + "." + fmt))

    def browse_output_file(self):
        from PySide6.QtWidgets import QFileDialog
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Select Output File", OUTPUT_FOLDER, "All Files (*)"
        )
        if file_path:
            self.output_line_edit.setText(file_path)

    def update_quality_label(self, value):
        self.quality_value_label.setText(f"{value}%")

    def start_conversion(self):
        input_file = self.input_line_edit.text().strip()
        output_file = self.output_line_edit.text().strip()
        if not input_file or not output_file:
            self.status_label.setText(
                "Please select both input and output files.")
            return
        self.convert_button.setEnabled(False)
        self.status_label.setText("Conversion in progress...")
        selected_format = self.get_selected_format().lower()
        extra_args = None
        if selected_format == "gif":
            # This will be overridden by our two-pass method.
            extra_args = ["-vf", "fps=10,scale=320:-1:flags=lanczos"]
        use_gpu = self.gpu_checkbox.isChecked()
        quality = self.quality_slider.value()
        self.worker = ConversionWorker(
            input_file, output_file, extra_args, use_gpu, quality)
        self.worker.conversionFinished.connect(self.conversion_finished)
        self.worker.conversionError.connect(self.conversion_error)
        self.worker.logMessage.connect(self.append_log)
        self.worker.start()

    @Slot(str, str)
    def conversion_finished(self, output_file, message):
        self.status_label.setText(message)
        self.convert_button.setEnabled(True)

    @Slot(str)
    def conversion_error(self, error_message):
        self.status_label.setText("Error: " + error_message)
        self.convert_button.setEnabled(True)

    @Slot(str)
    def append_log(self, text):
        self.log_text_edit.appendPlainText(text)

    def show_about(self):
        self.status_label.setText(
            "Vidium Video Converter v1.0\nBuilt with PySide6 and bundled FFmpeg.")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
