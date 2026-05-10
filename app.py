import sys
import os
import json
import struct
import math
import re
import time
import ctypes
from threading import Thread, Lock
from PySide6.QtGui import QIcon, QPainter, QColor, QPen, QBrush, QPainterPath, QPixmap


import pywinusb.hid as hid
from PySide6.QtCore import Qt, Signal, QObject, QTimer, QSize
from PySide6.QtWidgets import QApplication, QFrame, QVBoxLayout, QHBoxLayout, QGridLayout, QWidget, QFileDialog, QInputDialog, QStackedWidget

from qfluentwidgets import (
    FluentWindow, SubtitleLabel, CaptionLabel, PushButton, PrimaryPushButton, 
    ComboBox, Slider, LineEdit, CheckBox, CardWidget, InfoBar, 
    StrongBodyLabel, setTheme, Theme, FluentIcon, BodyLabel, 
    TransparentToolButton, SmoothScrollArea, SimpleCardWidget,
    NavigationItemPosition
)

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

# --- Protocol Constants ---
VID, PID = 0x3302, 0x43E8
SETTINGS_FILE = "settings.json"
REPORT_ID = 0x4B
WRITE, READ, END = 0x01, 0x80, 0x00
CMD_PEQ_VALUES, CMD_GLOBAL_GAIN = 0x09, 0x03
CMD_VERSION, CMD_TEMP_WRITE, CMD_FLASH_EQ = 0x0C, 0x0A, 0x01
CMD_MIC_GAIN_ADDR = 0x02
TYPE_CODES = {"PK": 0x02, "LS": 0x03, "HS": 0x04}
INV_TYPE_CODES = {v: k for k, v in TYPE_CODES.items()}


FILTER_MAP = {0x01: "FAST-LL", 0x02: "Fast-PC (BEST)", 0x03: "Slow-LL", 0x04: "SLOW-PC", 0x05: "NOS"}
GAIN_MAP = {0x00: "LOW", 0x01: "HIGH"}
AMP_MAP = {0x00: "CLASS H", 0x01: "CLASS AB"}
DEFAULT_FREQS = [31, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 20000]
VOL_MIN_RAW, VOL_MAX_RAW, UNITS_PER_DB = -9472, 6440, 256

# --- Math Helpers for the Graph ---
def _calc_coeffs(t, f, q, g, fs=48000):
    if f <= 0: f = 1
    if q <= 0: q = 0.01
    A = 10**(g/40)
    w0 = 2*math.pi*f/fs
    sn = math.sin(w0)
    cs = math.cos(w0)
    alpha = sn/(2*q)
    
    if t == "PK":
        b0, b1, b2 = 1+alpha*A, -2*cs, 1-alpha*A
        a0, a1, a2 = 1+alpha/A, -2*cs, 1-alpha/A
    elif t in ["LS", "HS"]:
        sqA, s = math.sqrt(A), 1 if t == "HS" else -1
        b0 = A*((A+1)+s*(A-1)*cs+2*sqA*alpha)
        b1 = -s*2*A*((A-1)+s*(A+1)*cs)
        b2 = A*((A+1)+s*(A-1)*cs-2*sqA*alpha)
        a0 = (A+1)-s*(A-1)*cs+2*sqA*alpha
        a1 = s*2*((A-1)-s*(A+1)*cs)
        a2 = (A+1)-s*(A-1)*cs-2*sqA*alpha
    else: return (1.0, 0.0, 0.0, 0.0, 0.0)
    
    return (b0/a0, b1/a0, b2/a0, a1/a0, a2/a0)

def biquad_response(coeffs, freq_hz, fs=48000):
    b0, b1, b2, a1, a2 = coeffs
    w = 2 * math.pi * freq_hz / fs
    cos_w, sin_w = math.cos(w), math.sin(w)
    cos_2w, sin_2w = math.cos(2*w), math.sin(2*w)
    
    num_re = b0 + b1*cos_w + b2*cos_2w
    num_im = -b1*sin_w - b2*sin_2w
    den_re = 1 + a1*cos_w + a2*cos_2w
    den_im = -a1*sin_w - a2*sin_2w
    
    mag2 = (num_re**2 + num_im**2) / (den_re**2 + den_im**2) if (den_re**2 + den_im**2) != 0 else 1.0
    return 10 * math.log10(mag2) if mag2 > 0 else 0.0

