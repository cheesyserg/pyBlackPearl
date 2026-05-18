import sys
import hid

TARGET_VID = 0x3302
TARGET_PID = 0x43E8

print(f"Python version: {sys.version}")
print("Scanning for connected HID devices...\n")

try:
    devices = hid.enumerate()
except Exception as e:
    print(f"[!] Critical Error calling hid.enumerate(): {e}")
    print("If you are on Linux, you might need the system library: sudo apt install libhidapi-hidraw0")
    sys.exit(1)

found_target = False

if not devices:
    print("[!] No HID devices detected at all. Check your USB connection or physical ports.")
else:
    print(f"Found {len(devices)} HID device interface paths:\n")
    print(f"{'Vendor ID':<10} | {'Product ID':<10} | {'Manufacturer':<15} | {'Product String'}")
    print("-" * 75)

    for d in devices:
        vid = d['vendor_id']
        pid = d['product_id']
        m_str = d.get('manufacturer_string') or "Unknown"
        p_str = d.get('product_string') or "Unknown"

        print(f"{f'0x{vid:04X}':<10} | {f'0x{pid:04X}':<10} | {str(m_str):<15} | {p_str}")

        if vid == TARGET_VID and pid == TARGET_PID:
            found_target = True

print("\n" + "="*50 + "\n")

if found_target:
    print(f"[+] Found your TRN DAC hardware (VID: 0x{TARGET_VID:04X}, PID: 0x{TARGET_PID:04X}).")
    print("Testing read/write permissions...")
    try:
        dev = hid.device()
        dev.open(TARGET_VID, TARGET_PID)
        print("[+] SUCCESS: Device opened successfully. Permissions are correct!")
        dev.close()
    except Exception as e:
        print(f"[!] PERMISSION ERROR: Hardware exists, but python cannot read it.")
        print(f"    Details: {e}")
        print("\nFix for Linux:")
        print("1. Create a rules file: sudo nano /etc/udev/rules.d/99-trn-dac.rules")
        print('2. Paste: SUBSYSTEMS=="usb|hidraw", ATTRS{idVendor}=="3302", ATTRS{idProduct}=="43e8", MODE="0666"')
        print("3. Reload: sudo udevadm control --reload-rules && sudo udevadm trigger")
        print("4. Unplug and replug the DAC.")
else:
    print(f"[!] TARGET NOT FOUND: TRN DAC (VID: 0x{TARGET_VID:04X}, PID: 0x{TARGET_PID:04X}) is absent.")
    print("Suggestions:")
    print("1. Verify the DAC is securely plugged in and playing audio.")
    print("2. Try a different USB port (avoid external hubs for debugging).")
    print("3. On Linux, run 'dmesg | tail -n 20' right after plugging it in to verify kernel detection.")
