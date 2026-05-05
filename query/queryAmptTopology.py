import pywinusb.hid as hid
import time

VID = 0x3302
PID = 0x43E8
# 4b (Header), 80 (Read), 1d (Amp Topology)
QUERY_AMP = [0x4b, 0x80, 0x1d] + ([0x00] * 61)

class AmpReader:
    def __init__(self):
        self.state = None

    def on_data(self, data):
        # Header check: 4b 80 1d 00
        if data[0:4] == [0x4b, 0x80, 0x1d, 0x00]:
            self.state = data[4]

    def get_topology(self):
        device = hid.HidDeviceFilter(vendor_id=VID, product_id=PID).get_devices()[0]
        try:
            device.open()
            device.set_raw_data_handler(self.on_data)
            device.find_output_reports()[0].send(QUERY_AMP)

            timeout = time.time() + 1.0
            while self.state is None and time.time() < timeout:
                time.sleep(0.01)
            return self.state
        finally:
            device.close()

if __name__ == "__main__":
    val = AmpReader().get_topology()
    if val is not None:
        # 0x01 is likely Class A or similar based on your trace
        print(f"Current Amp Topology: 0x{val:02x}")
    else:
        print("Query timed out.")