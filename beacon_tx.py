#!/usr/bin/env python3
"""Send timestamped beacon frames over a transparent serial radio link."""

import argparse
import sys
import time
from datetime import datetime

try:
    import serial
except ImportError:
    serial = None


MAX_FRAME_BYTES = 40
REQUIRED_BAUDRATE = 9600


def iso_time(timestamp):
    """Format a local wall-clock timestamp for the final report."""
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="milliseconds")


def build_frame(sequence):
    """Build one newline-delimited ASCII beacon frame."""
    frame = "#{:04d},{:.2f}\n".format(sequence, time.time()).encode("ascii")
    if len(frame) > MAX_FRAME_BYTES:
        raise ValueError("generated frame is {} bytes; limit is {}".format(
            len(frame), MAX_FRAME_BYTES
        ))
    return frame


def parse_args():
    parser = argparse.ArgumentParser(
        description="Transmit numbered beacon frames through a serial radio module."
    )
    parser.add_argument("--port", default="/dev/ttyUSB0", help="serial device path")
    parser.add_argument(
        "--baud", type=int, default=REQUIRED_BAUDRATE, help="must remain 9600"
    )
    parser.add_argument(
        "--frequency", type=float, default=2.0, help="beacon frequency in Hz"
    )
    parser.add_argument(
        "--duration", type=float, default=60.0, help="test duration in seconds"
    )
    args = parser.parse_args()

    if args.baud != REQUIRED_BAUDRATE:
        parser.error("--baud must be {} to prevent radio buffer overflow".format(
            REQUIRED_BAUDRATE
        ))
    if args.frequency <= 0:
        parser.error("--frequency must be greater than zero")
    if args.duration <= 0:
        parser.error("--duration must be greater than zero")
    return args


def report(sent_count, start_time, end_time):
    print("\n=== Transmit summary ===")
    print("Frames sent: {}".format(sent_count))
    print("Start time: {}".format(iso_time(start_time) if start_time else "N/A"))
    print("End time:   {}".format(iso_time(end_time) if end_time else "N/A"))


def run(args):
    if serial is None:
        print("ERROR: pyserial is not installed. Run: pip3 install --user pyserial", file=sys.stderr)
        return 1

    try:
        port = serial.Serial(
            port=args.port,
            baudrate=args.baud,
            timeout=1,
            write_timeout=2,
        )
    except (serial.SerialException, OSError) as error:
        print("ERROR: unable to open {}: {}".format(args.port, error), file=sys.stderr)
        return 1

    sent_count = 0
    first_send_time = None
    last_send_time = None
    exit_code = 0
    interval = 1.0 / args.frequency
    test_deadline = time.monotonic() + args.duration
    next_send = time.monotonic()

    print(
        "Transmitting to {} at {} baud, {} Hz for {} seconds.".format(
            args.port, args.baud, args.frequency, args.duration
        )
    )
    try:
        while True:
            now = time.monotonic()
            if now >= test_deadline:
                break
            if now < next_send:
                time.sleep(min(next_send - now, 0.1))
                continue

            frame = build_frame(sent_count + 1)
            port.write(frame)
            port.flush()
            send_time = time.time()
            if first_send_time is None:
                first_send_time = send_time
            last_send_time = send_time
            sent_count += 1

            # Avoid a burst of catch-up frames if the host was paused or the port stalled.
            next_send += interval
            if time.monotonic() > next_send:
                next_send = time.monotonic() + interval
    except KeyboardInterrupt:
        print("\nINFO: transmission stopped by user.", file=sys.stderr)
    except (serial.SerialException, OSError, ValueError) as error:
        exit_code = 1
        print("\nERROR: transmission stopped: {}".format(error), file=sys.stderr)
    finally:
        try:
            port.close()
        except (serial.SerialException, OSError) as error:
            exit_code = 1
            print("ERROR: unable to close serial port: {}".format(error), file=sys.stderr)
        report(sent_count, first_send_time, last_send_time)

    return exit_code


def main():
    return run(parse_args())


if __name__ == "__main__":
    sys.exit(main())
