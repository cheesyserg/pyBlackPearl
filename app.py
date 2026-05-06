import sys
import os
import json
import struct
import math
import re
import time
import ctypes
from threading import Thread
from PySide6.QtGui import QIcon, QPainter, QColor, QPen, QBrush, QPainterPath

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
        
        # Interactive Legend States
        self.visibility = {"eq": True, "eq_meas": True, "raw": True, "target": True}
        self.legend_rects = {}
        
        self.setAttribute(Qt.WA_TranslucentBackground)

    def update_data(self, filters, active_idx, preamp=0.0):
        self.filters = filters
        self.active_idx = active_idx
        self.preamp = preamp
        self.update()

    def set_target_curve(self, data):
        self.target_curve = data
        self.update()

    def set_measurement_curve(self, data):
        self.measurement_curve = data
        self.update()

    def _x_to_f(self, x):
        return 20.0 * (1000.0 ** (x / max(1, self.width())))

    def _f_to_x(self, f):
        return self.width() * math.log10(max(1, f) / 20.0) / 3.0

    def _db_to_y(self, db):
        return (self.height() / 2.0) - (db / self.max_db_scale) * (self.height() / 2.0)

    def _y_to_db(self, y):
        return ((self.height() / 2.0) - y) / (self.height() / 2.0) * self.max_db_scale

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        
        active_filters = []
        has_active_filters = False
        if self.filters:
            for f in self.filters:
                if f.get("enabled", True):
                    has_active_filters = True
                    active_filters.append(_calc_coeffs(f["type"], f["freq"], f["q"], f["gain"]))
                    
        def get_eq_db(freq):
            total = self.preamp
            for coeffs in active_filters:
                total += biquad_response(coeffs, freq)
            return total

        # Compute max bounds
        max_val = 18.0
        if self.target_curve and self.visibility["target"]:
            for _, db in self.target_curve: max_val = max(max_val, abs(db))
                
        curve_points = []
        if self.filters:
            step = 3
            for x in range(0, w + step, step):
                freq = self._x_to_f(x)
                db_total = get_eq_db(freq)
                curve_points.append((x, db_total))
                if self.visibility["eq"]: max_val = max(max_val, abs(db_total))
                
            for f in self.filters:
                if f.get("enabled", True) and self.visibility["eq"]:
                    max_val = max(max_val, abs(f["gain"] + self.preamp))

        eq_meas_points = []
        if self.measurement_curve:
            for f, db in self.measurement_curve:
                if f < 20 or f > 20000: continue
                eq_db = db + get_eq_db(f)
                eq_meas_points.append((f, eq_db))
                if self.visibility["raw"]: max_val = max(max_val, abs(db))
                if self.visibility["eq_meas"]: max_val = max(max_val, abs(eq_db))

        self.max_db_scale = max(18.0, math.ceil(max_val / 6.0) * 6.0)

        # Draw Grid
        painter.setFont(self.font())
        metrics = painter.fontMetrics()
        
        gain_steps = list(range(int(self.max_db_scale), int(-self.max_db_scale)-1, -6))
        for db in gain_steps:
            y = int(self._db_to_y(db))
            if db == 0:
                painter.setPen(QPen(QColor(255, 255, 255, 80), 1))
            else:
                painter.setPen(QPen(QColor(255, 255, 255, 15), 1))
            painter.drawLine(0, y, w, y)
            
            if db != 0:
                painter.setPen(QPen(QColor(255, 255, 255, 100)))
                text = f"{db} dB"
                text_y = y - 4 if db < 0 else y + 12
                painter.drawText(5, text_y, text)
                
        freq_lines = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]
        freq_labels = {20: "20", 50: "50", 100: "100", 200: "200", 500: "500", 1000: "1k", 2000: "2k", 5000: "5k", 10000: "10k", 20000: "20k"}
        for f in freq_lines:
            x = int(self._f_to_x(f))
            painter.setPen(QPen(QColor(255, 255, 255, 15), 1))
            painter.drawLine(x, 0, x, h)
            if f in freq_labels:
                painter.setPen(QPen(QColor(255, 255, 255, 100)))
                text = freq_labels[f]
                tw = metrics.horizontalAdvance(text)
                text_x = max(5, min(w - tw - 5, x - tw//2))
                painter.drawText(text_x, h - 5, text)

        # Target Curve
        if self.target_curve and self.visibility["target"]:
            path = QPainterPath()
            painter.setPen(QPen(QColor(255, 255, 255, 80), 2, Qt.DashLine))
            first = True
            for f, db in self.target_curve:
                if f < 20 or f > 20000: continue
                x = self._f_to_x(f)
                y = self._db_to_y(db)
                if first: path.moveTo(x, y); first = False
                else: path.lineTo(x, y)
            if not first: painter.drawPath(path)
            
        # Raw Measurement
        if self.measurement_curve and self.visibility["raw"]:
            path = QPainterPath()
            painter.setPen(QPen(QColor(255, 136, 0, 100), 2))
            first = True
            for f, db in self.measurement_curve:
                if f < 20 or f > 20000: continue
                x = self._f_to_x(f)
                y = self._db_to_y(db)
                if first: path.moveTo(x, y); first = False
                else: path.lineTo(x, y)
            if not first: painter.drawPath(path)

        # EQ'd Compensated Measurement
        if eq_meas_points and self.visibility["eq_meas"]:
            path = QPainterPath()
            painter.setPen(QPen(QColor(0, 208, 132, 200), 2))
            first = True
            for f, db in eq_meas_points:
                x = self._f_to_x(f)
                y = self._db_to_y(db)
                if first: path.moveTo(x, y); first = False
                else: path.lineTo(x, y)
            if not first: painter.drawPath(path)
        
        # EQ Curve & Control Points
        if self.filters and self.visibility["eq"]:
            path = QPainterPath()
            for i, (x, db) in enumerate(curve_points):
                y = self._db_to_y(db)
                if i == 0: path.moveTo(x, y)
                else: path.lineTo(x, y)
            painter.setPen(QPen(QColor("#0078D4"), 2))
            painter.drawPath(path)
            
            for i, f in enumerate(self.filters):
                cx, cy = self._f_to_x(f["freq"]), self._db_to_y(f["gain"] + self.preamp)
                if i == self.active_idx:
                    painter.setPen(QPen(QColor("#FFFFFF"), 2))
                    painter.setBrush(QBrush(QColor("#0078D4")))
                    painter.drawEllipse(int(cx)-6, int(cy)-6, 12, 12)
                else:
                    painter.setPen(QPen(QColor("#888888"), 1))
                    painter.setBrush(QBrush(QColor("#444444")))
                    painter.drawEllipse(int(cx)-4, int(cy)-4, 8, 8)

        # Draw Interactive Legend (Nudged to the right)
        legend_y = h - 25
        legend_x = 55 
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

        if has_active_filters or self.preamp != 0:
            draw_legend("eq", "EQ Curve", QColor("#0078D4"))
        if self.measurement_curve and (has_active_filters or self.preamp != 0):
            draw_legend("eq_meas", "EQ'd Measurement", QColor(0, 208, 132, 200))
        if self.measurement_curve:
            draw_legend("raw", "Raw Measurement", QColor(255, 136, 0, 100))
        if self.target_curve:
            draw_legend("target", "Target Curve", QColor(255, 255, 255, 80), is_dashed=True)

    def mousePressEvent(self, event):
        x, y = event.position().x(), event.position().y()
        
        # Check if Legend was clicked
        for key, (rx, ry, rw, rh) in self.legend_rects.items():
            if rx <= x <= rx + rw and ry <= y <= ry + rh:
                self.visibility[key] = not self.visibility.get(key, True)
                self.update()
                return

        # If EQ curve is hidden, don't allow interacting with points
        if not self.visibility.get("eq", True):
            return

        min_dist, best_idx = 400, -1
        for i, f in enumerate(self.filters):
            if not f.get("enabled", True): continue
            cx, cy = self._f_to_x(f["freq"]), self._db_to_y(f["gain"] + self.preamp)
            dist = (cx-x)**2 + (cy-y)**2
            if dist < min_dist:
                min_dist, best_idx = dist, i
                
        if best_idx != -1 and min_dist <= 100:
            self.active_idx = best_idx
            self.dragging_idx = best_idx
            self.point_clicked.emit(best_idx)
            self.update()

    def mouseMoveEvent(self, event):
        if self.dragging_idx != -1:
            x, y = event.position().x(), event.position().y()
            f = self.filters[self.dragging_idx]
            new_f, new_g = f["freq"], f["gain"]
            
            if not f.get("lock_freq", False):
                new_f = max(20, min(20000, self._x_to_f(x)))
            if not f.get("lock_gain", False):
                new_g = max(-18.0, min(18.0, self._y_to_db(y) - self.preamp))
                
            self.point_moved.emit(self.dragging_idx, new_f, new_g)

    def mouseReleaseEvent(self, event):
        self.dragging_idx = -1

    def wheelEvent(self, event):
        if self.active_idx != -1 and self.visibility.get("eq", True):
            f = self.filters[self.active_idx]
            if not f.get("lock_q", False):
                # Force Q to stay at 0.1
                new_q = 0.1 
                self.q_changed.emit(self.active_idx, new_q)

# --- Communicator ---
class Communicator(QObject):
    sync_finished = Signal(object, object, object)
    status_msg = Signal(str)

# --- Main App ---
class FluentDACController(FluentWindow):
    def __init__(self):
        setTheme(Theme.DARK)
        super().__init__()
        self.setWindowTitle("TRN Control Panel")
        self.setWindowIcon(QIcon(resource_path("icon.ico")))
        self.resize(1180, 850)

        self.read_results, self.parsed_filters = {}, {}
        self.hw_info = {"Man": "Loading...", "Prod": "Loading...", "SN": "Loading...", "FW": "Loading..."}
        self.is_syncing, self.active_device = False, None
        
        self.active_band_idx = 0
        self._updating_ui = False
        self.list_widgets = []

        self.comm = Communicator()
        self.settings_data = self.load_settings()
        
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

        QTimer.singleShot(500, self.refresh)

    def load_settings(self):
        default_filters = [{"type": "PK", "freq": DEFAULT_FREQS[i], "q": 1.0, "gain": 0.0, "enabled": True, "lock_freq": False, "lock_gain": False, "lock_q": False} for i in range(10)]
        default_data = {"balance": 0, "last_preset": 0, "presets": [{"name": "Default", "preamp": 0.0, "filters": default_filters}]}
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r") as f: return {**default_data, **json.load(f)}
            except: pass
        return default_data

    def _setup_ui(self):
        self.addSubInterface(self.dac_interface, FluentIcon.SETTING, "DAC Settings")
        self.addSubInterface(self.eq_interface, FluentIcon.MUSIC, "Parametric EQ")

        # --- DAC Interface ---
        dac_layout = QVBoxLayout(self.dac_interface)
        dac_layout.setContentsMargins(40, 40, 40, 40)
        
       

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
        info_l.addWidget(self.lbl_man, 0, 0); info_l.addWidget(self.lbl_prod, 0, 1)
        info_l.addWidget(self.lbl_fw, 1, 0); info_l.addWidget(self.lbl_sn, 1, 1)
        dac_layout.addWidget(info_card)

         # --- Mic Gain Section ---
        dac_layout.addSpacing(10)
        dac_layout.addWidget(SubtitleLabel("Microphone Settings", self))
        
        mic_card = CardWidget(self)
        mic_l = QVBoxLayout(mic_card)
        mic_header = QHBoxLayout()
        mic_header.addWidget(StrongBodyLabel("Mic Gain", mic_card))
        self.mic_txt = CaptionLabel("0 dB", mic_card)
        mic_header.addStretch(1); mic_header.addWidget(self.mic_txt)
        mic_l.addLayout(mic_header)
        
        self.mic_slider = Slider(Qt.Horizontal, mic_card)
        self.mic_slider.setRange(-15, 15) # Range from your uploaded script
        self.mic_slider.setValue(0)
        self.mic_slider.valueChanged.connect(self._on_mic_gain_change)
        mic_l.addWidget(self.mic_slider)
        dac_layout.addWidget(mic_card)
        
        dac_layout.setSpacing(15)

        # 2. Channel Balance
        dac_layout.addSpacing(10)
        dac_layout.addWidget(SubtitleLabel("Channel Balance", self))
        
        bal_card = CardWidget(self)
        bal_l = QVBoxLayout(bal_card)
        bal_header = QHBoxLayout()
        bal_header.addWidget(StrongBodyLabel("Balance Control", bal_card))
        self.bal_txt = CaptionLabel("Center", bal_card)
        bal_header.addStretch(1)
        bal_header.addWidget(self.bal_txt)
        bal_l.addLayout(bal_header)
        self.bal_slider = Slider(Qt.Horizontal, bal_card)
        self.bal_slider.setRange(-15, 15)
        self.bal_slider.setValue(self.settings_data["balance"])
        self.bal_slider.valueChanged.connect(self._on_balance_change)
        bal_l.addWidget(self.bal_slider)
        dac_layout.addWidget(bal_card)

        # 3. DAC Settings
        dac_layout.addSpacing(10)
        dac_layout.addWidget(SubtitleLabel("DAC Settings", self))

        self.cb_filter, self.desc_filter = self._create_row(dac_layout, "Digital Filter", FILTER_MAP, 0x11, "Selects the internal DAC reconstruction filter algorithm.")
        self.cb_gain, self.desc_gain = self._create_row(dac_layout, "Gain Mode", GAIN_MAP, 0x19, "Adjusts the amplifier's base volume scaling for sensitive IEMs or demanding headphones.")
        self.cb_amp, self.desc_amp = self._create_row(dac_layout, "Amp Topology", AMP_MAP, 0x1D, "Switches between Class AB (Maximum audio performance) and Class H (Higher power efficiency).")
        
        self.cb_filter.currentTextChanged.connect(lambda t: self.desc_filter.setText(self.filter_desc_map.get(t, "Select a filter.")))
        
        # 4. Factory Reset
        dac_layout.addSpacing(10)
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

        # --- EQ Interface ---
        eq_layout = QVBoxLayout(self.eq_interface)
        header_frame = QFrame(self); header_l = QVBoxLayout(header_frame); header_l.setContentsMargins(40, 40, 40, 0)
        
        top_bar = QHBoxLayout()
        self.btn_toggle_view = PushButton("Switch to List View", header_frame)
        self.btn_toggle_view.clicked.connect(self._toggle_view)
        
        self.preset_cb = ComboBox(header_frame); self.preset_cb.addItems([p["name"] for p in self.settings_data["presets"]])
        self.preset_cb.setCurrentIndex(self.settings_data["last_preset"]); self.preset_cb.currentIndexChanged.connect(self._load_preset_ui)
        self.refresh_status_lbl_eq = CaptionLabel("", header_frame)
        self.eq_refresh_btn = TransparentToolButton(FluentIcon.SYNC, header_frame); self.eq_refresh_btn.clicked.connect(self.refresh)
        
        self.btn_add = TransparentToolButton(FluentIcon.ADD, header_frame); self.btn_add.clicked.connect(self._new_preset)
        self.btn_save_hw = PrimaryPushButton(FluentIcon.SAVE, "Save to Hardware", header_frame); self.btn_save_hw.clicked.connect(self._commit_to_flash)
        self.btn_reset = PushButton("Flat EQ", header_frame); self.btn_reset.clicked.connect(self._reset_eq)
        
        self.btn_import_target = PushButton("Import Target", header_frame); self.btn_import_target.clicked.connect(self._import_rew_target)
        self.btn_import_meas = PushButton("Import Measurement", header_frame); self.btn_import_meas.clicked.connect(self._import_measurement)
        self.btn_import = PushButton("Import AutoEQ", header_frame); self.btn_import.clicked.connect(self._import_squig)

        top_bar.addWidget(SubtitleLabel("Parametric EQ", header_frame)); top_bar.addStretch(1)
        top_bar.addWidget(self.btn_toggle_view)
        top_bar.addWidget(CaptionLabel("Preset", header_frame)); top_bar.addWidget(self.preset_cb)
        top_bar.addWidget(self.refresh_status_lbl_eq); top_bar.addWidget(self.eq_refresh_btn); top_bar.addWidget(self.btn_add)
        top_bar.addWidget(self.btn_save_hw); top_bar.addWidget(self.btn_reset); 
        top_bar.addWidget(self.btn_import_target); top_bar.addWidget(self.btn_import_meas); top_bar.addWidget(self.btn_import)
        header_l.addLayout(top_bar)
        
        pre_card = CardWidget(header_frame); pre_l = QHBoxLayout(pre_card); active_p = self.settings_data["presets"][self.settings_data["last_preset"]]
        pre_l.addWidget(StrongBodyLabel("Preamp", pre_card))
        
        self.pre_slider = Slider(Qt.Horizontal, pre_card); self.pre_slider.setRange(-16, 6); self.pre_slider.setValue(int(active_p["preamp"]))
        self.preamp_val = LineEdit(pre_card); self.preamp_val.setText(str(int(active_p["preamp"]))); self.preamp_val.setFixedWidth(65)
        
        self.pre_slider.valueChanged.connect(self._on_preamp_slider_changed)
        self.preamp_val.editingFinished.connect(self._on_preamp_val_changed)
        
        pre_l.addWidget(self.pre_slider, 1); pre_l.addWidget(self.preamp_val); pre_l.addWidget(CaptionLabel("dB", pre_card))
        header_l.addWidget(pre_card); eq_layout.addWidget(header_frame)

        # STACKED WIDGET FOR VIEWS
        self.stack = QStackedWidget(self)
        eq_layout.addWidget(self.stack, 1)

        # --- VIEW 0: Graph UI ---
        self.graph_page = QWidget()
        graph_layout = QVBoxLayout(self.graph_page); graph_layout.setContentsMargins(40, 10, 40, 30)
        
        self.graph = EQGraph(self.graph_page)
        self.graph.setMinimumHeight(350)
        self.graph.point_moved.connect(self._on_graph_point_moved)
        self.graph.q_changed.connect(self._on_graph_q_changed)
        self.graph.point_clicked.connect(self.set_active_band)
        graph_layout.addWidget(self.graph, 1)

        self.active_band_card = SimpleCardWidget(self.graph_page)
        self.active_band_card.setStyleSheet("SimpleCardWidget { background: transparent; border: 1px solid rgba(255, 255, 255, 0.08); }")
        bl = QHBoxLayout(self.active_band_card)
        self.band_lbl = StrongBodyLabel("Band 1", self.active_band_card); bl.addWidget(self.band_lbl)
        
        self.band_enable = CheckBox(self.active_band_card); self.band_enable.setFixedWidth(30)
        
        self.band_type = ComboBox(self.active_band_card); self.band_type.addItems(["PK", "LS", "HS"]); self.band_type.setFixedWidth(85)
        self.band_freq = LineEdit(self.active_band_card); self.band_freq.setFixedWidth(70)
        self.lock_freq = CheckBox("Lock", self.active_band_card)
        bl.addWidget(self.band_enable); bl.addWidget(self.band_type); bl.addWidget(self.band_freq); bl.addWidget(CaptionLabel("Hz", self.active_band_card)); bl.addWidget(self.lock_freq)
        self.band_gain_sld = Slider(Qt.Horizontal, self.active_band_card); self.band_gain_sld.setRange(-100, 100)
        self.band_gain_val = LineEdit(self.active_band_card); self.band_gain_val.setFixedWidth(55)
        self.lock_gain = CheckBox("Lock", self.active_band_card)
        bl.addSpacing(15); bl.addWidget(self.band_gain_sld, 1); bl.addWidget(self.band_gain_val); bl.addWidget(CaptionLabel("dB", self.active_band_card)); bl.addWidget(self.lock_gain)
        self.band_q_val = LineEdit(self.active_band_card); self.band_q_val.setFixedWidth(55)
        self.lock_q = CheckBox("Lock", self.active_band_card)
        bl.addSpacing(15); bl.addWidget(CaptionLabel("Q:", self.active_band_card)); bl.addWidget(self.band_q_val); bl.addWidget(self.lock_q)
        
        graph_layout.addWidget(self.active_band_card)
        self.stack.addWidget(self.graph_page)

        # --- VIEW 1: List UI ---
        self.list_page = QWidget()
        list_page_layout = QVBoxLayout(self.list_page); list_page_layout.setContentsMargins(40, 10, 40, 10)
        
        self.small_graph = EQGraph(self.list_page)
        self.small_graph.setFixedHeight(200)
        self.small_graph.point_moved.connect(self._on_graph_point_moved)
        self.small_graph.q_changed.connect(self._on_graph_q_changed)
        self.small_graph.point_clicked.connect(self.set_active_band)
        list_page_layout.addWidget(self.small_graph)

        scroll = SmoothScrollArea(self.list_page); scroll.setWidgetResizable(True); scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        scroll.viewport().setStyleSheet("background: transparent;")
        list_container = QWidget(); list_container.setStyleSheet("background: transparent;")
        self.bands_layout = QVBoxLayout(list_container); self.bands_layout.setContentsMargins(0, 10, 0, 30)

        for i in range(10):
            band_card = SimpleCardWidget(list_container)
            band_card.setStyleSheet("SimpleCardWidget { background: transparent; border: 1px solid rgba(255, 255, 255, 0.08); }")
            lbl_layout = QHBoxLayout(band_card)
            lbl_layout.addWidget(StrongBodyLabel(f"{i+1}", band_card))
            
            chk = CheckBox(band_card); chk.setFixedWidth(30)
            typ = ComboBox(band_card); typ.addItems(["PK", "LS", "HS"]); typ.setFixedWidth(85)
            freq = LineEdit(band_card); freq.setFixedWidth(70)
            
            lbl_layout.addWidget(chk); lbl_layout.addWidget(typ); lbl_layout.addWidget(freq); lbl_layout.addWidget(CaptionLabel("Hz", band_card))
            sld, gain = Slider(Qt.Horizontal, band_card), LineEdit(band_card)
            sld.setRange(-100, 100); gain.setFixedWidth(55)
            lbl_layout.addSpacing(15); lbl_layout.addWidget(sld, 1); lbl_layout.addWidget(gain); lbl_layout.addWidget(CaptionLabel("dB", band_card))
            qv = LineEdit(band_card); qv.setFixedWidth(55)
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

    def _sync_all_uis(self):
        self._updating_ui = True
        p = self.settings_data["presets"][self.preset_cb.currentIndex()]
        
        for idx in range(10):
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
                
        self.graph.update_data(p["filters"], self.active_band_idx, p.get("preamp", 0.0))
        self.small_graph.update_data(p["filters"], self.active_band_idx, p.get("preamp", 0.0))
        self._updating_ui = False

    def _on_visual_ui_changed(self, *_):
        if self._updating_ui: return
        idx = self.active_band_idx
        p = self.settings_data["presets"][self.preset_cb.currentIndex()]
        f = p["filters"][idx]
        
        f["enabled"] = self.band_enable.isChecked()
        f["type"] = self.band_type.currentText()
        
        # Freq: 20Hz - 20,000Hz
        try: f["freq"] = max(20, min(20000, int(self.band_freq.text())))
        except: pass
        
        # Gain: -10dB to +10dB
        f["gain"] = self.band_gain_sld.value() / 10.0
        self.band_gain_val.setText(str(f["gain"]))
        
        # Q Factor: Fixed at 0.1
        f["q"] = 0.1 
        self.band_q_val.setText("0.1")
        try: f["q"] = max(0.1, min(18.0, float(self.band_q_val.text())))
        except: pass
        
        f["lock_freq"] = self.lock_freq.isChecked()
        f["lock_gain"] = self.lock_gain.isChecked()
        f["lock_q"] = self.lock_q.isChecked()
        
        self._sync_all_uis()
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
        
        self._sync_all_uis()
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
        
        # Constraints for mouse movement
        f["freq"] = max(20, min(20000, freq)) 
        f["gain"] = max(-10.0, min(10.0, gain))
        
        self._sync_all_uis()
        self._apply_filter(idx)

    def _on_graph_q_changed(self, idx, q):
        p = self.settings_data["presets"][self.preset_cb.currentIndex()]
        f = p["filters"][idx]
        f["q"] = q
        self._sync_all_uis()
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
        objs = [self.bal_slider, self.cb_filter, self.cb_gain, self.cb_amp, self.preset_cb, 
                self.pre_slider, self.preamp_val, self.btn_save_hw, self.btn_reset, self.btn_add, 
                self.btn_import, self.btn_import_target, self.btn_import_meas,
                self.band_enable, self.band_type, self.band_freq, self.band_gain_sld, 
                self.band_gain_val, self.band_q_val, self.lock_freq, self.lock_gain, self.lock_q,
                self.btn_factory_reset]
        for o in objs: o.setEnabled(enabled)
        for lw in self.list_widgets:
            for k, w in lw.items(): w.setEnabled(enabled)

    def _create_row(self, layout, title, mapping, cmd, default_desc=""):
        card = SimpleCardWidget(self)
        main_layout = QHBoxLayout(card)
        main_layout.setContentsMargins(16, 16, 16, 16)
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
        if self.active_device and self.active_device.is_opened(): return self.active_device
        devs = hid.HidDeviceFilter(vendor_id=VID, product_id=PID).get_devices()
        if devs: self.active_device = devs[0]
        else: self.active_device = None
        return self.active_device

    def refresh(self):
        if self.is_syncing: return
        self.is_syncing = True; self.refresh_status_lbl_dac.setText("refreshing..."); self.refresh_status_lbl_eq.setText("refreshing...")
        def run():
            dev = self.get_device()
            if not dev: 
                self.comm.status_msg.emit("Disconnected"); self.is_syncing = False; return
            try:
                dev.open(); dev.set_raw_data_handler(self.on_data)
                # Added 0x02 for Mic Gain query
                for cmd in [CMD_VERSION, 0x11, 0x19, 0x1D, CMD_GLOBAL_GAIN, 0x02]:
                    if cmd == 0x02:
                        dev.find_output_reports()[0].send([REPORT_ID, READ, 0x02, 0x02] + [0x00]*60)
                    else:
                        dev.find_output_reports()[0].send([REPORT_ID, READ, cmd, END] + [0x00]*60)
                    time.sleep(0.05)
                for i in range(10):
                    dev.find_output_reports()[0].send([REPORT_ID, READ, CMD_PEQ_VALUES, 0x00, 0x00, i, END] + [0x00]*57); time.sleep(0.05)
                time.sleep(0.3); dev.close()
                self.comm.sync_finished.emit({"SN": dev.serial_number}, self.read_results, self.parsed_filters)
            except: self.is_syncing = False
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
            elif cmd == CMD_GLOBAL_GAIN: self.read_results[cmd] = struct.unpack("b", bytes([data[5]]))[0]
            # Parse Mic Gain response: 4b 80 02 02
            elif cmd == 0x02 and data[3] == 0x02:
                self.read_results["mic_gain"] = struct.unpack('b', bytes([data[5]]))[0]
            else: self.read_results[cmd] = data[4]

    def _connect_logic(self):
        self.comm.sync_finished.connect(self.update_ui_state)
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
        self.lbl_sn.setText(f"Serial: {info['SN']}"); self.lbl_fw.setText(f"Firmware: {self.hw_info['FW']}"); self.toggle_controls(True)
        mapping = {0x11: (self.cb_filter, FILTER_MAP), 0x19: (self.cb_gain, GAIN_MAP), 0x1D: (self.cb_amp, AMP_MAP)}
        for cmd, (w, m) in mapping.items():
            if cmd in results: w.setCurrentText(m.get(results[cmd], "Unknown"))
        
        self.desc_filter.setText(self.filter_desc_map.get(self.cb_filter.currentText(), ""))

        p = self.settings_data["presets"][self.preset_cb.currentIndex()]
        if CMD_GLOBAL_GAIN in results:
            g_val = results[CMD_GLOBAL_GAIN]
            p["preamp"] = float(g_val)
            self._updating_ui = True
            self.preamp_val.setText(str(int(g_val)))
            self.pre_slider.setValue(int(g_val))
            self._updating_ui = False
            
        for idx, f in filters.items():
            if idx < 10:
                p["filters"][idx].update({"freq": f["freq"], "q": f["q"], "gain": f["gain"], "type": f["type"]})
                
        self._sync_all_uis()
        self.is_syncing = False; self.refresh_status_lbl_dac.setText(""); self.refresh_status_lbl_eq.setText("")

        if "mic_gain" in results:
                    self._updating_ui = True
                    val = results["mic_gain"]
                    self.mic_slider.setValue(val)
                    self.mic_txt.setText(f"{val:+d} dB")
                    self._updating_ui = False

    def _apply_filter(self, idx):
        if self.is_syncing: return
        dev = self.get_device()
        if not dev: return
        try:
            dev.open(); report = dev.find_output_reports()[0]
            if idx >= 0:
                p = self.settings_data["presets"][self.preset_cb.currentIndex()]
                f_data = p["filters"][idx]
                g = 0.0 if not f_data.get("enabled", True) else float(f_data["gain"])
                f = max(1, int(f_data["freq"]))
                q = max(0.01, float(f_data["q"]))
                t = f_data["type"]
                
                A, w0 = 10**(g/40), 2*math.pi*f/48000; sn, cs = math.sin(w0), math.cos(w0); alpha = sn/(2*q)
                if t == "PK": b0, b1, b2, a0, a1, a2 = 1+alpha*A, -2*cs, 1-alpha*A, 1+alpha/A, -2*cs, 1-alpha/A
                elif t in ["LS", "HS"]:
                    sqA, s = math.sqrt(A), 1 if t == "HS" else -1
                    b0, b1, b2 = A*((A+1)+s*(A-1)*cs+2*sqA*alpha), -s*2*A*((A-1)+s*(A+1)*cs), A*((A+1)+s*(A-1)*cs-2*sqA*alpha)
                    a0, a1, a2 = (A+1)-s*(A-1)*cs+2*sqA*alpha, s*2*((A-1)-s*(A+1)*cs), (A+1)-s*(A-1)*cs-2*sqA*alpha
                else: b0,b1,b2,a0,a1,a2 = 1,0,0,1,0,0
                hw_biquads = b"".join(struct.pack("<f", c/a0) for c in [b0, b1, b2, a1, a2])

                pkt = [WRITE, CMD_PEQ_VALUES, 0x18, 0x00, idx, 0x00, 0x00] + list(hw_biquads)
                slot_id = getattr(self, 'active_slot', 0x00)
                pkt += list(struct.pack("<H", f)) + list(struct.pack("<H", int(q*256))) + list(struct.pack("<h", int(g*256))) + [TYPE_CODES.get(t, 0x02), 0x00, slot_id, END]
                report.send([REPORT_ID] + pkt + ([0x00] * (63 - len(pkt))))
            else:
                p_val = int(float(self.preamp_val.text().replace(',', '.') or 0)) & 0xFF
                report.send([REPORT_ID, WRITE, CMD_GLOBAL_GAIN, 0x02, 0x00, p_val] + [0x00]*58)
            report.send([REPORT_ID, WRITE, CMD_TEMP_WRITE, 0x04, 0x00, 0x00, 0xFF, 0xFF, END] + [0x00]*55)
            dev.close(); self.save_settings()
        except: pass

    def _commit_to_flash(self):
        dev = self.get_device()
        if not dev: return
        try:
            dev.open(); report = dev.find_output_reports()[0]
            report.send([REPORT_ID, WRITE, CMD_FLASH_EQ, 0x01, END] + [0x00]*59); dev.close()
            InfoBar.success("Success", "EQ saved to permanent memory", duration=2000, parent=self)
        except: InfoBar.error("Error", "Flash write failed", parent=self)

    def _on_balance_change(self):
        v = self.bal_slider.value(); self.bal_txt.setText(f"L {abs(v)}" if v<0 else f"R {v}" if v>0 else "Center")
        dev = self.get_device()
        if dev and not self.is_syncing:
            try:
                dev.open(); sf, mag = (0x01, 256+v) if v<0 else (0x00, 256-v) if v>0 else (0x00, 0x00)
                dev.find_output_reports()[0].send([REPORT_ID, 0x01, 0x16, 0x04, sf, 0x00, mag] + [0x00]*57); dev.close(); self.save_settings()
            except: pass
            
    def _on_mic_gain_change(self, v):
        self.mic_txt.setText(f"{v:+d} dB")
        if self.is_syncing: return
        
        dev = self.get_device()
        if dev:
            try:
                dev.open()
                # Command from setMicGain.py: [ID, Write, Addr1, Addr2, Constant, Val]
                pkt = [REPORT_ID, WRITE, CMD_MIC_GAIN_ADDR, 0x02, 0x80, v & 0xFF] + ([0x00] * 58)
                dev.find_output_reports()[0].send(pkt)
                dev.close()
            except Exception:
                pass

    def _reset_eq(self):
        self._updating_ui = True
        self.preamp_val.setText("0")
        self.pre_slider.setValue(0)
        self._updating_ui = False
        
        p = self.settings_data["presets"][self.preset_cb.currentIndex()]
        p["preamp"] = 0.0
        
        # Apply preamp first
        self._apply_filter(-1)
        
        # CRITICAL: Wait 100ms for the DAC to finish the Global Gain write before flooding filters
        time.sleep(0.1) 
        
        for idx, f in enumerate(p["filters"]):
            f.update({"gain": 0.0, "enabled": True, "q": 1.0, "type": "PK"})
            self._apply_filter(idx)
            # Optional: Add a tiny sleep here if filters still fail to apply
            time.sleep(0.02) 
        
        # Clear graph curves and sync UI
        self.graph.set_target_curve(None)
        self.small_graph.set_target_curve(None)
        self.graph.set_measurement_curve(None)
        self.small_graph.set_measurement_curve(None)
        self._sync_all_uis()

    def _new_preset(self):
        name, ok = QInputDialog.getText(self, "New Preset", "Name:")
        if ok and name:
            df = [{"type": "PK", "freq": DEFAULT_FREQS[i], "q": 1.0, "gain": 0.0, "enabled": True, "lock_freq": False, "lock_gain": False, "lock_q": False} for i in range(10)]
            self.settings_data["presets"].append({"name": name, "preamp": 0.0, "filters": df})
            self.preset_cb.addItem(name); self.preset_cb.setCurrentIndex(self.preset_cb.count() - 1)

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
                        # Clamp preamp between -16 and +6
                        val = float(m.group(1))
                        p["preamp"] = max(-16.0, min(6.0, val))
                        self._updating_ui = True
                        self.preamp_val.setText(str(int(p["preamp"])))
                        self.pre_slider.setValue(int(p["preamp"]))
                        self._updating_ui = False
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

    def _load_preset_ui(self):
        p = self.settings_data["presets"][self.preset_cb.currentIndex()]
        self._updating_ui = True
        self.preamp_val.setText(str(int(p["preamp"]))); self.pre_slider.setValue(int(p["preamp"]))
        self._updating_ui = False
        self._sync_all_uis()

    def write_val(self, cmd, selection, n_map):
        dev = self.get_device(); inv = {v: k for k, v in n_map.items()}
        if dev:
            try:
                dev.open(); dev.find_output_reports()[0].send([REPORT_ID, WRITE, cmd, 0x01, inv.get(selection)] + [0x00]*59); dev.close()
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
