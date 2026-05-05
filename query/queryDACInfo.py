import pywinusb.hid as hid
import time

VID = 0x3302
PID = 0x43E8
QUERY_CMD = "4b800c00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"

def rx_handler(data):
    # This catches the custom firmware response
    payload = bytes(data[4:])
    version = payload.decode('ascii', errors='ignore').split('\x00')[0].strip()
    if version:
        print(f"[Internal] Firmware Version : {version}")

def get_complete_info():
    filter = hid.HidDeviceFilter(vendor_id=VID, product_id=PID)
    devices = filter.get_devices()

    if not devices:
        print("DAC not found.")
        return

    device = devices[0]
    
    try:
        # 1. Pull the Standard USB Descriptors (Wireshark Frame 2240 data)
        # These are populated by the OS immediately upon connection
        print("--- Hardware Info (USB Descriptors) ---")
        print(f"Manufacturer : {device.vendor_name}")
        print(f"Product      : {device.product_name}")
        print(f"Serial No.   : {device.serial_number}")
        print("-" * 39)

        # 2. Pull the Custom HID Info (The '4b' commands)
        device.open()
        device.set_raw_data_handler(rx_handler)
        
        reports = device.find_output_reports()
        if reports:
            cmd_bytes = [int(QUERY_CMD[i:i+2], 16) for i in range(0, len(QUERY_CMD), 2)]
            reports[0].send(cmd_bytes)
            time.sleep(0.5) # Wait for version string

    except Exception as e:
        print(f"Error: {e}")
    finally:
        device.close()

if __name__ == "__main__":
    get_complete_info()