#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
跑在 Pico 上：读本机 RS232 数据口 → 提取时间、三种气体和经纬度 → 写 LoRa 串口发出
帧格式: $MIRA,<序号>,<时间戳>,<CH4>,<H2O>,<C2H6>,<Latitude>,<Longitude>
经纬度缺失（nan / No Data / 空值）统一发送 nan
带序号是为了地面站能统计丢包率（干扰测试要用）

用法：
  python3 pico_ch4_bridge.py --dry-run    # 只解析打印，不发送（先验证列对不对）
  python3 pico_ch4_bridge.py              # 正式运行
"""

import serial
import time
import sys
import logging
import math
import datetime
from logging.handlers import RotatingFileHandler

SRC = '/dev/ttyUSB_RS232'    # Pico 数据口（用别名，防止端口漂移）
DST = '/dev/ttyUSB_LORA'      # LoRa 模块口（udev 规则绑定的固定名）
BAUD = 9600
MAX_SOURCE_BYTES = 1024     # 完整仪器行通常不到 200 字节

# 列索引（0 起）：0=TimeStamp, 6=CH4, 7=H2O, 8=C2H6, 15=Latitude, 16=Longitude
IDX_TS, IDX_CH4, IDX_H2O, IDX_C2H6 = 0, 6, 7, 8
IDX_LAT, IDX_LON = 15, 16

DRY_RUN = '--dry-run' in sys.argv


def sensor_time(value):
    for fmt in ('%m/%d/%Y %H:%M:%S.%f', '%m/%d/%Y %H:%M:%S'):
        try:
            return datetime.datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise ValueError('invalid instrument timestamp')


def gas_value(value):
    """缺测值不丢整行；保留有效浓度（包括 0 和高值）的原始精度。"""
    if value.lower() in ('', 'nan', 'no data'):
        return 'nan'
    number = float(value)  # 其他非数字视为格式损坏，不当作测量值
    return value if math.isfinite(number) else 'nan'


def coordinate(value):
    """保留有效坐标的原始精度；无定位或无效值统一为 nan。"""
    value = value.strip()
    try:
        if math.isfinite(float(value)):
            return value
    except ValueError:
        pass
    return 'nan'


def open_port(path):
    while True:
        try:
            s = serial.Serial(path, BAUD, timeout=1, write_timeout=2)
            logging.info('opened %s', path)
            return s
        except Exception as e:
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
    if len(p) != IDX_LON + 1:  # 仪器完整行应为 17 列，拒绝缺列或拼接错位的行
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
    lat = coordinate(p[IDX_LAT])
    lon = coordinate(p[IDX_LON])
    return ts, ch4, h2o, c2h6, lat, lon


def main():
    logging.basicConfig(
        handlers=[RotatingFileHandler('/tmp/ch4_bridge.log', maxBytes=1024 * 1024,
                                      backupCount=2)],
        level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    src = dst = None
    seq = 0
    pending = b''
    discarding = False
    try:
        src = open_port(SRC)
        dst = None if DRY_RUN else open_port(DST)
        print('DRY RUN 只解析不发送' if DRY_RUN else '桥接已启动', flush=True)
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
                    pending = b''
                    discarding = not line.endswith(b'\n')
                    continue
                if not pending.endswith(b'\n'):
                    continue  # readline 超时可能只返回半行，等到换行再解析
                line, pending = pending, b''
                r = parse(line)
                if r is None:
                    continue
                frame = "$MIRA,%d,%s,%s,%s,%s,%s,%s\n" % ((seq,) + r)
                payload = frame.encode('ascii')
                if len(payload) > 121:  # LoRa 单帧硬上限
                    logging.warning('frame exceeds 121 bytes, skipped: %s', frame.strip())
                    continue  # 不截断，否则可能把经纬度截成错误的有效数字
                print(frame, end='', flush=True)
                if dst:
                    if dst.write(payload) != len(payload):
                        raise serial.SerialException('incomplete LoRa serial write')
                    dst.flush()
                seq += 1
            except (serial.SerialException, OSError) as e:
                logging.error('serial error: %s', e)
                pending = b''
                discarding = False
                for port in (src, dst):
                    if port is not None:
                        try:
                            port.close()
                        except Exception:
                            logging.exception('serial close failed')
                src = dst = None
                src = open_port(SRC)
                dst = None if DRY_RUN else open_port(DST)
    except KeyboardInterrupt:
        pass
    finally:
        for port in (src, dst):
            if port is not None:
                try:
                    port.close()
                except Exception:
                    logging.exception('serial close failed')


if __name__ == '__main__':
    main()
