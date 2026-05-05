import pywinusb.hid as hid
import time

# Device Config
VID = 0x3302
PID = 0x43E8
# 64-byte Query for Section 0x11
QUERY_CMD = [0x4b, 0x80, 0x11] + ([0x00] * 61)

class WalkplayReader:
    def __init__(self):
        self.filter_val = None

    def on_data(self, data):
        # Adjusted for your specific REC output: 4b starts at data[0]
        if data[0:4] == [0x4b, 0x80, 0x11, 0x00]:
            self.filter_val = data[4]

    def read_section_11(self):
        devices = hid.HidDeviceFilter(vendor_id=VID, product_id=PID).get_devices()
        if not devices:
            print("DAC not found.")
            return

        device = devices[0]
        try:
            device.open()
            device.set_raw_data_handler(self.on_data)
            
            reports = device.find_output_reports()
            if not reports:
                print("No writable report found.")
                return

            reports[0].send(QUERY_CMD)

            # Wait for response
            timeout = time.time() + 1.0
            while self.filter_val is None and time.time() < timeout:
                time.sleep(0.01)

            return self.filter_val
        finally:
            device.close()

if __name__ == "__main__":
    reader = WalkplayReader()
    result = reader.read_section_11()
    
    if result is not None:
        print(f"Digital Filter Section Byte: 0x{result:02x}")
    else:
        print("Error: Could not read section (Timeout).")