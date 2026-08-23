#!/usr/bin/env python3
"""
Tuya Mini Split UART controller

Replays/reproduces the UART commands captured from the Tuya WiFi module.

Frame format:
    A5 01 01 21 <seq> 00 00 <len> <crc16 hi> <crc16 lo> <payload>

The 2-byte checksum is CRC-16/XMODEM over every byte before the checksum.
"""

import argparse
import time
import serial


def crc16_xmodem(data: bytes) -> int:
    """CRC-16/XMODEM: poly 0x1021, init 0x0000."""
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def make_state_frame(payload: bytes, seq: int = 0) -> bytes:
    """Build a module -> board STATE frame with correct CRC placement."""
    header = bytes([0xA5, 0x01, 0x01, 0x21, seq & 0xFF, 0x00, 0x00])
    length = 10 + len(payload)  # 7-byte header + 1 length + 2 CRC + payload
    pre_checksum = header + bytes([length]) + payload
    crc = crc16_xmodem(pre_checksum)

    # CRC goes between length byte and payload
    return (
        header
        + bytes([length])
        + bytes([(crc >> 8) & 0xFF, crc & 0xFF])
        + payload
    )


def simple_dp(dp_id: int, value: int) -> bytes:
    """Build a payload for a single 1-byte DP command."""
    return bytes([0x0A, 0x0A, 0x00, dp_id & 0xFF, value & 0xFF])


def temperature_payload(temp_f: int) -> bytes:
    """Build a payload for setting the target temperature in °F."""
    return bytes([0x0A, 0x0A, 0x02, 0x27, 0x00, 0x00, 0x00, temp_f & 0xFF])


# Known DP IDs and human-readable names
DP_NAMES = {
    0x01: "power",
    0x05: "fan",
    0x0E: "h_louver",
    0x11: "v_louver",
    0x12: "mode",
    0x22: "sleep",
    0x25: "beep",
    0x27: "temp_F",
    0x73: "display_flag",
}


def format_dp_value(dp: int, val: int):
    """Convert raw DP values to readable strings for known DPs."""
    if dp == 0x01:
        return "on" if val else "off"

    if dp == 0x05:
        mapping = {
            0: "auto", 1: "mute", 2: "low", 3: "mid_low",
            4: "mid", 5: "mid_high", 6: "high", 7: "extra_high"
        }
        return mapping.get(val, f"unknown({val})")

    if dp == 0x0E:
        mapping = {
            8: "off",
            1: "left_right_flow", 2: "left_flow", 3: "middle_flow", 4: "right_flow",
            9: "left_fix", 10: "bit_left_fix", 11: "middle_fix",
            12: "bit_right_fix", 13: "right_fix"
        }
        return mapping.get(val, f"unknown({val})")

    if dp == 0x11:
        mapping = {
            8: "off",
            1: "up_down_flow", 2: "up_flow", 3: "down_flow",
            9: "up_fix", 10: "above_up_fix", 11: "middle_fix",
            12: "above_down_fix", 13: "down_fix"
        }
        return mapping.get(val, f"unknown({val})")

    if dp == 0x12:
        return {
            0: "auto", 1: "cool", 2: "dry", 3: "fan", 4: "heat"
        }.get(val, f"unknown({val})")

    if dp == 0x22:
        return {
            0: "off",
            1: "standard",
            2: "aged",
            3: "child"
        }.get(val, f"unknown({val})")

    if dp == 0x25:
        return "on" if val else "off"

    if dp == 0x27:
        if val == 0:
            return None
        if 0 <= val <= 130:
            return f"{val}F"
        return None  # invalid temperature – hide it

    if dp == 0x73:
        return str(val)

    return str(val)


def parse_state_frame(frame: bytes):
    """Extract known DPs from a full state frame.

    Stops at the first malformed entry instead of guessing,
    so we don't produce garbage like temp_F=0F.
    """
    if len(frame) < 12:
        return None

    payload = frame[10:]  # header(7) + length(1) + crc(2)
    if not payload.startswith(b"\x0c\x0c"):
        return None

    data = payload[2:]
    i = 0
    state = {}

    while i < len(data):
        typ = data[i]
        if typ not in (0x00, 0x01, 0x02):
            break  # out of sync – stop

        if i + 1 >= len(data):
            break

        dp = data[i + 1]

        if typ == 0x00:
            val_len = 1
        elif typ == 0x01:
            val_len = 2
        else:  # 0x02
            val_len = 4

        if i + 2 + val_len > len(data):
            break

        val = int.from_bytes(data[i + 2:i + 2 + val_len], "big")

        if dp in DP_NAMES:
            formatted = format_dp_value(dp, val)
            if formatted is not None:
                state[DP_NAMES[dp]] = formatted

        i += 2 + val_len

    return state if state else None