# --- Graph Widget ---
class EQGraph(QWidget):
    point_moved = Signal(int, float, float)
    q_changed = Signal(int, float)
    point_clicked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        self.filters = []
        self.target_curve = None
        self.measurement_curve = None
        self.active_idx = 0
        self.dragging_idx = -1
        self.preamp = 0.0
        self.max_db_scale = 18.0
        
        self.visibility = {"eq": True, "eq_meas": True, "raw": True, "target": True, "ceiling": True}
        self.legend_rects = {}
        self.is_clipping = False  
        self.headroom_db = 100.0  
        
        self.setAttribute(Qt.WA_TranslucentBackground)

    def _downsample(self, data, max_points=300):
        """Compresses large arrays once during import so the paintEvent stays fast"""
        if not data: return None
        step = max(1, len(data) // max_points)
        return data[::step]

    def set_target_curve(self, data):
        self.target_curve = self._downsample(data)
        self.update()

    def set_measurement_curve(self, data):
        self.measurement_curve = self._downsample(data)
        self.update()

    def update_data(self, filters, active_idx, preamp=0.0):
        self.filters = filters
        self.active_idx = active_idx
        self.preamp = preamp
        self.update()

    # --- Math Helpers ---
    def _x_to_f(self, x): return 20.0 * (1000.0 ** (x / max(1, self.width())))
    def _f_to_x(self, f): return self.width() * math.log10(max(1, f) / 20.0) / 3.0
    def _db_to_y(self, db): return (self.height() / 2.0) - (db / self.max_db_scale) * (self.height() / 2.0)
    def _y_to_db(self, y): return ((self.height() / 2.0) - y) / (self.height() / 2.0) * self.max_db_scale

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        
        active_filters = [_calc_coeffs(f["type"], f["freq"], f["q"], f["gain"]) 
                          for f in self.filters if f.get("enabled", True)]
                          
        def get_eq_db(freq):
            return self.preamp + sum(biquad_response(c, freq) for c in active_filters)

        max_val = 18.0

        # 1. Main EQ Line Math
        curve_points = []
        step = 3 
        for x in range(0, w + step, step):
            freq = self._x_to_f(x)
            db_total = get_eq_db(freq)
            curve_points.append((x, db_total))
            if self.visibility.get("eq", True): 
                max_val = max(max_val, abs(db_total))

        # 2. Measurement Math (Now extremely fast due to downsampling)
        eq_meas_points = []
        if self.measurement_curve:
            for f, db in self.measurement_curve:
                if f < 20 or f > 20000: continue
                eq_db = db + get_eq_db(f)
                eq_meas_points.append((f, eq_db))
                if self.visibility.get("raw", True): max_val = max(max_val, abs(db))
                if self.visibility.get("eq_meas", True): max_val = max(max_val, abs(eq_db))

        if self.target_curve and self.visibility.get("target", True):
            for _, db in self.target_curve: max_val = max(max_val, abs(db))

        self.max_db_scale = max(18.0, math.ceil(max_val / 6.0) * 6.0)

        # Draw Grid
        metrics = painter.fontMetrics()
        gain_steps = list(range(int(self.max_db_scale), int(-self.max_db_scale)-1, -6))
        for db in gain_steps:
            y = int(self._db_to_y(db))
            painter.setPen(QPen(QColor(255, 255, 255, 15 if db != 0 else 80), 1))
            painter.drawLine(0, y, w, y)
            if db != 0:
                painter.setPen(QPen(QColor(255, 255, 255, 100)))
                painter.drawText(5, y - 4 if db < 0 else y + 12, f"{db} dB")
                
        freq_lines = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]
        freq_labels = {20: "20", 50: "50", 100: "100", 200: "200", 500: "500", 1000: "1k", 2000: "2k", 5000: "5k", 10000: "10k", 20000: "20k"}
        for f in freq_lines:
            x = int(self._f_to_x(f))
            painter.setPen(QPen(QColor(255, 255, 255, 15), 1))
            painter.drawLine(x, 0, x, h)
            if f in freq_labels:
                painter.setPen(QPen(QColor(255, 255, 255, 100)))
                tw = metrics.horizontalAdvance(freq_labels[f])
                # Increase the offset for the 20Hz label to prevent clashing with the dB scale
                x_pos = x - tw//2
                if f == 20: 
                    x_pos = max(45, x_pos) # Increased from 30 to 45 for better clearance
                painter.drawText(min(w - tw - 5, x_pos), h - 5, freq_labels[f])

        # Target Curve
        if self.target_curve and self.visibility.get("target", True):
            path = QPainterPath()
            painter.setPen(QPen(QColor(255, 255, 255, 80), 2, Qt.DashLine))
            first = True
            for f, db in self.target_curve:
                if f < 20 or f > 20000: continue
                x, y = self._f_to_x(f), self._db_to_y(db)
                if first: path.moveTo(x, y); first = False
                else: path.lineTo(x, y)
            if not first: painter.drawPath(path)
            
        # Raw Measurement
        if self.measurement_curve and self.visibility.get("raw", True):
            path = QPainterPath()
            painter.setPen(QPen(QColor(255, 136, 0, 100), 2))
            first = True
            for f, db in self.measurement_curve:
                if f < 20 or f > 20000: continue
                x, y = self._f_to_x(f), self._db_to_y(db)
                if first: path.moveTo(x, y); first = False
                else: path.lineTo(x, y)
            if not first: painter.drawPath(path)

        # EQ'd Compensated Measurement
        if eq_meas_points and self.visibility.get("eq_meas", True):
            path = QPainterPath()
            painter.setPen(QPen(QColor(0, 208, 132, 200), 2))
            first = True
            for f, db in eq_meas_points:
                x, y = self._f_to_x(f), self._db_to_y(db)
                if first: path.moveTo(x, y); first = False
                else: path.lineTo(x, y)
            if not first: painter.drawPath(path)
        
        # Main EQ Curve & Control Points
        if self.visibility.get("eq", True):
            path = QPainterPath()
            for i, (x, db) in enumerate(curve_points):
                y = self._db_to_y(db)
                if i == 0: path.moveTo(x, y)
                else: path.lineTo(x, y)
                
            hy = int(self._db_to_y(self.headroom_db))
            painter.setPen(QPen(QColor(255, 165, 0, 200), 2, Qt.DashLine))
            painter.drawLine(0, hy, w, hy)

            color = QColor("#ff4d4d") if self.is_clipping else QColor("#0078D4")
            painter.setPen(QPen(color, 2))
            painter.drawPath(path)
            
            for i, f in enumerate(self.filters):
                cx, cy = self._f_to_x(f["freq"]), self._db_to_y(f["gain"] + self.preamp)
                painter.setPen(QPen(QColor("#FFFFFF" if i == self.active_idx else "#888888"), 2 if i == self.active_idx else 1))
                painter.setBrush(QBrush(color if i == self.active_idx else QColor("#444444")))
                painter.drawEllipse(int(cx)-5, int(cy)-5, 10, 10)

        # Draw Interactive Legend
        legend_y, legend_x = h - 25, 55 
        self.legend_rects.clear()

        def draw_legend(key, text, color, is_dashed=False):
            nonlocal legend_y
            is_visible = self.visibility.get(key, True)
            
            draw_color = color if is_visible else QColor(100, 100, 100, 100)
            painter.setPen(QPen(draw_color, 2, Qt.DashLine if is_dashed else Qt.SolidLine))
            painter.drawLine(legend_x, legend_y - 4, legend_x + 20, legend_y - 4)
            
            text_color = QColor(255, 255, 255, 200) if is_visible else QColor(100, 100, 100, 150)
            painter.setPen(QPen(text_color))
            painter.drawText(legend_x + 28, legend_y, text)
            
            self.legend_rects[key] = (legend_x - 5, legend_y - 15, 150, 20)
            legend_y -= 22

        if self.filters or self.preamp != 0: draw_legend("eq", "EQ Curve", QColor("#0078D4"))
        if self.measurement_curve: draw_legend("eq_meas", "EQ'd Measurement", QColor(0, 208, 132, 200))
        if self.measurement_curve: draw_legend("raw", "Raw Measurement", QColor(255, 136, 0, 100))
        if self.target_curve: draw_legend("target", "Target Curve", QColor(255, 255, 255, 80), is_dashed=True)
        if self.visibility.get("eq", True): draw_legend("ceiling", "Digital Ceiling", QColor(255, 165, 0, 200), is_dashed=True)

    # --- Mouse Events ---
    def mousePressEvent(self, event):
        if not self.isEnabled(): return # <--- Fix: Ignore input if disabled
        x, y = event.position().x(), event.position().y()
        for key, (rx, ry, rw, rh) in self.legend_rects.items():
            if rx <= x <= rx + rw and ry <= y <= ry + rh:
                self.visibility[key] = not self.visibility.get(key, True)
                self.update()
                return

        if not self.visibility.get("eq", True): return

        min_dist, best_idx = 400, -1
        for i, f in enumerate(self.filters):
            if not f.get("enabled", True): continue
            cx, cy = self._f_to_x(f["freq"]), self._db_to_y(f["gain"] + self.preamp)
            dist = (cx-x)**2 + (cy-y)**2
            if dist < min_dist: min_dist, best_idx = dist, i
                
        if best_idx != -1 and min_dist <= 100:
            self.active_idx = best_idx
            self.dragging_idx = best_idx
            self.point_clicked.emit(best_idx)
            self.update()

    def mouseMoveEvent(self, event):
        if not self.isEnabled(): return
        if self.dragging_idx != -1:
            x, y = event.position().x(), event.position().y()
            f = self.filters[self.dragging_idx]
            new_f, new_g = f["freq"], f["gain"]
            
            if not f.get("lock_freq", False): new_f = max(20, min(20000, self._x_to_f(x)))
            if not f.get("lock_gain", False): new_g = max(-18.0, min(18.0, self._y_to_db(y) - self.preamp))
                
            self.point_moved.emit(self.dragging_idx, new_f, new_g)

    def mouseReleaseEvent(self, event):
        self.dragging_idx = -1

    def wheelEvent(self, event):
        if not self.isEnabled(): return
        if self.active_idx != -1 and self.visibility.get("eq", True):
            f = self.filters[self.active_idx]
            if not f.get("lock_q", False):
                delta = event.angleDelta().y() / 120.0
                new_q = max(0.1, min(18.0, f["q"] + delta * 0.1))
                self.q_changed.emit(self.active_idx, new_q)
                
# --- Communicator ---
class Communicator(QObject):
    sync_finished = Signal(object, object, object)
    status_msg = Signal(str)
    hw_vol_changed = Signal(int)  # Emitted when physical DAC buttons are pressed

