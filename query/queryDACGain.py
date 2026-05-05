import pywinusb.hid as hid
import time

VID = 0x3302
PID = 0x43E8
# 4b (Header), 80 (Read), 19 (Gain Mode)
QUERY_GAIN = [0x4b, 0x80, 0x19] + ([0x00] * 61)

class GainReader:
    def __init__(self):
        self.state = None

    def on_data(self, data):
        # Header check for Gain response: 4b 80 19 00
        if data[0:4] == [0x4b, 0x80, 0x19, 0x00]:
            self.state = data[4]

    def get_gain(self):
        target = hid.HidDeviceFilter(vendor_id=VID, product_id=PID).get_devices()
        if not target: return "Device not found"
        
        device = target[0]
        try:
            device.open()
            device.set_raw_data_handler(self.on_data)
            
            # Trigger the query
            device.find_output_reports()[0].send(QUERY_GAIN)

            # Wait for response
            timeout = time.time() + 1.0
            while self.state is None and time.time() < timeout:
                time.sleep(0.01)
            return self.state
        finally:
            device.close()

if __name__ == "__main__":
    val = GainReader().get_gain()
    if val is not None:
        # Usually 0x00 for Low Gain, 0x01 for High Gain
        print(f"0x{val:02x}")
    else:
        print("Query timed out.")