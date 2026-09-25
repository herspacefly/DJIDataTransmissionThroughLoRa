#!/usr/bin/env python3
"""Receive, log, and summarize beacon frames from a transparent serial radio link."""

import argparse
import csv
import os
import re
import sys
import time
from datetime import datetime

try:
    import serial
except ImportError:
    serial = None


FRAME_PATTERN = re.compile(r"^#(?P<sequence>\d{4,}),(?P<timestamp>\d+\.\d{2})$")
RECONNECT_INTERVAL = 2.0
REQUIRED_BAUDRATE = 9600


def local_iso_time():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def default_output_path():
    return "beacon_rx_{}.csv".format(datetime.now().strftime("%Y%m%d_%H%M%S"))


def parse_args():
    parser = argparse.ArgumentParser(
        description="Receive numbered beacon frames and calculate loss statistics."
    )
    parser.add_argument("--port", required=True, help="serial port, for example COM3")
    parser.add_argument(
        "--baud", type=int, default=REQUIRED_BAUDRATE, help="must remain 9600"
    )
    parser.add_argument(
        "--expected-frames",
        type=int,
        default=120,
        help="numbered frames expected from the transmitter",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=5.0,
        help="seconds without a new valid frame before automatic completion",
    )
    parser.add_argument(
        "--output",
        default=default_output_path(),
        help="path for the raw-frame CSV log",
    )
    args = parser.parse_args()

    if args.baud != REQUIRED_BAUDRATE:
        parser.error("--baud must be {} to prevent radio buffer overflow".format(
            REQUIRED_BAUDRATE
        ))
    if args.expected_frames <= 0:
        parser.error("--expected-frames must be greater than zero")
    if args.idle_timeout <= 0:
        parser.error("--idle-timeout must be greater than zero")
    return args


def classify_frame(raw_frame, expected_frames, received_sequences):
    """Return the CSV parse status and optional parsed values for one complete line."""
    match = FRAME_PATTERN.match(raw_frame.rstrip("\r"))
    if not match:
        return "invalid", None, None

    sequence = int(match.group("sequence"))
    tx_timestamp = match.group("timestamp")
    if sequence < 1 or sequence > expected_frames:
        return "out_of_range", sequence, tx_timestamp
    if sequence in received_sequences:
        return "duplicate", sequence, tx_timestamp
    return "valid", sequence, tx_timestamp


def longest_missing_run(expected_frames, received_sequences):
    longest = 0
    current = 0
    for sequence in range(1, expected_frames + 1):
        if sequence in received_sequences:
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    return longest


def report(expected_frames, received_sequences, reason, output_path):
    received_count = len(received_sequences)
    lost_count = expected_frames - received_count
    loss_rate = (lost_count * 100.0) / expected_frames
    print("\n=== Receive summary ===")
    print("Completion reason: {}".format(reason))
    print("Received frames: {} / {}".format(received_count, expected_frames))
    print("Packet loss: {:.2f}% ({} frames)".format(loss_rate, lost_count))
    print("Max consecutive loss: {}".format(
        longest_missing_run(expected_frames, received_sequences)
    ))
    print("CSV log: {}".format(os.path.abspath(output_path)))


def write_record(writer, log_file, raw_frame, sequence, tx_timestamp, status):
    writer.writerow([local_iso_time(), raw_frame, sequence or "", tx_timestamp or "", status])
    log_file.flush()


def open_serial_port(args):
    return serial.Serial(
        port=args.port,
        baudrate=args.baud,
        timeout=0.25,
    )


def close_port(port):
    if port is None:
        return
    try:
        port.close()
    except (serial.SerialException, OSError):
        pass


def run(args):
    if serial is None:
        print("ERROR: pyserial is not installed. Run: python -m pip install pyserial", file=sys.stderr)
        return 1

    output_directory = os.path.dirname(os.path.abspath(args.output))
    try:
        if output_directory:
            os.makedirs(output_directory, exist_ok=True)
        log_file = open(args.output, "w", newline="", encoding="utf-8")
    except OSError as error:
        print("ERROR: unable to create CSV log {}: {}".format(args.output, error), file=sys.stderr)
        return 1

    writer = csv.writer(log_file)
    writer.writerow(["received_at", "raw_frame", "sequence", "tx_timestamp", "parse_status"])
    log_file.flush()

    received_sequences = set()
    buffer = bytearray()
    port = None
    first_valid_time = None
    last_valid_time = None
    next_reconnect_time = 0.0
    reason = "stopped before receiving a valid frame"

    print(
        "Listening on {} at {} baud. Expected frames: {}; idle timeout: {} seconds.".format(
            args.port, args.baud, args.expected_frames, args.idle_timeout
        )
    )

    def record_incomplete_buffer():
        if not buffer:
            return
        raw_frame = bytes(buffer).decode("ascii", errors="replace")
        write_record(writer, log_file, raw_frame, None, None, "incomplete")
        buffer.clear()

    try:
        while True:
            now = time.monotonic()
            if len(received_sequences) >= args.expected_frames:
                reason = "all expected frames received"
                break
            if last_valid_time is not None and now - last_valid_time >= args.idle_timeout:
                reason = "idle timeout after last valid frame"
                break

            if port is None:
                if now < next_reconnect_time:
                    time.sleep(min(next_reconnect_time - now, 0.1))
                    continue
                try:
                    port = open_serial_port(args)
                    print("INFO: serial port opened: {}".format(args.port), file=sys.stderr)
                except (serial.SerialException, OSError) as error:
                    print(
                        "WARN: unable to open {}: {}; retrying in {} seconds.".format(
                            args.port, error, RECONNECT_INTERVAL
                        ),
                        file=sys.stderr,
                    )
                    next_reconnect_time = now + RECONNECT_INTERVAL
                    continue

            try:
                chunk = port.read(port.in_waiting or 1)
            except (serial.SerialException, OSError) as error:
                print(
                    "WARN: serial port disconnected: {}; retrying in {} seconds.".format(
                        error, RECONNECT_INTERVAL
                    ),
                    file=sys.stderr,
                )
                record_incomplete_buffer()
                close_port(port)
                port = None
                next_reconnect_time = time.monotonic() + RECONNECT_INTERVAL
                continue

            if not chunk:
                continue

            buffer.extend(chunk)
            while b"\n" in buffer:
                line, _, remainder = buffer.partition(b"\n")
                buffer = bytearray(remainder)
                raw_frame = line.decode("ascii", errors="replace")
                status, sequence, tx_timestamp = classify_frame(
                    raw_frame, args.expected_frames, received_sequences
                )
                write_record(writer, log_file, raw_frame, sequence, tx_timestamp, status)

                if status == "valid":
                    received_sequences.add(sequence)
                    current_time = time.monotonic()
                    if first_valid_time is None:
                        first_valid_time = current_time
                    last_valid_time = current_time
                    print("Received #{:04d}".format(sequence))
                elif status == "invalid":
                    print("WARN: invalid frame logged: {!r}".format(raw_frame), file=sys.stderr)
                else:
                    print("INFO: {} frame logged: {!r}".format(status, raw_frame), file=sys.stderr)
    except KeyboardInterrupt:
        reason = "stopped by user"
        print("\nINFO: receiver stopped by user.", file=sys.stderr)
    finally:
        record_incomplete_buffer()
        close_port(port)
        log_file.close()
        report(args.expected_frames, received_sequences, reason, args.output)

    return 0


def main():
    return run(parse_args())


if __name__ == "__main__":
    sys.exit(main())