def frame_valid(frame: bytes) -> bool:
    """Check length and CRC for a Tuya UART frame."""
    if len(frame) < 10:
        return False

    length = frame[7]
    if len(frame) != length:
        return False

    expected = (frame[8] << 8) | frame[9]
    payload = frame[10:]
    calc = crc16_xmodem(frame[:8] + payload)

    return calc == expected


def extract_temperature_from_payload(payload: bytes):
    """
    Search for the temperature pattern:
      02 27 00 00 00 <temp>   (normal)
      02 27 00 00 00 00 <temp>  (extra zero variant)
    """
    idx = 0
    while True:
        idx = payload.find(b"\x02\x27", idx)
        if idx == -1:
            return None

        # Need at least 4 bytes of value after DP id
        if idx + 6 <= len(payload):
            val_bytes = payload[idx + 2:idx + 6]

            # Normal: 00 00 00 <temp>
            if val_bytes[:3] == b"\x00\x00\x00":
                temp = val_bytes[3]
                if 0 <= temp <= 130:
                    return temp

            # Extra zero: 00 00 00 00 <temp>
            if val_bytes == b"\x00\x00\x00\x00":
                if idx + 7 <= len(payload):
                    temp = payload[idx + 6]
                    if 60 <= temp <= 90:
                        return temp

        idx += 2

    return None

def extract_frames(data: bytes):
    """Split raw RX bytes into complete Tuya frames, ignoring leading noise."""
    frames = []
    i = 0
    while i < len(data):
        if data[i] != 0xA5:
            i += 1
            continue
        if i + 8 > len(data):
            break
        length = data[i + 7]
        if i + length > len(data):
            break
        frames.append(data[i:i + length])
        i += length
    return frames


