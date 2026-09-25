#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
跑在 Pico 上：读本机 RS232 数据口 → 提取 TimeStamp/CH4/H2O/C2H6 → 写 LoRa 串口发出
帧格式: $MIRA,<序号>,<时间戳>,<CH4>,<H2O>,<C2H6>   约 40~50 字节
带序号是为了地面站能统计丢包率（干扰测试要用）

用法：
  python3 pico_ch4_bridge.py --dry-run    # 只解析打印，不发送（先验证列对不对）
  python3 pico_ch4_bridge.py              # 正式运行
"""

import serial
import time
import sys
import logging

SRC = '/dev/ttyUSB_RS232'    # Pico 数据口（用别名，防止端口漂移）
DST = '/dev/ttyUSB_LORA'      # LoRa 模块口（udev 规则绑定的固定名）
BAUD = 9600

# 列索引（0 起）：0=TimeStamp, 6=CH4, 7=H2O, 8=C2H6
IDX_TS, IDX_CH4, IDX_H2O, IDX_C2H6 = 0, 6, 7, 8

DRY_RUN = '--dry-run' in sys.argv
logging.basicConfig(filename='/tmp/ch4_bridge.log', level=logging.INFO)


def open_port(path):
    while True:
        try:
            s = serial.Serial(path, BAUD, timeout=1)
            logging.info('opened %s', path)
            return s
        except Exception as e:
            logging.warning('%s: %s, retry 2s', path, e)
            time.sleep(2)


def parse(line):
    txt = line.decode('ascii', errors='ignore').strip()
    if not txt:
        return None
    p = [x.strip() for x in txt.split(',')]
    if len(p) <= IDX_C2H6:
        return None
    ts, ch4, h2o, c2h6 = p[IDX_TS], p[IDX_CH4], p[IDX_H2O], p[IDX_C2H6]
    try:
        float(ch4)          # CH4 不是数字 = 表头行，跳过
    except ValueError:
        return None
    return ts, ch4, h2o, c2h6


src = open_port(SRC)
dst = None if DRY_RUN else open_port(DST)
print('DRY RUN 只解析不发送' if DRY_RUN else '桥接已启动', flush=True)

seq = 0
while True:
    try:
        line = src.readline()
        if not line:
            continue
        r = parse(line)
        if r is None:
            continue
        frame = "$MIRA,%d,%s,%s,%s,%s\n" % ((seq,) + r)
        if len(frame) > 121:          # LoRa 单帧硬上限
            frame = frame[:120] + '\n'
        print(frame, end='', flush=True)
        if dst:
            dst.write(frame.encode())
            dst.flush()
        seq += 1
    except KeyboardInterrupt:
        break
    except Exception as e:
        logging.error(str(e))
        src = open_port(SRC)
        if not DRY_RUN:
            dst = open_port(DST)