# --- Main App ---
class FluentDACController(FluentWindow):
    def __init__(self):
        setTheme(Theme.DARK)
        super().__init__()

        self.setWindowTitle("TRN Control Panel")
        self.setWindowIcon(QIcon(resource_path("icon.ico")))
        self.resize(1000, 750) # Smaller default for laptop screens
        self.setMinimumSize(800, 600)

        self.read_results, self.parsed_filters = {}, {}
        self.hw_info = {"Man": "Loading...", "Prod": "Loading...", "SN": "Loading...", "FW": "Loading..."}
        self.is_syncing, self.active_device = False, None
        
        self.active_band_idx = 0
        self._updating_ui = False
        self.list_widgets = []

        self.comm = Communicator()
        self.usb_lock = Lock() # <--- Add this line
        self.settings_data = self.load_settings()
        self.actual_sn, self.sn_hidden, self.last_raw_vol = "", True, 0
        self.last_user_vol_change = 0  # Tracks when you grab the slider
        
        # Optimization: USB Debounce Queue
        self.dirty_usb_tasks = set()
        self.usb_timer = QTimer(self)
        self.usb_timer.setInterval(40)
        self.usb_timer.timeout.connect(self._process_usb_queue)

        # Auto-Flash Timer: Waits 3 seconds after inactivity to commit to hardware flash
        self.auto_flash_timer = QTimer(self)
        self.auto_flash_timer.setSingleShot(True)
        self.auto_flash_timer.setInterval(3000) 
        self.auto_flash_timer.timeout.connect(self._commit_to_flash)
        
        self.filter_desc_map = {
            "FAST-LL": "Fast roll-off, Low Latency. Best for gaming.",
            "Fast-PC (BEST)": "Fast roll-off, Phase Compensated. Recommended for general listening.",
            "Slow-LL": "Slow roll-off, Low Latency. Smoother treble transients.",
            "SLOW-PC": "Slow roll-off, Phase Compensated. Natural transient response.",
            "NOS": "Non-Oversampling. Purest signal path, high frequency roll-off."
        }
        
        self.dac_interface = QFrame(self)
        self.eq_interface = QFrame(self)
        self.about_interface = QFrame(self)
        
        self.dac_interface.setObjectName("dac_interface")
        self.eq_interface.setObjectName("eq_interface")
        self.about_interface.setObjectName("about_interface")
        
        self._setup_ui()
        self._connect_logic()
        
        self.conn_timer = QTimer(self)
        self.conn_timer.timeout.connect(self._check_connection)
        self.conn_timer.start(2000)
        
        # Fast Poller to detect physical DAC button presses
        self.vol_poll_timer = QTimer(self)
        self.vol_poll_timer.timeout.connect(self._poll_hw_volume)
        self.vol_poll_timer.start(300)

        QTimer.singleShot(500, self.refresh)

    def load_settings(self):
        default_filters = [{"type": "PK", "freq": DEFAULT_FREQS[i], "q": 1.0, "gain": 0.0, "enabled": True, "lock_freq": False, "lock_gain": False, "lock_q": False} for i in range(10)]
        default_data = {"balance": 0, "last_preset": 0, "presets": [{"name": "Default", "preamp": 0.0, "filters": default_filters}]}
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r") as f: 
                    data = {**default_data, **json.load(f)}
                    # FAIL-SAFE: Ensure the saved preset index actually exists
                    if data.get("last_preset", 0) >= len(data.get("presets", [])):
                        data["last_preset"] = 0
                    return data
            except: pass
        return default_data
        

    def _setup_ui(self):
        self.addSubInterface(self.dac_interface, FluentIcon.SETTING, "DAC Settings")
        self.addSubInterface(self.eq_interface, FluentIcon.MUSIC, "Parametric EQ")

        # --- DAC Interface with Scroll Support ---
        self.dac_scroll = SmoothScrollArea(self.dac_interface)
        self.dac_scroll.setWidgetResizable(True)
        self.dac_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.dac_scroll.viewport().setStyleSheet("background: transparent;")
        
        dac_container = QWidget()
        dac_container.setObjectName("dac_container")
        dac_container.setStyleSheet("#dac_container { background: transparent; }") # Explicitly target the ID
        dac_layout = QVBoxLayout(dac_container)
        dac_layout.setContentsMargins(20, 20, 20, 20)
        dac_layout.setSpacing(2)
        
       

        # 1. Hardware Info
        hw_header = QHBoxLayout()
        hw_header.addWidget(SubtitleLabel("Hardware Info", self))
        
        self.refresh_status_lbl_dac = CaptionLabel("", self)
        
        self.dac_refresh_btn = TransparentToolButton(FluentIcon.SYNC, self)
        self.dac_refresh_btn.clicked.connect(self.refresh)
        hw_header.addStretch(1)
        hw_header.addWidget(self.refresh_status_lbl_dac)
        hw_header.addWidget(self.dac_refresh_btn)
        dac_layout.addLayout(hw_header)
        
        info_card = CardWidget(self)
        info_l = QGridLayout(info_card)
        self.lbl_man = BodyLabel("Manufacturer: Loading...", info_card)
        self.lbl_prod = BodyLabel("Product: Loading...", info_card)
        self.lbl_fw = BodyLabel(f"Firmware: Loading...", info_card)
        self.lbl_sn = BodyLabel(f"Serial: Loading...", info_card)
        self.lbl_sn.setCursor(Qt.PointingHandCursor)
        self.lbl_sn.mousePressEvent = self._toggle_sn
        info_l.addWidget(self.lbl_man, 0, 0); info_l.addWidget(self.lbl_prod, 0, 1)
        info_l.addWidget(self.lbl_fw, 1, 0); info_l.addWidget(self.lbl_sn, 1, 1)
        dac_layout.addWidget(info_card)

         # --- Mic Gain Section ---
        dac_layout.addSpacing(20)
        dac_layout.addWidget(SubtitleLabel("Microphone Settings", self))
        
        mic_card = CardWidget(self)
        mic_l = QVBoxLayout(mic_card)
        mic_header = QHBoxLayout()
        mic_header.addWidget(StrongBodyLabel("Mic Gain", mic_card))
        self.mic_txt = BodyLabel("0 dB", mic_card)
        mic_header.addStretch(1); mic_header.addWidget(self.mic_txt)
        mic_l.addLayout(mic_header)
        
        self.mic_slider = Slider(Qt.Horizontal, mic_card)
        self.mic_slider.setRange(-15, 15) # Range from your uploaded script
        self.mic_slider.setValue(0)
        self.mic_slider.valueChanged.connect(self._on_mic_gain_change)
        mic_l.addWidget(self.mic_slider)
        dac_layout.addWidget(mic_card)

        # 2. Audio Settings
        dac_layout.addSpacing(20)
        dac_layout.addWidget(SubtitleLabel("Audio Settings", self))
        
        audio_card = CardWidget(self)
        audio_l = QVBoxLayout(audio_card)
        
        vol_header = QHBoxLayout()
        vol_header.addWidget(StrongBodyLabel("Hardware Volume", audio_card))
        self.vol_txt = BodyLabel("50%", audio_card)
        vol_header.addStretch(1); vol_header.addWidget(self.vol_txt)
        audio_l.addLayout(vol_header)
        
        self.vol_slider = Slider(Qt.Horizontal, audio_card)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.valueChanged.connect(self._on_volume_slider_changed)
        audio_l.addWidget(self.vol_slider)
        
        self.vol_warning_lbl = CaptionLabel("⚠️ Clipping Risk: DAC volume exceeds safe EQ headroom!", audio_card)
        self.vol_warning_lbl.setStyleSheet("color: #ff4d4d; font-weight: bold;"); self.vol_warning_lbl.hide()
        audio_l.addWidget(self.vol_warning_lbl); audio_l.addSpacing(15)

        bal_header = QHBoxLayout()
        bal_header.addWidget(StrongBodyLabel("Channel Balance", audio_card))
        self.bal_txt = BodyLabel("Center", audio_card)
        bal_header.addStretch(1); bal_header.addWidget(self.bal_txt)
        audio_l.addLayout(bal_header)
        
        self.bal_slider = Slider(Qt.Horizontal, audio_card)
        self.bal_slider.setRange(-15, 15)
        self.bal_slider.setValue(self.settings_data["balance"])
        self.bal_slider.valueChanged.connect(self._on_balance_change)
        audio_l.addWidget(self.bal_slider)
        dac_layout.addWidget(audio_card)

        # 3. DAC Settings
        dac_layout.addSpacing(20)
        dac_layout.addWidget(SubtitleLabel("DAC Settings", self))

        self.cb_filter, self.desc_filter = self._create_row(dac_layout, "Digital Filter", FILTER_MAP, 0x11, "Selects the internal DAC reconstruction filter algorithm.")
        self.cb_gain, self.desc_gain = self._create_row(dac_layout, "Gain Mode", GAIN_MAP, 0x19, "Adjusts the amplifier's base volume scaling for sensitive IEMs or demanding headphones.")
        self.cb_amp, self.desc_amp = self._create_row(dac_layout, "Amp Topology", AMP_MAP, 0x1D, "Switches between Class AB (Maximum audio performance) and Class H (Higher power efficiency).")
        
        self.cb_filter.currentTextChanged.connect(lambda t: self.desc_filter.setText(self.filter_desc_map.get(t, "Select a filter.")))
        
        # 4. Factory Reset
        dac_layout.addSpacing(20)
        dac_layout.addWidget(SubtitleLabel("Factory Reset", self))
        
        reset_card = SimpleCardWidget(self)
        reset_layout = QHBoxLayout(reset_card)
        reset_layout.setContentsMargins(16, 16, 16, 16)
        reset_layout.setAlignment(Qt.AlignVCenter)
        
        reset_text = QVBoxLayout()
        reset_text.setSpacing(4)
        reset_text.setAlignment(Qt.AlignVCenter)
        reset_text.addWidget(StrongBodyLabel("Reset System", reset_card))
        
        reset_desc = CaptionLabel("Restore all device settings to factory defaults.", reset_card)
        reset_desc.setTextColor(QColor(150, 150, 150))
        reset_text.addWidget(reset_desc)
        
        reset_layout.addLayout(reset_text)
        reset_layout.addStretch(1)
        
        self.btn_factory_reset = PushButton("Reset Device", reset_card)
        self.btn_factory_reset.clicked.connect(self._factory_reset)
        self.btn_factory_reset.setFixedWidth(220)
        reset_layout.addWidget(self.btn_factory_reset)
        
        dac_layout.addWidget(reset_card)
        dac_layout.addStretch(1)
        self.dac_scroll.setWidget(dac_container)
        
        # Set the main layout for the interface frame
        main_dac_layout = QVBoxLayout(self.dac_interface)
        main_dac_layout.setContentsMargins(0, 0, 0, 0)
        main_dac_layout.addWidget(self.dac_scroll)

        # --- EQ Interface ---
        eq_layout = QVBoxLayout(self.eq_interface); eq_layout.setContentsMargins(0, 0, 0, 0)
        
        header_frame = QWidget(self)
        header_l = QVBoxLayout(header_frame)
        
        # Reduced top margin from 20 to 10 and vertical spacing from 12 to 8
        header_l.setContentsMargins(15, 10, 15, 0)
        header_l.setSpacing(8)

        # --- Row 1: Core EQ Controls ---
        top_bar = QHBoxLayout()
        top_bar.setSpacing(8) # Tightens the gap between buttons/combo boxes
        top_bar.addWidget(SubtitleLabel("Parametric EQ", header_frame))
        top_bar.addStretch(1)
        
        self.btn_toggle_view = PushButton("List View", header_frame)
        self.btn_toggle_view.clicked.connect(self._toggle_view)
        top_bar.addWidget(self.btn_toggle_view)
        
        top_bar.addWidget(CaptionLabel("Preset:", header_frame))
        self.preset_cb = ComboBox(header_frame)
        self.preset_cb.addItems([p["name"] for p in self.settings_data["presets"]])
        self.preset_cb.setCurrentIndex(self.settings_data["last_preset"])
        self.preset_cb.currentIndexChanged.connect(self._load_preset_ui)
        top_bar.addWidget(self.preset_cb)
        
        self.btn_add = TransparentToolButton(FluentIcon.ADD, header_frame)
        self.btn_add.setToolTip("Create New Preset")
        self.btn_add.clicked.connect(self._new_preset)
        top_bar.addWidget(self.btn_add)

        self.btn_reset = PushButton("Flat EQ", header_frame)
        self.btn_reset.clicked.connect(self._reset_eq)
        top_bar.addWidget(self.btn_reset)

        header_l.addLayout(top_bar)
        
        # --- Row 2: File Operations (Clean Layout) ---
        io_bar = QHBoxLayout()
        
        # 1. Left Group: Custom Measurements/Targets
        self.btn_import_meas = PushButton(FluentIcon.MICROPHONE, "Import Measurement", header_frame)
        self.btn_import_meas.clicked.connect(self._import_measurement)
        io_bar.addWidget(self.btn_import_meas)

        self.btn_import_target = PushButton(FluentIcon.PIN, "Import Target Curve", header_frame)
        self.btn_import_target.clicked.connect(self._import_rew_target)
        io_bar.addWidget(self.btn_import_target)

        io_bar.addStretch(1) # Pushes AutoEQ to the far right

        # 2. Right Group: AutoEQ (No Refresh Icon)
        io_bar.addWidget(CaptionLabel("AutoEQ Config:", header_frame))
        
        self.btn_import = PushButton(FluentIcon.DOWNLOAD, "Import", header_frame)
        self.btn_import.clicked.connect(self._import_squig)
        io_bar.addWidget(self.btn_import)

        self.btn_export = PushButton(FluentIcon.SHARE, "Export", header_frame)
        self.btn_export.clicked.connect(self._export_autoeq)
        io_bar.addWidget(self.btn_export)

        header_l.addSpacing(4) 
        io_bar.setSpacing(8) 
        header_l.addLayout(io_bar)
        
        # EQ Tab Volume Card
        eq_vol_card = SimpleCardWidget(header_frame)
        eq_vol_l = QVBoxLayout(eq_vol_card)
        eq_vol_l.setContentsMargins(10, 9, 10, 9) # Tighter internal padding

        # Top row for Title, Warning, and Percentage
        eq_vol_top = QHBoxLayout()
        eq_vol_top.addWidget(StrongBodyLabel("Master Volume", eq_vol_card))
        
        self.eq_vol_warning_lbl = CaptionLabel("  ⚠️ Clipping Risk!", eq_vol_card)
        self.eq_vol_warning_lbl.setStyleSheet("color: #ff4d4d; font-weight: bold;")
        self.eq_vol_warning_lbl.hide()
        eq_vol_top.addWidget(self.eq_vol_warning_lbl)
        
        eq_vol_top.addStretch(1) # Pushes the percentage text to the far right
        
        self.eq_vol_txt = BodyLabel("50%", eq_vol_card)
        eq_vol_top.addWidget(self.eq_vol_txt)
        
        eq_vol_l.addLayout(eq_vol_top)

        # Bottom row dedicated entirely to the slider
        self.eq_vol_slider = Slider(Qt.Horizontal, eq_vol_card)
        self.eq_vol_slider.setRange(0, 100)
        self.eq_vol_slider.valueChanged.connect(self._on_volume_slider_changed)
        
        eq_vol_l.addWidget(self.eq_vol_slider)

        # Add a small negative spacing if necessary, or just keep it tight
        header_l.addSpacing(-4) 
        header_l.addWidget(eq_vol_card)
        eq_layout.addWidget(header_frame)
        eq_layout.setSpacing(0)

        # STACKED WIDGET FOR VIEWS
        self.stack = QStackedWidget(self)
        eq_layout.addWidget(self.stack, 1)

        # --- VIEW 0: Graph UI ---
        self.graph_page = QWidget()
        graph_layout = QVBoxLayout(self.graph_page); graph_layout.setContentsMargins(15, 0, 15, 20)
        
        self.graph = EQGraph(self.graph_page)
        self.graph.setMinimumHeight(350)
        self.graph.point_moved.connect(self._on_graph_point_moved)
        self.graph.q_changed.connect(self._on_graph_q_changed)
        self.graph.point_clicked.connect(self.set_active_band)
        graph_layout.addWidget(self.graph, 1)

        self.active_band_card = SimpleCardWidget(self.graph_page)
        self.active_band_card.setStyleSheet("SimpleCardWidget { background: transparent; border: 1px solid rgba(255, 255, 255, 0.08); }")
        bl = QHBoxLayout(self.active_band_card)
        bl.setContentsMargins(10, 5, 10, 5)

        self.band_lbl = StrongBodyLabel("Band 1", self.active_band_card); bl.addWidget(self.band_lbl)
        self.band_enable = CheckBox(self.active_band_card); self.band_enable.setFixedWidth(30); bl.addWidget(self.band_enable)
        
        self.band_type = ComboBox(self.active_band_card); self.band_type.addItems(["PK", "LS", "HS"]); self.band_type.setFixedWidth(80); bl.addWidget(self.band_type)
        self.band_freq = LineEdit(self.active_band_card); self.band_freq.setFixedWidth(65); bl.addWidget(self.band_freq); bl.addWidget(CaptionLabel("Hz", self.active_band_card))
        self.lock_freq = CheckBox("Lock", self.active_band_card)
        bl.addWidget(self.lock_freq)

        self.band_gain_sld = Slider(Qt.Horizontal, self.active_band_card); self.band_gain_sld.setRange(-100, 100); bl.addWidget(self.band_gain_sld, 1)
        self.band_gain_val = LineEdit(self.active_band_card); self.band_gain_val.setFixedWidth(45); bl.addWidget(self.band_gain_val); bl.addWidget(CaptionLabel("dB", self.active_band_card))
        self.lock_gain = CheckBox("Lock", self.active_band_card)
        bl.addWidget(self.lock_gain)

        self.band_q_val = LineEdit(self.active_band_card); self.band_q_val.setFixedWidth(45); bl.addWidget(CaptionLabel("Q:", self.active_band_card)); bl.addWidget(self.band_q_val)
        self.lock_q = CheckBox("Lock", self.active_band_card)
        bl.addWidget(self.lock_q)

        
        
        graph_layout.addWidget(self.active_band_card)
        self.stack.addWidget(self.graph_page)

        # --- VIEW 1: List UI ---
        # --- VIEW 1: List UI ---
        self.list_page = QWidget()
        # Horizontal margins changed to 15 to match top bar
        list_page_layout = QVBoxLayout(self.list_page); list_page_layout.setContentsMargins(15, 10, 15, 10)

        self.small_graph = EQGraph(self.list_page)
        self.small_graph.setFixedHeight(200)
        self.small_graph.point_moved.connect(self._on_graph_point_moved)
        self.small_graph.q_changed.connect(self._on_graph_q_changed)
        self.small_graph.point_clicked.connect(self.set_active_band)
        list_page_layout.addWidget(self.small_graph)

        scroll = SmoothScrollArea(self.list_page); scroll.setWidgetResizable(True); scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        scroll.viewport().setStyleSheet("background: transparent;")
        list_container = QWidget(); list_container.setStyleSheet("background: transparent;")
        
        # Margins and spacing set to 0 to stack bands perfectly
        self.bands_layout = QVBoxLayout(list_container)
        self.bands_layout.setContentsMargins(0, 0, 0, 0)
        self.bands_layout.setSpacing(0)
        # Set margins and spacing to 0 to remove all gaps between the rows
        self.bands_layout.setContentsMargins(0, 0, 0, 0)
        self.bands_layout.setSpacing(0)

        for i in range(10):
            band_card = SimpleCardWidget(list_container)
            # Set fixed height and bottom-only border for clean stacking
            band_card.setFixedHeight(50)
            band_card.setStyleSheet("SimpleCardWidget { background: transparent; border: none; border-bottom: 1px solid rgba(255, 255, 255, 0.08); }")
            
            lbl_layout = QHBoxLayout(band_card)
            lbl_layout.setContentsMargins(10, 0, 10, 0)
            lbl_layout.setSpacing(8)
            
            # FIXED WIDTH for index label prevents "10" from pushing other widgets
            band_num_lbl = StrongBodyLabel(f"{i+1}", band_card)
            band_num_lbl.setFixedWidth(20) 
            lbl_layout.addWidget(band_num_lbl)
            
            chk = CheckBox(band_card); chk.setFixedWidth(30)
            typ = ComboBox(band_card); typ.addItems(["PK", "LS", "HS"]); typ.setFixedWidth(80)
            freq = LineEdit(band_card); freq.setFixedWidth(65)
            
            lbl_layout.addWidget(chk); lbl_layout.addWidget(typ); lbl_layout.addWidget(freq); lbl_layout.addWidget(CaptionLabel("Hz", band_card))
            
            sld, gain = Slider(Qt.Horizontal, band_card), LineEdit(band_card)
            sld.setRange(-100, 100); gain.setFixedWidth(45)
            
            lbl_layout.addWidget(sld, 1) # This expands to fill the middle space
            lbl_layout.addWidget(gain); lbl_layout.addWidget(CaptionLabel("dB", band_card))
            
            qv = LineEdit(band_card); qv.setFixedWidth(45)
            lbl_layout.addSpacing(15); lbl_layout.addWidget(CaptionLabel("Q:", band_card)); lbl_layout.addWidget(qv)
            
            chk.stateChanged.connect(lambda _, idx=i: self._on_list_ui_changed(idx))
            typ.currentIndexChanged.connect(lambda _, idx=i: self._on_list_ui_changed(idx))
            sld.valueChanged.connect(lambda v, idx=i, le=gain: [le.setText(str(v/10)), self._on_list_ui_changed(idx)])
            gain.editingFinished.connect(lambda s=sld, le=gain: s.setValue(int(float(le.text().replace(',', '.') or 0) * 10)))
            freq.editingFinished.connect(lambda idx=i: self._on_list_ui_changed(idx))
            qv.editingFinished.connect(lambda idx=i: self._on_list_ui_changed(idx))
            
            self.list_widgets.append({"enabled": chk, "type": typ, "freq": freq, "gain": gain, "q": qv, "slider": sld})
            self.bands_layout.addWidget(band_card)
            
        scroll.setWidget(list_container); list_page_layout.addWidget(scroll)
        self.stack.addWidget(self.list_page)

        # Visual UI Logic Bindings
        self.band_enable.stateChanged.connect(self._on_visual_ui_changed)
        self.band_type.currentIndexChanged.connect(self._on_visual_ui_changed)
        self.band_freq.editingFinished.connect(self._on_visual_ui_changed)
        self.band_gain_sld.valueChanged.connect(self._on_visual_ui_changed)
        self.band_gain_val.editingFinished.connect(self._on_gain_val_edited)
        self.band_q_val.editingFinished.connect(self._on_visual_ui_changed)
        self.lock_freq.stateChanged.connect(self._on_visual_ui_changed)
        self.lock_gain.stateChanged.connect(self._on_visual_ui_changed)
        self.lock_q.stateChanged.connect(self._on_visual_ui_changed)

        # --- About Interface (Fluent UI Card) ---
        about_layout = QVBoxLayout(self.about_interface)
        about_layout.setAlignment(Qt.AlignCenter)
        
        about_card = CardWidget(self.about_interface)
        about_card.setFixedSize(400, 250)
        card_layout = QVBoxLayout(about_card)
        card_layout.setAlignment(Qt.AlignCenter)
        card_layout.setSpacing(15)
        
        icon_btn = TransparentToolButton(about_card)
        icon_btn.setIcon(QIcon(resource_path("icon.ico")))
        icon_btn.setIconSize(QSize(64, 64))
        card_layout.addWidget(icon_btn, 0, Qt.AlignCenter)
        
        card_layout.addWidget(SubtitleLabel("TRN Control Panel", about_card), 0, Qt.AlignCenter)
        
        author_lbl = BodyLabel("by KDRN", about_card)
        author_lbl.setTextColor(QColor(150, 150, 150))
        card_layout.addWidget(author_lbl, 0, Qt.AlignCenter)
        
        about_layout.addWidget(about_card)
        
        self.addSubInterface(self.about_interface, FluentIcon.INFO, "About", position=NavigationItemPosition.BOTTOM)

        self._sync_all_uis()
        self.toggle_controls(False)

    # --- UI Logic Methods ---
    def _toggle_sn(self, event):
        if not self.actual_sn: return
        self.sn_hidden = not self.sn_hidden
        self.lbl_sn.setText(f"Serial: {'********' if self.sn_hidden else self.actual_sn}")

    def _poll_hw_volume(self):
        """Only polls when the user is NOT interacting with the app"""
        # If the USB queue is active (slider moving), skip polling to save bandwidth
        if self.is_syncing or self.usb_timer.isActive(): 
            return
            
        with self.usb_lock:
            dev = self.get_device()
            if dev:
                try: dev.find_output_reports()[0].send([REPORT_ID, READ, CMD_GLOBAL_GAIN, END] + [0x00]*60)
                except: pass

    def _sync_hw_volume_ui(self, raw_vol):
        """Triggered when you press a button on the DAC"""
        # Decouple: Ignore the DAC's reported volume if the PC is actively sending volume commands
        if self.usb_timer.isActive() or -1 in self.dirty_usb_tasks:
            return
            
        self.last_raw_vol = raw_vol
        self._check_headroom(auto_level=False)

    def _check_headroom(self, auto_level=False):
        # Calculate available digital ceiling based on current volume
        ceiling_db = (VOL_MAX_RAW - self.last_raw_vol) / UNITS_PER_DB
        self.graph.headroom_db = ceiling_db
        self.small_graph.headroom_db = ceiling_db
        
        p = self.settings_data["presets"][self.preset_cb.currentIndex()]
        active_filters = [f for f in p["filters"] if f.get("enabled", True)]
        active_coeffs = [_calc_coeffs(f["type"], f["freq"], f["q"], f["gain"]) for f in active_filters]
        
        # Accurately find the max EQ peak by checking standard frequencies PLUS the exact center of every active band
        max_db = 0.0
        if active_coeffs:
            freqs_to_check = [31, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
            freqs_to_check.extend([f.get("freq", 1000) for f in active_filters])
            
            for f_hz in freqs_to_check:
                max_db = max(max_db, sum(biquad_response(c, f_hz) for c in active_coeffs))
                
        safe_max = VOL_MAX_RAW - int(max(0, max_db) * UNITS_PER_DB)
        
        # Auto-level volume if EQ is modified and causes clipping
        if auto_level and self.last_raw_vol > safe_max:
            self.last_raw_vol = safe_max
            self._apply_filter(-1) # <-- FIX: Queue the new safe volume to physically send to the DAC!
            
        is_clipping = self.last_raw_vol > safe_max
        
        # Update Visuals
        self.graph.is_clipping = is_clipping
        self.small_graph.is_clipping = is_clipping
        self.vol_warning_lbl.setVisible(is_clipping)
        self.eq_vol_warning_lbl.setVisible(is_clipping)
        
        style = "QSlider::handle { background: #ff4d4d; }" if is_clipping else ""
        self.vol_slider.setStyleSheet(style)
        self.eq_vol_slider.setStyleSheet(style)
        
        # Force the graphs to redraw the orange headroom line
        self.graph.update()
        self.small_graph.update()
        
        # Sync Slider UI
        pct = max(0, min(100, int(((self.last_raw_vol - VOL_MIN_RAW) / (VOL_MAX_RAW - VOL_MIN_RAW)) * 100)))
        self._updating_ui = True
        self.vol_slider.setValue(pct); self.vol_txt.setText(f"{pct}%")
        self.eq_vol_slider.setValue(pct); self.eq_vol_txt.setText(f"{pct}%")
        self._updating_ui = False

    def _on_volume_slider_changed(self, pos):
        if self._updating_ui or self.is_syncing: return
        
        # 1. Update the raw hardware math based on the slider position
        self.last_raw_vol = int(VOL_MIN_RAW + (pos / 100.0) * (VOL_MAX_RAW - VOL_MIN_RAW))
        
        # 2. Check headroom (This automatically syncs the sliders and text visually)
        self._check_headroom(auto_level=False)
        
        # 3. Queue the USB command silently in the background
        self._apply_filter(-1)

    def _factory_reset(self):
        if self.is_syncing: return
        
        # 1. Reset DAC Settings
        self.bal_slider.setValue(0)
        self.cb_filter.setCurrentText("Fast-PC (BEST)")
        self.cb_gain.setCurrentText("LOW")
        self.cb_amp.setCurrentText("CLASS H")
        
        # 2. Reset Mic Gain to 0 dB
        self.mic_slider.setValue(0) # This triggers _on_mic_gain_change automatically
        
        # 3. Reset PEQ and Preamp
        self._reset_eq() 
        
        # 4. Commit all changes to hardware flash
        self._commit_to_flash()
    
        InfoBar.success("Factory Reset", "All settings, including Mic Gain, restored to defaults.", parent=self)

    def _on_preamp_slider_changed(self, v):
        if self._updating_ui: return
        val = float(v)
        self.preamp_val.setText(str(int(val)))
        p = self.settings_data["presets"][self.preset_cb.currentIndex()]
        p["preamp"] = val
        
        self.graph.update_data(p["filters"], self.active_band_idx, val)
        self.small_graph.update_data(p["filters"], self.active_band_idx, val)
        self._apply_filter(-1)

    def _on_preamp_val_changed(self):
        if self._updating_ui: return
        try:
            val = int(float(self.preamp_val.text().replace(',', '.')))
            self.pre_slider.setValue(val)
        except ValueError:
            pass

    def _toggle_view(self):
        if self.stack.currentIndex() == 0:
            self.stack.setCurrentIndex(1)
            self.btn_toggle_view.setText("Switch to Graph View")
        else:
            self.stack.setCurrentIndex(0)
            self.btn_toggle_view.setText("Switch to List View")

    def set_active_band(self, idx):
        if idx < 0 or idx >= 10: return
        self.active_band_idx = idx
        self._sync_all_uis()

    def _sync_all_uis(self, update_idx=None):
        self._updating_ui = True
        p = self.settings_data["presets"][self.preset_cb.currentIndex()]
        
        # Optimization: Only update the UI elements for the specific band being dragged
        indices = range(10) if update_idx is None else [update_idx]
        
        for idx in indices:
            f = p["filters"][idx]
            
            lw = self.list_widgets[idx]
            lw["enabled"].setChecked(f.get("enabled", True))
            lw["type"].setCurrentText(f.get("type", "PK"))
            lw["freq"].setText(str(int(f.get("freq", 100))))
            lw["slider"].setValue(int(f.get("gain", 0) * 10))
            lw["gain"].setText(str(round(f.get("gain", 0), 1)))
            lw["q"].setText(str(round(f.get("q", 1.0), 2)))
            
            if idx == self.active_band_idx:
                self.band_lbl.setText(f"Band {idx+1}")
                self.band_enable.setChecked(f.get("enabled", True))
                self.band_type.setCurrentText(f.get("type", "PK"))
                self.band_freq.setText(str(int(f.get("freq", 100))))
                self.band_gain_sld.setValue(int(f.get("gain", 0) * 10))
                self.band_gain_val.setText(str(round(f.get("gain", 0), 1)))
                self.band_q_val.setText(str(round(f.get("q", 1.0), 2)))
                self.lock_freq.setChecked(f.get("lock_freq", False))
                self.lock_gain.setChecked(f.get("lock_gain", False))
                self.lock_q.setChecked(f.get("lock_q", False))
                
        self.graph.update_data(p["filters"], self.active_band_idx, 0.0)
        self.small_graph.update_data(p["filters"], self.active_band_idx, 0.0)
        self._updating_ui = False
        self._check_headroom(auto_level=True) # Check headroom after syncing UI

    def _on_visual_ui_changed(self, *_):
        if self._updating_ui: return
        idx = self.active_band_idx
        p = self.settings_data["presets"][self.preset_cb.currentIndex()]
        f = p["filters"][idx]
        
        f["enabled"] = self.band_enable.isChecked()
        f["type"] = self.band_type.currentText()
        try: f["freq"] = max(20, min(20000, int(self.band_freq.text())))
        except: pass
        
        f["gain"] = self.band_gain_sld.value() / 10.0
        self.band_gain_val.setText(str(f["gain"]))
        
        # Remove the hardcoded 0.1 and read the value from the UI
        try: f["q"] = max(0.1, min(18.0, float(self.band_q_val.text().replace(',', '.'))))
        except: pass
        
        f["lock_freq"] = self.lock_freq.isChecked()
        f["lock_gain"] = self.lock_gain.isChecked()
        f["lock_q"] = self.lock_q.isChecked()
        
        self._sync_all_uis(update_idx=idx)
        self._apply_filter(idx)

    def _on_list_ui_changed(self, idx):
        if self._updating_ui: return
        p = self.settings_data["presets"][self.preset_cb.currentIndex()]
        f = p["filters"][idx]
        lw = self.list_widgets[idx]
        
        f["enabled"] = lw["enabled"].isChecked()
        f["type"] = lw["type"].currentText()
        try: f["freq"] = max(20, min(20000, int(lw["freq"].text())))
        except: pass
        f["gain"] = lw["slider"].value() / 10.0
        try: f["q"] = max(0.1, min(18.0, float(lw["q"].text())))
        except: pass
        
        self._sync_all_uis(update_idx=idx)
        self._apply_filter(idx)
        
    def _on_gain_val_edited(self):
        if self._updating_ui: return
        try: 
            val = float(self.band_gain_val.text().replace(',', '.'))
            self.band_gain_sld.setValue(int(val * 10))
        except: pass

    def _on_graph_point_moved(self, idx, freq, gain):
        p = self.settings_data["presets"][self.preset_cb.currentIndex()]
        f = p["filters"][idx]
        f["freq"] = max(20, min(20000, freq)) 
        f["gain"] = max(-10.0, min(10.0, gain))
        
        # OPTIMIZATION: Only sync the 1 row being moved, not all 10
        self._sync_all_uis(update_idx=idx)
        self._apply_filter(idx)

    def _on_graph_q_changed(self, idx, q):
        p = self.settings_data["presets"][self.preset_cb.currentIndex()]
        f = p["filters"][idx]
        f["q"] = q
        self._sync_all_uis(update_idx=idx)
        self._apply_filter(idx)

    def _parse_rew_file(self, path):
        data = []
        with open(path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('*') or line.startswith(';') or line.startswith('#'): continue
                parts = line.replace(',', ' ').split()
                if len(parts) >= 2:
                    try: data.append((float(parts[0]), float(parts[1])))
                    except ValueError: pass
        if data:
            closest = min(data, key=lambda x: abs(x[0] - 1000.0))
            offset = closest[1]
            return [(f, db - offset) for f, db in data]
        return None

    def _import_rew_target(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open REW Target", "", "Text/CSV Files (*.txt *.csv);;All Files (*)")
        if not path: return
        try:
            normalized = self._parse_rew_file(path)
            if normalized:
                self.graph.set_target_curve(normalized)
                self.small_graph.set_target_curve(normalized)
                InfoBar.success("Success", f"Loaded target curve ({len(normalized)} points)", parent=self)
            else:
                InfoBar.warning("Warning", "No valid frequency/dB data found.", parent=self)
        except Exception as e:
            InfoBar.error("Error", f"Failed to load file: {e}", parent=self)

    def _import_measurement(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open Measurement", "", "Text/CSV Files (*.txt *.csv);;All Files (*)")
        if not path: return
        try:
            normalized = self._parse_rew_file(path)
            if normalized:
                self.graph.set_measurement_curve(normalized)
                self.small_graph.set_measurement_curve(normalized)
                InfoBar.success("Success", f"Loaded measurement curve ({len(normalized)} points)", parent=self)
            else:
                InfoBar.warning("Warning", "No valid frequency/dB data found.", parent=self)
        except Exception as e:
            InfoBar.error("Error", f"Failed to load file: {e}", parent=self)

    # --- Hardware & Application Logic ---
    def _check_connection(self):
        dev = self.get_device()
        if not dev and self.lbl_sn.text() != "Serial: Disconnected":
            self.comm.status_msg.emit("Disconnected")
        elif dev and self.lbl_sn.text() == "Serial: Disconnected":
            self.refresh()

    def toggle_controls(self, enabled):
        objs = [self.vol_slider, self.eq_vol_slider, self.bal_slider, self.cb_filter, 
                self.cb_gain, self.cb_amp, self.preset_cb, 
                self.btn_reset, self.btn_add, self.btn_import_meas, self.btn_import_target,
                self.btn_import, self.btn_export, self.graph, self.small_graph,
                self.btn_toggle_view, self.active_band_card,
                self.band_enable, self.band_type, self.band_freq, self.band_gain_sld, 
                self.band_gain_val, self.band_q_val, self.lock_freq, self.lock_gain, self.lock_q,
                self.btn_factory_reset, self.mic_slider]
        for o in objs: o.setEnabled(enabled)
        for lw in self.list_widgets:
            for k, w in lw.items(): w.setEnabled(enabled)

    def _create_row(self, layout, title, mapping, cmd, default_desc=""):
        card = SimpleCardWidget(self)
        main_layout = QHBoxLayout(card)
        main_layout.setContentsMargins(16, 15, 16, 15)
        main_layout.setAlignment(Qt.AlignVCenter)
        
        text_layout = QVBoxLayout()
        text_layout.setSpacing(4)
        text_layout.setAlignment(Qt.AlignVCenter)
        text_layout.addWidget(StrongBodyLabel(title, card))
        
        desc_lbl = CaptionLabel(default_desc, card)
        desc_lbl.setTextColor(QColor(150, 150, 150))
        text_layout.addWidget(desc_lbl)
        
        main_layout.addLayout(text_layout)
        main_layout.addStretch(1)
        
        cb = ComboBox(card); cb.addItems(list(mapping.values())); cb.setFixedWidth(220)
        cb.currentIndexChanged.connect(lambda: self.write_val(cmd, cb.currentText(), mapping))
        main_layout.addWidget(cb)
        
        layout.addWidget(card)
        return cb, desc_lbl

    def get_device(self):
        # Keeps the device permanently open to prevent pywinusb queue crashes
        if self.active_device and self.active_device.is_plugged():
            if not self.active_device.is_opened():
                try: 
                    self.active_device.open()
                    self.active_device.set_raw_data_handler(self.on_data)
                except: pass
            return self.active_device
            
        devs = hid.HidDeviceFilter(vendor_id=VID, product_id=PID).get_devices()
        if devs: 
            self.active_device = devs[0]
            try:
                self.active_device.open()
                self.active_device.set_raw_data_handler(self.on_data)
            except: pass
        else: self.active_device = None
        return self.active_device

    def refresh(self):
        if self.is_syncing: return
        self.is_syncing = True
        
        # Only update the DAC label since the EQ label was removed
        self.refresh_status_lbl_dac.setText("Refreshing...")
        
        self.read_results.clear()
        self.parsed_filters.clear()

        def run():
            with self.usb_lock:
                dev = self.get_device()
                if not dev: 
                    self.comm.status_msg.emit("Disconnected")
                    self.is_syncing = False
                    return
                try:
                    report = dev.find_output_reports()[0]
                    for cmd in [CMD_VERSION, 0x11, 0x19, 0x1D, CMD_GLOBAL_GAIN, 0x02, "BAL_L", "BAL_R"]:
                        if cmd == 0x02:
                            report.send([REPORT_ID, READ, 0x02, 0x02] + [0x00]*60)
                        elif cmd == "BAL_L":
                            report.send([REPORT_ID, READ, 0x16, 0x04, 0x01, 0x00, 0x00] + [0x00]*57)
                        elif cmd == "BAL_R":
                            report.send([REPORT_ID, READ, 0x16, 0x04, 0x00, 0x00, 0x00] + [0x00]*57)
                        else:
                            report.send([REPORT_ID, READ, cmd, END] + [0x00]*60)
                        time.sleep(0.06)
                    
                    for i in range(10):
                        report.send([REPORT_ID, READ, CMD_PEQ_VALUES, 0x00, 0x00, i, END] + [0x00]*57)
                        time.sleep(0.06)
                    
                    time.sleep(0.3)
                    self.comm.sync_finished.emit({"SN": dev.serial_number}, self.read_results, self.parsed_filters)
                except: 
                    self.is_syncing = False
        Thread(target=run, daemon=True).start()
        
    def on_data(self, data):
        if data[0] == REPORT_ID and data[1] == READ:
            cmd = data[2]
            if cmd == CMD_VERSION: self.hw_info["FW"] = bytes(data[4:]).split(b'\x00')[0].decode('ascii', errors='ignore').strip() or "Unknown"
            elif cmd == CMD_PEQ_VALUES and len(data) >= 37:
                idx, f, q, g = data[5], data[28]|(data[29]<<8), round((data[30]|(data[31]<<8))/256.0, 2), data[32]|(data[33]<<8)
                if g > 32767: g -= 65536
                self.parsed_filters[idx] = {"freq": f, "q": q, "gain": round(g/256.0, 1), "type": INV_TYPE_CODES.get(data[34], "PK")}
                self.active_slot = data[36]
            elif cmd == CMD_GLOBAL_GAIN: 
                raw_vol = struct.unpack("<h", bytes(data[4:6]))[0]
                self.read_results[cmd] = struct.unpack("b", bytes([data[6]]))[0]
                
                if self.is_syncing:
                    self.last_raw_vol = raw_vol
                elif raw_vol != getattr(self, 'last_raw_vol', 0) and not getattr(self, '_updating_ui', False):
                    self.comm.hw_vol_changed.emit(raw_vol)
            elif cmd == 0x16: # <--- New Balance Logic
                sf, mag = data[4], data[6]
                if sf == 0x01: 
                    self.read_results["bal_l"] = (mag - 256) if mag > 0 else 0
                else: 
                    self.read_results["bal_r"] = (256 - mag) if mag > 0 else 0
            # Parse Mic Gain response: 4b 80 02 02
            elif cmd == 0x02 and data[3] == 0x02:
                self.read_results["mic_gain"] = struct.unpack('b', bytes([data[5]]))[0]
            else: self.read_results[cmd] = data[4]

    def _connect_logic(self):
        self.comm.sync_finished.connect(self.update_ui_state)
        self.comm.hw_vol_changed.connect(self._sync_hw_volume_ui)
        self.comm.status_msg.connect(lambda m: [
            InfoBar.error("Status", m, duration=2000, parent=self),
            self.toggle_controls(False),
            self.lbl_sn.setText("Serial: Disconnected"),
            self.lbl_man.setText("Manufacturer: N/A"),
            self.lbl_prod.setText("Product: N/A"),
            self.lbl_fw.setText("Firmware: N/A")
        ])

    def update_ui_state(self, info, results, filters):
        self.is_syncing = True
        self.lbl_man.setText("Manufacturer: TRN"); self.lbl_prod.setText("Product: Black Pearl(TE-C)")
        self.actual_sn = info['SN']
        self.lbl_sn.setText(f"Serial: {'********' if self.sn_hidden else self.actual_sn}")
        self.lbl_fw.setText(f"Firmware: {self.hw_info['FW']}"); self.toggle_controls(True)
        
        # Consolidation Logic for Balance
        # Better Consolidation: Choose the strongest offset and ignore hardware noise (+/- 1)
        bal_l = results.get("bal_l", 0)
        bal_r = results.get("bal_r", 0)
        bal = bal_l if abs(bal_l) > abs(bal_r) else bal_r
        
        # Snap-to-Zero: If hardware reports a tiny offset of 1 (firmware bug), treat it as 0
        if abs(bal) <= 1: bal = 0
        
        self._updating_ui = True
        self.bal_slider.setValue(bal)
        self.bal_txt.setText(f"L {abs(bal)}" if bal < 0 else f"R {bal}" if bal > 0 else "Center")
        self._updating_ui = False

        self._check_headroom(auto_level=False)
        mapping = {0x11: (self.cb_filter, FILTER_MAP), 0x19: (self.cb_gain, GAIN_MAP), 0x1D: (self.cb_amp, AMP_MAP)}
        for cmd, (w, m) in mapping.items():
            if cmd in results: w.setCurrentText(m.get(results[cmd], "Unknown"))
        
        self.desc_filter.setText(self.filter_desc_map.get(self.cb_filter.currentText(), ""))

        p = self.settings_data["presets"][self.preset_cb.currentIndex()]
        if CMD_GLOBAL_GAIN in results:
            g_val = results[CMD_GLOBAL_GAIN]
            p = self.settings_data["presets"][self.preset_cb.currentIndex()]
            p["preamp"] = float(g_val)
            self._updating_ui = False
           
                
        self._sync_all_uis()
        self.is_syncing = False; self.refresh_status_lbl_dac.setText("");

        if "mic_gain" in results:
                    self._updating_ui = True
                    val = results["mic_gain"]
                    self.mic_slider.setValue(val)
                    self.mic_txt.setText(f"{val:+d} dB")
                    self._updating_ui = False
        
        if not hasattr(self, '_boot_sync_complete'):
            self._boot_sync_complete = True
            # The hardware is now ready. Force the PC's saved preset onto the DAC.
            self._load_preset_ui()

    def _apply_filter(self, idx):
        if self.is_syncing: return
        self.dirty_usb_tasks.add(idx)
        self.auto_flash_timer.start() 
        if not self.usb_timer.isActive():
            self.usb_timer.start()

    def _process_usb_queue(self):
        """Processes EQ and Volume changes with corrected packet lengths"""
        if not self.dirty_usb_tasks or self.is_syncing:
            self.usb_timer.stop()
            return
            
        with self.usb_lock: 
            dev = self.get_device()
            if not dev: 
                self.dirty_usb_tasks.clear()
                return
                
            try:
                report = dev.find_output_reports()[0]
                tasks = list(self.dirty_usb_tasks)
                self.dirty_usb_tasks.clear()
                p = self.settings_data["presets"][self.preset_cb.currentIndex()]
                
                for idx in tasks:
                    if idx >= 0:
                        # Standard EQ Filter Logic
                        f_data = p["filters"][idx]
                        g = 0.0 if not f_data.get("enabled", True) else float(f_data["gain"])
                        f, q, t = max(1, int(f_data["freq"])), max(0.01, float(f_data["q"])), f_data["type"]
                        A, w0 = 10**(g/40), 2*math.pi*f/48000; sn, cs = math.sin(w0), math.cos(w0); alpha = sn/(2*q)
                        if t == "PK": b0, b1, b2, a0, a1, a2 = 1+alpha*A, -2*cs, 1-alpha*A, 1+alpha/A, -2*cs, 1-alpha/A
                        elif t in ["LS", "HS"]:
                            sqA, s = math.sqrt(A), 1 if t == "HS" else -1
                            b0, b1, b2 = A*((A+1)+s*(A-1)*cs+2*sqA*alpha), -s*2*A*((A-1)+s*(A+1)*cs), A*((A+1)+s*(A-1)*cs-2*sqA*alpha)
                            a0, a1, a2 = (A+1)-s*(A-1)*cs+2*sqA*alpha, s*2*((A-1)-s*(A+1)*cs), (A+1)-s*(A-1)*cs-2*sqA*alpha
                        else: b0,b1,b2,a0,a1,a2 = 1,0,0,1,0,0
                        hw_biquads = b"".join(struct.pack("<f", c/a0) for c in [b0, b1, b2, a1, a2])
                        pkt = [WRITE, CMD_PEQ_VALUES, 0x18, 0x00, idx, 0x00, 0x00] + list(hw_biquads)
                        pkt += list(struct.pack("<H", f)) + list(struct.pack("<H", int(q*256))) + list(struct.pack("<h", int(g*256))) + [TYPE_CODES.get(t, 0x02), 0x00, getattr(self, 'active_slot', 0x00), END]
                        report.send([REPORT_ID] + pkt + ([0x00] * (63 - len(pkt))))
                    else:
                        # RESTORED: 3-byte volume payload (LSB, MSB, 0x00)
                        # Length byte set to 0x03. This is the standard for live volume updates.
                        v_bytes = struct.pack("<h", int(self.last_raw_vol))
                        report.send([REPORT_ID, WRITE, CMD_GLOBAL_GAIN, 0x03, v_bytes[0], v_bytes[1], 0x00] + ([0x00] * 57))
                        
                # AGGRESSIVE LATCH: Use 0xFF bitmask to force the DAC to apply all buffer changes
                # This ensures the volume and EQ are both pushed to the hardware output stage instantly.
                report.send([REPORT_ID, WRITE, CMD_TEMP_WRITE, 0x04, 0xFF, 0xFF, 0xFF, 0xFF, END] + [0x00]*55)
                
                self.save_settings()
            except: pass

    def _commit_to_flash(self):
        """Writes current latched state to permanent memory in the background"""
        if self.is_syncing: return

        def run_save():
            with self.usb_lock:
                dev = self.get_device()
                if not dev: return
                try:
                    report = dev.find_output_reports()[0]
                    # CMD_FLASH_EQ (0x01) saves the Volume + EQ buffer permanently
                    report.send([REPORT_ID, WRITE, CMD_FLASH_EQ, 0x01, END] + [0x00]*59)
                    time.sleep(0.2) # Give the hardware a moment to process the physical write
                except: pass

        # Fire and forget in a background thread to keep the UI at 60fps
        Thread(target=run_save, daemon=True).start()

    def _on_balance_change(self):
        v = self.bal_slider.value()
        self.bal_txt.setText(f"L {abs(v)}" if v<0 else f"R {v}" if v>0 else "Center")
        if self.is_syncing: return
        
        with self.usb_lock:
            dev = self.get_device()
            if dev:
                try:
                    report = dev.find_output_reports()[0]
                    
                    # 1. Calculate Magnitudes (0x00 clears the channel)
                    mag_l = 256 + v if v < 0 else 0x00
                    mag_r = 256 - v if v > 0 else 0x00
                    
                    # 2. Write Left Channel (Side Flag 0x01)
                    report.send([REPORT_ID, 0x01, 0x16, 0x04, 0x01, 0x00, mag_l] + [0x00]*57)
                    time.sleep(0.01) # Small delay so the DAC chips don't bottleneck
                    
                    # 3. Write Right Channel (Side Flag 0x00)
                    report.send([REPORT_ID, 0x01, 0x16, 0x04, 0x00, 0x00, mag_r] + [0x00]*57)
                    
                    self.save_settings()
                except: pass
            
    def _on_mic_gain_change(self, v):
        self.mic_txt.setText(f"{v:+d} dB")
        if self.is_syncing: return
        self.auto_flash_timer.start() # Changed from auto_save_timer
        with self.usb_lock:
            dev = self.get_device()
            if dev:
                try:
                    pkt = [REPORT_ID, WRITE, CMD_MIC_GAIN_ADDR, 0x02, 0x80, v & 0xFF] + ([0x00] * 58)
                    dev.find_output_reports()[0].send(pkt)
                except: pass

    def _reset_eq(self):
        self._updating_ui = True
        p = self.settings_data["presets"][self.preset_cb.currentIndex()]
        p["preamp"] = 0.0
        
        # Apply preamp
        self._apply_filter(-1)
        
        # Reset all filters to flat
        for idx, f in enumerate(p["filters"]):
            f.update({"gain": 0.0, "enabled": True, "q": 1.0, "type": "PK"})
            self._apply_filter(idx)
        
        # Note: target_curve and measurement_curve are intentionally NOT cleared here.
        
        self._updating_ui = False
        self._sync_all_uis()


    def _new_preset(self):
        name, ok = QInputDialog.getText(self, "New Preset", "Name:")
        if ok and name:
            df = [{"type": "PK", "freq": DEFAULT_FREQS[i], "q": 1.0, "gain": 0.0, "enabled": True, "lock_freq": False, "lock_gain": False, "lock_q": False} for i in range(10)]
            self.settings_data["presets"].append({"name": name, "preamp": 0.0, "filters": df})
            self.preset_cb.addItem(name)
            self.preset_cb.setCurrentIndex(self.preset_cb.count() - 1)

    def _import_squig(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open AutoEQ File", "", "Text Files (*.txt);;All Files (*)")
        if not path: return
        try:
            p = self.settings_data["presets"][self.preset_cb.currentIndex()]
            with open(path, 'r') as f: lines = f.readlines()
            idx = 0
            for line in lines:
                if "Preamp:" in line:
                    m = re.search(r"Preamp:\s*([-+]?[\d.]+)", line)
                    if m: 
                        val = float(m.group(1))
                        p["preamp"] = max(-16.0, min(6.0, val))
                if "Filter" in line and idx < 10:
                    f_data = p["filters"][idx]
                    f_data["enabled"] = "ON" in line
                    f_data["type"] = "PK" if " PK " in line else "LS" if " LS " in line else "HS"
                    fc, gn, qv = re.search(r"Fc\s+([\d.]+)", line), re.search(r"Gain\s+([-+.\d]+)", line), re.search(r"Q\s+([\d.]+)", line)
                    # Frequency: Clamp between 20Hz and 20,000Hz
                    if fc: f_data["freq"] = max(20, min(20000, float(fc.group(1))))
                    
                    # Gain: Clamp between -10dB and +10dB
                    if gn: f_data["gain"] = max(-10.0, min(10.0, float(gn.group(1))))
                    
                    # Q Factor: Use the value from the file, with a minimum of 0.1
                    if qv: f_data["q"] = max(0.1, float(qv.group(1)))
                    idx += 1
            for i in range(10): self._apply_filter(i)
            self._sync_all_uis()
        except: pass
        
    def _export_autoeq(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export AutoEQ File", "Custom_AutoEQ.txt", "Text Files (*.txt);;All Files (*)")
        if not path: return
        
        try:
            p = self.settings_data["presets"][self.preset_cb.currentIndex()]
            lines = [f"Preamp: {p.get('preamp', 0.0):.1f} dB\n"]
            
            for i, f in enumerate(p["filters"]):
                state = "ON" if f.get("enabled", True) else "OFF"
                t = f.get("type", "PK")
                fc = f.get("freq", 1000)
                g = f.get("gain", 0.0)
                q = f.get("q", 1.0)
                lines.append(f"Filter {i+1}: {state} {t} Fc {fc:.1f} Hz Gain {g:.1f} dB Q {q:.2f}\n")
                
            with open(path, 'w') as file:
                file.writelines(lines)
                
            InfoBar.success("Success", f"Preset exported to {os.path.basename(path)}", parent=self)
        except Exception as e:
            InfoBar.error("Export Error", str(e), parent=self)

    def _load_preset_ui(self):
        """
        Triggered when the preset dropdown changes. 
        Updates the UI elements, then explicitly flags all bands 
        and global volume to be transmitted to the hardware.
        """
        self._sync_all_uis()
        if self.is_syncing:
            return          
        self._apply_filter(-1)
        for i in range(10):
            self._apply_filter(i)

    def write_val(self, cmd, selection, n_map):
        inv = {v: k for k, v in n_map.items()}
        self.auto_flash_timer.start() # Changed from auto_save_timer
        with self.usb_lock:
            dev = self.get_device()
            if dev:
                try:
                    dev.find_output_reports()[0].send([REPORT_ID, WRITE, cmd, 0x01, inv.get(selection)] + [0x00]*59)
                except: pass

    def save_settings(self):
        self.settings_data["last_preset"] = self.preset_cb.currentIndex()
        self.settings_data["balance"] = self.bal_slider.value()
        with open(SETTINGS_FILE, "w") as f: json.dump(self.settings_data, f)

if __name__ == "__main__":
    try:
        myappid = u'trn.controlpanel.v1'
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(resource_path("icon.ico")))
    window = FluentDACController()
    window.show()
    sys.exit(app.exec())
