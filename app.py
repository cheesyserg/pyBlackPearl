import sys
import os
import json
import struct
import math
import re
import time
import ctypes
from threading import Thread
from PySide6.QtGui import QIcon

import pywinusb.hid as hid
from PySide6.QtCore import Qt, Signal, QObject, QTimer
from PySide6.QtWidgets import QApplication, QFrame, QVBoxLayout, QHBoxLayout, QGridLayout, QWidget, QFileDialog, QInputDialog

from qfluentwidgets import (
    FluentWindow, SubtitleLabel, CaptionLabel, PushButton, PrimaryPushButton, 
    ComboBox, Slider, LineEdit, CheckBox, CardWidget, InfoBar, 
    StrongBodyLabel, setTheme, Theme, FluentIcon, BodyLabel, 
    TransparentToolButton, SmoothScrollArea, SimpleCardWidget
)

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
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
TYPE_CODES = {"PK": 0x02, "LS": 0x03, "HS": 0x04}
INV_TYPE_CODES = {v: k for k, v in TYPE_CODES.items()}

FILTER_MAP = {0x01: "FAST-LL", 0x02: "Fast-PC (BEST)", 0x03: "Slow-LL", 0x04: "SLOW-PC", 0x05: "NOS"}
GAIN_MAP = {0x00: "LOW", 0x01: "HIGH"}
AMP_MAP = {0x00: "CLASS H", 0x01: "CLASS AB"}
DEFAULT_FREQS = [31, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 20000]

