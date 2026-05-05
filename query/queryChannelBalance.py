import pywinusb.hid as hid
import time

VID = 0x3302
PID = 0x43E8

def dual_channel_center():
    devices = hid.HidDeviceFilter(vendor_id=VID, product_id=PID).get_devices()
    if not devices:
        print("DAC not found.")
        return
    
    device = devices[0]
    device.open()
    try:
        report = device.find_output_reports()[0]
        print("Sending 'Both Channels Active' center command...")

        # Structure: 4b 01 16 04 [FLAG] 00 [MAG]
        # Flag 0x03 is the standard 'L+R' bitmask for many audio chips
        center_both = [0x4b, 0x01, 0x16, 0x04, 0x03, 0x00, 0x00] + ([0x00] * 57)
        
        # We also try Flag 0x00 again just in case, but after the 0x03
        center_null = [0x4b, 0x01, 0x16, 0x04, 0x00, 0x00, 0x00] + ([0x00] * 57)

        # 'Punch' the command through
        for _ in range(5):
            report.send(center_both)
            time.sleep(0.02)
            report.send(center_null)
            time.sleep(0.02)

        print("Command finished. Check if sound is balanced.")
    finally:
        device.close()

if __name__ == "__main__":
    # Make sure the Walkplay driver is NOT running in the background!
    dual_channel_center()