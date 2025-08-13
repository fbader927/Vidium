import sys
import os
import asyncio
import tempfile
import subprocess
import math
import time
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLineEdit, QFileDialog, QLabel, QMenu, QComboBox, QPlainTextEdit,
    QCheckBox, QSlider, QListWidget, QListWidgetItem, QSizePolicy, QProgressBar,
    QGroupBox, QStyle, QTabWidget, QStatusBar, QSpacerItem, QGraphicsOpacityEffect,
    QStackedLayout, QGridLayout
)
from PySide6.QtGui import QStandardItemModel, QStandardItem, QFont, QPainter, QIcon, QColor, QPen
from PySide6.QtCore import Qt, QThread, Signal, Slot, QTimer, QSettings, QPoint, QUrl, QSize, QEvent, QPropertyAnimation, QCoreApplication
from PySide6.QtCore import QRectF
try:
    from PySide6.QtWebEngineWidgets import QWebEngineView
    WEBENGINE_AVAILABLE = True
except Exception:
    WEBENGINE_AVAILABLE = False
from converter import convert_file, OUTPUT_FOLDER, get_input_bitrate, run_ffmpeg, get_ffmpeg_path, get_ffprobe_path
from downloader import DownloadWorker, TrimWorker
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
import uuid  

# Ensure Chromium doesn't throttle timers/painting while resizing or occluded
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-background-timer-throttling --disable-renderer-backgrounding --disable-backgrounding-occluded-windows')
# Prefer desktop OpenGL for smoother compositing
try:
    QCoreApplication.setAttribute(Qt.AA_UseDesktopOpenGL)
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
except Exception:
    pass

# allow for editing timestamps for trimming videos
class FixedTimeLineEdit(QLineEdit):
    FORMAT = "00:00:00:00" # default timestamp format (HH:MM:SS:FF)
    DIGIT_POSITIONS = [0, 1, 3, 4, 6, 7, 9, 10] # positions of digits for edit

    def __init__(self, parent=None): # initialize required elements for GUI
        super().__init__(parent)
        self.setText(self.FORMAT) # set initial text to default format
        self.setFixedWidth(100) 
        self.setContextMenuPolicy(Qt.NoContextMenu) # disable right click
        self.setAlignment(Qt.AlignCenter) # text alignment
        self.setStyleSheet("margin: 0px; padding: 0px;") # remove spacing

    def keyPressEvent(self, event): # allow for user editing of the trim textboxes with strict rules
        key = event.key()
        text = event.text()
        if key in (Qt.Key_Backspace, Qt.Key_Delete):
            event.ignore() # block backspace and delete to prevent breaking format
            return
        if key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Tab, Qt.Key_Backtab, Qt.Key_Home, Qt.Key_End):
            super().keyPressEvent(event) # allow navigation keys
            return
        if not text.isdigit(): # only allow digits, ignore everything else
            event.ignore()
            return
        current = list(self.text()) # convert text to list for easier manip
        cursor = self.cursorPosition() 
        if self.hasSelectedText(): # if text is selected, overwrite from point of selection
            start = self.selectionStart()
            indices = [i for i in self.DIGIT_POSITIONS if i >=
                       start and i < start + len(self.selectedText())]
            pos = indices[0] if indices else start
        else:
            pos = cursor # if no selection use cursor
        while pos not in self.DIGIT_POSITIONS and pos < len(current):
            pos += 1 # skip non-digit positions
        i = 0
        while i < len(text) and pos < len(current): # insert new digits into correct posish
            if pos in self.DIGIT_POSITIONS:
                current[pos] = text[i]
                i += 1
            pos += 1
            while pos < len(current) and pos not in self.DIGIT_POSITIONS:
                pos += 1 # skip over colons if encountered
        new_text = "".join(current)
        self.setText(new_text) # update text with new values
        next_pos = pos if pos in self.DIGIT_POSITIONS else len(new_text) # move cursor
        self.setCursorPosition(next_pos)
        event.accept() # mark event handled

class ClickableSlider(QSlider): # slider element for conversion quality
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton: # only respond to left click
            new_value = QStyle.sliderValueFromPosition(
                self.minimum(), self.maximum(), event.x(), self.width()) # calculate value based on click position
            self.setValue(new_value) # set slider to new value
            self.sliderMoved.emit(new_value) # 
            event.accept() # mark event handled
        super().mousePressEvent(event) # ensure default behavior still applies 

class PlaceholderListWidget(QListWidget): # widget with placeholder text if empty
    def __init__(self, placeholder, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.placeholder = placeholder # text to display if list is empty

    def paintEvent(self, event): # override paintEvent to draw placeholder
        super().paintEvent(event) # keep default painting behavior
        if self.count() == 0: # if list is empty, show placeholder
            painter = QPainter(self.viewport())
            painter.setPen(QColor(0, 255, 255))
            # Give the text some horizontal padding and wrap so it's never cut off
            rect = self.viewport().rect().adjusted(12, 0, -12, 0)
            flags = Qt.AlignCenter | Qt.TextWordWrap
            painter.drawText(rect, flags, self.placeholder)

class ScanlineOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.flicker_alpha = 0.0
        self._tick = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._animate)
        self.timer.start(80)

    def _animate(self):
        self._tick = (self._tick + 1) % 50
        self.flicker_alpha = 0.02 if self._tick % 10 in (2, 3) else 0.0
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        if self.flicker_alpha > 0:
            painter.fillRect(self.rect(), QColor(0, 0, 0, int(self.flicker_alpha * 255)))
        height = self.height()
        painter.setPen(QColor(0, 0, 0, 60))
        y = 0
        while y < height:
            painter.drawLine(0, y + 2, self.width(), y + 2)
            y += 4


class OverlayContainer(QWidget):
    def __init__(self, base_widget: QWidget, overlay_widget: QWidget, parent=None):
        super().__init__(parent)
        self.base_widget = base_widget
        self.overlay_widget = overlay_widget
        self.reserve_right_ratio = 0.35  # portion of width reserved for sphere/right-dock
        # Allow per-container margin tuning so tabs can differ visually
        self._left_margin = 20
        self._top_margin = 20
        self._bottom_margin = 20
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.base_widget)
        # Reparent overlay to this container and place above
        self.overlay_widget.setParent(self)
        self.overlay_widget.raise_()

    def set_right_reserve_ratio(self, ratio: float):
        self.reserve_right_ratio = max(0.2, min(0.5, ratio))

    def set_top_margin(self, pixels: int):
        self._top_margin = max(0, min(40, int(pixels)))
        # Apply immediately if layout is available
        lay = self.overlay_widget.layout()
        if lay is not None:
            right_margin = int(self.width() * self.reserve_right_ratio)
            lay.setContentsMargins(self._left_margin, self._top_margin, right_margin, self._bottom_margin)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.overlay_widget is not None:
            self.overlay_widget.setGeometry(self.rect())
            # keep left UI clear of sphere area by adding dynamic right margin
            lay = self.overlay_widget.layout()
            if lay is not None:
                right_margin = int(self.width() * self.reserve_right_ratio)
                lay.setContentsMargins(self._left_margin, self._top_margin, right_margin, self._bottom_margin)
            # position any child panel named 'rightDock' into the reserved area
            dock = self.overlay_widget.findChild(QWidget, 'rightDock')
            if dock is not None:
                right_margin = int(self.width() * self.reserve_right_ratio)
                x = self.width() - right_margin + 10
                w = right_margin - 30
                if w < 200:
                    w = max(200, right_margin - 20)
                dock.setGeometry(x, 20, w, self.height() - 40)

class SphereWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._progress = 0
        self._angle_deg = 0.0
        self._pulse = 0.0
        self._offset_ratio = 0.0  # X axis offset: -0.5 .. 0.5
        self._offset_y_ratio = 0.0  # Y axis offset: -0.5 .. 0.5 (negative = up)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)
        self.setMinimumHeight(220)

    def set_progress(self, value: int):
        v = max(0, min(100, int(value)))
        if v != self._progress:
            self._progress = v
            self.update()

    def _tick(self):
        self._angle_deg = (self._angle_deg + 0.18) % 360
        self._pulse = (math.sin(math.radians(self._angle_deg*2)) + 1) * 0.5
        self.update()

    def sizeHint(self):
        return QSize(360, 200)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(10, 10, -10, -10)
        # Shift horizontally according to offset ratio so we can align under different side panels
        cx = rect.center().x() + int(max(-0.5, min(0.5, self._offset_ratio)) * (rect.width() * 0.5))
        cy = rect.center().y() + int(max(-0.5, min(0.5, self._offset_y_ratio)) * (rect.height() * 0.5))
        radius = min(rect.width(), rect.height()) // 4
        circle_rect = QRect = rect.adjusted(rect.width()//2 - radius - (rect.center().x()-cx),
                                            rect.height()//2 - radius - (rect.center().y()-cy),
                                            -(rect.width()//2 - radius) + (rect.center().x()-cx),
                                            -(rect.height()//2 - radius) + (rect.center().y()-cy))
        circle_rect = QRect
        # Blue inner glow
        glow_color = QColor(0, 119, 255, 60 + int(80*self._pulse))
        painter.setBrush(glow_color)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPoint(cx, cy), int(radius*0.95), int(radius*0.95))
        # Outer ring
        painter.setPen(QPen(QColor(255, 72, 0, 110), 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QPoint(cx, cy), radius, radius)
        # Secondary rings
        painter.setPen(QPen(QColor(255, 72, 0, 80), 1))
        painter.drawEllipse(QPoint(cx, cy), int(radius*0.8), int(radius*0.8))
        painter.setPen(QPen(QColor(255, 72, 0, 60), 1))
        painter.drawEllipse(QPoint(cx, cy), int(radius*0.6), int(radius*0.6))
        # Crosshair
        painter.setPen(QPen(QColor(255, 72, 0, 180), 2))
        painter.drawLine(cx - radius, cy, cx + radius, cy)
        painter.drawLine(cx, cy - radius, cx, cy + radius)
        # Cyan wireframe: meridians and parallels
        painter.save()
        painter.translate(cx, cy)
        painter.rotate(self._angle_deg)
        wire_pen = QPen(QColor(0, 255, 255, 120))
        wire_pen.setWidth(1)
        painter.setPen(wire_pen)
        for i in range(0, 12):
            angle = i * 15
            painter.save()
            painter.rotate(angle)
            painter.drawEllipse(QPoint(0, 0), radius, int(radius*0.15))
            painter.restore()
        for j in range(-3, 4):
            k = j / 4.0
            h = int(radius * (1 - abs(k)*0.85))
            painter.drawEllipse(QPoint(0, 0), radius, h)
        painter.restore()
        # Progress arc (cyan)
        painter.setRenderHint(QPainter.Antialiasing, True)
        arc_pen = QPen(QColor(0, 255, 255, 220), 5)
        arc_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(arc_pen)
        arc_rect = QRectF(cx - radius + 3, cy - radius + 3, (radius - 3)*2, (radius - 3)*2)
        start_angle = -90 * 16
        span_angle = int(360 * 16 * (self._progress/100.0))
        painter.drawArc(arc_rect, start_angle, span_angle)

        # Subtle noisy overlay on sphere to mimic CRT
        painter.setPen(QPen(QColor(0, 0, 0, 40), 1))
        for y in range(int(cy - radius), int(cy + radius), 4):
            painter.drawLine(int(cx - radius), y, int(cx + radius), y)

    def set_offset_ratio(self, ratio: float):
        # Accept the same API as the WebEngine version
        try:
            self._offset_ratio = max(-0.5, min(0.5, float(ratio)))
            self.update()
        except Exception:
            pass

    def set_offset_y_ratio(self, ratio: float):
        try:
            self._offset_y_ratio = max(-0.5, min(0.5, float(ratio)))
            self.update()
        except Exception:
            pass

    def set_offset_ratios(self, x_ratio: float, y_ratio: float):
        try:
            self._offset_ratio = max(-0.5, min(0.5, float(x_ratio)))
            self._offset_y_ratio = max(-0.5, min(0.5, float(y_ratio)))
            self.update()
        except Exception:
            pass

class ThreeSphereView(QWebEngineView if WEBENGINE_AVAILABLE else QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        if WEBENGINE_AVAILABLE:
            from pathlib import Path
            # Inline HTML based on UI_reference.html with 3D sphere and terminal
            html = _build_html(self)
            self.setHtml(html)
            # Let overlay UI receive all mouse events
            try:
                self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                self.setFocusPolicy(Qt.NoFocus)
                self.setEnabled(False)
            except Exception:
                pass
            self._web_ready = False
            self._pending_offset_ratio = None
            try:
                self.loadFinished.connect(self._on_web_loaded)
            except Exception:
                pass
        else:
            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            self.fallback = SphereWidget()
            layout.addWidget(self.fallback)
            self.hud = QPlainTextEdit()
            self.hud.setReadOnly(True)
            self.hud.setFixedHeight(76)
            layout.addWidget(self.hud)
        # Store pending offsets until the Web page is ready
        self._pending_canvas_offset = None

    def set_progress(self, percent: int):
        if WEBENGINE_AVAILABLE:
            try:
                self.page().runJavaScript(f"window.__vidiumSetProgress({int(percent)});")
            except Exception:
                pass
        else:
            try:
                self.fallback.set_progress(percent)
            except Exception:
                pass

    def append_log(self, text: str):
        if WEBENGINE_AVAILABLE:
            import json
            try:
                js_arg = json.dumps(text)
            except Exception:
                js_arg = '"' + text.replace("\\", "\\\\").replace("\n", " ").replace("\"", "\\\"") + '"'
            try:
                self.page().runJavaScript(f"window.__vidiumAppendLog({js_arg});")
            except Exception:
                pass
        else:
            try:
                self.hud.appendPlainText(text)
            except Exception:
                pass

    def set_offset_ratio(self, ratio: float):
        # Move sphere horizontally within view (-0.5..0.5)
        if WEBENGINE_AVAILABLE:
            try:
                if getattr(self, "_web_ready", False):
                    self.page().runJavaScript(f"window.__vidiumSetOffsetRatio({ratio});")
                else:
                    self._pending_offset_ratio = ratio
            except Exception:
                pass
        else:
            # Fallback widget: forward to internal sphere
            try:
                self.fallback.set_offset_ratio(ratio)
            except Exception:
                pass

    def set_dock_width_ratio(self, ratio: float):
        if WEBENGINE_AVAILABLE:
            try:
                self.page().runJavaScript(f"window.__vidiumSetDockWidth({max(0.2, min(0.5, ratio))});")
            except Exception:
                pass

    def set_canvas_offset(self, x_pixels: float, y_pixels: float):
        if WEBENGINE_AVAILABLE:
            try:
                if getattr(self, "_web_ready", False):
                    self.page().runJavaScript(f"window.__vidiumSetCanvasOffset && window.__vidiumSetCanvasOffset({float(x_pixels)}, {float(y_pixels)});")
                else:
                    self._pending_canvas_offset = (float(x_pixels), float(y_pixels))
            except Exception:
                pass

    def _on_web_loaded(self, ok: bool):
        try:
            # External driver to keep animation running even during window resize
            self._anim_timer = QTimer(self)
            try:
                self._anim_timer.setTimerType(Qt.PreciseTimer)
            except Exception:
                pass
            self._anim_timer.setInterval(16)
            self._anim_timer.timeout.connect(lambda: self.page().runJavaScript("window.__vidiumExternalTick && window.__vidiumExternalTick();"))
            self._anim_timer.start()
            self._web_ready = True
            if getattr(self, '_pending_offset_ratio', None) is not None:
                try:
                    self.page().runJavaScript(f"window.__vidiumSetOffsetRatio({float(self._pending_offset_ratio)});")
                except Exception:
                    pass
                self._pending_offset_ratio = None
            if self._pending_canvas_offset is not None:
                try:
                    x, y = self._pending_canvas_offset
                    self.page().runJavaScript(f"window.__vidiumSetCanvasOffset && window.__vidiumSetCanvasOffset({float(x)}, {float(y)});")
                except Exception:
                    pass
                self._pending_canvas_offset = None
        except Exception:
            pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if WEBENGINE_AVAILABLE:
            try:
                self.page().runJavaScript("window.__vidiumScheduleCommit && window.__vidiumScheduleCommit();")
            except Exception:
                pass

class AutoScrollTerminal(QPlainTextEdit):
    def __init__(self, parent=None, line_limit: int = 1200, trim_step: int = 200,
                 tick_ms: int = 16, pixels_per_tick: int = 1):
        super().__init__(parent)
        self.setReadOnly(True)
        self._line_limit = max(200, line_limit)
        self._trim_step = max(50, min(trim_step, self._line_limit // 2))
        self._paused_hover = False
        self._paused_focus = False
        self._pixels_per_tick = max(1, pixels_per_tick)
        # Drift control (time-based drift for smoothness)
        self._last_append_s = time.monotonic()
        self._last_cleared_s = 0.0
        self._drift_speed_px_s = 10.0  # pixels per second (cinematic smooth)
        self._subpixel_acc = 0.0
        self._last_tick_time = time.monotonic()
        self._resume_after_s = 0.0  # delay before resuming after mouse leaves
        self._reset_on_resume = False
        self._resume_delay_s = 2.0  # 2s resume delay per requirement
        self._idle_clear_s = 2.0    # only clear if idle (no new lines) for at least this long
        self._min_clear_interval_s = 5.0  # avoid rapid clear-loop
        self._fade_lines_remaining = 0     # number of blank lines to append for natural fade-out
        self._auto_timer = QTimer(self)
        self._auto_timer.timeout.connect(self._auto_scroll_step)
        self._auto_timer.start(tick_ms)
        # Hide scrollbars by default; reveal on hover so it looks clean
        try:
            self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        except Exception:
            pass
        self._rehydrate_provider = None
        self._direction = -1  # -1: upward (content moves up), +1: downward (content moves down)

    def set_rehydrate_provider(self, provider):
        # provider: Callable[[], str]
        self._rehydrate_provider = provider

    def _auto_scroll_step(self):
        now = time.monotonic()
        dt = max(0.0, now - self._last_tick_time)
        self._last_tick_time = now
        # Respect pause only if the cursor is truly inside the terminal rect; if you're elsewhere
        # in the same window but not hovering the terminal, keep drifting.
        if (self._paused_hover or self._paused_focus) and self.underMouse():
            return
        if now < self._resume_after_s:
            return
        sb = self.verticalScrollBar()
        if not sb:
            return
        # No snap; rely on smooth drift only
        if self._reset_on_resume and not (self._paused_hover or self._paused_focus):
            self._reset_on_resume = False
        # Smooth time-based drift (direction controlled)
        self._subpixel_acc += self._drift_speed_px_s * dt
        steps = int(self._subpixel_acc)
        if steps > 0:
            self._subpixel_acc -= steps
            if self._direction < 0:
                # Upward drift (content moves up): reduce scrollbar value
                if sb.value() > sb.minimum():
                    sb.setValue(max(sb.value() - steps, sb.minimum()))
                    return
            else:
                # Downward drift (content moves down): increase scrollbar value
                if sb.value() < sb.maximum():
                    sb.setValue(min(sb.value() + steps, sb.maximum()))
                    return
        # Natural fade-out: when idle and not hovered, append blank lines to let content scroll out
        idle_enough = (now - self._last_append_s) >= self._idle_clear_s
        can_clear = (now - self._last_cleared_s) >= self._min_clear_interval_s
        not_interacting = (not self.underMouse()) and (not self.hasFocus())
        if not_interacting and idle_enough and can_clear and ((self._direction < 0 and sb.value() <= sb.minimum()) or (self._direction > 0 and sb.value() >= sb.maximum())):
            try:
                self.document().clear()
            except Exception:
                pass
            self._last_cleared_s = now
            sb.setValue(sb.minimum() if self._direction < 0 else sb.maximum())

    def enterEvent(self, event):
        self._paused_hover = True
        try:
            self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        except Exception:
            pass
        # Rehydrate full log view when user hovers and visible buffer is empty
        try:
            if self._rehydrate_provider is not None and self.document().blockCount() <= 1:
                full_text = self._rehydrate_provider() or ""
                if full_text:
                    self.setPlainText(full_text)
                    sb = self.verticalScrollBar()
                    if sb:
                        sb.setValue(sb.maximum())
        except Exception:
            pass
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._paused_hover = False
        try:
            self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        except Exception:
            pass
        # Start a small resume delay so drift won't jump immediately
        self._resume_after_s = time.monotonic() + self._resume_delay_s
        self._reset_on_resume = True
        super().leaveEvent(event)

    def focusInEvent(self, event):
        self._paused_focus = True
        # Keep scrollbars visible while interacting
        try:
            self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        except Exception:
            pass
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self._paused_focus = False
        # Hide when focus leaves and mouse not hovering
        if not self._paused_hover:
            try:
                self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
                self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            except Exception:
                pass
        super().focusOutEvent(event)

    def wheelEvent(self, event):
        self._paused_focus = True
        try:
            self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        except Exception:
            pass
        super().wheelEvent(event)

    def append_scrolling(self, text: str):
        # Append without forcing scroll jumps; let drift create the motion
        sb = self.verticalScrollBar()
        old_at_bottom = (sb.value() == sb.maximum()) if sb else False
        self.appendPlainText(text.rstrip("\n"))
        self._trim_old_lines()
        self._last_append_s = time.monotonic()
        # If user is interacting and we were at bottom, keep pinned to bottom for readability
        if sb and (self._paused_hover or self._paused_focus) and old_at_bottom:
            try:
                sb.setValue(sb.maximum())
            except Exception:
                pass

    def _trim_old_lines(self):
        doc = self.document()
        blocks = doc.blockCount()
        if blocks <= self._line_limit:
            return
        extra = blocks - self._line_limit + self._trim_step
        extra = max(self._trim_step, extra)
        from PySide6.QtGui import QTextCursor
        cur = QTextCursor(doc)
        start_pos = doc.findBlockByNumber(0).position()
        end_pos = doc.findBlockByNumber(min(extra, blocks-1)).position()
        cur.setPosition(start_pos)
        cur.setPosition(end_pos, QTextCursor.KeepAnchor)
        cur.removeSelectedText()

    def set_progress(self, percent: int):
        if WEBENGINE_AVAILABLE:
            self.page().runJavaScript(f"window.__vidiumSetProgress({int(percent)});")
        else:
            self.fallback.set_progress(percent)

    def append_log(self, text: str):
        if WEBENGINE_AVAILABLE:
            import json
            try:
                js_arg = json.dumps(text)
            except Exception:
                # fallback minimal escape if json fails
                js_arg = '"' + text.replace("\\", "\\\\").replace("\n", " ").replace("\"", "\\\"") + '"'
            self.page().runJavaScript(f"window.__vidiumAppendLog({js_arg});")
        else:
            self.hud.appendPlainText(text)

    def set_offset_ratio(self, ratio: float):
        # Move sphere horizontally within view (-0.5..0.5)
        if WEBENGINE_AVAILABLE:
            try:
                if getattr(self, "_web_ready", False):
                    self.page().runJavaScript(f"window.__vidiumSetOffsetRatio({ratio});")
                else:
                    self._pending_offset_ratio = ratio
            except Exception:
                pass

    def set_dock_width_ratio(self, ratio: float):
        if WEBENGINE_AVAILABLE:
            try:
                self.page().runJavaScript(f"window.__vidiumSetDockWidth({max(0.2,min(0.5,ratio))});")
            except Exception:
                pass

    def _on_web_loaded(self, ok: bool):
        try:
            # External driver to keep animation running even during window resize
            self._anim_timer = QTimer(self)
            try:
                self._anim_timer.setTimerType(Qt.PreciseTimer)
            except Exception:
                pass
            self._anim_timer.setInterval(16)
            self._anim_timer.timeout.connect(lambda: self.page().runJavaScript("window.__vidiumExternalTick && window.__vidiumExternalTick();"))
            self._anim_timer.start()
            self._web_ready = True
            if self._pending_offset_ratio is not None:
                try:
                    self.page().runJavaScript(f"window.__vidiumSetOffsetRatio({float(self._pending_offset_ratio)});")
                except Exception:
                    pass
                self._pending_offset_ratio = None
        except Exception:
            pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if WEBENGINE_AVAILABLE:
            try:
                self.page().runJavaScript("window.__vidiumScheduleCommit && window.__vidiumScheduleCommit();")
            except Exception:
                pass

def _build_html(self) -> str:
    return r"""
<!DOCTYPE html>
<html lang='en'>
<head>
  <meta charset='UTF-8' />
  <meta name='viewport' content='width=device-width, initial-scale=1.0' />
  <style>
    html, body { margin:0; height:100%; background:#000; overflow:hidden; }
    #wrap { position:relative; width:100%; height:100%; background:#000; }
    #sphere { position:absolute; inset:0; will-change: transform; contain: strict; }
    #overlay { position:absolute; inset:0; pointer-events:none; }
    /* Disable grid overlay entirely */
    .grid-lines { display:none !important; }
    :root { --dockW: 35%; }
    #term { position:absolute; top:20px; bottom:20px; right:10px; width:calc(var(--dockW) - 20px); color:#00ffff; font-family:'Courier New', monospace; font-size:11px; line-height:1.25; overflow:hidden; opacity:0.85; }
    #termInner { position:absolute; left:0; right:0; bottom:-100%; display:block; will-change: transform; transform: translateZ(0); }
    .noiseLine { color:#00ffff; opacity:0.12; white-space:nowrap; }
    .realLine { color:#00ffff; opacity:0.95; text-shadow:0 0 8px rgba(0,255,255,0.75); white-space:normal; word-wrap:break-word; overflow-wrap:anywhere; }
  </style>
</head>
<body>
  <div id='wrap'>
    <div id='sphere'></div>
    <div id='overlay'>
      <div class='grid-lines' id='grid'></div>
      <div id='term'><div id='termInner'></div></div>
    </div>
  </div>
  <script src='https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js'></script>
  <script>
    const container = document.getElementById('sphere');
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(75, 1, 0.1, 1000);
    camera.position.z = 5;
    const renderer = new THREE.WebGLRenderer({ antialias:true, alpha:false, powerPreference:'high-performance' });
    container.appendChild(renderer.domElement);
    // Center canvas in its container so layout remains symmetrical
    const canvas = renderer.domElement;
    canvas.style.position = 'absolute';
    canvas.style.left = '50%';
    canvas.style.top = '50%';
    let canvasOffsetX = 0; let canvasOffsetY = 0; // pixel offsets
    function applyCanvasOffset(){
      canvas.style.transform = `translate(calc(-50% + ${canvasOffsetX}px), calc(-50% + ${canvasOffsetY}px))`;
    }
    applyCanvasOffset();

    const group = new THREE.Group(); scene.add(group);
    const geometry = new THREE.SphereGeometry(2, 32, 32); // doubled size
    const wireframe = new THREE.WireframeGeometry(geometry);
    const lineMaterial = new THREE.LineBasicMaterial({ color:0x00ffff, transparent:true, opacity:0.6 });
    const wireframeMesh = new THREE.LineSegments(wireframe, lineMaterial);
    group.add(wireframeMesh);

    const innerMaterial = new THREE.MeshBasicMaterial({ color:0x0077FF, transparent:true, opacity:0.18 });
    const innerSphere = new THREE.Mesh(geometry, innerMaterial);
    innerSphere.scale.set(0.95,0.95,0.95);
    group.add(innerSphere);

    let progress = 0; // 0..100
    let offsetRatio = 0.0; // -0.5..0.5
    function buildGrid(){ const overlay = document.getElementById('grid'); if (overlay) overlay.innerHTML=''; }
    // Fixed-size canvas cap: never exceed half the window's smaller dimension
    function computeCanvasSize(w,h){
      const s = Math.min(w, h);
      const side = Math.max(1, Math.floor(s * 0.5));
      return [side, side, w];
    }
    function resize(){
      const w = container.clientWidth; const h = container.clientHeight;
      const [cw, ch, usableW] = computeCanvasSize(w, h);
      renderer.setSize(cw, ch, false);
      camera.aspect=cw/ch; camera.updateProjectionMatrix();
      buildGrid();
      // Keep 3D group centered; any user offset is applied in world units (small)
      group.position.x = offsetRatio; // -0.5..0.5 world units
      applyCanvasOffset();
    }
    const clock = new THREE.Clock();
    function animate(){ requestAnimationFrame(animate); const dt = clock.getDelta(); group.rotation.x += 0.8*dt; group.rotation.y += 1.2*dt; const t = clock.elapsedTime; const pulse = 0.95 + 0.03*Math.sin(t); innerSphere.scale.set(pulse,pulse,pulse); renderer.render(scene,camera); }
    resize(); animate(); window.addEventListener('resize', resize);

    // Progress (no hex overlay now)
    function setProgress(p){ progress = Math.max(0, Math.min(100, p|0)); }
    // Noise terminal elements (lighter)
    const term = document.getElementById('term');
    const termInner = document.getElementById('termInner');
    let scrollY = 0;
    function noiseTick(){
      const lines = Math.random()>0.7 ? 2 : 1;
      for(let i=0;i<lines;i++){
        const d = document.createElement('div');
        d.className = 'noiseLine';
        d.textContent = genNoise();
        termInner.appendChild(d);
      }
      scrollY += 12;
      termInner.style.transform = `translateY(${ -scrollY }px)`;
      while (termInner.childElementCount > 150) termInner.removeChild(termInner.firstChild);
    }
    function genNoise(){
      const t = Date.now().toString(16).slice(-6);
      const r = Math.random().toString(16).slice(2, 10);
      return `0x${t} :: ${r}`;
    }
    setInterval(noiseTick, 220);
    function appendLog(line){
      const div = document.createElement('div');
      div.className = 'realLine';
      div.textContent = line;
      termInner.appendChild(div);
      scrollY += 14;
      termInner.style.transform = `translateY(${ -scrollY }px)`;
      while (termInner.childElementCount > 300) termInner.removeChild(termInner.firstChild);
    }
    function setDockWidth(r){ document.documentElement.style.setProperty('--dockW', ((r*100)|0)+'%'); resize(); }
    window.__vidiumSetProgress = setProgress;
    window.__vidiumAppendLog = appendLog;
    window.__vidiumSetOffsetRatio = function(r){
      offsetRatio = Math.max(-0.5, Math.min(0.5, r));
      group.position.x = offsetRatio;
    };
    window.__vidiumSetDockWidth = setDockWidth;
    window.__vidiumSetCanvasOffset = function(x,y){
      canvasOffsetX = isFinite(x) ? x : 0; canvasOffsetY = isFinite(y) ? y : 0; applyCanvasOffset();
    };
  </script>
 </body>
 </html>
 """

def convert_file_with_full_args(args: list) -> str: # runs ffmpeg with full arguments and returns log
    ffmpeg_path = get_ffmpeg_path() 
    cmd = [ffmpeg_path] + args # build full ffmpeg command list
    print(f"Running command: {' '.join(cmd)}")

    async def run_cmd():
        from asyncio.subprocess import PIPE
        process = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE) # run ffmpeg asynchronously 
        stdout, stderr = await process.communicate() # wait for process to finish
        log = ""
        if stdout:
            log += f"[stdout]\n{stdout.decode()}\n" # capture standard output
        if stderr:
            log += f"[stderr]\n{stderr.decode()}\n" # capture error output
        if process.returncode != 0:
            raise RuntimeError(f"Command failed with code {process.returncode}. Log: {log}")
        return log
    return asyncio.run(run_cmd()) # run async function in sync context

class PreviewConversionWorker(QThread): # worker thread for short preview conversion of videos
    conversionFinished = Signal(str) # signal emitted when conversion is done

    def __init__(self, input_file, output_file, use_gpu=False):
        super().__init__()
        self.input_file = input_file # path to input file
        self.output_file = output_file # path for converted preview
        self.use_gpu = use_gpu # flag to enable GPU acceleration

    def run(self): # preview file conversion
        ffmpeg_path = get_ffmpeg_path()
        cmd = [ffmpeg_path, "-y"] #  start ffmpeg with overwrite flag
        if self.use_gpu: # if gpu accel enabled
            gpu_flags = ["-hwaccel", "cuda", "-hwaccel_output_format", "nv12"]
            cmd.extend(gpu_flags)
        cmd.extend([ # build ffmpeg command
            "-i", self.input_file,
            "-t", "30",
            "-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "500k",
            "-c:a", "libvorbis",
            "-f", "webm",
            self.output_file
        ])
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        self.conversionFinished.emit(self.output_file) # signal ffmpeg process was finished


 

class ConversionWorker(QThread): # worker thread for handling video conversion with async operations
    conversionFinished = Signal(str, str) # emitted signal when conversion completes
    conversionError = Signal(str) # emit error
    logMessage = Signal(str) # emit logs
    progressUpdated = Signal(int) # emit progress percent (0-100)

    def __init__(self, input_file, output_file, extra_args=None, use_gpu=False, quality=100):
        super().__init__()
        self.input_file = input_file
        self.output_file = output_file
        self.extra_args = extra_args
        self.use_gpu = use_gpu
        self.quality = quality
        self._stop_event = None # used for signaling cancellation
        self._loop = None # asyncio loop event

    def stop(self): # safely trigger stop event from outside event loop
        if self._stop_event and self._loop: 
            self._loop.call_soon_threadsafe(self._stop_event.set)

    async def run_command_with_args(self, args: list) -> str: # run fmmpeg async and handle cancellations
        ffmpeg_path = get_ffmpeg_path()
        cmd = [ffmpeg_path] + args
        print(f"Running command: {' '.join(cmd)}")
        from asyncio.subprocess import PIPE
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=PIPE,
            stderr=PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        wait_tasks = [asyncio.create_task(process.communicate()),
                      asyncio.create_task(self._stop_event.wait())]
        done, pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED) # wait for either process completion or stop event
        if wait_tasks[1] in done: # stop event trigger
            process.kill()
            await process.wait()
            for t in pending:
                t.cancel()
            if os.path.exists(self.output_file): # clean up partial file
                os.remove(self.output_file)
            raise asyncio.CancelledError("Conversion was stopped.")
        else: # process finished normally
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

    async def do_conversion(self): # handle different file types and prepare ffmpeg argument
        from converter import run_ffmpeg, get_input_bitrate, convert_file
        ext = os.path.splitext(self.output_file)[1].lower()
        log = ""
        if ext == '.gif': # convert GIF with palette generation
            desired_fps = 30 if self.quality >= 80 else 10
            palette_file = os.path.join(OUTPUT_FOLDER, f"palette_temp_{uuid.uuid4().hex}.png")
            os.makedirs(os.path.dirname(palette_file), exist_ok=True)
            try: # generate color palette for GIF
                palette_args = ["-y", "-i", self.input_file, "-vf",
                                f"fps={desired_fps},scale=320:-1:flags=lanczos,palettegen",
                                "-frames:v", "1", palette_file]
                ret = await run_ffmpeg(palette_args, self._stop_event)
                if ret != 0:
                    raise RuntimeError("Palette generation for GIF failed.")
                gif_args = ["-y", "-i", self.input_file, "-i", palette_file, "-filter_complex",
                            f"fps={desired_fps},scale=320:-1:flags=lanczos[x];[x][1:v]paletteuse", self.output_file] # apply palette for GIF
                log = await self.run_command_with_args(gif_args)
            finally: 
                if os.path.exists(palette_file):
                    try:
                        os.remove(palette_file) # cleanup temp palette file
                    except Exception:
                        pass
        elif ext in ['.mp4', '.webm', '.mkv']: # handle video formats
            input_bitrate = get_input_bitrate(self.input_file)
            bitrate_arg = None
            target_bitrate_k = None
            if input_bitrate:
                target_bitrate = int(input_bitrate * self.quality / 100)
                target_bitrate_k = target_bitrate // 1000
                bitrate_arg = f"{target_bitrate_k}k"
            if self.extra_args is None:
                if ext == ".webm":  # use VP9 settings since webm doesnt support GPU acceleration
                    base_extra_args = [
                        "-pix_fmt", "yuv420p", "-r", "60",
                        "-c:v", "libvpx-vp9", "-quality", "good", "-cpu-used", "4",
                        "-tile-columns", "6", "-frame-parallel", "1",
                        "-crf", "30", "-b:v", "0"
                    ]
                else:
                    base_extra_args = [
                        "-pix_fmt", "yuv420p", "-r", "60",
                        "-c:v", "libx264", "-preset", "fast", "-crf", "23"
                    ]
            else:
                base_extra_args = self.extra_args.copy()
            if not self.use_gpu and self.quality == 100 and ext != ".webm": # adjust settings for quality/GPU usage
                if "-crf" in base_extra_args:
                    idx = base_extra_args.index("-crf")
                    del base_extra_args[idx:idx+2]
                if self.input_file.lower().endswith(".webm"):
                    base_extra_args += ["-crf", "23", "-preset", "veryslow"]
                else:
                    base_extra_args += ["-crf", "18", "-preset", "veryslow"]
            if self.use_gpu:
                if "-pix_fmt" in base_extra_args:
                    idx = base_extra_args.index("-pix_fmt")
                    del base_extra_args[idx:idx+2]
                if "-crf" in base_extra_args:
                    idx = base_extra_args.index("-crf")
                    del base_extra_args[idx:idx+2]
                # Preserve input framerate when using NVENC; remove forced -r 60
                if "-r" in base_extra_args:
                    try:
                        idx = base_extra_args.index("-r")
                        del base_extra_args[idx:idx+2]
                    except Exception:
                        pass
                if "-c:v" in base_extra_args:
                    idx = base_extra_args.index("-c:v")
                    base_extra_args[idx+1] = "h264_nvenc"
                else:
                    base_extra_args = ["-c:v", "h264_nvenc",
                                       "-preset", "fast"] + base_extra_args
                if self.input_file.lower().endswith(".webm"):
                    base_extra_args += ["-tile-columns",
                                        "6", "-frame-parallel", "1"]
                # Choose NVENC rate-control tuned by quality setting
                # Map quality slider (10..100) → CQ value roughly (26..20)
                try:
                    cq_value = 22
                    if self.quality >= 95:
                        cq_value = 20
                    elif self.quality >= 85:
                        cq_value = 22
                    elif self.quality >= 75:
                        cq_value = 24
                    else:
                        cq_value = 26
                except Exception:
                    cq_value = 22
                if bitrate_arg:
                    # VBR-HQ with target around source bitrate keeps sizes sane and fast
                    base_extra_args += ["-rc", "vbr_hq", "-cq", str(cq_value),
                                        "-b:v", bitrate_arg, "-maxrate", bitrate_arg,
                                        "-bufsize", f"{(target_bitrate_k * 2)}k"]
                else:
                    # Fallback to CQ only
                    base_extra_args += ["-rc", "vbr_hq", "-cq", str(cq_value)]
                if self.quality == 100:
                    from converter import is_video_10bit
                    if is_video_10bit(self.input_file):
                        if "-c:v" in base_extra_args:
                            idx = base_extra_args.index("-c:v")
                            base_extra_args[idx+1] = "hevc_nvenc"
                        else:
                            base_extra_args = [
                                "-c:v", "hevc_nvenc", "-preset", "fast"] + base_extra_args
                        base_extra_args += ["-profile:v",
                                            "main10", "-pix_fmt", "p010le"]
                # Copy audio to avoid re-encoding time and keep size reasonable
                if "-c:a" not in base_extra_args:
                    base_extra_args += ["-c:a", "copy"]
            from converter import convert_file
            # Emit progress from worker thread; UI will update via signal in main thread
            def _on_progress(pct: int):
                try:
                    self.progressUpdated.emit(int(pct))
                except Exception:
                    pass
            def _on_log(chunk: str):
                try:
                    self.logMessage.emit(chunk)
                except Exception:
                    pass
            log = await convert_file(self.input_file, self.output_file, base_extra_args,
                                     use_gpu=self.use_gpu, stop_event=self._stop_event,
                                     progress_callback=_on_progress, log_callback=_on_log)
        else: # for unsupported extensions just use extra_args
            from converter import convert_file
            def _on_progress2(pct: int):
                try:
                    self.progressUpdated.emit(int(pct))
                except Exception:
                    pass
            def _on_log2(chunk: str):
                try:
                    self.logMessage.emit(chunk)
                except Exception:
                    pass
            log = await convert_file(self.input_file, self.output_file, self.extra_args,
                                     stop_event=self._stop_event,
                                     progress_callback=_on_progress2, log_callback=_on_log2)
        return log

    def run(self): # entry point for when thread starts
        self._stop_event = asyncio.Event()
        loop = asyncio.new_event_loop()
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

class MainWindow(QMainWindow): # main app window
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Vidium")
        # Fixed window size for consistent layout
        self.setFixedSize(1280, 720)
        
        if getattr(sys, 'frozen', False): # handle app icon path based on executable or dev environment
            base_path = os.path.dirname(sys.executable)
            icon_path = os.path.join(base_path, "_internal", "vicon.ico")
        else:
            base_path = os.path.abspath(".")
            icon_path = os.path.join(base_path, "vicon.ico")
        self.setWindowIcon(QIcon(icon_path))
        self.setStatusBar(QStatusBar())
        self.current_index = 0 # initialize state variables 
        self.overall_progress = 0.0
        self.settings = QSettings("FBB", "VidiumConverter")
        self.conversion_aborted = False
        self.conversion_active = False
        self.worker = None # worker references for background tasks
        self.download_worker = None
        self.download_conversion_worker = None
        self.preview_conversion_worker = None
        self.trim_worker = None
        self.gpu_checkbox = QCheckBox("Use GPU (Very Fast)")
        try:
            self.gpu_checkbox.setChecked(self.settings.value("gpu_enabled", True, type=bool))
            self.gpu_checkbox.setStyleSheet("color: #00FFFF;")
        except Exception:
            self.gpu_checkbox.setChecked(True)
        try:
            self.gpu_checkbox.toggled.connect(self.on_gpu_checkbox_toggled)
        except Exception:
            pass
        self.intermediate_file = None
        self.initUI() # build UI
        self.setAcceptDrops(True) # enable drag and drop
        self.init_drop_overlay()
        if hasattr(self, 'video_widget') and self.video_widget is not None:
            self.video_widget.installEventFilter(self)
        # CRT overlay (on top, mouse transparent)
        self.crt_overlay = ScanlineOverlay(self)
        self.crt_overlay.setGeometry(self.rect())
        self.crt_overlay.raise_()
        self.crt_overlay.show()
        # Replace countdown with static iteration label
        self.countdown_secs = None
        if hasattr(self, 'time_label'):
            self.time_label.setText("Iteration: 2V")

    def eventFilter(self, obj, event): 
        if hasattr(self, 'video_widget') and obj == self.video_widget and self.conversion_active:
            if event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick):
                return True
        return super().eventFilter(obj, event)

    def initUI(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        # Header (NERV style)
        header = QWidget()
        header.setObjectName("HeaderBar")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(10, 6, 10, 6)
        hl.setSpacing(12)
        self.logo_label = QLabel("▶ VIDIUM")
        self.logo_label.setObjectName("Logo")
        self.status_label = QLabel("STATUS: IDLE")
        self.status_label.setObjectName("Status")
        self.time_label = QLabel("Iteration: 2V")
        self.time_label.setObjectName("Time")
        opacity = QGraphicsOpacityEffect(self.status_label)
        self.status_label.setGraphicsEffect(opacity)
        self._blink_anim = QPropertyAnimation(opacity, b"opacity", self)
        self._blink_anim.setStartValue(0.55)
        self._blink_anim.setEndValue(1.0)
        self._blink_anim.setDuration(1200)
        self._blink_anim.setLoopCount(-1)
        self._blink_anim.start()
        hl.addWidget(self.logo_label, 0, Qt.AlignLeft)
        hl.addWidget(self.status_label, 0, Qt.AlignCenter)
        hl.addWidget(self.time_label, 0, Qt.AlignRight)
        main_layout.addWidget(header)
        # Simple preview area to back media_player usage
        self.video_widget = QVideoWidget()
        self.media_player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.media_player.setVideoOutput(self.video_widget)
        self.media_player.setAudioOutput(self.audio_output)
        # Provide hidden preview controls (play/pause toggle and volume)
        # Some workflow logic references these even if the controls are not shown
        try:
            self.play_icon = self.style().standardIcon(QStyle.SP_MediaPlay)
            self.pause_icon = self.style().standardIcon(QStyle.SP_MediaPause)
        except Exception:
            self.play_icon = QIcon()
            self.pause_icon = QIcon()
        self.toggle_button = QPushButton(self)
        self.toggle_button.setIcon(self.play_icon)
        self.toggle_button.clicked.connect(self.toggle_play_pause)
        self.toggle_button.setVisible(False)
        self.volume_slider = QSlider(Qt.Horizontal, self)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(50)
        try:
            self.audio_output.setVolume(0.5)
            self.volume_slider.valueChanged.connect(lambda v: self.audio_output.setVolume(max(0.0, min(1.0, v/100.0))))
        except Exception:
            pass
        self.volume_slider.setVisible(False)
        self.video_widget.setMinimumHeight(1)
        self.tab_widget = QTabWidget()
        # ------------- Convert Tab (full-screen grid overlay) -------------
        self.convert_tab = QWidget()
        # Base sphere + grid background
        self.sphere_view = ThreeSphereView()
        # Overlay UI
        convert_overlay = QWidget(self.convert_tab)
        convert_overlay.setAttribute(Qt.WA_StyledBackground, True)
        convert_overlay.setStyleSheet("background: transparent;")
        cg = QGridLayout(convert_overlay)
        cg.setContentsMargins(24, 24, 24, 24)
        cg.setHorizontalSpacing(16)
        cg.setVerticalSpacing(12)
        # Encourage generous space for the center-left controls, and keep the
        # progress area on the right relatively compact so controls don't crush
        try:
            stretch_map = {0:2, 1:2, 2:2, 3:3, 4:3, 5:3, 6:3, 7:2, 8:1, 9:1, 10:1}
            for col, val in stretch_map.items():
                cg.setColumnStretch(col, val)
        except Exception:
            pass
        # Files panel (left)
        files_panel = QWidget()
        files_panel.setStyleSheet("background: rgba(0,0,0,0.25); border: 1px solid #333;")
        fp_layout = QVBoxLayout(files_panel)
        self.files_tabwidget = QTabWidget()
        input_tab = QWidget(); input_tab_layout = QVBoxLayout(input_tab)
        self.input_list = PlaceholderListWidget("Add, or Drag and Drop in Files")
        self.input_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.input_list.customContextMenuRequested.connect(self.input_list_context_menu)
        self.input_list.currentItemChanged.connect(self.preview_selected_file)
        input_tab_layout.addWidget(self.input_list)
        btns = QHBoxLayout();
        self.input_browse_button = QPushButton("Browse"); self.input_browse_button.clicked.connect(self.browse_input_files); self.input_browse_button.setObjectName("ActionButton")
        # Restore the stronger hover glow for file list actions
        try:
            self.input_browse_button.setStyleSheet(
                "QPushButton{color:#00FFFF; border:1px solid #222; background: rgba(0,0,0,0.25); padding:6px 12px;}"
                "QPushButton:hover{border-color:#00FFFF; background: rgba(0,255,255,0.12);}"
            )
        except Exception:
            pass
        self.clear_all_button = QPushButton("Clear All"); self.clear_all_button.clicked.connect(self.clear_input_files); self.clear_all_button.setObjectName("ActionButton")
        try:
            self.clear_all_button.setStyleSheet(
                "QPushButton{color:#00FFFF; border:1px solid #222; background: rgba(0,0,0,0.25); padding:6px 12px;}"
                "QPushButton:hover{border-color:#00FFFF; background: rgba(0,255,255,0.12);}"
            )
        except Exception:
            pass
        btns.addWidget(self.input_browse_button); btns.addWidget(self.clear_all_button); btns.addStretch(1)
        input_tab_layout.addLayout(btns)
        self.files_tabwidget.addTab(input_tab, "Input")
        output_tab = QWidget(); output_tab_layout = QVBoxLayout(output_tab)
        self.output_list = PlaceholderListWidget("Output files will appear here")
        self.output_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.output_list.currentItemChanged.connect(self.preview_selected_file)
        output_tab_layout.addWidget(self.output_list)
        self.files_tabwidget.addTab(output_tab, "Output")
        fp_layout.addWidget(self.files_tabwidget)
        cg.addWidget(files_panel, 0, 0, 7, 3)
        # Controls rows (split into two lines to avoid crowding)
        # Row A: Mode + GPU
        controls_top = QWidget(); ctop = QHBoxLayout(controls_top); ctop.setContentsMargins(8,0,8,0)
        self.convert_mode_combo = QComboBox(); self.convert_mode_combo.setObjectName("ModeCombo"); self.convert_mode_combo.addItems(["Convert Only", "Trim Only", "Trim & Convert"]); self.convert_mode_combo.currentIndexChanged.connect(self.convert_mode_changed)
        try:
            self.convert_mode_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
            self.convert_mode_combo.setMinimumContentsLength(16)
            self.convert_mode_combo.setMinimumWidth(170)
        except Exception:
            pass
        ctop.addWidget(QLabel("Mode:")); ctop.addWidget(self.convert_mode_combo)
        try:
            ctop.addSpacing(14); ctop.addWidget(self.gpu_checkbox)
        except Exception:
            pass
        ctop.addStretch(1)
        self.convert_controls_top = controls_top
        cg.addWidget(controls_top, 8, 2, 1, 7)

        # Prepare output/quality widgets
        self.output_format_combo = QComboBox(); self.output_format_combo.setObjectName("OutputCombo"); self.populate_output_format_combo()
        try:
            self.output_format_combo.setMinimumWidth(120)
            self.output_format_combo.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        except Exception:
            pass
        self.quality_slider = ClickableSlider(Qt.Horizontal); self.quality_slider.setRange(10,100); self.quality_slider.setValue(100); self.quality_slider.valueChanged.connect(self.update_quality_label)
        try:
            self.quality_slider.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.quality_slider.setMinimumWidth(260)
        except Exception:
            pass
        self.quality_value_label = QLabel("100%")
        try:
            # Ensure the value is clearly visible to the right of the slider
            self.quality_value_label.setMinimumWidth(44)
            self.quality_value_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            # Make the label ignore mouse events so it never blocks slider clicks
            self.quality_value_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self.quality_value_label.setTextInteractionFlags(Qt.NoTextInteraction)
        except Exception:
            pass

        # Container for output label + combo to allow hiding as a block
        self.output_label = QLabel("Output:")
        self.output_format_widget = QWidget(); ofl = QHBoxLayout(self.output_format_widget); ofl.setContentsMargins(0,0,0,0)
        ofl.addWidget(self.output_label); ofl.addWidget(self.output_format_combo)

        # Row B: Output + Quality
        controls_mid = QWidget(); cmid = QHBoxLayout(controls_mid); cmid.setContentsMargins(8,0,8,0); cmid.setSpacing(10)
        cmid.addWidget(self.output_format_widget)
        cmid.addSpacing(14)
        cmid.addWidget(QLabel("Quality:")); cmid.addWidget(self.quality_slider); cmid.addSpacing(6); cmid.addWidget(self.quality_value_label)
        cmid.addStretch(1)
        self.convert_controls_mid = controls_mid
        cg.addWidget(controls_mid, 9, 2, 1, 7)

        # Actions row: Convert / Stop centered
        self.convert_button = QPushButton("Convert"); self.convert_button.setObjectName("ActionButton"); self.convert_button.clicked.connect(self.start_conversion_queue)
        self.stop_button = QPushButton("Stop"); self.stop_button.setObjectName("ActionButton"); self.stop_button.clicked.connect(self.stop_conversion); self.stop_button.setEnabled(False)
        actions_row = QWidget(); ar = QHBoxLayout(actions_row); ar.setContentsMargins(8,0,8,0)
        ar.addStretch(1)
        ar.addWidget(self.convert_button); ar.addWidget(self.stop_button)
        ar.addStretch(1)
        self.convert_actions_row = actions_row
        cg.addWidget(actions_row, 11, 2, 1, 7)
        # Convert trim widget (hidden by default)
        self.convert_trim_widget = QWidget(); ctwl = QHBoxLayout(self.convert_trim_widget); ctwl.setContentsMargins(8,0,8,0)
        self.convert_trim_label = QLabel("Trim Range:")
        self.convert_trim_start_edit = FixedTimeLineEdit(); self.convert_trim_start_edit.setText("00:00:00:00"); 
        try:
            self.convert_trim_start_edit.setMinimumWidth(96)
        except Exception:
            pass
        self.convert_trim_to_label = QLabel("to")
        self.convert_trim_end_edit = FixedTimeLineEdit(); self.convert_trim_end_edit.setText("00:00:00:00"); 
        try:
            self.convert_trim_end_edit.setMinimumWidth(96)
        except Exception:
            pass
        ctwl.addWidget(self.convert_trim_label); ctwl.addWidget(self.convert_trim_start_edit); ctwl.addWidget(self.convert_trim_to_label); ctwl.addWidget(self.convert_trim_end_edit); ctwl.addStretch(1)
        self.convert_trim_widget.hide()
        # Keep trim row left of the progress area to prevent any overlay
        cg.addWidget(self.convert_trim_widget, 7, 2, 1, 7)
        # Output folder + buttons (top center)
        folder_container = QWidget(); fc = QVBoxLayout(folder_container); fc.setContentsMargins(8,0,8,0); fc.setSpacing(2)
        fr_top = QHBoxLayout(); fr_top.setContentsMargins(0,0,0,0)
        fr_top.addWidget(QLabel("Output Folder:"))
        self.output_browse_button = QPushButton("Browse"); self.output_browse_button.setObjectName("ActionButton"); self.output_browse_button.clicked.connect(self.browse_output_folder)
        self.goto_folder_button = QPushButton("Go To Folder"); self.goto_folder_button.setObjectName("ActionButton"); self.goto_folder_button.clicked.connect(self.goto_output_folder)
        fr_top.addWidget(self.output_browse_button); fr_top.addWidget(self.goto_folder_button); fr_top.addStretch(1)
        fc.addLayout(fr_top)
        fr_bottom = QHBoxLayout(); fr_bottom.setContentsMargins(0,0,0,0)
        self.output_folder_edit = QLineEdit(); self.output_folder_edit.setText(self.settings.value("default_output_folder", OUTPUT_FOLDER))
        try:
            self.output_folder_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        except Exception:
            pass
        fr_bottom.addWidget(self.output_folder_edit, 1)
        self.default_checkbox = QCheckBox("Default"); self.default_checkbox.setChecked(self.settings.value("default_checked", True, type=bool)); self.default_checkbox.stateChanged.connect(self.default_checkbox_changed)
        try:
            self.default_checkbox.setStyleSheet("color: #00FFFF;")
        except Exception:
            pass
        fr_bottom.addWidget(self.default_checkbox)
        fc.addLayout(fr_bottom)
        cg.addWidget(folder_container, 0, 3, 1, 7)
        # Progress (center area under Output/Quality)
        progress_panel = QWidget(); pl = QVBoxLayout(progress_panel); pl.setContentsMargins(8,0,8,0); pl.setSpacing(2)
        self.overall_progress_label = QLabel("Progress:")
        self.current_progress_bar = QProgressBar(); self.current_progress_bar.setRange(0,100); self.current_progress_bar.setTextVisible(False)
        pl.addWidget(self.overall_progress_label); pl.addWidget(self.current_progress_bar)
        # Place progress directly below the Output controls
        self.convert_progress_panel = progress_panel
        cg.addWidget(progress_panel, 10, 2, 1, 7)
        # Right dock for Convert terminal
        self.convert_right_dock = QWidget(convert_overlay)
        self.convert_right_dock.setObjectName('rightDock')
        dock_layout_c = QVBoxLayout(self.convert_right_dock)
        dock_layout_c.setContentsMargins(6,6,6,6)
        dock_layout_c.setSpacing(6)
        # Auto-scroll terminal with smooth drift that pauses on hover/focus
        self.convert_terminal = AutoScrollTerminal(); self.convert_terminal.setPlaceholderText(""); self.convert_terminal.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        try:
            self.convert_terminal.set_rehydrate_provider(lambda: self.log_text_edit.toPlainText())
        except Exception:
            pass
        # Make terminal area visually seamless with the scene (no boxed border)
        self.convert_right_dock.setStyleSheet("background: transparent; border: none;")
        self.convert_terminal.setStyleSheet(
            """
            QPlainTextEdit { background-color: transparent; color:#00FFFF; border: none; }
            QPlainTextEdit QScrollBar:vertical { background-color: transparent; width: 11px; }
            QPlainTextEdit QScrollBar::handle:vertical { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 rgba(0,255,255,0.35), stop:1 rgba(0,160,160,0.45)); border:1px solid rgba(0,255,255,0.65); border-radius:5px; min-height:28px; }
            QPlainTextEdit QScrollBar::add-line:vertical, QPlainTextEdit QScrollBar::sub-line:vertical { height: 0; background: transparent; }
            QPlainTextEdit QScrollBar::add-page:vertical, QPlainTextEdit QScrollBar::sub-page:vertical { background-color: rgba(0,0,0,0.15); }
            QPlainTextEdit QScrollBar:horizontal { background-color: transparent; height: 11px; }
            QPlainTextEdit QScrollBar::handle:horizontal { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 rgba(0,255,255,0.35), stop:1 rgba(0,160,160,0.45)); border:1px solid rgba(0,255,255,0.65); border-radius:5px; min-width:28px; }
            QPlainTextEdit QScrollBar::add-line:horizontal, QPlainTextEdit QScrollBar::sub-line:horizontal { width: 0; background: transparent; }
            QPlainTextEdit QScrollBar::add-page:horizontal, QPlainTextEdit QScrollBar::sub-page:horizontal { background-color: rgba(0,0,0,0.15); }
            """
        )
        dock_layout_c.addWidget(self.convert_terminal)
        # Wrap base + overlay so overlay is always above
        # Center sphere between left pane and terminal by using a modest right offset
        if hasattr(self.sphere_view, "set_offset_ratio"):
            # Slight shift only to account for reserved terminal width; visually centered
            self.sphere_view.set_offset_ratio(0.15)
        convert_container = OverlayContainer(self.sphere_view, convert_overlay, parent=self.convert_tab)
        # Keep references for centering calculations
        self.convert_overlay = convert_overlay
        self.files_panel = files_panel
        container_layout = QVBoxLayout(self.convert_tab)
        container_layout.setContentsMargins(0,0,0,0)
        container_layout.setSpacing(0)
        container_layout.addWidget(convert_container)
        self.tab_widget.addTab(self.convert_tab, "Convert")

        # ------------- Download Tab (full-screen grid overlay) -------------
        self.download_tab = QWidget()
        self.download_sphere_view = ThreeSphereView()
        if hasattr(self.download_sphere_view, "set_offset_ratio"):
            # Push the sphere further right to open up left column
            self.download_sphere_view.set_offset_ratio(0.5)
        download_overlay = QWidget(self.download_tab); download_overlay.setAttribute(Qt.WA_StyledBackground, True); download_overlay.setStyleSheet("background: transparent;")
        dg = QGridLayout(download_overlay); dg.setContentsMargins(24,24,24,24); dg.setHorizontalSpacing(16); dg.setVerticalSpacing(12)
        try:
            stretch_map_d = {0:2, 1:2, 2:2, 3:3, 4:3, 5:3, 6:3, 7:2, 8:1, 9:1, 10:1}
            for col, val in stretch_map_d.items():
                dg.setColumnStretch(col, val)
        except Exception:
            pass
        # URL row
        url_row = QWidget(); ur = QHBoxLayout(url_row); ur.setContentsMargins(8,0,8,0)
        ur.addWidget(QLabel("Video URL:"))
        self.video_url_edit = QLineEdit(); self.video_url_edit.setPlaceholderText("Enter Video URL here...")
        try:
            # We'll keep a fixed policy but compute the exact width after layout to line up with the folder row
            self.video_url_edit.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        except Exception:
            pass
        ur.addWidget(self.video_url_edit)
        ur.addStretch(1)
        # Folder row
        folder_row2 = QWidget(); fr2 = QHBoxLayout(folder_row2); fr2.setContentsMargins(8,0,8,0); fr2.setSpacing(10)
        fr2.addWidget(QLabel("Download Folder:"))
        DEFAULT_DOWNLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Downloads")
        self.download_folder_edit = QLineEdit(); self.download_folder_edit.setObjectName("DlFolderEdit"); self.download_folder_edit.setText(self.settings.value("default_download_folder", DEFAULT_DOWNLOAD_FOLDER))
        try:
            self.download_folder_edit.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            # Keep path field comfortably short so it ends before the buttons; it will scroll when text is long
            self.download_folder_edit.setMinimumWidth(360)
            self.download_folder_edit.setMaximumWidth(600)
        except Exception:
            pass
        # Hide the right border so no vertical seam can appear beneath adjacent buttons
        try:
            self.download_folder_edit.setStyleSheet("QLineEdit#DlFolderEdit { border-right: 0px; padding-right: 6px; }")
        except Exception:
            pass
        fr2.addWidget(self.download_folder_edit, 1)
        self.download_browse_button = QPushButton("Browse"); self.download_browse_button.setObjectName("DlBrowseBtn"); self.download_browse_button.clicked.connect(self.browse_download_folder)
        self.download_goto_button = QPushButton("Go To Folder"); self.download_goto_button.setObjectName("DlGoBtn"); self.download_goto_button.clicked.connect(self.goto_download_folder)
        # Ensure both buttons avoid any seam: give them opaque background and extra left padding on 'Go To Folder'
        try:
            self.download_browse_button.setStyleSheet(
                "QPushButton#DlBrowseBtn { background-color: rgba(0,0,0,0.25); border:1px solid #333; padding:6px 12px; border-radius:3px; }"
                "QPushButton#DlBrowseBtn:hover { border-color:#00FFFF; color:#00FFFF; }"
                "QPushButton#DlBrowseBtn:focus { outline: none; }"
            )
            self.download_goto_button.setStyleSheet(
                "QPushButton#DlGoBtn { background-color: rgba(0,0,0,0.25); border:1px solid #333; padding:6px 14px 6px 16px; border-radius:3px; }"
                "QPushButton#DlGoBtn:hover { border-color:#00FFFF; color:#00FFFF; }"
                "QPushButton#DlGoBtn:focus { outline: none; }"
            )
        except Exception:
            pass
        self.download_default_checkbox = QCheckBox("Default"); self.download_default_checkbox.setChecked(self.settings.value("default_download_checked", True, type=bool)); self.download_default_checkbox.stateChanged.connect(self.download_default_checkbox_changed)
        btn_group = QWidget(); bgl = QHBoxLayout(btn_group); bgl.setContentsMargins(0,0,0,0); bgl.setSpacing(6)
        bgl.addWidget(self.download_browse_button)
        bgl.addWidget(self.download_goto_button)
        fr2.addWidget(btn_group); fr2.addWidget(self.download_default_checkbox); fr2.addStretch(1)
        # Stack URL + Folder as tightly as possible
        header_block = QWidget(); hb = QVBoxLayout(header_block); hb.setContentsMargins(0,0,0,0); hb.setSpacing(0)
        try:
            header_block.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        except Exception:
            pass
        hb.addWidget(url_row)
        hb.addWidget(folder_row2)
        # header_block will be added together with mode row via top_stack below
        # dg.addWidget(header_block, 0, 0, 1, 10)
        # Mode/output row (aligned with Convert tab layout)
        dl_controls = QWidget(); dlc = QHBoxLayout(dl_controls); dlc.setContentsMargins(8,0,8,0); dlc.setSpacing(10)
        try:
            dl_controls.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        except Exception:
            pass
        self.download_mode_combo = QComboBox(); self.download_mode_combo.addItems(["Download Only", "Download & Convert", "Download & Trim", "Download & Convert & Trim"]); self.download_mode_combo.currentIndexChanged.connect(self.download_mode_changed)
        try:
            # Lock a fixed width so the row layout never reflows on selection
            # 230 fits the longest label "Download & Convert & Trim"
            self.download_mode_combo.setFixedWidth(230)
            self.download_mode_combo.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        except Exception:
            pass
        dlc.addWidget(self.download_mode_combo, 0, Qt.AlignVCenter)
        # Spacer before output controls so the mode combo stays at a fixed x-position
        self.download_output_gap = QWidget(); self.download_output_gap.setFixedWidth(14); dlc.addWidget(self.download_output_gap)
        self.download_output_label = QLabel("Output:"); dlc.addWidget(self.download_output_label, 0, Qt.AlignVCenter)
        self.download_output_format_combo = QComboBox(); self.populate_output_format_combo(self.download_output_format_combo)
        try:
            fixed_output_width = max(180, self.download_output_format_combo.sizeHint().width())
            self.download_output_format_combo.setFixedWidth(fixed_output_width)
        except Exception:
            try:
                self.download_output_format_combo.setMinimumWidth(180)
            except Exception:
                pass
        dlc.addWidget(self.download_output_format_combo, 0, Qt.AlignVCenter)
        # Normalize control heights so the row doesn't shift between modes/themes
        try:
            h = max(self.download_mode_combo.sizeHint().height(), self.download_output_format_combo.sizeHint().height())
            self.download_mode_combo.setFixedHeight(h)
            self.download_output_format_combo.setFixedHeight(h)
        except Exception:
            pass
        try:
            dlc.addStretch(1)
        except Exception:
            pass
        # Place header + mode rows in a single tight vertical stack to remove inter-row gaps
        top_stack = QWidget(); ts = QVBoxLayout(top_stack); ts.setContentsMargins(0,0,0,0); ts.setSpacing(2)
        try:
            top_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        except Exception:
            pass
        ts.addWidget(header_block)
        ts.addWidget(dl_controls)
        # Pin to the top-left to eliminate any centering when internal width changes
        dg.addWidget(top_stack, 0, 0, 1, 10, alignment=Qt.AlignLeft | Qt.AlignTop)
        # Keep a reference for centering math
        self.download_top_stack = top_stack
        # Insert a spacer block to mirror the Convert tab's left files panel vertical footprint,
        # so subsequent rows (trim/progress/actions) line up visually with Convert.
        try:
            spacer_top_area = QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding)
            dg.addItem(spacer_top_area, 1, 0, 6, 10)
        except Exception:
            pass
        # Trim widget for download flows (hidden) inside a fixed-height container so
        # the overall layout below (progress bar) does not shift when toggled
        self.trim_widget = QWidget(); dltw = QHBoxLayout(self.trim_widget); dltw.setContentsMargins(8,0,8,0); dltw.setSpacing(6)
        self.trim_label = QLabel("Trim Range:")
        self.trim_start_edit = FixedTimeLineEdit(); self.trim_start_edit.setText("00:00:00:00")
        self.trim_to_label = QLabel("to")
        self.trim_end_edit = FixedTimeLineEdit(); self.trim_end_edit.setText("00:00:00:00")
        dltw.addWidget(self.trim_label); dltw.addWidget(self.trim_start_edit); dltw.addWidget(self.trim_to_label); dltw.addWidget(self.trim_end_edit); dltw.addStretch(1)
        self.trim_widget.hide()
        self.trim_container = QWidget(); tc = QVBoxLayout(self.trim_container); tc.setContentsMargins(0,0,0,0); tc.setSpacing(0)
        tc.addWidget(self.trim_widget)
        try:
            self.trim_container.setFixedHeight(max(34, self.trim_widget.sizeHint().height()))
        except Exception:
            self.trim_container.setFixedHeight(36)
        # Position to align with Convert tab's trim row
        dg.addWidget(self.trim_container, 7, 0, 1, 10)
        # Add an expanding spacer below the trim container to push progress to the bottom consistently
        try:
            spacer_bottom_push = QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Minimum)
            dg.addItem(spacer_bottom_push, 2, 0, 1, 10)
        except Exception:
            pass
        # Download button centered under progress (match Convert tab row/width)
        self.download_button = QPushButton("Download"); dg.addWidget(self.download_button, 11, 2, 1, 7, alignment=Qt.AlignCenter)
        self.download_button.clicked.connect(self.start_download)
        # Right dock for Download terminal
        self.download_right_dock = QWidget(download_overlay)
        self.download_right_dock.setObjectName('rightDock')
        dock_layout_d = QVBoxLayout(self.download_right_dock)
        dock_layout_d.setContentsMargins(6,6,6,6)
        dock_layout_d.setSpacing(6)
        self.download_terminal = AutoScrollTerminal(); self.download_terminal.setPlaceholderText(""); self.download_terminal.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        try:
            self.download_terminal.set_rehydrate_provider(lambda: self.log_text_edit.toPlainText())
        except Exception:
            pass
        self.download_right_dock.setStyleSheet("background: transparent; border: none;")
        self.download_terminal.setStyleSheet(
            """
            QPlainTextEdit { background-color: transparent; color:#00FFFF; border: none; }
            QPlainTextEdit QScrollBar:vertical { background-color: transparent; width: 11px; }
            QPlainTextEdit QScrollBar::handle:vertical { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 rgba(0,255,255,0.35), stop:1 rgba(0,160,160,0.45)); border:1px solid rgba(0,255,255,0.65); border-radius:5px; min-height:28px; }
            QPlainTextEdit QScrollBar::add-line:vertical, QPlainTextEdit QScrollBar::sub-line:vertical { height: 0; background: transparent; }
            QPlainTextEdit QScrollBar::add-page:vertical, QPlainTextEdit QScrollBar::sub-page:vertical { background-color: rgba(0,0,0,0.15); }
            QPlainTextEdit QScrollBar:horizontal { background-color: transparent; height: 11px; }
            QPlainTextEdit QScrollBar::handle:horizontal { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 rgba(0,255,255,0.35), stop:1 rgba(0,160,160,0.45)); border:1px solid rgba(0,255,255,0.65); border-radius:5px; min-width:28px; }
            QPlainTextEdit QScrollBar::add-line:horizontal, QPlainTextEdit QScrollBar::sub-line:horizontal { width: 0; background: transparent; }
            QPlainTextEdit QScrollBar::add-page:horizontal, QPlainTextEdit QScrollBar::sub-page:horizontal { background-color: rgba(0,0,0,0.15); }
            """
        )
        dock_layout_d.addWidget(self.download_terminal)
        # Show output controls only for conversion modes; initialize visibility now
        try:
            self.download_mode_changed(self.download_mode_combo.currentIndex())
        except Exception:
            pass
        # Progress (match Convert tab position and width)
        self.download_progress_label = QLabel("Progress:")
        self.download_progress_bar = QProgressBar(); self.download_progress_bar.setRange(0,100); self.download_progress_bar.setTextVisible(False)
        download_progress_panel = QWidget(); dp = QVBoxLayout(download_progress_panel); dp.setContentsMargins(8,0,8,0); dp.setSpacing(2)
        dp.addWidget(self.download_progress_label); dp.addWidget(self.download_progress_bar)
        dg.addWidget(download_progress_panel, 10, 2, 1, 7)
        # Reference for centering
        self.download_progress_panel = download_progress_panel
        download_container = OverlayContainer(self.download_sphere_view, download_overlay, parent=self.download_tab)
        # Match top margin used by Convert tab for consistent vertical alignment
        download_container.set_top_margin(20)
        # Keep references for centering in download tab
        self.download_overlay = download_overlay
        dl_container_layout = QVBoxLayout(self.download_tab)
        dl_container_layout.setContentsMargins(0,0,0,0)
        dl_container_layout.setSpacing(0)
        dl_container_layout.addWidget(download_container)
        self.tab_widget.addTab(self.download_tab, "Download")
        main_layout.addWidget(self.tab_widget)
        # Hidden log (we mirror logs to the inline terminal and 3D HUD)
        self.log_text_edit = QPlainTextEdit(); self.log_text_edit.hide()
        self.conversion_queue = []
        self.total_files = 0
        self.current_index = 0
        self.file_timer = QTimer(self)
        self.file_timer.timeout.connect(self.update_current_progress)
        self.current_file_progress = 0
        self.download_browse_button.clicked.connect(self.browse_download_folder)
        self.download_default_checkbox.stateChanged.connect(self.download_default_checkbox_changed)
        self.download_goto_button.clicked.connect(self.goto_download_folder)
        self.download_button.clicked.connect(self.start_download)
        # Apply visual style
        self.apply_sci_fi_styles()
        self.apply_cabinet_styles()

        # Center the canvas between left panel and right dock after layout is ready
        QTimer.singleShot(0, self.center_sphere_canvas)
        # Align the URL field width to end at the same right edge as the folder row controls
        QTimer.singleShot(0, self._align_download_url_width)

    def init_drop_overlay(self):
        self.drop_overlay = QWidget(self)
        self.drop_overlay.setGeometry(self.rect())
        self.drop_overlay.setStyleSheet("background-color: rgba(0, 0, 0, 0.2);")
        self.drop_overlay.hide()
        self.overlay_label = QLabel("Drop Files Here", self.drop_overlay)
        self.overlay_label.setStyleSheet("color: white; font-size: 24pt;")
        self.overlay_label.setAlignment(Qt.AlignCenter)
        layout = QVBoxLayout(self.drop_overlay)
        layout.addWidget(self.overlay_label, alignment=Qt.AlignCenter)

    def _init_countdown_timer(self):
        # No-op: countdown removed in favor of static iteration text
        pass

    def _tick_countdown(self):
        # No-op with static iteration text
        if hasattr(self, 'time_label') and self.time_label.text() != "Iteration: 2V":
            self.time_label.setText("Iteration: 2V")

    def _align_download_url_width(self):
        try:
            # Compute right edge of the folder edit + buttons block and align URL width to that (minus a small margin)
            if not hasattr(self, 'video_url_edit'):
                return
            # Find the parent layout containing the folder row
            folder_row_parent = getattr(self, 'download_folder_edit', None)
            browse_btn = getattr(self, 'download_browse_button', None)
            goto_btn = getattr(self, 'download_goto_button', None)
            if folder_row_parent is None or browse_btn is None or goto_btn is None:
                return
            # Rightmost x among the folder edit + buttons
            widgets = [folder_row_parent, browse_btn, goto_btn]
            right_edges = []
            for w in widgets:
                p = w.mapToGlobal(w.rect().topRight())
                right_edges.append(p.x())
            if not right_edges:
                return
            rightmost = max(right_edges)
            # Compute left x of URL edit to set width
            left = self.video_url_edit.mapToGlobal(self.video_url_edit.rect().topLeft()).x()
            new_w = max(200, rightmost - left - 19)
            self.video_url_edit.setFixedWidth(new_w)
        except Exception:
            pass

    def apply_sci_fi_styles(self):
        self.setStyleSheet(
            """
            QWidget { background-color: #000000; color: #FFFFFF; font-family: 'Courier New', monospace; }
            QGroupBox { border: 1px solid #333; background-color: rgba(20,20,20,0.7); margin-top: 10px; }
            QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 2px 6px; color: #FF4800; }
            #HeaderBar { border: 1px solid #333; background-color: rgba(0,0,0,0.5); }
            #Logo { color: #00FFFF; font-weight: bold; font-size: 18px; }
            #Status { color: #00FFFF; font-size: 13px; }
            #Time { color: #00FFFF; font-size: 13px; }
            QPlainTextEdit { background-color: rgba(10,10,10,0.7); border: 1px solid #333; color: #00FFFF; }
            QPushButton { background: transparent; border: 1px solid #333; padding: 6px 10px; }
            QPushButton:hover { border-color: #00FFFF; color: #00FFFF; }
            /* Remove any inner vertical separators some platforms draw */
            QPushButton::separator { width: 0px; height: 0px; }
            QSlider::groove:horizontal { height: 6px; background-color: #111; border: 1px solid #333; }
            QSlider::handle:horizontal { width: 14px; background: #00FFFF; margin: -5px 0; border-radius: 2px; }
            QProgressBar { border: 1px solid #333; background-color: #111; }
            QProgressBar::chunk { background: #00FFFF; }
            QTabBar::tab { background-color: #111; color: #FFF; padding: 6px 10px; border: 1px solid #333; }
            QTabBar::tab:selected { color: #00FFFF; border-color: #00FFFF; }
            QLineEdit { background-color: #0a0a0a; border: 1px solid #333; color: #00FFFF; }
            QComboBox { background-color: #0a0a0a; border: 1px solid #333; color: #00FFFF; }
            QListWidget { background-color: #0a0a0a; border: 1px solid #333; }
            QCheckBox { color: #FFFFFF; }
            """
        )

    def apply_cabinet_styles(self):
        # Cabinet arcade style: bold orange labels with cyan content
        orange = "#FF4800"; cyan = "#00FFFF"; dark = "#080808"; grid = "#222"
        style = f"""
        QLabel {{ color: {cyan}; }}
        QPushButton {{
          color: {cyan}; border: 1px solid {grid}; background: rgba(0,0,0,0.25); padding: 6px 12px; letter-spacing: 1px;
        }}
        QPushButton:hover, QPushButton#ActionButton:hover {{ border-color: {cyan}; color: {cyan}; background: rgba(0,255,255,0.12); }}
        QPushButton#ActionButton {{ border: 1px solid {grid}; }}
        QComboBox, QLineEdit, QComboBox#ModeCombo, QComboBox#OutputCombo {{ color: {cyan}; border:1px solid {grid}; background: rgba(0,0,0,0.25); padding: 4px; }}
        QProgressBar {{ color: {cyan}; border:1px solid {grid}; background: rgba(0,0,0,0.2); }}
        QProgressBar::chunk {{ background: {cyan}; }}
        /* Orange title badges */
        QWidget[role='badge'] QLabel {{ color: {orange}; font-weight: bold; text-transform: uppercase; }}
        """
        self.setStyleSheet(self.styleSheet() + style)

    def populate_output_format_combo(self, combo=None):
        if combo is None:
            combo = self.output_format_combo
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
        combo.setModel(model)
        combo.setCurrentIndex(1)

    def update_quality_label(self, value):
        self.quality_value_label.setText(f"{value}%")

    def update_analysis_panel(self, file_path: str):
        # Extract simple ffprobe stats
        try:
            ffprobe = get_ffprobe_path()
            import subprocess
            def run(args):
                return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
            # duration
            dur = run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file_path])
            # size
            size_bytes = os.path.getsize(file_path)
            size_mb = size_bytes/(1024*1024)
            # resolution
            w = run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width", "-of", "default=noprint_wrappers=1:nokey=1", file_path])
            h = run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=height", "-of", "default=noprint_wrappers=1:nokey=1", file_path])
            # fps
            r = run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate", "-of", "default=noprint_wrappers=1:nokey=1", file_path])
            fps = "-"
            if "/" in r:
                num, den = r.split("/")
                try:
                    denf = float(den) if float(den) != 0 else 1
                    fps = f"{float(num)/denf:.2f}"
                except Exception:
                    fps = r
            # codec
            codec = run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1", file_path]) or "-"
            # bitrate
            from converter import get_input_bitrate
            br = get_input_bitrate(file_path)
            br_k = f"{br//1000}k" if br else "-"
            text = (
                f"RES: {w}x{h}    FPS: {fps}\n"
                f"CODEC: {codec}    BITRATE: {br_k}\n"
                f"DURATION: {float(dur):.2f}s    SIZE: {size_mb:.1f}MB\n"
                f"WARNING: --"
            )
            self.analysis_grid.setPlainText(text)
        except Exception:
            self.analysis_grid.setPlainText("RES: --    FPS: --\nCODEC: --    BITRATE: --\nDURATION: --    SIZE: --\nWARNING: --")

    def browse_input_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Select Input File(s)", "",
                                                "Video Files (*.mp4 *.mkv *.webm *.avi *.mov *.flv *.wmv *.mpeg *.mpg)")
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

    def clear_input_files(self):
        self.input_list.clear()

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

    def preview_selected_file(self, current, previous):
        if self.conversion_active:
            return
        if current:
            file_path = current.data(Qt.UserRole) if current.data(
                Qt.UserRole) is not None else current.text()
            ext = os.path.splitext(file_path)[1].lower()
            # Update analysis info for any supported file
            try:
                self.update_analysis_panel(file_path)
            except Exception:
                pass
            if ext == ".mkv":
                base = os.path.splitext(os.path.basename(file_path))[0]
                temp = os.path.splitext(file_path)[1]
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
        self.files_tabwidget.setEnabled(False)
        self.conversion_active = True

    def enable_preview(self):
        self.video_widget.setEnabled(True)
        self.toggle_button.setEnabled(True)
        self.volume_slider.setEnabled(True)
        self.files_tabwidget.setEnabled(True)
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
        self.conversion_queue = []
        for i in range(count):
            input_path = self.input_list.item(i).text()
            base = os.path.splitext(os.path.basename(input_path))[0]
            selected_format = self.output_format_combo.currentText().strip() if self.output_format_combo.count() > 0 else "mp4"
            if selected_format.endswith(":"):
                selected_format = "mp4"
            output_path = os.path.join(out_folder, base + "." + selected_format)
            self.conversion_queue.append((input_path, output_path))
        self.total_files = len(self.conversion_queue)
        self.current_index = 0
        # Overall progress refers to the queue completion percent; reuse the label but update only the current bar
        if hasattr(self, 'current_progress_bar'):
            self.current_progress_bar.setValue(0)
        self.progress_label_update()
        self.convert_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.conversion_aborted = False
        self.start_next_conversion()

    def progress_label_update(self):
        if self.current_index < self.total_files:
            current_file = os.path.basename(
                self.conversion_queue[self.current_index][0])
            mode = self.convert_mode_combo.currentText()
            self.progress_label = f"Processing file \"{current_file}\" ({self.current_index+1}/{self.total_files}) in {mode} mode"
            # Keep label text updated; current bar is set via worker progress
            # (no overall bar widget in convert view any more)
        else:
            self.progress_label = "All operations complete."
        self.update_progress_labels()

    def update_progress_labels(self):
        # Display the current file progress percentage to match the moving bar
        try:
            self.overall_progress_label.setText(f"Progress: {int(self.current_file_progress)}%")
        except Exception:
            pass

    def update_current_progress(self):
        if self.current_file_progress < 100:
            self.current_file_progress += 2
            if self.current_file_progress > 100:
                self.current_file_progress = 100
            self.update_progress_labels()
        else:
            self.file_timer.stop()
        if hasattr(self, 'sphere_view'):
            try:
                self.sphere_view.set_progress(self.current_file_progress)
            except Exception:
                pass

    def _on_worker_progress(self, pct: int):
        # Update UI from worker signal in main thread
        try:
            self.current_file_progress = max(0, min(100, int(pct)))
            self.current_progress_bar.setValue(self.current_file_progress)
            self.update_progress_labels()
            if hasattr(self, 'sphere_view'):
                self.sphere_view.set_progress(self.current_file_progress)
        except Exception:
            pass

    def start_next_conversion(self):
        if self.conversion_aborted:
            self.convert_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self.append_log("Conversion aborted.")
            self.enable_preview()
            return
        if self.current_index < self.total_files:
            mode = self.convert_mode_combo.currentText()
            input_file, output_file = self.conversion_queue[self.current_index]
            if mode == "Convert Only":
                desired_format = self.get_selected_format().lower()
                if desired_format.endswith(":"):
                    desired_format = "mp4"
                input_ext = os.path.splitext(input_file)[1].lower()
                if input_ext == "." + desired_format:
                    self.append_log(f"Skipping conversion for {input_file} as it is already in the desired format.")
                    item = QListWidgetItem(os.path.basename(input_file))
                    item.setData(Qt.UserRole, input_file)
                    self.output_list.addItem(item)
                    self.current_index += 1
                    # overall percent reflected in label only
                    pass
                    self.start_next_conversion()
                    return
            self.progress_label_update()
            if mode == "Convert Only":
                extra_args = None
                if self.get_selected_format().lower() == "gif":
                    extra_args = ["-vf", "fps=12,scale=320:-1:flags=lanczos"]
                use_gpu_flag = self.gpu_checkbox.isChecked()
                if self.get_selected_format().lower() == "webm":
                    use_gpu_flag = False
                from converter import convert_file
                self.worker = ConversionWorker(
                    input_file, output_file, extra_args, use_gpu_flag, self.quality_slider.value())
                self.worker.setParent(self)
                self.worker.finished.connect(
                    lambda: setattr(self, "worker", None))
                self.worker.conversionFinished.connect(
                    self.file_conversion_finished)
                self.worker.conversionError.connect(self.file_conversion_error)
                self.worker.logMessage.connect(self.append_log)
                try:
                    self.worker.progressUpdated.connect(lambda p: self._on_worker_progress(p))
                except Exception:
                    pass
                # Sci‑Fi status + sphere reset
                if hasattr(self, 'status_label'):
                    self.status_label.setText("STATUS: CONVERTING...")
                if hasattr(self, 'sphere_view'):
                    try:
                        self.sphere_view.set_progress(0)
                    except Exception:
                        pass
                self.current_file_progress = 0
                # During conversion, make the terminal drift downward (content moves down) for a dynamic feel
                try:
                    if hasattr(self, 'convert_terminal'):
                        self.convert_terminal._direction = +1
                except Exception:
                    pass
                self.file_timer.start(500)
                self.worker.start()
            elif mode in ["Trim Only", "Trim & Convert"]:
                self.append_log("Starting trimming for file: " + input_file)
                self.media_player.stop()
                use_gpu_flag = self.gpu_checkbox.isChecked()
                copy_mode_flag = True
                self.trim_worker = TrimWorker(input_file, self.convert_trim_start_edit.text(),
                                              self.convert_trim_end_edit.text(), use_gpu=use_gpu_flag, delete_original=False, output_folder=self.output_folder_edit.text(), copy_mode=copy_mode_flag)
                self.trim_worker.finished.connect(self.convert_trim_finished)
                self.trim_worker.error.connect(self.convert_trim_error)
                if hasattr(self, 'status_label'):
                    self.status_label.setText("STATUS: TRIMMING...")
                if hasattr(self, 'sphere_view'):
                    try:
                        self.sphere_view.set_progress(0)
                    except Exception:
                        pass
                self.trim_worker.start()
            else:
                from converter import convert_file
                self.worker = ConversionWorker(input_file, output_file, extra_args=None, use_gpu=False, quality=self.quality_slider.value())
                self.worker.conversionFinished.connect(self.file_conversion_finished)
                self.worker.conversionError.connect(self.file_conversion_error)
                self.worker.logMessage.connect(self.append_log)
                try:
                    self.worker.progressUpdated.connect(lambda p: self._on_worker_progress(p))
                except Exception:
                    pass
                if hasattr(self, 'status_label'):
                    self.status_label.setText("STATUS: CONVERTING...")
                if hasattr(self, 'sphere_view'):
                    try:
                        self.sphere_view.set_progress(0)
                    except Exception:
                        pass
                self.worker.start()
        else:
            if hasattr(self, 'current_progress_bar'):
                self.current_progress_bar.setValue(100)
            self.progress_label_update()
            self.media_player.stop()
            self.media_player.setSource(QUrl())
            self.toggle_button.setIcon(self.play_icon)
            self.convert_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self.enable_preview()

    @Slot(str, str)
    def file_conversion_finished(self, output_file, message):
        self.file_timer.stop()
        self.current_file_progress = 100
        self.append_log(f"{output_file}: {message}")
        if hasattr(self, 'status_label'):
            # If there are more files, keep converting state; otherwise idle
            if self.current_index + 1 >= self.total_files:
                self.status_label.setText("STATUS: IDLE")
            else:
                self.status_label.setText("STATUS: CONVERTING...")
        if hasattr(self, 'sphere_view'):
            try:
                self.sphere_view.set_progress(100)
            except Exception:
                pass
        # After conversion, restore terminal drift to cinematic upward glide
        try:
            if hasattr(self, 'convert_terminal'):
                self.convert_terminal._direction = -1
        except Exception:
            pass
        if self.convert_mode_combo.currentText() == "Trim & Convert" and self.intermediate_file:
            try:
                if os.path.exists(self.intermediate_file):
                    os.remove(self.intermediate_file)
                    self.append_log(f"Removed intermediate trimmed file: {self.intermediate_file}")
            except Exception as e:
                self.append_log(f"Could not remove intermediate trimmed file: {e}")
            self.intermediate_file = None
        item = QListWidgetItem(os.path.basename(output_file))
        item.setData(Qt.UserRole, output_file)
        self.output_list.addItem(item)
        self.current_index += 1
        # overall percent reflected in label only
        self.start_next_conversion()

    @Slot(str)
    def file_conversion_error(self, error_message):
        self.file_timer.stop()
        self.append_log(f"Error: {error_message}")
        if hasattr(self, 'status_label'):
            self.status_label.setText("STATUS: ERROR")
        if not self.conversion_aborted:
            # Remove the item corresponding to the current index to avoid desync
            if 0 <= self.current_index < self.input_list.count():
                self.input_list.takeItem(self.current_index)
            self.current_index += 1
            # overall percent reflected in label only
            self.start_next_conversion()
        else:
            self.convert_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self.append_log("Conversion aborted.")
            self.enable_preview()

    def get_selected_format(self):
        text = self.output_format_combo.currentText().strip()
        if text.endswith(":"):
            return "mp4"
        return text

    def output_format_changed(self):
        # Reflect GPU availability for selected format
        try:
            fmt = self.get_selected_format().lower()
            if fmt.endswith(":"):
                fmt = "mp4"
            self.gpu_checkbox.setEnabled(fmt not in ("webm", "gif", "mp3", "wav"))
        except Exception:
            pass

    def show_about(self):
        self.statusBar().showMessage("Vidium Video Converter v1.0\nBuilt with PySide6 and bundled FFmpeg.")

    def on_gpu_checkbox_toggled(self, checked: bool):
        try:
            self.settings.setValue("gpu_enabled", bool(checked))
        except Exception:
            pass

    def convert_mode_changed(self):
        mode = self.convert_mode_combo.currentText()
        if mode == "Convert Only":
            self.output_format_widget.show()
            self.convert_trim_widget.hide()
        elif mode == "Trim Only":
            self.output_format_widget.hide()
            self.convert_trim_widget.show()
        elif mode == "Trim & Convert":
            self.output_format_widget.show()
            self.convert_trim_widget.show()
        # Enable/disable GPU checkbox based on output format when visible
        try:
            selected_fmt = self.get_selected_format().lower()
            if selected_fmt.endswith(":"):
                selected_fmt = "mp4"
            self.gpu_checkbox.setEnabled(selected_fmt not in ("webm", "gif", "mp3", "wav"))
        except Exception:
            pass

    def download_mode_changed(self, index):
        mode = self.download_mode_combo.currentText()
        enable_format = mode in ["Download & Convert", "Download & Convert & Trim"]
        # Show/hide the Output controls entirely for non-conversion modes
        if hasattr(self, 'download_output_format_combo') and self.download_output_format_combo is not None:
            self.download_output_format_combo.setVisible(enable_format)
        # Keep the label visible as well, matching the combo's enabled state
        if hasattr(self, 'download_output_label') and self.download_output_label is not None:
            self.download_output_label.setVisible(enable_format)
        # Hide/show the small gap before output to avoid a stray space when hidden
        if hasattr(self, 'download_output_gap') and self.download_output_gap is not None:
            self.download_output_gap.setVisible(enable_format)
        # GPU is not applicable to WebM and some audio-only outputs; reflect that in the checkbox state
        try:
            if "Convert" in mode:
                fmt = self.download_output_format_combo.currentText().strip().lower()
                gpu_ok = fmt not in ("webm", "gif", "mp3", "wav")
                self.gpu_checkbox.setEnabled(gpu_ok)
            else:
                self.gpu_checkbox.setEnabled(True)
        except Exception:
            pass
        if "Trim" in mode:
            self.trim_widget.show()
        else:
            self.trim_widget.hide()

    @Slot(str, str)
    def convert_trim_finished(self, message, trimmed_file):
        self.append_log(message)
        mode = self.convert_mode_combo.currentText()
        if mode == "Trim & Convert":
            self.intermediate_file = trimmed_file
            selected_format = self.output_format_combo.currentText().strip()
            if selected_format.endswith(":"):
                selected_format = "mp4"
            base = os.path.splitext(trimmed_file)[0]
            new_file = base + "." + selected_format
            if os.path.splitext(trimmed_file)[1].lower() == "." + selected_format.lower():
                new_file = base + "_converted." + selected_format
            self.append_log(
                f"Starting conversion of trimmed file to {selected_format}...")
            use_gpu_flag = self.gpu_checkbox.isChecked()
            if selected_format.lower() == "webm":
                use_gpu_flag = False
            from converter import convert_file
            self.worker = ConversionWorker(trimmed_file, new_file, extra_args=None, use_gpu=use_gpu_flag, quality=self.quality_slider.value())
            self.worker.conversionFinished.connect(self.file_conversion_finished)
            self.worker.conversionError.connect(self.file_conversion_error)
            self.worker.logMessage.connect(self.append_log)
            self.worker.start()
        else:
            item = QListWidgetItem(os.path.basename(trimmed_file))
            item.setData(Qt.UserRole, trimmed_file)
            self.output_list.addItem(item)
            self.current_index += 1
        # overall percent reflected in label only
            self.start_next_conversion()

    @Slot(str)
    def convert_trim_error(self, error_message):
        self.append_log("Trim error: " + error_message)
        self.current_index += 1
        # overall percent reflected in label only
        self.start_next_conversion()

    @Slot(str, str)
    def trim_finished(self, message, trimmed_file):
        self.append_log(message)
        mode = self.download_mode_combo.currentText()
        if mode == "Download & Convert & Trim":
            selected_format = self.download_output_format_combo.currentText().strip()
            if selected_format.endswith(":"):
                selected_format = "mp4"
            base = os.path.splitext(trimmed_file)[0]
            new_file = base + "." + selected_format
            if os.path.splitext(trimmed_file)[1].lower() == "." + selected_format.lower():
                new_file = base + "_converted." + selected_format
            self.append_log(
                f"Starting conversion of trimmed file to {selected_format}...")
            use_gpu_flag = self.gpu_checkbox.isChecked()
            if selected_format.lower() == "webm":
                use_gpu_flag = False
            self.download_conversion_worker = ConversionWorker(
                trimmed_file, new_file, extra_args=None, use_gpu=use_gpu_flag, quality=self.quality_slider.value())
            self.download_conversion_worker.conversionFinished.connect(
                self.download_conversion_finished)
            self.download_conversion_worker.conversionError.connect(
                self.download_conversion_error)
            self.download_conversion_worker.logMessage.connect(self.append_log)
            self.download_conversion_worker.start()
        elif mode == "Download & Trim":
            item = QListWidgetItem(os.path.basename(trimmed_file))
            item.setData(Qt.UserRole, trimmed_file)
            self.output_list.addItem(item)
            self.download_button.setEnabled(True)
            self.download_browse_button.setEnabled(True)
            self.video_url_edit.setEnabled(True)
            self.download_folder_edit.setEnabled(True)
            self.download_default_checkbox.setEnabled(True)
            self.download_goto_button.setEnabled(True)

    @Slot(str)
    def trim_error(self, error_message):
        self.append_log("Trim error: " + error_message)
        self.download_button.setEnabled(True)
        self.download_browse_button.setEnabled(True)
        self.video_url_edit.setEnabled(True)
        self.download_folder_edit.setEnabled(True)
        self.download_default_checkbox.setEnabled(True)
        self.download_goto_button.setEnabled(True)

    @Slot(str, str)
    def download_conversion_finished(self, output_file, message):
        self.append_log(f"Download conversion finished: {message}")
        mode = self.download_mode_combo.currentText()
        if mode == "Download & Convert":
            original_file = self.download_conversion_worker.input_file
            if os.path.exists(original_file):
                try:
                    os.remove(original_file)
                    self.append_log(f"Removed original file: {original_file}")
                except Exception as e:
                    self.append_log(f"Could not remove original file: {e}")
            item = QListWidgetItem(os.path.basename(output_file))
            item.setData(Qt.UserRole, output_file)
            self.output_list.addItem(item)
            self.download_button.setEnabled(True)
            self.download_browse_button.setEnabled(True)
            self.video_url_edit.setEnabled(True)
            self.download_folder_edit.setEnabled(True)
            self.download_default_checkbox.setEnabled(True)
            self.download_goto_button.setEnabled(True)
        elif mode == "Download & Convert & Trim":
            original_trimmed_file = self.download_conversion_worker.input_file
            if os.path.exists(original_trimmed_file):
                try:
                    os.remove(original_trimmed_file)
                    self.append_log(
                        f"Removed trimmed file: {original_trimmed_file}")
                except Exception as e:
                    self.append_log(f"Could not remove trimmed file: {e}")
            item = QListWidgetItem(os.path.basename(output_file))
            item.setData(Qt.UserRole, output_file)
            self.output_list.addItem(item)
            self.download_button.setEnabled(True)
            self.download_browse_button.setEnabled(True)
            self.video_url_edit.setEnabled(True)
            self.download_folder_edit.setEnabled(True)
            self.download_default_checkbox.setEnabled(True)
            self.download_goto_button.setEnabled(True)

    @Slot(str)
    def download_conversion_error(self, error_message):
        self.append_log("Download conversion error: " + error_message)
        self.download_button.setEnabled(True)
        self.download_browse_button.setEnabled(True)
        self.video_url_edit.setEnabled(True)
        self.download_folder_edit.setEnabled(True)
        self.download_default_checkbox.setEnabled(True)
        self.download_goto_button.setEnabled(True)

    @Slot(str)
    def download_error(self, error_message):
        self.append_log("Download error: " + error_message)
        self.download_button.setEnabled(True)
        self.download_browse_button.setEnabled(True)
        self.video_url_edit.setEnabled(True)
        self.download_folder_edit.setEnabled(True)
        self.download_default_checkbox.setEnabled(True)
        self.download_goto_button.setEnabled(True)

    @Slot(int)
    def update_download_progress(self, progress):
        self.download_progress_label.setText(f"Progress: {progress}%")
        self.download_progress_bar.setValue(progress)
        if hasattr(self, 'download_sphere_view'):
            try:
                self.download_sphere_view.set_progress(progress)
            except Exception:
                pass
        if hasattr(self, 'status_label'):
            self.status_label.setText(f"STATUS: DOWNLOADING... {progress}%")
        if hasattr(self, 'sphere_view'):
            try:
                self.sphere_view.set_progress(progress)
            except Exception:
                pass

    def browse_download_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Download Folder", self.download_folder_edit.text().strip())
        if folder:
            self.download_folder_edit.setText(folder)
            self.download_default_checkbox.setChecked(False)
            self.settings.setValue("default_download_folder", folder)
            self.settings.setValue("default_download_checked", False)

    def goto_download_folder(self):
        folder = self.download_folder_edit.text().strip()
        if folder and os.path.isdir(folder):
            os.startfile(folder)
        else:
            self.statusBar().showMessage("Download folder not found.")

    def download_default_checkbox_changed(self, state):
        DEFAULT_DOWNLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Downloads")
        if state == Qt.Checked:
            self.download_folder_edit.setText(DEFAULT_DOWNLOAD_FOLDER)
            self.download_folder_edit.setReadOnly(True)
            self.settings.setValue("default_download_folder", DEFAULT_DOWNLOAD_FOLDER)
            self.settings.setValue("default_download_checked", True)
        else:
            self.download_folder_edit.setReadOnly(False)
            self.settings.setValue("default_download_checked", False)

    def start_download(self):
        url = self.video_url_edit.text().strip()
        if not url:
            self.statusBar().showMessage("Please enter a video URL.")
            return
        download_folder = self.download_folder_edit.text().strip()
        if not download_folder:
            self.statusBar().showMessage("Please select a download folder.")
            return
        self.download_button.setEnabled(False)
        self.download_browse_button.setEnabled(False)
        self.video_url_edit.setEnabled(False)
        self.download_folder_edit.setEnabled(False)
        self.download_default_checkbox.setEnabled(False)
        self.download_goto_button.setEnabled(False)
        self.append_log("Starting download...")
        if hasattr(self, 'status_label'):
            self.status_label.setText("STATUS: DOWNLOADING...")
        if hasattr(self, 'sphere_view'):
            try:
                self.sphere_view.set_progress(0)
            except Exception:
                pass
        if hasattr(self, 'download_sphere_view'):
            try:
                self.download_sphere_view.set_progress(0)
            except Exception:
                pass
        # Ensure any prior worker is cleanly stopped to avoid QThread destruction crash
        try:
            if hasattr(self, 'download_worker') and self.download_worker is not None and self.download_worker.isRunning():
                self.download_worker.terminate()
                self.download_worker.wait()
        except Exception:
            pass
        self.download_worker = DownloadWorker(url, download_folder)
        self.download_worker.finished.connect(self.download_finished)
        self.download_worker.error.connect(self.download_error)
        self.download_worker.progress.connect(self.update_download_progress)
        self.download_worker.start()

    @Slot(str, str)
    def download_finished(self, message, downloaded_file):
        self.append_log(message)
        if hasattr(self, 'status_label'):
            self.status_label.setText("STATUS: DOWNLOAD COMPLETE")
        # Force progress to 100 on completion for consistent bar behavior (after any merge)
        self.download_progress_label.setText("Progress: 100%")
        self.download_progress_bar.setValue(100)
        mode = self.download_mode_combo.currentText()
        if mode == "Download & Convert":
            selected_format = self.download_output_format_combo.currentText().strip()
            if selected_format.endswith(":"):
                selected_format = "mp4"
            download_ext = os.path.splitext(downloaded_file)[1].lower()
            if download_ext == "." + selected_format.lower():
                self.append_log(f"Skipping conversion for {downloaded_file} as it is already in the desired format.")
                item = QListWidgetItem(os.path.basename(downloaded_file))
                item.setData(Qt.UserRole, downloaded_file)
                self.output_list.addItem(item)
                self.download_button.setEnabled(True)
                self.download_browse_button.setEnabled(True)
                self.video_url_edit.setEnabled(True)
                self.download_folder_edit.setEnabled(True)
                self.download_default_checkbox.setEnabled(True)
                self.download_goto_button.setEnabled(True)
                if hasattr(self, 'sphere_view'):
                    try:
                        self.sphere_view.set_progress(100)
                    except Exception:
                        pass
                if hasattr(self, 'download_sphere_view'):
                    try:
                        self.download_sphere_view.set_progress(100)
                    except Exception:
                        pass
                return
            base = os.path.splitext(downloaded_file)[0]
            new_file = base + "." + selected_format
            self.append_log(f"Starting conversion of downloaded file to {selected_format}...")
            use_gpu_flag = self.gpu_checkbox.isChecked()
            if selected_format.lower() == "webm":
                use_gpu_flag = False
            from converter import convert_file
            self.download_conversion_worker = ConversionWorker(
                downloaded_file, new_file, extra_args=None, use_gpu=use_gpu_flag, quality=self.quality_slider.value())
            self.download_conversion_worker.conversionFinished.connect(
                self.download_conversion_finished)
            self.download_conversion_worker.conversionError.connect(
                self.download_conversion_error)
            self.download_conversion_worker.logMessage.connect(self.append_log)
            self.download_conversion_worker.start()
        elif mode == "Download & Convert & Trim":
            self.append_log("Starting trimming of downloaded file (for conversion and trimming)...")
            if hasattr(self, 'status_label'):
                self.status_label.setText("STATUS: TRIMMING...")
            self.trim_worker = TrimWorker(downloaded_file, self.trim_start_edit.text(
            ), self.trim_end_edit.text(), use_gpu=self.gpu_checkbox.isChecked(), copy_mode=False)
            self.trim_worker.finished.connect(self.trim_finished)
            self.trim_worker.error.connect(self.trim_error)
            self.trim_worker.start()
        elif mode == "Download & Trim":
            self.append_log("Starting trimming of downloaded file...")
            if hasattr(self, 'status_label'):
                self.status_label.setText("STATUS: TRIMMING...")
            self.trim_worker = TrimWorker(downloaded_file, self.trim_start_edit.text(
            ), self.trim_end_edit.text(), use_gpu=self.gpu_checkbox.isChecked(), copy_mode=True)
            self.trim_worker.finished.connect(self.trim_finished)
            self.trim_worker.error.connect(self.trim_error)
            self.trim_worker.start()
        else:
            self.download_button.setEnabled(True)
            self.download_browse_button.setEnabled(True)
            self.video_url_edit.setEnabled(True)
            self.download_folder_edit.setEnabled(True)
            self.download_default_checkbox.setEnabled(True)
            self.download_goto_button.setEnabled(True)
            if hasattr(self, 'sphere_view'):
                try:
                    self.sphere_view.set_progress(100)
                except Exception:
                    pass

    @Slot(str)
    def append_log(self, text):
        self.log_text_edit.appendPlainText(text)
        # Mirror to the active tab's inline terminal only
        if self.tab_widget.currentIndex() == 0 and hasattr(self, 'convert_terminal'):
            try:
                self.convert_terminal.append_scrolling(text)
            except Exception:
                self.convert_terminal.appendPlainText(text)
        elif self.tab_widget.currentIndex() == 1 and hasattr(self, 'download_terminal'):
            try:
                self.download_terminal.append_scrolling(text)
            except Exception:
                self.download_terminal.appendPlainText(text)
        if hasattr(self, 'sphere_view'):
            try:
                self.sphere_view.append_log(text)
            except Exception:
                pass

    def file_already_added(self, file_path):
        for i in range(self.input_list.count()):
            if self.input_list.item(i).text() == file_path:
                return True
        return False

    def is_supported_file(self, file_path):
        supported_exts = {'.mp4', '.mkv', '.webm', '.avi',
                          '.mov', '.flv', '.wmv', '.mpeg', '.mpg'}
        ext = os.path.splitext(file_path)[1].lower()
        return ext in supported_exts

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
            # Always route to Convert tab and Input subtab
            if self.tab_widget.currentIndex() != 0:
                self.tab_widget.setCurrentIndex(0)
            if self.files_tabwidget.currentIndex() != 0:
                self.files_tabwidget.setCurrentIndex(0)
            urls = event.mimeData().urls()
            for url in urls:
                file_path = url.toLocalFile()
                if self.is_supported_file(file_path) and not self.file_already_added(file_path):
                    self.input_list.addItem(file_path)
            if self.input_list.currentItem() is None and self.input_list.count() > 0:
                self.input_list.setCurrentRow(0)
        else:
            event.ignore()

    def show_drop_overlay(self):
        self.drop_overlay.setGeometry(self.rect())
        self.drop_overlay.raise_()
        self.drop_overlay.show()

    def hide_drop_overlay(self):
        self.drop_overlay.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'drop_overlay'):
            self.drop_overlay.setGeometry(self.rect())
        if hasattr(self, 'crt_overlay') and self.crt_overlay is not None:
            self.crt_overlay.setGeometry(self.rect())
        self.center_sphere_canvas()
        # Keep URL bar aligned with folder row after resizes
        try:
            self._align_download_url_width()
        except Exception:
            pass

    def center_sphere_canvas(self):
        try:
            # Convert tab centering
            if hasattr(self, 'convert_overlay') and self.convert_overlay is not None:
                w = max(1, self.width())
                h = max(1, self.height())
                # Left panel width
                left_panel = getattr(self, 'files_panel', None)
                left_w = left_panel.width() if left_panel else int(w * 0.28)
                # Right dock width
                right_dock = getattr(self, 'convert_right_dock', None)
                right_w = right_dock.width() if right_dock else int(w * 0.35)
                # Reference Y: vertically center between controls mid and actions
                try:
                    # Vertical midpoint between the overall top UI block and the progress bar,
                    # then bias upward by ~15% of the usable region height to avoid overlap
                    top_y = self.output_folder_edit.mapTo(self, self.output_folder_edit.rect().topLeft()).y()
                    bottom_y = self.convert_progress_panel.mapTo(self, self.convert_progress_panel.rect().bottomLeft()).y()
                    region_height = max(1.0, float(bottom_y - top_y))
                    region_center_y = (top_y + bottom_y) / 2.0 - 0.15 * region_height
                except Exception:
                    region_center_y = h / 2.0
                region_center_x = left_w + (w - left_w - right_w) / 2.0
                dx = region_center_x - (w / 2.0)
                dy = region_center_y - (h / 2.0)
                x_ratio = max(-0.5, min(0.5, dx / (w / 2.0)))
                y_ratio = max(-0.5, min(0.5, dy / (h / 2.0)))
                if hasattr(self, 'sphere_view'):
                    try:
                        # Also nudge the canvas position in pixels for exact alignment
                        px_x = dx; px_y = dy
                        if WEBENGINE_AVAILABLE and hasattr(self.sphere_view, 'set_canvas_offset'):
                            self.sphere_view.set_canvas_offset(px_x, px_y)
                        if hasattr(self.sphere_view, 'set_offset_ratios'):
                            self.sphere_view.set_offset_ratios(float(x_ratio), float(y_ratio))
                        else:
                            self.sphere_view.set_offset_ratio(float(x_ratio))
                    except Exception:
                        pass
            # Download tab centering
            if hasattr(self, 'download_right_dock') and self.download_right_dock is not None:
                if hasattr(self, 'download_sphere_view'):
                    try:
                        w = max(1, self.width()); h = max(1, self.height())
                        right_w = self.download_right_dock.width() if self.download_right_dock else int(w * 0.35)
                        left_w = 0
                        # Align vertically between trim row and progress in download tab
                        try:
                            # Vertical midpoint between the download tab top block and its progress bar,
                            # with a slight upward bias to match Convert tab positioning
                            dl_top = self.download_top_stack.mapTo(self, self.download_top_stack.rect().topLeft()).y()
                            dl_bottom = self.download_progress_panel.mapTo(self, self.download_progress_panel.rect().bottomLeft()).y()
                            region_height = max(1.0, float(dl_bottom - dl_top))
                            region_center_y = (dl_top + dl_bottom) / 2.0 - 0.15 * region_height
                        except Exception:
                            region_center_y = h / 2.0
                        region_center_x = left_w + (w - left_w - right_w) / 2.0
                        dx = region_center_x - (w / 2.0)
                        dy = region_center_y - (h / 2.0)
                        x_ratio = max(-0.5, min(0.5, dx / (w / 2.0)))
                        y_ratio = max(-0.5, min(0.5, dy / (h / 2.0)))
                        if WEBENGINE_AVAILABLE and hasattr(self.download_sphere_view, 'set_canvas_offset'):
                            self.download_sphere_view.set_canvas_offset(dx, dy)
                        if hasattr(self.download_sphere_view, 'set_offset_ratios'):
                            self.download_sphere_view.set_offset_ratios(float(x_ratio), float(y_ratio))
                        else:
                            self.download_sphere_view.set_offset_ratio(float(x_ratio))
                    except Exception:
                        pass
        except Exception:
            pass

    def stop_conversion(self):
        self.conversion_aborted = True
        threads = [self.worker, self.trim_worker, self.download_conversion_worker,
                   self.download_worker, self.preview_conversion_worker]
        for thread in threads:
            if thread is not None and thread.isRunning():
                if hasattr(thread, "stop"):
                    thread.stop()
                thread.wait()
        self.convert_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.enable_preview()
        self.append_log("Stop requested. Conversion aborted.")

    def closeEvent(self, event):
        threads = [self.worker, self.trim_worker, self.download_conversion_worker,
                    self.download_worker, self.preview_conversion_worker]
        for thread in threads:
            if thread is not None and thread.isRunning():
                try:
                    if hasattr(thread, "stop"):
                        thread.stop()
                    thread.terminate()
                    thread.wait()
                except Exception:
                    pass
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    if getattr(sys, 'frozen', False):
        base_path = os.path.dirname(sys.executable)
        icon_path = os.path.join(base_path, "_internal", "vicon.ico")
    else:
        base_path = os.path.abspath(".")
        icon_path = os.path.join(base_path, "vicon.ico")
    app.setWindowIcon(QIcon(icon_path))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