# Exact full frames captured from the Tuya module (module -> board).
# These are known-good and should be replayed as-is.
PAYLOADS = {
    # Power
    "power_on": bytes.fromhex("a5 01 01 21 09 00 00 12 be b6 0a 0a 00 01 01 00 13 00"),
    "power_off": bytes.fromhex("a5 01 01 21 0a 00 00 0f 62 dd 0a 0a 00 01 00"),

    # Mode
    "mode_auto": bytes.fromhex("a5 01 01 21 0e 00 00 24 a7 eb 0a 0a 00 02 00 00 0a 28 02 27 00 00 00 4f 00 12 00 00 05 02 00 73 00 00 13 00"),
    "mode_cool": bytes.fromhex("a5 01 01 21 0f 00 00 24 d7 e6 0a 0a 00 02 00 00 0a 28 02 27 00 00 00 4f 00 12 01 00 05 00 00 73 01 00 13 00"),
    "mode_dry": bytes.fromhex("a5 01 01 21 10 00 00 24 96 2b 0a 0a 00 02 00 00 0a 28 02 27 00 00 00 4f 00 12 02 00 05 02 00 73 00 00 13 00"),
    "mode_fan": bytes.fromhex("a5 01 01 21 11 00 00 24 f0 71 0a 0a 00 02 00 00 0a 28 02 27 00 00 00 4f 00 12 03 00 05 02 00 73 00 00 13 00"),
    "mode_heat": bytes.fromhex("a5 01 01 21 12 00 00 24 ee ee 0a 0a 00 02 00 00 0a 28 02 27 00 00 00 4f 00 12 04 00 05 05 00 73 00 00 13 00"),

    # Fan speed
    "fan_auto": bytes.fromhex("a5 01 01 21 16 00 00 12 7e bc 0a 0a 00 05 00 00 73 01"),
    "fan_mute": bytes.fromhex("a5 01 01 21 17 00 00 12 1b 5c 0a 0a 00 05 01 00 73 00"),
    "fan_low": bytes.fromhex("a5 01 01 21 18 00 00 12 93 63 0a 0a 00 05 02 00 73 00"),
    "fan_mid_low": bytes.fromhex("a5 01 01 21 19 00 00 12 e6 a2 0a 0a 00 05 03 00 73 00"),
    "fan_mid": bytes.fromhex("a5 01 01 21 1a 00 00 12 b2 10 0a 0a 00 05 04 00 73 00"),
    "fan_mid_high": bytes.fromhex("a5 01 01 21 1b 00 00 12 c7 d1 0a 0a 00 05 05 00 73 00"),
    "fan_high": bytes.fromhex("a5 01 01 21 1c 00 00 12 54 46 0a 0a 00 05 06 00 73 00"),
    "fan_extra_high": bytes.fromhex("a5 01 01 21 1d 00 00 15 4d cf 0a 0a 00 26 00 00 05 07 00 73 00"),

    # Horizontal / left-right louver
    "louver_h_left_right_flow": bytes.fromhex("a5 01 01 21 38 00 00 0f 50 a7 0a 0a 00 0e 01"),
    "louver_h_left_flow": bytes.fromhex("a5 01 01 21 39 00 00 0f 8b e7 0a 0a 00 0e 02"),
    "louver_h_middle_flow": bytes.fromhex("a5 01 01 21 3a 00 00 0f b6 82 0a 0a 00 0e 03"),
    "louver_h_right_flow": bytes.fromhex("a5 01 01 21 3b 00 00 0f 2d 46 0a 0a 00 0e 04"),
    "louver_h_left_fix": bytes.fromhex("a5 01 01 21 3c 00 00 0f 4d 40 0a 0a 00 0e 09"),
    "louver_h_bit_left_fix": bytes.fromhex("a5 01 01 21 3d 00 00 0f 96 00 0a 0a 00 0e 0a"),
    "louver_h_middle_fix": bytes.fromhex("a5 01 01 21 3e 00 00 0f ab 65 0a 0a 00 0e 0b"),
    "louver_h_bit_right_fix": bytes.fromhex("a5 01 01 21 3f 00 00 0f 30 a1 0a 0a 00 0e 0c"),
    "louver_h_right_fix": bytes.fromhex("a5 01 01 21 40 00 00 0f 13 0f 0a 0a 00 0e 0d"),

    # Vertical / up-down louver
    "louver_v_up_down_flow": bytes.fromhex("a5 01 01 21 43 00 00 0f ec 8a 0a 0a 00 11 01"),
    "louver_v_up_flow": bytes.fromhex("a5 01 01 21 44 00 00 0f 6d 42 0a 0a 00 11 02"),
    "louver_v_down_flow": bytes.fromhex("a5 01 01 21 45 00 00 0f 96 40 0a 0a 00 11 03"),
    "louver_v_up_fix": bytes.fromhex("a5 01 01 21 46 00 00 0f 1a 4e 0a 0a 00 11 09"),
    "louver_v_above_up_fix": bytes.fromhex("a5 01 01 21 47 00 00 0f c1 0e 0a 0a 00 11 0a"),
    "louver_v_middle_fix": bytes.fromhex("a5 01 01 21 48 00 00 0f 49 7b 0a 0a 00 11 0b"),
    "louver_v_above_down_fix": bytes.fromhex("a5 01 01 21 49 00 00 0f d2 bf 0a 0a 00 11 0c"),
    "louver_v_down_fix": bytes.fromhex("a5 01 01 21 4a 00 00 0f ef da 0a 0a 00 11 0d"),

    # Sleep mode
    "sleep_standard": bytes.fromhex("a5 01 01 21 50 00 00 0f c2 f6 0a 0a 00 22 01"),
    "sleep_aged": bytes.fromhex("a5 01 01 21 51 00 00 0f 19 b6 0a 0a 00 22 02"),
    "sleep_child": bytes.fromhex("a5 01 01 21 52 00 00 0f 24 d3 0a 0a 00 22 03"),

    # Beep
    "beep_off": bytes.fromhex("a5 01 01 21 21 00 00 0f 0b b8 0a 0a 00 25 00"),
    "beep_on": bytes.fromhex("a5 01 01 21 22 00 00 0f 36 dd 0a 0a 00 25 01"),

    # Temperature (exact captured frames)
    "temp_80": bytes.fromhex("a5 01 01 21 23 00 00 12 f9 44 0a 0a 02 27 00 00 00 50"),
    "temp_79": bytes.fromhex("a5 01 01 21 24 00 00 12 12 d1 0a 0a 02 27 00 00 00 4f"),
    "temp_78": bytes.fromhex("a5 01 01 21 25 00 00 12 01 85 0a 0a 02 27 00 00 00 4e"),
}


