import pywinusb.hid as hid
import time
import math

VID = 0x3302
PID = 0x43E8

def continuous_pan(speed=0.5):
    devices = hid.HidDeviceFilter(vendor_id=VID, product_id=PID).get_devices()
    if not devices:
        print("DAC not found.")
        return
    
    device = devices[0]
    device.open()
    report = device.find_output_reports()[0]

    print("--- Oscillating Pan Active ---")
    print("Press Ctrl+C to stop and return to center.")
    
    start_time = time.time()
    try:
        while True:
            elapsed = time.time() - start_time
            
            # math.sin goes from -1 to 1. We scale to -15 to +15.
            # Formula: level = sin(t * speed * pi) * 15
            raw_level = math.sin(elapsed * speed * math.pi) * 15
            level = int(raw_level)

            if level < 0:
                # LEFT (Trace Match: Flag 01, Mag f1-ff)
                side_flag = 0x01
                magnitude = 256 + level  # e.g., 256 + (-15) = 241 (f1)
            elif level > 0:
                # RIGHT (Trace Match: Flag 00, Mag f1-ff)
                side_flag = 0x00
                magnitude = 256 - level  # e.g., 256 - 15 = 241 (f1)
            else:
                # CENTER
                side_flag = 0x00
                magnitude = 0x00

            # Packet: [4b, 01, 16, 04, SideFlag, 00, Magnitude]
            packet = [0x4b, 0x01, 0x16, 0x04, side_flag, 0x00, magnitude] + ([0x00] * 57)
            report.send(packet)

            # Console visualizer
            bar_width = 15
            pos = " " * (bar_width + level) + "#" + " " * (bar_width - level)
            print(f"L |{pos}| R  (Level: {level:>3})", end='\r')
            
            time.sleep(0.05) # 20Hz update rate for smoothness

    except KeyboardInterrupt:
        print("\n\nStopping... Resetting to Center.")
    finally:
        # Final reset to absolute center
        center_pkt = [0x4b, 0x01, 0x16, 0x04, 0x00, 0x00, 0x00] + ([0x00] * 57)
        report.send(center_pkt)
        device.close()

if __name__ == "__main__":
    # speed: 0.5 is a slow wave, 1.0 is faster.
    continuous_pan(speed=0.4)