import pywinusb.hid as hid

def set_balance(device, level):
    """
    Sets the DAC balance level.
    level: integer from -15 (Full Left) to +15 (Full Right). 0 is center.
    """
    if not device:
        return

    # Packet Structure: [4b, 01, 16, 04, SideFlag, 00, Magnitude]
    if level < 0:
        # LEFT (Flag 01)
        side_flag = 0x01
        magnitude = 256 + level  # e.g., level -15 -> 241 (0xf1)
    elif level > 0:
        # RIGHT (Flag 00)
        side_flag = 0x00
        magnitude = 256 - level  # e.g., level +15 -> 241 (0xf1)
    else:
        # CENTER
        side_flag = 0x00
        magnitude = 0x00

    try:
        packet = [0x4b, 0x01, 0x16, 0x04, side_flag, 0x00, magnitude] + ([0x00] * 57)
        reports = device.find_output_reports()
        if reports:
            reports[0].send(packet)
    except Exception as e:
        print(f"Balance error: {e}")