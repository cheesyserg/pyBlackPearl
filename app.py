import tkinter as tk
from tkinter import ttk, messagebox
import pywinusb.hid as hid
import time
import threading
import sv_ttk 
import json
import os

# Import the balance function from your functions folder
from functions.balance import set_balance

VID, PID = 0x3302, 0x43E8
SETTINGS_FILE = "settings.json"

FILTER_MAP = {
    0x01: "FAST-LL",
    0x02: "Fast-PC (RECCOMENDED)",
    0x03: "Slow-LL",
    0x04: "SLOW-PC",
    0x05: "NOS"
}
GAIN_MAP = {0x00: "LOW", 0x01: "HIGH"}
AMP_MAP = {0x00: "CLASS H", 0x01: "CLASS AB"}

class DACController:
    def __init__(self, root):
        self.root = root
        self.root.title("TRN Black Pearl Control Panel (ALPHA)")
        self.root.geometry("780x580")
        self.read_results = {}
        self.hw_info = {"Man": "Loading...", "Prod": "Loading...", "SN": "Loading...", "FW": "Loading..."}
        self.is_syncing = False
        
        # Load last balance state from file
        self.last_balance = self.load_settings()
        
        sv_ttk.set_theme("dark")

        self.sidebar = ttk.Frame(self.root, width=220, padding="15", style="Card.TFrame")
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        self.content_area = ttk.Frame(self.root, padding="10")
        self.content_area.pack(side="right", fill="both", expand=True)

        self._build_sidebar()
        self._build_tabs()
        
        self.status = ttk.Label(self.content_area, text="Status: Ready", font=("Segoe UI", 9))
        self.status.pack(side="bottom", anchor="w", pady=5)

        # Auto-Sync and Apply last balance
        self.root.after(100, self.refresh)

    def load_settings(self):
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r") as f:
                    return json.load(f).get("balance", 0)
            except: pass
        return 0

    def save_settings(self, balance_val):
        with open(SETTINGS_FILE, "w") as f:
            json.dump({"balance": balance_val}, f)

    def _build_sidebar(self):
        ttk.Label(self.sidebar, text="DEVICE INFO", font=("Segoe UI Variable", 10, "bold")).pack(anchor="w", pady=(0, 15))
        self.lbl_man = self._info_item("Manufacturer", self.hw_info["Man"])
        self.lbl_prod = self._info_item("Product", self.hw_info["Prod"])
        self.lbl_sn = self._info_item("Serial No", self.hw_info["SN"])
        self.lbl_fw = self._info_item("Firmware", self.hw_info["FW"])
        ttk.Separator(self.sidebar, orient=tk.HORIZONTAL).pack(fill="x", pady=20)
        ttk.Button(self.sidebar, text="Refresh", style="Accent.TButton", command=self.refresh).pack(fill="x")

    def _info_item(self, label, value):
        ttk.Label(self.sidebar, text=label, font=("Segoe UI", 8, "bold"), foreground="gray").pack(anchor="w", pady=(5, 0))
        lbl = ttk.Label(self.sidebar, text=value, font=("Segoe UI", 9), wraplength=180)
        lbl.pack(anchor="w", pady=(0, 5))
        return lbl

    def _build_tabs(self):
        self.tabs = ttk.Notebook(self.content_area)
        self.tabs.pack(fill="both", expand=True)

        self.dac_tab = ttk.Frame(self.tabs, padding="20")
        self.tabs.add(self.dac_tab, text=" DAC Settings ")
        
        ttk.Label(self.dac_tab, text="Hardware Configuration", font=("Segoe UI Variable", 16, "bold")).pack(anchor="w", pady=(0, 15))

        # Balance Control
        balance_frame = ttk.LabelFrame(self.dac_tab, text=" Channel Balance ", padding="15")
        balance_frame.pack(fill="x", pady=10)
        
        self.bal_var = tk.IntVar(value=self.last_balance)
        self.bal_slider = ttk.Scale(balance_frame, from_=-15, to=15, variable=self.bal_var, orient="horizontal", command=self._on_balance_change)
        self.bal_slider.pack(fill="x", side="top", pady=5)
        
        lbl_box = ttk.Frame(balance_frame)
        lbl_box.pack(fill="x")
        ttk.Label(lbl_box, text="L", font=("Segoe UI", 9, "bold")).pack(side="left")
        ttk.Label(lbl_box, text="R", font=("Segoe UI", 9, "bold")).pack(side="right")
        self.bal_lbl = ttk.Label(lbl_box, text="Center", font=("Segoe UI", 8))
        self.bal_lbl.pack(side="top")
        self._update_balance_label(self.last_balance)

        self._create_auto_row(self.dac_tab, "Digital Filter", FILTER_MAP, 0x11)
        self._create_auto_row(self.dac_tab, "Gain Mode", GAIN_MAP, 0x19)
        self._create_auto_row(self.dac_tab, "Amp Topology", AMP_MAP, 0x1d)

    def _create_auto_row(self, parent, label_text, name_map, cmd_byte):
        container = ttk.LabelFrame(parent, text=f" {label_text} ", padding="10")
        container.pack(fill="x", pady=5)
        var = tk.StringVar(value="Loading...")
        setattr(self, f"var_{cmd_byte:x}", var)
        inv_map = {v: k for k, v in name_map.items()}
        setattr(self, f"map_{cmd_byte:x}", inv_map)
        cb = ttk.Combobox(container, textvariable=var, values=list(name_map.values()), state="readonly")
        cb.pack(fill="x")
        cb.bind("<<ComboboxSelected>>", lambda e: self.write_val(cmd_byte, var.get()))

    def _update_balance_label(self, val):
        val = int(val)
        if val < 0: self.bal_lbl.config(text=f"Left {abs(val)}")
        elif val > 0: self.bal_lbl.config(text=f"Right {val}")
        else: self.bal_lbl.config(text="Center")

    def _on_balance_change(self, event=None):
        val = int(self.bal_var.get())
        self._update_balance_label(val)
        self.save_settings(val)
        
        if not self.is_syncing:
            dev = self.get_device()
            if dev:
                try:
                    dev.open()
                    set_balance(dev, val)
                    dev.close()
                except: pass

    def get_device(self):
        devices = hid.HidDeviceFilter(vendor_id=VID, product_id=PID).get_devices()
        return devices[0] if devices else None

    def on_data(self, data):
        if data[0:2] == [0x4b, 0x80] and data[3] == 0x00:
            cmd = data[2]
            if cmd == 0x0c:
                payload = bytes(data[4:])
                self.hw_info["FW"] = payload.decode('ascii', errors='ignore').split('\x00')[0].strip()
            else:
                self.read_results[cmd] = data[4]

    def refresh(self):
        def task():
            self.is_syncing = True
            dev = self.get_device()
            if not dev:
                self.root.after(0, lambda: self.status.config(text="Status: Disconnected"))
                self.is_syncing = False
                return
            try:
                self.hw_info["Man"] = dev.vendor_name or "Unknown"
                self.hw_info["Prod"] = dev.product_name or "Unknown"
                self.hw_info["SN"] = dev.serial_number or "N/A"
                dev.open()
                
                # Apply last balance state on startup
                set_balance(dev, self.last_balance)
                
                dev.set_raw_data_handler(self.on_data)
                report = dev.find_output_reports()[0]
                for cmd in [0x0c, 0x11, 0x19, 0x1d]:
                    report.send([0x4b, 0x80, cmd] + ([0x00] * 61))
                    time.sleep(0.1) 
                dev.close()
                self.root.after(0, self.update_ui)
            finally:
                self.is_syncing = False
        
        self.status.config(text="Status: Syncing...")
        threading.Thread(target=task, daemon=True).start()

    def update_ui(self):
        self.lbl_man.config(text=self.hw_info["Man"])
        self.lbl_prod.config(text=self.hw_info["Prod"])
        self.lbl_sn.config(text=self.hw_info["SN"])
        self.lbl_fw.config(text=self.hw_info["FW"])
        maps = {0x11: FILTER_MAP, 0x19: GAIN_MAP, 0x1d: AMP_MAP}
        for cmd, name_map in maps.items():
            if cmd in self.read_results:
                getattr(self, f"var_{cmd:x}").set(name_map.get(self.read_results[cmd], "Unknown"))
        self.status.config(text="Status: Synchronized")

    def write_val(self, cmd, selection):
        if self.is_syncing or "Loading" in selection: return
        dev = self.get_device()
        if not dev: return
        try:
            val = getattr(self, f"map_{cmd:x}").get(selection)
            dev.open()
            packet = [0x4b, 0x01, cmd, 0x01, val] + ([0x00] * 59)
            dev.find_output_reports()[0].send(packet)
            dev.close()
            self.status.config(text=f"Status: Applied {selection}")
        except Exception as e:
            messagebox.showerror("Error", str(e))

if __name__ == "__main__":
    root = tk.Tk()
    app = DACController(root)
    root.mainloop()