class Communicator(QObject):
    sync_finished = Signal(object, object, object)
    status_msg = Signal(str)

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
        self.filter_widgets = []
        self.comm = Communicator()
        self.settings_data = self.load_settings()
        
        self.dac_interface = QFrame(self)
        self.eq_interface = QFrame(self)
        self.dac_interface.setObjectName("dac_interface")
        self.eq_interface.setObjectName("eq_interface")
        
        self._setup_ui()
        self._connect_logic()
        
        self.conn_timer = QTimer(self)
        self.conn_timer.timeout.connect(self._check_connection)
        self.conn_timer.start(2000)

        QTimer.singleShot(500, self.refresh)

    def load_settings(self):
        default_filters = [{"type": "PK", "freq": DEFAULT_FREQS[i], "q": 1.0, "gain": 0.0, "enabled": True} for i in range(10)]
        default_data = {"balance": 0, "last_preset": 0, "presets": [{"name": "Default", "preamp": 0.0, "filters": default_filters}]}
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r") as f: return {**default_data, **json.load(f)}
            except: pass
        return default_data

    def _setup_ui(self):
        self.addSubInterface(self.dac_interface, FluentIcon.SETTING, "DAC Settings")
        self.addSubInterface(self.eq_interface, FluentIcon.MUSIC, "Parametric EQ")

        dac_layout = QVBoxLayout(self.dac_interface); dac_layout.setContentsMargins(40, 40, 40, 40)
        hw_header = QHBoxLayout(); hw_header.addWidget(SubtitleLabel("Hardware Configuration", self))
        self.refresh_status_lbl_dac = CaptionLabel("", self)
        self.dac_refresh_btn = TransparentToolButton(FluentIcon.SYNC, self); self.dac_refresh_btn.clicked.connect(self.refresh)
        hw_header.addStretch(1); hw_header.addWidget(self.refresh_status_lbl_dac); hw_header.addWidget(self.dac_refresh_btn)
        dac_layout.addLayout(hw_header)
        
        info_card = CardWidget(self); info_l = QGridLayout(info_card)
        self.lbl_man = BodyLabel("Manufacturer: Loading...", info_card); self.lbl_prod = BodyLabel("Product: Loading...", info_card)
        self.lbl_fw = BodyLabel(f"Firmware: Loading...", info_card); self.lbl_sn = BodyLabel(f"Serial: Loading...", info_card)
        info_l.addWidget(self.lbl_man, 0, 0); info_l.addWidget(self.lbl_prod, 0, 1); info_l.addWidget(self.lbl_fw, 1, 0); info_l.addWidget(self.lbl_sn, 1, 1); dac_layout.addWidget(info_card)

        bal_card = CardWidget(self); bal_l = QVBoxLayout(bal_card); bal_header = QHBoxLayout()
        bal_header.addWidget(StrongBodyLabel("Channel Balance", bal_card))
        self.bal_txt = CaptionLabel("Center", bal_card); bal_header.addStretch(1); bal_header.addWidget(self.bal_txt); bal_l.addLayout(bal_header)
        self.bal_slider = Slider(Qt.Horizontal, bal_card); self.bal_slider.setRange(-15, 15); self.bal_slider.setValue(self.settings_data["balance"])
        self.bal_slider.valueChanged.connect(self._on_balance_change); bal_l.addWidget(self.bal_slider); dac_layout.addWidget(bal_card)

        self.cb_filter = self._create_row(dac_layout, "Digital Filter", FILTER_MAP, 0x11)
        self.cb_gain = self._create_row(dac_layout, "Gain Mode", GAIN_MAP, 0x19)
        self.cb_amp = self._create_row(dac_layout, "Amp Topology", AMP_MAP, 0x1D); dac_layout.addStretch(1)

        eq_layout = QVBoxLayout(self.eq_interface)
        header_frame = QFrame(self); header_l = QVBoxLayout(header_frame); header_l.setContentsMargins(40, 40, 40, 0)
        
        top_bar = QHBoxLayout()
        self.preset_cb = ComboBox(header_frame); self.preset_cb.addItems([p["name"] for p in self.settings_data["presets"]])
        self.preset_cb.setCurrentIndex(self.settings_data["last_preset"]); self.preset_cb.currentIndexChanged.connect(self._load_preset_ui)
        self.refresh_status_lbl_eq = CaptionLabel("", header_frame)
        self.eq_refresh_btn = TransparentToolButton(FluentIcon.SYNC, header_frame); self.eq_refresh_btn.clicked.connect(self.refresh)
        
        self.btn_add = TransparentToolButton(FluentIcon.ADD, header_frame); self.btn_add.clicked.connect(self._new_preset)
        self.btn_save_hw = PrimaryPushButton(FluentIcon.SAVE, "Save to Hardware", header_frame); self.btn_save_hw.clicked.connect(self._commit_to_flash)
        self.btn_reset = PushButton("Flat EQ", header_frame); self.btn_reset.clicked.connect(self._reset_eq)
        self.btn_import = PushButton("Import AutoEQ", header_frame); self.btn_import.clicked.connect(self._import_squig)

        top_bar.addWidget(SubtitleLabel("Parametric EQ", header_frame)); top_bar.addStretch(1)
        top_bar.addWidget(CaptionLabel("Preset", header_frame)); top_bar.addWidget(self.preset_cb)
        top_bar.addWidget(self.refresh_status_lbl_eq); top_bar.addWidget(self.eq_refresh_btn); top_bar.addWidget(self.btn_add)
        top_bar.addWidget(self.btn_save_hw); top_bar.addWidget(self.btn_reset); top_bar.addWidget(self.btn_import)
        header_l.addLayout(top_bar)
        
        pre_card = CardWidget(header_frame); pre_l = QHBoxLayout(pre_card); active_p = self.settings_data["presets"][self.settings_data["last_preset"]]
        pre_l.addWidget(StrongBodyLabel("Preamp", pre_card))
        self.pre_slider = Slider(Qt.Horizontal, pre_card); self.pre_slider.setRange(-120, 120); self.pre_slider.setValue(int(active_p["preamp"] * 10))
        self.preamp_val = LineEdit(pre_card); self.preamp_val.setText(str(active_p["preamp"])); self.preamp_val.setFixedWidth(65)
        self.pre_slider.valueChanged.connect(lambda v: [self.preamp_val.setText(str(v/10)), self._apply_filter(-1)])
        self.preamp_val.editingFinished.connect(lambda: self.pre_slider.setValue(int(float(self.preamp_val.text().replace(',', '.') or 0) * 10)))
        pre_l.addWidget(self.pre_slider, 1); pre_l.addWidget(self.preamp_val); pre_l.addWidget(CaptionLabel("dB", pre_card))
        header_l.addWidget(pre_card); eq_layout.addWidget(header_frame)

        scroll = SmoothScrollArea(self); scroll.setWidgetResizable(True); scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        scroll.viewport().setStyleSheet("background: transparent;")
        
        container = QWidget(); container.setStyleSheet("background: transparent;")
        self.bands_layout = QVBoxLayout(container); self.bands_layout.setContentsMargins(40, 10, 40, 30)

        for i in range(10):
            f_cfg = active_p["filters"][i]
            band_card = SimpleCardWidget(container)
            band_card.setStyleSheet("SimpleCardWidget { background: transparent; border: 1px solid rgba(255, 255, 255, 0.08); }")
            bl = QHBoxLayout(band_card)
            bl.addWidget(StrongBodyLabel(f"{i+1}", band_card))
            chk, typ = CheckBox(band_card), ComboBox(band_card)
            chk.setChecked(f_cfg.get("enabled", True)); typ.addItems(["PK", "LS", "HS"]); typ.setCurrentText(f_cfg["type"]); typ.setFixedWidth(85)
            freq = LineEdit(band_card); freq.setText(str(f_cfg["freq"])); freq.setFixedWidth(70)
            bl.addWidget(chk); bl.addWidget(typ); bl.addWidget(freq); bl.addWidget(CaptionLabel("Hz", band_card))
            sld, gain = Slider(Qt.Horizontal, band_card), LineEdit(band_card)
            sld.setRange(-120, 120); sld.setValue(int(f_cfg["gain"] * 10)); gain.setText(str(f_cfg["gain"])); gain.setFixedWidth(55)
            bl.addSpacing(15); bl.addWidget(sld, 1); bl.addWidget(gain); bl.addWidget(CaptionLabel("dB", band_card))
            qv = LineEdit(band_card); qv.setText(str(f_cfg["q"])); qv.setFixedWidth(55)
            bl.addSpacing(15); bl.addWidget(CaptionLabel("Q:", band_card)); bl.addWidget(qv)
            
            chk.stateChanged.connect(lambda _, idx=i: self._apply_filter(idx))
            typ.currentIndexChanged.connect(lambda _, idx=i: self._apply_filter(idx))
            sld.valueChanged.connect(lambda v, idx=i, le=gain: [le.setText(str(v/10)), self._apply_filter(idx)])
            gain.editingFinished.connect(lambda s=sld, le=gain: s.setValue(int(float(le.text().replace(',', '.') or 0) * 10)))
            freq.editingFinished.connect(lambda idx=i: self._apply_filter(idx))
            qv.editingFinished.connect(lambda idx=i: self._apply_filter(idx))
            self.filter_widgets.append({"enabled": chk, "type": typ, "freq": freq, "gain": gain, "q": qv, "slider": sld})
            self.bands_layout.addWidget(band_card)
            
        scroll.setWidget(container); eq_layout.addWidget(scroll); self.toggle_controls(False)

    def _check_connection(self):
        dev = self.get_device()
        if not dev and self.lbl_sn.text() != "Serial: Disconnected":
            self.comm.status_msg.emit("Disconnected")
        elif dev and self.lbl_sn.text() == "Serial: Disconnected":
            self.refresh()

    def toggle_controls(self, enabled):
        objs = [self.bal_slider, self.cb_filter, self.cb_gain, self.cb_amp, self.preset_cb, self.pre_slider, self.preamp_val, self.btn_save_hw, self.btn_reset, self.btn_add, self.btn_import]
        for o in objs: o.setEnabled(enabled)
        for w in self.filter_widgets:
            for k in w: w[k].setEnabled(enabled)

    def _create_row(self, layout, title, mapping, cmd):
        card = CardWidget(self); l = QHBoxLayout(card); l.addWidget(StrongBodyLabel(title, card))
        cb = ComboBox(card); cb.addItems(list(mapping.values())); cb.setFixedWidth(220)
        cb.currentIndexChanged.connect(lambda: self.write_val(cmd, cb.currentText(), mapping))
        l.addStretch(1); l.addWidget(cb); layout.addWidget(card); return cb

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
                for cmd in [CMD_VERSION, 0x11, 0x19, 0x1D, CMD_GLOBAL_GAIN]:
                    dev.find_output_reports()[0].send([REPORT_ID, READ, cmd, END] + [0x00]*60); time.sleep(0.05)
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
            elif cmd == CMD_PEQ_VALUES and len(data) >= 35:
                idx, f, q, g = data[5], data[28]|(data[29]<<8), round((data[30]|(data[31]<<8))/256.0, 2), data[32]|(data[33]<<8)
                if g > 32767: g -= 65536
                self.parsed_filters[idx] = {"freq": f, "q": q, "gain": round(g/256.0, 1), "type": INV_TYPE_CODES.get(data[34], "PK")}
            elif cmd == CMD_GLOBAL_GAIN: self.read_results[cmd] = struct.unpack("b", bytes([data[4]]))[0]
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
        if CMD_GLOBAL_GAIN in results:
            p = results[CMD_GLOBAL_GAIN]; self.preamp_val.setText(str(p)); self.pre_slider.setValue(int(p * 10))
        for idx, f in filters.items():
            if idx < 10:
                w = self.filter_widgets[idx]; w["freq"].setText(str(f["freq"])); w["q"].setText(str(f["q"]))
                w["gain"].setText(str(f["gain"])); w["slider"].setValue(int(f["gain"]*10)); w["type"].setCurrentText(f["type"])
        self.is_syncing = False; self.refresh_status_lbl_dac.setText(""); self.refresh_status_lbl_eq.setText("")

    def _apply_filter(self, idx):
        if self.is_syncing: return
        dev = self.get_device()
        if not dev: return
        try:
            dev.open(); report = dev.find_output_reports()[0]
            if idx >= 0:
                w = self.filter_widgets[idx]; g = 0.0 if not w["enabled"].isChecked() else float(w["gain"].text().replace(',', '.') or 0)
                f, q, t = max(1, int(float(w["freq"].text().replace(',', '.') or 100))), max(0.01, float(w["q"].text().replace(',', '.') or 1.0)), w["type"].currentText()
                pkt = [WRITE, CMD_PEQ_VALUES, 0x20, 0x00, idx, 0x00, 0x00] + list(self._calculate_biquad(t, f, q, g))
                pkt += list(struct.pack("<H", f)) + list(struct.pack("<H", int(q*256))) + list(struct.pack("<h", int(g*256))) + [TYPE_CODES.get(t, 0x02), 0x00, 0x00, END]
                report.send([REPORT_ID] + pkt + ([0x00] * (63 - len(pkt))))
            else:
                p = int(float(self.preamp_val.text().replace(',', '.') or 0)) & 0xFF
                report.send([REPORT_ID, WRITE, CMD_GLOBAL_GAIN, 0x02, 0x00, p] + [0x00]*58)
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

    def _calculate_biquad(self, t, f, q, g, fs=48000):
        A, w0 = 10**(g/40), 2*math.pi*f/fs; sn, cs = math.sin(w0), math.cos(w0); alpha = sn/(2*q)
        if t == "PK": b0, b1, b2, a0, a1, a2 = 1+alpha*A, -2*cs, 1-alpha*A, 1+alpha/A, -2*cs, 1-alpha/A
        elif t in ["LS", "HS"]:
            sqA, s = math.sqrt(A), 1 if t == "HS" else -1
            b0, b1, b2 = A*((A+1)+s*(A-1)*cs+2*sqA*alpha), -s*2*A*((A-1)+s*(A+1)*cs), A*((A+1)+s*(A-1)*cs-2*sqA*alpha)
            a0, a1, a2 = (A+1)-s*(A-1)*cs+2*sqA*alpha, s*2*((A-1)-s*(A+1)*cs), (A+1)-s*(A-1)*cs-2*sqA*alpha
        else: return b"\x00"*20
        return b"".join(struct.pack("<f", c/a0) for c in [b0, b1, b2, a1, a2])

    def _on_balance_change(self):
        v = self.bal_slider.value(); self.bal_txt.setText(f"L {abs(v)}" if v<0 else f"R {v}" if v>0 else "Center")
        dev = self.get_device()
        if dev and not self.is_syncing:
            try:
                dev.open(); sf, mag = (0x01, 256+v) if v<0 else (0x00, 256-v) if v>0 else (0x00, 0x00)
                dev.find_output_reports()[0].send([REPORT_ID, 0x01, 0x16, 0x04, sf, 0x00, mag] + [0x00]*57); dev.close(); self.save_settings()
            except: pass

    def _reset_eq(self):
        self.preamp_val.setText("0.0"); self.pre_slider.setValue(0)
        for w in self.filter_widgets:
            w["gain"].setText("0.0"); w["slider"].setValue(0); w["enabled"].setChecked(True); w["q"].setText("1.0"); w["type"].setCurrentText("PK")
        self._apply_filter(0)

    def _new_preset(self):
        name, ok = QInputDialog.getText(self, "New Preset", "Name:")
        if ok and name:
            df = [{"type": "PK", "freq": DEFAULT_FREQS[i], "q": 1.0, "gain": 0.0, "enabled": True} for i in range(10)]
            self.settings_data["presets"].append({"name": name, "preamp": 0.0, "filters": df})
            self.preset_cb.addItem(name); self.preset_cb.setCurrentIndex(self.preset_cb.count() - 1)

    def _import_squig(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open AutoEQ File")
        if not path: return
        try:
            with open(path, 'r') as f: lines = f.readlines()
            idx = 0
            for line in lines:
                if "Preamp:" in line:
                    m = re.search(r"Preamp:\s*([-+]?[\d.]+)", line)
                    if m: self.preamp_val.setText(m.group(1)); self.pre_slider.setValue(int(float(m.group(1))*10))
                if "Filter" in line and idx < 10:
                    w = self.filter_widgets[idx]; w["enabled"].setChecked("ON" in line)
                    w["type"].setCurrentText("PK" if " PK " in line else "LS" if " LS " in line else "HS")
                    fc, gn, qv = re.search(r"Fc\s+([\d.]+)", line), re.search(r"Gain\s+([-+.\d]+)", line), re.search(r"Q\s+([\d.]+)", line)
                    if fc: w["freq"].setText(fc.group(1).split('.')[0])
                    if gn: [w["gain"].setText(gn.group(1)), w["slider"].setValue(int(float(gn.group(1)) * 10))]
                    if qv: w["q"].setText(qv.group(1))
                    idx += 1
            self._apply_filter(0)
        except: pass

    def _load_preset_ui(self):
        p = self.settings_data["presets"][self.preset_cb.currentIndex()]
        self.preamp_val.setText(str(p["preamp"])); self.pre_slider.setValue(int(p["preamp"]*10))
        for i, f in enumerate(p["filters"]):
            w = self.filter_widgets[i]; w["enabled"].setChecked(f["enabled"]); w["type"].setCurrentText(f["type"])
            w["freq"].setText(str(f["freq"])); w["gain"].setText(str(f["gain"])); w["q"].setText(str(f["q"])); w["slider"].setValue(int(f["gain"]*10))

    def write_val(self, cmd, selection, n_map):
        dev = self.get_device(); inv = {v: k for k, v in n_map.items()}
        if dev:
            try:
                dev.open(); dev.find_output_reports()[0].send([REPORT_ID, WRITE, cmd, 0x01, inv.get(selection)] + [0x00]*59); dev.close()
            except: pass

    def save_settings(self):
        idx = self.preset_cb.currentIndex()
        if idx >= 0:
            c = self.settings_data["presets"][idx]; c["preamp"] = float(self.preamp_val.text().replace(',', '.') or 0)
            c["filters"] = [{"type": w["type"].currentText(), "freq": int(float(w["freq"].text().replace(',', '.') or 0)), "gain": float(w["gain"].text().replace(',', '.') or 0), "q": float(w["q"].text().replace(',', '.') or 1), "enabled": w["enabled"].isChecked()} for w in self.filter_widgets]
            self.settings_data["last_preset"] = idx
        self.settings_data["balance"] = self.bal_slider.value()
        with open(SETTINGS_FILE, "w") as f: json.dump(self.settings_data, f)

if __name__ == "__main__":
    # Taskbar Fix
    try:
        myappid = u'trn.controlpanel.v1'
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception:
        pass

    app = QApplication(sys.argv)
    
    # Global Application Icon (solves taskbar/window defaults)
    app.setWindowIcon(QIcon(resource_path("icon.ico")))
    
    window = FluentDACController()
    window.show()
    sys.exit(app.exec())
