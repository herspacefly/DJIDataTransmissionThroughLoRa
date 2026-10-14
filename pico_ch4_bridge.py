#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pico 气体数据 → LoRa，后台可选 USB GPS 提供经纬度与高程。
帧格式: $MIRA,<序号>,<时间戳>,<CH4>,<H2O>,<C2H6>,<纬度>,<经度>,<高程>
高程为 GGA 的平均海平面海拔（米）；缺测统一发送 nan。
无 GPS 时继续发送气体，位置回退到仪器第 16/17 列，高程为 nan。

python3 pico_ch4_bridge.py --dry-run   # 只解析打印，不发送
python3 pico_ch4_bridge.py --gps-test  # 只测 GPS，不打开气体/LoRa 口
python3 pico_ch4_bridge.py --no-gps    # 不探测外接 GPS
"""

import argparse
import datetime
import logging
import math
import os
import tempfile
import threading
import time
from logging.handlers import RotatingFileHandler

import serial
from serial.tools import list_ports

SRC = '/dev/ttyUSB_RS232'
DST = '/dev/ttyUSB_LORA'
BAUD = 9600
MAX_SOURCE_BYTES = 1024
MAX_FRAME_BYTES = 121
IDX_TS, IDX_CH4, IDX_H2O, IDX_C2H6 = 0, 6, 7, 8
IDX_LAT, IDX_LON = 15, 16

GPS_CANDIDATES = [
    '/dev/ttyGPS',
    '/dev/serial/by-id/'
    'usb-u-blox_AG_-_www.u-blox.com_u-blox_7_-_GPS_GNSS_Receiver-if00',
]
GPS_PORT = GPS_CANDIDATES
GPS_BAUD = 9600
GPS_BAUDS = (GPS_BAUD, 4800, 38400, 115200)
GPS_MAX_AGE = 10           # 定位/高程分别计时，过期后不能继续当作实时测量
GPS_PROBE_SEC = 8
GPS_RETRY_SEC = 10
MAX_NMEA_BYTES = 256


def sensor_time(value):
    for fmt in ('%m/%d/%Y %H:%M:%S.%f', '%m/%d/%Y %H:%M:%S'):
        try:
            return datetime.datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise ValueError('invalid instrument timestamp')


def gas_value(value):
    """保持远程版的缺测处理及有效浓度精度。"""
    if value.lower() in ('', 'nan', 'no data'):
        return 'nan'
    return value if math.isfinite(float(value)) else 'nan'


def coordinate(value):
    value = value.strip()
    try:
        if math.isfinite(float(value)):
            return value
    except ValueError:
        pass
    return 'nan'


def dm2dd(dm, hemi):
    """NMEA 度分转十进制度，拒绝错误半球、越界坐标及非有限值。"""
    hemi = hemi.upper()
    if hemi not in ('N', 'S', 'E', 'W'):
        return None
    try:
        value = float(dm)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    deg = int(value // 100)
    minutes = value - deg * 100
    limit = 90 if hemi in ('N', 'S') else 180
    if minutes >= 60 or deg > limit or (deg == limit and minutes != 0):
        return None
    dd = deg + minutes / 60
    return -dd if hemi in ('S', 'W') else dd


def nmea_valid(line):
    if not line.startswith('$') or '*' not in line:
        return False
    body, chk = line[1:].rsplit('*', 1)
    if len(chk) != 2:
        return False
    calc = 0
    for char in body:
        calc ^= ord(char)
    try:
        return int(chk, 16) == calc
    except ValueError:
        return False


def parse_gga(p):
    """GGA 高程为 MSL 海拔（米），不是椭球高或离地高度。"""
    if len(p) < 11 or p[6] not in ('1', '2', '3', '4', '5', '6', '7', '8'):
        return None
    if p[3] not in ('N', 'S') or p[5] not in ('E', 'W'):
        return None
    lat, lon = dm2dd(p[2], p[3]), dm2dd(p[4], p[5])
    if lat is None or lon is None:
        return None
    alt = coordinate(p[9]) if p[10] == 'M' else 'nan'
    return lat, lon, None if alt == 'nan' else float(alt)


def parse_rmc(p):
    if len(p) < 7 or p[2] != 'A':
        return None
    if p[4] not in ('N', 'S') or p[6] not in ('E', 'W'):
        return None
    lat, lon = dm2dd(p[3], p[4]), dm2dd(p[5], p[6])
    return None if lat is None or lon is None else (lat, lon, None)


class GpsReader(threading.Thread):
    def __init__(self, port=None):
        super().__init__(daemon=True)
        self.port = port
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.lat = self.lon = self.alt = None
        self.fix_time = self.alt_time = None
        self.sats = 0

    def stop(self):
        self.stop_event.set()

    def get(self):
        now = time.monotonic()
        with self.lock:
            if self.fix_time is None or now - self.fix_time >= GPS_MAX_AGE:
                return None, None, None
            alt = self.alt if self.alt_time is not None and now - self.alt_time < GPS_MAX_AGE else None
            return self.lat, self.lon, alt

    def status(self):
        with self.lock:
            fresh = self.fix_time is not None and time.monotonic() - self.fix_time < GPS_MAX_AGE
            return f'卫星{self.sats} 定位' + ('正常' if fresh else '缺失/过期')

    def _clear(self):
        with self.lock:
            self.lat = self.lon = self.alt = None
            self.fix_time = self.alt_time = None
            self.sats = 0

    def _candidate_ports(self):
        """只探测指定别名或 USB u-blox；绝不盲扫气体口/LoRa 口。"""
        infos = list(list_ports.comports())
        wish = [self.port] if isinstance(self.port, str) else (self.port or [])
        candidates = [p for p in wish if os.path.exists(p)]
        candidates.extend(p.device for p in infos if p.vid == 0x1546)
        reserved = {os.path.realpath(SRC), os.path.realpath(DST)}
        seen, result = set(), []
        for path in candidates:
            real = os.path.realpath(path)
            info = next((p for p in infos if os.path.realpath(p.device) == real), None)
            if real in reserved or real in seen or (info and info.vid not in (None, 0x1546)):
                continue
            seen.add(real)
            result.append(path)
        return result

    def _consume(self, line):
        """有效的无定位语句也能识别设备；丢失定位立即清除旧值。"""
        if not nmea_valid(line):
            return False
        p = line[1:].split('*', 1)[0].split(',')
        kind = p[0][-3:]
        if kind not in ('GGA', 'RMC'):
            return False
        if (kind == 'GGA' and len(p) > 6 and p[6] == '0') or (
                kind == 'RMC' and len(p) > 2 and p[2] == 'V'):
            self._clear()
            return True
        result = parse_gga(p) if kind == 'GGA' else parse_rmc(p)
        if result is not None:
            now = time.monotonic()
            with self.lock:
                self.lat, self.lon = result[:2]
                self.fix_time = now
                if kind == 'GGA':
                    self.alt = result[2]
                    self.alt_time = now if self.alt is not None else None
                    self.sats = int(p[7]) if p[7].isdigit() else 0
        return True

    def _read_port(self, port, baud):
        with serial.Serial(port, baud, timeout=1, exclusive=True) as stream:
            pending, discarding = b'', False
            last_sentence = time.monotonic()
            locked = False
            while not self.stop_event.is_set():
                timeout = GPS_MAX_AGE if locked else GPS_PROBE_SEC
                if time.monotonic() - last_sentence >= timeout:
                    return
                raw = stream.readline(MAX_NMEA_BYTES + 1)
                if not raw:
                    continue
                if discarding:
                    discarding = not raw.endswith(b'\n')
                    continue
                pending += raw
                if len(pending) > MAX_NMEA_BYTES:
                    pending, discarding = b'', not raw.endswith(b'\n')
                    continue
                if not pending.endswith(b'\n'):
                    continue
                line, pending = pending, b''
                try:
                    valid = self._consume(line.decode('ascii').strip())
                except UnicodeDecodeError:
                    valid = False
                if valid:
                    last_sentence = time.monotonic()
                    if not locked:
                        locked = True
                        logging.info('GPS detected %s @%d', port, baud)
                        print(f'[GPS] 已识别 {port}@{baud}', flush=True)

    def run(self):
        while not self.stop_event.is_set():
            try:
                for port in self._candidate_ports():
                    for baud in GPS_BAUDS:
                        if self.stop_event.is_set():
                            return
                        try:
                            self._read_port(port, baud)
                        except (serial.SerialException, OSError) as e:
                            logging.info('GPS %s @%d: %s', port, baud, e)
                        finally:
                            self._clear()
            except (serial.SerialException, OSError) as e:
                logging.warning('GPS discovery: %s', e)
            # 没插 GPS 时此线程等待，气体收发线程照常运行；支持稍后插入。
            self.stop_event.wait(GPS_RETRY_SEC)


def open_port(path):
    while True:
        try:
            stream = serial.Serial(path, BAUD, timeout=1, write_timeout=2)
            logging.info('opened %s', path)
            return stream
        except (serial.SerialException, OSError) as e:
            logging.warning('%s: %s, retry 2s', path, e)
            time.sleep(2)


def parse(line):
    try:
        txt = line.decode('ascii').strip()
    except UnicodeDecodeError:
        logging.warning('non-ASCII source line skipped')
        return None
    if not txt:
        return None
    p = [x.strip() for x in txt.split(',')]
    if len(p) != IDX_LON + 1:
        logging.warning('source field count is %d, expected 17', len(p))
        return None
    if p[IDX_TS] == 'Time Stamp':
        return None
    ts, ch4, h2o, c2h6 = p[IDX_TS], p[IDX_CH4], p[IDX_H2O], p[IDX_C2H6]
    try:
        sensor_time(ts)
        ch4, h2o, c2h6 = (gas_value(v) for v in (ch4, h2o, c2h6))
    except ValueError:
        logging.warning('invalid source timestamp or gas value: %s', txt)
        return None
    return ts, ch4, h2o, c2h6, coordinate(p[IDX_LAT]), coordinate(p[IDX_LON])


def fmt(value, nd=6):
    return f'{value:.{nd}f}' if value is not None and math.isfinite(value) else 'nan'


def build_frame(seq, reading, gps):
    ts, ch4, h2o, c2h6, lat, lon = reading
    gps_lat, gps_lon, alt = gps.get() if gps else (None, None, None)
    if gps_lat is not None and gps_lon is not None:
        lat, lon = fmt(gps_lat), fmt(gps_lon)
    frame = '$MIRA,%d,%s,%s,%s,%s,%s,%s,%s\n' % (
        seq, ts, ch4, h2o, c2h6, lat, lon, fmt(alt, 1))
    payload = frame.encode('ascii')
    if len(payload) > MAX_FRAME_BYTES:
        logging.warning('frame exceeds 121 bytes, skipped: %s', frame.strip())
        return None  # 不截断测量值，不产生缺列或错误的有效数字
    return payload


def close_ports(*ports):
    for port in ports:
        if port is not None:
            try:
                port.close()
            except Exception:
                logging.exception('serial close failed')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--gps-test', action='store_true')
    mode.add_argument('--no-gps', action='store_true')
    args = parser.parse_args(argv)
    logging.basicConfig(
        handlers=[RotatingFileHandler(os.path.join(tempfile.gettempdir(), 'ch4_bridge.log'),
                                      maxBytes=1024 * 1024, backupCount=2)],
        level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    src = dst = None
    gps = None if args.no_gps else GpsReader(GPS_PORT)
    seq, pending, discarding = 0, b'', False
    try:
        if gps:
            gps.start()
        if args.gps_test:
            print('仅测试 GPS，Ctrl+C 停止', flush=True)
            while True:
                lat, lon, alt = gps.get()
                print(f'纬度 {fmt(lat)} 经度 {fmt(lon)} 高程 {fmt(alt, 1)} [{gps.status()}]', flush=True)
                time.sleep(1)
        src = open_port(SRC)
        dst = None if args.dry_run else open_port(DST)
        print('DRY RUN 只解析不发送' if args.dry_run else '桥接已启动', flush=True)
        while True:
            try:
                line = src.readline(MAX_SOURCE_BYTES + 1)
                if not line:
                    continue
                if discarding:
                    discarding = not line.endswith(b'\n')
                    continue
                pending += line
                if len(pending) > MAX_SOURCE_BYTES:
                    logging.warning('oversized source line discarded')
                    pending, discarding = b'', not line.endswith(b'\n')
                    continue
                if not pending.endswith(b'\n'):
                    continue
                line, pending = pending, b''
                reading = parse(line)
                if reading is None:
                    continue
                payload = build_frame(seq, reading, gps)
                if payload is None:
                    continue
                print(payload.decode('ascii'), end='', flush=True)
                if dst:
                    if dst.write(payload) != len(payload):
                        raise serial.SerialException('incomplete LoRa serial write')
                    dst.flush()
                seq += 1
            except (serial.SerialException, OSError) as e:
                logging.error('serial error: %s', e)
                pending, discarding = b'', False
                close_ports(src, dst)
                src = dst = None
                src = open_port(SRC)
                dst = None if args.dry_run else open_port(DST)
    except KeyboardInterrupt:
        pass
    finally:
        close_ports(src, dst)
        if gps:
            gps.stop()
            gps.join(timeout=2)


if __name__ == '__main__':
    main()