def send_and_read(ser: serial.Serial, frame: bytes, label: str, quiet_period: float = 0.5):
    """Send a frame and read responses until the line is quiet for quiet_period."""
    print(f"TX {label}: {frame.hex(' ').upper()}")
    ser.reset_input_buffer()
    ser.write(frame)

    last_data_time = time.time()
    while True:
        resp = ser.read(1024)
        if resp:
            print(f"RX: {resp.hex(' ').upper()}")
            for f in extract_frames(resp):
                if len(f) >= 10 and f[3] == 0x21:
                    state = parse_state_frame(f)
                    if state:
                        # If temperature was missing/invalid, try direct pattern search
                        if "temp_F" not in state:
                            payload = f[10:]  # raw payload after CRC
                            temp = extract_temperature_from_payload(payload)
                            if temp is not None:
                                state["temp_F"] = f"{temp}F"
                        print("  STATE: " + ", ".join(f"{k}={v}" for k, v in state.items()))
            last_data_time = time.time()
        else:
            if time.time() - last_data_time >= quiet_period:
                break


def main():
    parser = argparse.ArgumentParser(description="Tuya Mini Split UART controller")
    parser.add_argument("--port", default="/dev/ttyUSB0", help="serial port")
    parser.add_argument("--baud", type=int, default=115200, help="baud rate")
    parser.add_argument("--command", help="named command from PAYLOADS")
    parser.add_argument("--hex", help="raw full frame hex to send, e.g. 'a5 01 ...'")
    parser.add_argument("--temp", type=int, help="set temperature in Fahrenheit (0-130)")
    parser.add_argument("--dp", nargs=2, metavar=("ID", "VALUE"),
                        help="single-DP command: --dp 0x05 0x06 for fan high")
    parser.add_argument("--seq", type=int, default=0, help="sequence byte when generating dynamic frames")
    parser.add_argument("--count", type=int, default=1, help="how many times to send")
    parser.add_argument("--delay", type=float, default=0.3, help="delay between sends")
    parser.add_argument("--list", action="store_true", help="list available named commands")
    args = parser.parse_args()

    if args.list:
        print("Available commands:")
        for name in PAYLOADS:
            print(f"  {name}")
        print("  --temp <F> to set temperature")
        print("  --dp <id> <value> for custom DP")
        return

    if not (args.command or args.hex or args.temp is not None or args.dp):
        parser.error("give --command, --hex, --temp, or --dp")

    try:
        ser = serial.Serial(
            port=args.port,
            baudrate=args.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.2
        )
    except Exception as exc:
        print(f"Failed to open {args.port}: {exc}")
        return

    seq = args.seq & 0xFF

    try:
        for i in range(args.count):
            if args.command:
                if args.command not in PAYLOADS:
                    print(f"Unknown command: {args.command}")
                    return
                frame = PAYLOADS[args.command]  # exact frame, no rebuilding
                send_and_read(ser, frame, args.command)

            elif args.temp is not None:
                if not (0 <= args.temp <= 130):
                    print("Temperature out of range: use 0-130°F")
                    return
                payload = temperature_payload(args.temp)
                frame = make_state_frame(payload, seq + i)
                send_and_read(ser, frame, f"temp {args.temp}")

            elif args.dp:
                try:
                    dp_id = int(args.dp[0], 0)
                    dp_val = int(args.dp[1], 0)
                except ValueError:
                    print("Invalid DP id/value. Use hex like 0x05 0x06")
                    return
                payload = simple_dp(dp_id, dp_val)
                frame = make_state_frame(payload, seq + i)
                send_and_read(ser, frame, f"dp {args.dp[0]} {args.dp[1]}")

            else:  # --hex
                hex_str = args.hex.replace(" ", "").replace("0x", "")
                try:
                    frame = bytes.fromhex(hex_str)
                except ValueError:
                    print("Invalid hex string")
                    return
                send_and_read(ser, frame, "hex")

            time.sleep(args.delay)
    finally:
        ser.close()


if __name__ == "__main__":
    main()
