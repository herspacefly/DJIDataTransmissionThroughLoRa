#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MIRA Pico 甲烷监测地面站 (Windows)
替代 PuTTY 的专用接收软件：三气体实时曲线 / 数值卡 / 经纬度 / 自动存 CSV / 丢包统计

【数据输出】
  自动在程序所在文件夹下创建 data/ 子目录
  每 30 分钟自动切换到新文件，文件名带起始时刻，例如：
      data/mira_20260925_183000.csv

【运行前】
  pip install pyserial matplotlib
  内置交互终端还需：pip install "pywinpty>=2,<3" "pyte>=0.8,<0.9"
  注意：运行本程序前请先关闭 PuTTY，串口不能被两个程序同时占用
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
import serial
import serial.tools.list_ports
import threading
import queue
import time
import csv
import os
import sys
import datetime
import math
import re
from tkinter import font as tkfont

TERMINAL_UNAVAILABLE_REASON = ''
try:
    from importlib.metadata import version
    from winpty import PtyProcess
    import pyte
    if version('pywinpty').split('.')[0] != '2' or version('pyte').split('.')[:2] != ['0', '8']:
        raise RuntimeError('pywinpty 或 pyte 版本不兼容')
    HAS_PTY = os.name == 'nt'
    if not HAS_PTY:
        TERMINAL_UNAVAILABLE_REASON = '交互终端仅支持 Windows'
except Exception as e:
    # 可选模块可能因缺包、DLL 加载或版本问题失败，均不阻止监测启动。
    HAS_PTY = False
    TERMINAL_UNAVAILABLE_REASON = str(e)

TERMINAL_UNAVAILABLE_MESSAGE = (
    '终端不可用：交互终端依赖尚未就绪。\n'
    '串口接收、气体数值、浓度曲线和 CSV 保存不受终端依赖影响。\n'
    '请在启动地面站的 Python 环境中安装：\n'
    'python -m pip install "pywinpty>=2,<3" "pyte>=0.8,<0.9"\n'
    '安装完成后重启地面站。\n'
    f'原因：{TERMINAL_UNAVAILABLE_REASON}\n')

try:
    import matplotlib
    matplotlib.use('TkAgg')
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
    HAS_MPL = True
except Exception:
    HAS_MPL = False

FONT = ('Microsoft YaHei', 9)
FONT_BIG = ('Microsoft YaHei', 13, 'bold')
MAX_POINTS = 300            # 曲线最多显示的点数
ROTATE_SEC = 30 * 60        # CSV 轮转间隔：30 分钟
MAX_FRAME_BYTES = 121
POLL_BATCH = 100            # 每轮最多处理 100 帧，给界面事件留出时间

# 三种气体的显示配置：(键, 中文名, 单位, 颜色)
GASES = [
    ('CH4', 'CH4 甲烷', 'ppm', '#d62728'),
    ('H2O', 'H2O 水汽', 'ppm', '#1f77b4'),
    ('C2H6', 'C2H6 乙烷', 'ppb', '#2ca02c'),
]


def app_dir():
    """程序所在文件夹（兼容打包成 exe 的情况）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def coordinate(value):
    """兼容 nan、No Data、空值和旧帧；CSV 中缺失坐标统一为 nan。"""
    value = value.strip()
    try:
        if math.isfinite(float(value)):
            return value
    except ValueError:
        pass
    return 'nan'


def sensor_time(value):
    for fmt in ('%m/%d/%Y %H:%M:%S.%f', '%m/%d/%Y %H:%M:%S'):
        try:
            return datetime.datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise ValueError('invalid instrument timestamp')


def gas_value(value):
    if value.lower() in ('', 'nan', 'no data'):
        return 'nan'
    return value if math.isfinite(float(value)) else 'nan'


class EmbeddedTerminal(ttk.Frame):
    """Windows 伪终端 + VT 屏幕；SSH 的密码、提示符和按键均经过真实终端。"""
    def __init__(self, parent):
        super().__init__(parent)
        self.proc = None
        self.events = queue.Queue()
        self.generation = 0
        self.closed = False
        self.resize_job = None
        self.screen = None
        self.stream = None
        self.status = tk.StringVar(value='未启动' if HAS_PTY else '终端不可用')
        self.controls = []
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X)
        local_button = ttk.Button(bar, text='本地终端', command=self.start_local)
        local_button.pack(side=tk.LEFT)
        self.controls.append(local_button)
        for label, key in [('Ctrl+C 中断', '\x03'), ('Ctrl+D', '\x04'), ('Tab', '\t')]:
            button = ttk.Button(bar, text=label, command=lambda k=key: self.write(k))
            button.pack(side=tk.LEFT, padx=3)
            self.controls.append(button)
        ttk.Label(bar, textvariable=self.status).pack(side=tk.RIGHT)
        self.font = tkfont.Font(family='Consolas', size=10)
        self.output = scrolledtext.ScrolledText(
            self, font=self.font, height=24, wrap=tk.NONE, bg='#1e1e1e',
            fg='#d4d4d4', insertbackground='white', state=tk.DISABLED)
        self.output.pack(fill=tk.BOTH, expand=True)
        self.output.tag_configure('cursor', background='#666666')
        self.output.bind('<Key>', self.on_key)
        self.output.bind('<<Paste>>', self.paste)
        self.output.bind('<Configure>', self.schedule_resize)
        entry_row = ttk.Frame(self)
        entry_row.pack(fill=tk.X, pady=4)
        ttk.Label(entry_row, text='命令 / 输入:').pack(side=tk.LEFT)
        self.entry = ttk.Entry(entry_row, font=self.font)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        self.entry.bind('<Return>', self.send_line)
        self.entry.bind('<Control-c>', lambda e: self.write('\x03') if not self.entry.selection_present() else None)
        send_button = ttk.Button(entry_row, text='发送', command=self.send_line)
        send_button.pack(side=tk.LEFT)
        self.controls.extend((send_button, self.entry))
        self.hidden_input = tk.BooleanVar(value=False)
        ttk.Checkbutton(entry_row, text='隐藏输入', variable=self.hidden_input,
                        command=self.update_input_mode).pack(side=tk.LEFT, padx=4)
        ttk.Label(self, text='可直接点击终端输入；Ctrl+Shift+C 复制，Ctrl+V 粘贴。SSH 中 exit 返回连接结束状态。',
                  font=FONT, foreground='gray').pack(anchor=tk.W)
        if HAS_PTY:
            self.message('点击「SSH 登录」进入 Pico，或点击「本地终端」运行本地命令。\n')
        else:
            for control in self.controls:
                control.configure(state=tk.DISABLED)
            self.message(TERMINAL_UNAVAILABLE_MESSAGE)
        self.poll_job = self.after(60, self.poll)

    def message(self, text):
        if self.proc is not None:
            # 应用提示不插进 VT 数据流，避免改变 SSH 的屏幕和光标位置。
            self.status.set(text.strip())
            return
        if self.stream:
            self.stream.feed(text.replace('\n', '\r\n'))
            self.render()
        else:
            self.output.configure(state=tk.NORMAL)
            self.output.insert(tk.END, text)
            self.output.configure(state=tk.DISABLED)
            self.output.see(tk.END)

    def dimensions(self):
        columns = max(40, (self.output.winfo_width() - 24) // max(1, self.font.measure('M')))
        rows = max(10, (self.output.winfo_height() - 8) // max(1, self.font.metrics('linespace')))
        return rows, columns

    def start(self, argv, label):
        if not HAS_PTY:
            self.status.set('终端不可用')
            self.message(TERMINAL_UNAVAILABLE_MESSAGE)
            return False
        self.stop()
        self.generation += 1
        generation = self.generation
        rows, columns = self.dimensions()
        self.screen = pyte.HistoryScreen(columns, rows, history=1000)
        self.screen.write_process_input = self.write
        self.stream = pyte.Stream(self.screen)
        self.entry.delete(0, tk.END)
        self.hidden_input.set(False)
        self.update_input_mode()
        try:
            env = dict(os.environ, TERM='xterm-256color')
            self.proc = PtyProcess.spawn(argv, cwd=app_dir(), env=env, dimensions=(rows, columns))
        except Exception as e:
            self.status.set('启动失败')
            self.message(f'终端启动失败: {e}\n')
            return False
        self.status.set(label)
        threading.Thread(target=self.read_output, args=(self.proc, generation), daemon=True).start()
        self.render()
        self.output.focus_set()
        return True

    def start_local(self):
        return self.start(['cmd.exe', '/Q'], '本地 Windows 终端')

    def read_output(self, proc, generation):
        try:
            while True:
                data = proc.read()
                if data:
                    self.events.put((generation, 'data', data))
        except EOFError:
            pass
        except Exception as e:
            self.events.put((generation, 'error', str(e)))
        finally:
            self.events.put((generation, 'exit', None))

    def write(self, text):
        if self.proc is None:
            return 'break'
        try:
            self.proc.write(text)
        except Exception as e:
            self.message(f'终端输入失败: {e}\n')
        return 'break'

    def send_line(self, event=None):
        text = self.entry.get()
        self.entry.delete(0, tk.END)
        self.write(text + '\r')  # 空输入也应传送 Enter；不把密码或命令额外写入日志
        return 'break'

    def send_command(self, command):
        if self.proc is None and not self.start_local():
            return
        self.write(command + '\r')

    def on_key(self, event):
        control = bool(event.state & 0x4)
        shift = bool(event.state & 0x1)
        key = event.keysym
        if control and key.lower() == 'c' and shift:
            try:
                self.clipboard_clear()
                self.clipboard_append(self.output.get(tk.SEL_FIRST, tk.SEL_LAST))
            except tk.TclError:
                pass
            return 'break'
        if control and key.lower() == 'v':
            return self.paste()
        if control and len(key) == 1 and 'a' <= key.lower() <= 'z':
            return self.write(chr(ord(key.lower()) - ord('a') + 1))
        mapping = {'Return': '\r', 'BackSpace': '\x7f', 'Tab': '\t', 'Escape': '\x1b',
                   'Up': '\x1b[A', 'Down': '\x1b[B', 'Right': '\x1b[C', 'Left': '\x1b[D',
                   'Home': '\x1b[H', 'End': '\x1b[F', 'Delete': '\x1b[3~',
                   'Prior': '\x1b[5~', 'Next': '\x1b[6~'}
        if key in mapping:
            return self.write(mapping[key])
        if event.char and event.char.isprintable():
            return self.write(event.char)
        return 'break'

    def paste(self, event=None):
        try:
            text = self.clipboard_get().replace('\r\n', '\n').replace('\n', '\r')
            self.write(text)
        except tk.TclError:
            pass
        return 'break'

    def update_input_mode(self):
        self.entry.configure(show='*' if self.hidden_input.get() else '')

    def render(self):
        if not self.screen:
            return
        # 缩小窗口后 pyte 可能暂时保留旧光标位置。
        self.screen.cursor.y = min(max(0, self.screen.cursor.y), self.screen.lines - 1)
        self.screen.cursor.x = min(max(0, self.screen.cursor.x), self.screen.columns - 1)
        history = [''.join(line[x].data for x in range(self.screen.columns))
                   for line in self.screen.history.top]
        lines = history + self.screen.display
        self.output.configure(state=tk.NORMAL)
        self.output.delete('1.0', tk.END)
        self.output.insert('1.0', '\n'.join(lines))
        # VT 光标用屏幕列数，Tk 用字符数；中文等双宽字符需要换算。
        cursor_column = len(''.join(self.screen.buffer[self.screen.cursor.y][x].data
                                  for x in range(self.screen.cursor.x)))
        cursor = f'{len(history) + self.screen.cursor.y + 1}.{cursor_column}'
        self.output.tag_add('cursor', cursor, cursor + '+1c')
        self.output.configure(state=tk.DISABLED)
        self.output.see(cursor)
        prompt = self.screen.display[self.screen.cursor.y].rstrip().lower()
        if re.search(r'(password|passphrase|密码).*[:：]\s*$', prompt):
            self.hidden_input.set(True)
            self.update_input_mode()
        elif self.hidden_input.get() and (prompt.endswith(('$', '#', '>'))):
            self.hidden_input.set(False)
            self.update_input_mode()

    def poll(self):
        changed = False
        try:
            for _ in range(POLL_BATCH):
                generation, kind, data = self.events.get_nowait()
                if generation != self.generation:
                    continue
                if kind == 'data':
                    self.stream.feed(data)
                    changed = True
                elif kind == 'error':
                    self.message(f'终端读取失败: {data}\n')
                elif kind == 'exit':
                    self.status.set('会话已结束；可重新登录或打开本地终端')
        except queue.Empty:
            pass
        except Exception as e:
            self.status.set(f'终端显示异常: {e}')
        finally:
            try:
                if changed:
                    self.render()
            except Exception as e:
                self.status.set(f'终端显示异常: {e}')
            if not self.closed:
                self.poll_job = self.after(60, self.poll)

    def schedule_resize(self, event=None):
        if self.resize_job:
            self.after_cancel(self.resize_job)
        self.resize_job = self.after(150, self.resize)

    def resize(self):
        self.resize_job = None
        if self.proc and self.screen:
            rows, columns = self.dimensions()
            try:
                self.proc.setwinsize(rows, columns)
                self.screen.resize(lines=rows, columns=columns)
                self.render()
            except Exception:
                pass  # 会话恰好退出时无需重试调整窗口

    def stop(self):
        self.generation += 1  # 已关闭会话的输出不能写入新会话
        proc, self.proc = self.proc, None
        if proc:
            try:
                proc.close(force=True)
            except Exception:
                pass

    def close(self):
        self.closed = True
        self.after_cancel(self.poll_job)
        if self.resize_job:
            self.after_cancel(self.resize_job)
        self.stop()


class GroundStation:
    def __init__(self, root):
        self.root = root
        self.root.title('MIRA Pico 甲烷监测地面站')
        self.root.geometry('1040x760')

        self.ser = None
        self.reader = None
        self.reader_stop = None
        self.q = queue.Queue()
        self.running = False

        # 统计
        self.rx_count = 0
        self.rx_bytes = 0
        self.first_seq = None
        self.last_seq = None
        self.numbered_rx_count = 0
        self.last_pico_time = None
        self.t_start = None
        self.timestamps = []
        self.series = {k: [] for k, _, _, _ in GASES}   # 各气体数值序列
        self.time_data = []
        self.plot_dirty = False

        # CSV
        self.csv_file = None
        self.csv_writer = None
        self.csv_path = None
        self.csv_opened_at = 0

        # 终端 / 远程控制
        self.terminal_windows = []

        # 标签页：监测 / 终端
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill=tk.BOTH, expand=True)

        self.tab_main = ttk.Frame(self.nb)
        self.tab_term = ttk.Frame(self.nb)
        self.nb.add(self.tab_main, text='  监测  ')
        self.nb.add(self.tab_term, text='  终端 / 远程控制  ' if HAS_PTY else '  终端 / 远程控制（不可用）  ')

        self.build_ui(self.tab_main)
        self.build_terminal(self.tab_term)

        self.refresh_ports()
        self.root.after(100, self.poll)
        self.root.protocol('WM_DELETE_WINDOW', self.on_close)

    # ---------------- UI ----------------
    def build_ui(self, parent):
        # ---- 外层滚动容器：内容超出窗口时可上下滚动 ----
        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True)

        self.scroll_canvas = tk.Canvas(wrap, highlightthickness=0)
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL,
                            command=self.scroll_canvas.yview)
        self.scroll_canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.scroll_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        inner = ttk.Frame(self.scroll_canvas)
        self.scroll_win = self.scroll_canvas.create_window(
            (0, 0), window=inner, anchor='nw')
        inner.bind('<Configure>',
                   lambda e: self.scroll_canvas.configure(
                       scrollregion=self.scroll_canvas.bbox('all')))
        self.scroll_canvas.bind('<Configure>', self._on_scroll_canvas_resize)

        # 后续所有控件改为放进可滚动区域
        parent = inner

        top = ttk.Frame(parent, padding=6)
        top.pack(fill=tk.X)

        ttk.Label(top, text='串口:', font=FONT).pack(side=tk.LEFT)
        self.port_var = tk.StringVar()
        # 未设置 state='readonly'，所以这个下拉框可以直接键盘输入
        self.port_cb = ttk.Combobox(top, textvariable=self.port_var,
                                    width=18, font=FONT)
        self.port_cb.pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text='刷新', command=self.refresh_ports).pack(side=tk.LEFT, padx=2)
        ttk.Label(top, text='(可直接输入，如 COM7)', font=FONT,
                  foreground='gray').pack(side=tk.LEFT, padx=2)

        ttk.Label(top, text='波特率:', font=FONT).pack(side=tk.LEFT, padx=(10, 0))
        self.baud_var = tk.StringVar(value='9600')
        ttk.Combobox(top, textvariable=self.baud_var, width=8, font=FONT,
                     values=['1200', '2400', '4800', '9600', '19200',
                             '38400', '57600', '115200']).pack(side=tk.LEFT, padx=4)

        self.btn_conn = ttk.Button(top, text='连接', command=self.toggle_conn)
        self.btn_conn.pack(side=tk.LEFT, padx=6)

        self.save_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text='自动存 CSV', variable=self.save_var,
                        command=self.toggle_csv).pack(side=tk.LEFT, padx=6)

        self.status = tk.StringVar(value='未连接')
        ttk.Label(top, textvariable=self.status, font=FONT,
                  foreground='gray').pack(side=tk.RIGHT)

        if not HAS_PTY:
            ttk.Label(parent, text='终端不可用：依赖尚未就绪。数据接收、数值、曲线和 CSV 保存可正常使用。'
                                  '安装终端依赖后重启程序。',
                      font=FONT, foreground='#a05200', wraplength=950).pack(fill=tk.X, padx=8)

        mid = ttk.Frame(parent, padding=6)
        mid.pack(fill=tk.BOTH, expand=True)

        # 左侧数值卡
        left = ttk.Frame(mid)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))

        self.vals = {}
        for key, cn, unit, color in GASES:
            card = ttk.LabelFrame(left, text=cn, padding=8)
            card.pack(fill=tk.X, pady=4)
            v = tk.StringVar(value='--')
            tk.Label(card, textvariable=v, font=FONT_BIG, fg=color).pack(anchor=tk.W)
            tk.Label(card, text=unit, font=FONT, fg='gray').pack(anchor=tk.W)
            self.vals[key] = v

        info = ttk.LabelFrame(left, text='状态', padding=8)
        info.pack(fill=tk.X, pady=4)
        self.info_vars = {}
        for k in ('Pico时间', '纬度', '经度', '接收帧数', '丢包率', '速率', '运行时长', '当前文件'):
            row = ttk.Frame(info)
            row.pack(fill=tk.X)
            ttk.Label(row, text=k + ':', font=FONT, width=9).pack(side=tk.LEFT)
            v = tk.StringVar(value='--')
            ttk.Label(row, textvariable=v, font=FONT).pack(side=tk.LEFT)
            self.info_vars[k] = v

        # 右侧三通道曲线
        right = ttk.LabelFrame(mid, text='实时曲线', padding=4)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        if HAS_MPL:
            self.fig = Figure(figsize=(5.5, 6), dpi=95)
            self.axes = {}
            self.lines = {}
            for i, (key, cn, unit, color) in enumerate(GASES):
                ax = self.fig.add_subplot(len(GASES), 1, i + 1)
                ax.set_ylabel(f'{key} ({unit})', fontsize=8)
                ax.grid(True, alpha=0.3)
                ax.tick_params(labelsize=7)
                ln, = ax.plot([], [], color=color, linewidth=1.2)
                self.axes[key] = ax
                self.lines[key] = ln
            self.axes[GASES[-1][0]].set_xlabel('Time (s)', fontsize=8)
            self.fig.tight_layout()
            self.canvas = FigureCanvasTkAgg(self.fig, master=right)
            self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        else:
            tk.Label(right, text='未安装 matplotlib\n\n如需曲线请运行:\npip install matplotlib',
                     font=FONT, fg='gray', justify=tk.CENTER).pack(expand=True)

        # 原始数据区：固定高度 280px（窗口高 760 的约 37%），曲线区相应变小
        bottom_wrap = tk.Frame(parent, height=280)
        bottom_wrap.pack(fill=tk.X, expand=False, padx=8, pady=(0, 4))
        bottom_wrap.pack_propagate(False)

        bottom = ttk.LabelFrame(bottom_wrap, text='原始数据', padding=4)
        bottom.pack(fill=tk.BOTH, expand=True)
        self.log = scrolledtext.ScrolledText(bottom, font=('Consolas', 9))
        self.log.pack(fill=tk.BOTH, expand=True)

        bar = ttk.Frame(parent, padding=4)
        bar.pack(fill=tk.X)
        ttk.Button(bar, text='清空日志',
                   command=lambda: self.log.delete('1.0', tk.END)).pack(side=tk.LEFT)
        ttk.Button(bar, text='导出日志', command=self.export_log).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text='打开数据文件夹',
                   command=self.open_data_dir).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text='重置统计', command=self.reset_stats).pack(side=tk.LEFT)

        # 滚轮滚动（跳过文本框，保留其自身滚动）
        self._bind_mousewheel(inner)
        self.scroll_canvas.bind('<MouseWheel>', self._on_mousewheel)

    # ---------------- 滚动支持 ----------------
    def _on_scroll_canvas_resize(self, event):
        try:
            self.scroll_canvas.itemconfig(self.scroll_win, width=event.width)
        except Exception:
            pass

    def _on_mousewheel(self, event):
        try:
            self.scroll_canvas.yview_scroll(
                int(-1 * (event.delta / 120)), 'units')
        except Exception:
            pass
        return 'break'

    def _bind_mousewheel(self, widget):
        try:
            if not isinstance(widget, (tk.Text, tk.Entry, ttk.Combobox)):
                widget.bind('<MouseWheel>', self._on_mousewheel, add='+')
        except Exception:
            pass
        try:
            for child in widget.winfo_children():
                self._bind_mousewheel(child)
        except Exception:
            pass

    # ---------------- 终端 / 远程控制 ----------------
    def build_terminal(self, parent):
        cfg = ttk.LabelFrame(parent, text='Pico 远程连接', padding=6)
        cfg.pack(fill=tk.X, padx=8, pady=(8, 4))

        ttk.Label(cfg, text='地址:', font=FONT).pack(side=tk.LEFT)
        self.pico_host = tk.StringVar(value=self.load_pico_host())
        ttk.Entry(cfg, textvariable=self.pico_host, width=24,
                  font=FONT).pack(side=tk.LEFT, padx=4)
        ttk.Label(cfg, text='(如 debian@192.168.1.1 —— 电脑连 Pico WiFi 后，其网关 IP)',
                  font=FONT, foreground='gray').pack(side=tk.LEFT, padx=4)
        ttk.Button(cfg, text='记住', command=self.save_pico_host).pack(side=tk.LEFT, padx=6)
        ttk.Button(cfg, text='免密设置说明', command=self.show_ssh_help).pack(side=tk.LEFT)

        acts = ttk.LabelFrame(parent, text='常用远程操作（无需直接关机）', padding=6)
        acts.pack(fill=tk.X, padx=8, pady=4)
        terminal_state = tk.NORMAL if HAS_PTY else tk.DISABLED
        for label, cmd in [
            ('停止采集', 'sudo systemctl stop pico-lora'),
            ('启动采集', 'sudo systemctl start pico-lora'),
            ('查看状态', 'systemctl status pico-lora'),
            ('实时日志', 'journalctl -u pico-lora -n 30'),
            ('重启 Pico', 'sudo reboot'),
            ('正常关机', 'sudo shutdown -h now'),
        ]:
            ttk.Button(acts, text=label, state=terminal_state,
                       command=lambda c=cmd: self.run_remote(c)).pack(side=tk.LEFT, padx=3)
        ttk.Button(acts, text='SSH 登录', state=terminal_state,
                   command=self.ssh_login).pack(side=tk.LEFT, padx=(14, 3))
        ttk.Button(acts, text='查网关 IP', state=terminal_state,
                   command=self.run_local_ipconfig).pack(side=tk.LEFT, padx=3)

        term = ttk.LabelFrame(parent, text='交互终端', padding=4)
        term.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        self.terminal = EmbeddedTerminal(term)
        self.terminal.pack(fill=tk.BOTH, expand=True)

    # ---- 终端基础 ----
    def term_put(self, text, autoscroll=True):
        self.terminal.message(text)

    # ---- 远程 ----
    def host(self):
        h = self.pico_host.get().strip()
        if not h:
            messagebox.showwarning('提示', '请先填写 Pico 地址，如 debian@192.168.1.1')
            return None
        if h.startswith('-') or not re.fullmatch(r'[A-Za-z0-9_.@:\[\]-]+', h):
            messagebox.showwarning('提示', '地址请填写为 debian@IP 或 debian@主机名。')
            return None
        return h

    def run_remote(self, remote_cmd):
        if not HAS_PTY:
            self.terminal.message(TERMINAL_UNAVAILABLE_MESSAGE)
            return
        h = self.host()
        if not h:
            return
        # 快捷操作单独使用终端，不打断用户正在操作的 SSH 会话。
        window = tk.Toplevel(self.root)
        window.title(f'Pico 远程操作：{remote_cmd}')
        window.geometry('920x600')
        terminal = EmbeddedTerminal(window)
        terminal.pack(fill=tk.BOTH, expand=True)
        self.terminal_windows.append((window, terminal))
        def close_window():
            terminal.close()
            self.terminal_windows.remove((window, terminal))
            window.destroy()
        window.protocol('WM_DELETE_WINDOW', close_window)
        window.update_idletasks()
        terminal.start(['ssh', '-tt', '-o', 'ConnectTimeout=10', h, remote_cmd], f'SSH {h}')

    def ssh_login(self):
        if not HAS_PTY:
            self.terminal.message(TERMINAL_UNAVAILABLE_MESSAGE)
            return
        h = self.host()
        if not h:
            return
        self.terminal.start(['ssh', '-tt', '-o', 'ConnectTimeout=10', h], f'SSH {h}')

    def run_local_ipconfig(self):
        """查本机默认网关 —— 电脑连 Pico WiFi 时，网关通常就是 Pico 自身 IP"""
        if not HAS_PTY:
            self.terminal.message(TERMINAL_UNAVAILABLE_MESSAGE)
            return
        cmd = ("powershell -NoProfile -Command \"Get-NetRoute -DestinationPrefix "
               "'0.0.0.0/0' | Sort-Object RouteMetric | Select-Object -First 1 "
               "-ExpandProperty NextHop\"")
        self.term_put('\n> ' + cmd + '\n')
        self.term_put('（输出的 IP 通常就是 Pico 的地址，前面加上 debian@ 填入上方）\n')
        self.term_put('（若电脑同时插着网线，可能显示的是路由器网关，请以 ipconfig 为准）\n')
        if self.terminal.start_local():
            self.terminal.send_command(cmd)

    def load_pico_host(self):
        try:
            with open(os.path.join(self.data_dir(), 'last_host.txt'),
                      'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception:
            return ''

    def save_pico_host(self):
        try:
            with open(os.path.join(self.data_dir(), 'last_host.txt'),
                      'w', encoding='utf-8') as f:
                f.write(self.pico_host.get().strip())
            self.term_put('已记住 Pico 地址\n')
        except Exception as e:
            messagebox.showerror('保存失败', str(e))

    def show_ssh_help(self):
        messagebox.showinfo(
            'SSH 免密登录设置（一次性）',
            '内置 SSH 终端可以直接输入登录密码，免密登录是可选的。\n'
            '如需配置免密，在本地终端中执行：\n\n'
            '1) 生成密钥（一路回车）\n'
            '   ssh-keygen\n\n'
            '2) 把公钥传到 Pico（把地址换成你的）\n'
            '   type $env:USERPROFILE\\.ssh\\id_rsa.pub | ssh debian@192.168.1.1 '
            '"mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"\n\n'
            '3) 测试登录（把地址换成你的）\n'
            '   ssh debian@192.168.1.1\n\n'
            '登录后可运行 pwd、ls、cd、cat 等 Linux 命令。\n'
            'sudo 如需密码，直接在终端输入即可。\n'
            '要从 Pico 复制文件到电脑，请使用本地终端中的 scp，\n'
            '例如：scp debian@IP:/实际路径/文件.csv .\n'
            '末尾的 . 表示本地终端的当前文件夹。')

    # ---------------- 串口 ----------------
    def refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_cb['values'] = ports
        # 只在输入框为空时自动填，避免覆盖用户手输的值
        if not self.port_var.get():
            last = self.load_last_port()
            if last:
                self.port_var.set(last)
            elif ports:
                self.port_var.set(ports[0])

    # ---- 记住上次成功连接的串口 ----
    def last_port_file(self):
        return os.path.join(self.data_dir(), 'last_port.txt')

    def load_last_port(self):
        try:
            with open(self.last_port_file(), 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception:
            return None

    def save_last_port(self, port):
        try:
            with open(self.last_port_file(), 'w', encoding='utf-8') as f:
                f.write(port)
        except Exception:
            pass

    def toggle_conn(self):
        self.disconnect() if self.running else self.connect()

    def connect(self):
        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning('提示', '请先选择串口')
            return
        try:
            self.ser = serial.Serial(port, int(self.baud_var.get()), timeout=0.5)
        except Exception as e:
            avail = ', '.join(p.device for p in serial.tools.list_ports.comports())
            messagebox.showerror('连接失败',
                                 f'{port} 打开失败:\n{e}\n\n'
                                 f'检查清单：\n'
                                 f'1. PuTTY 是否已关闭（串口不能同时被占用）\n'
                                 f'2. 设备管理器 → 端口(COM 和 LPT) 查看实际口号\n'
                                 f'3. 当前可用端口: {avail or "无"}')
            return
        self.save_last_port(port)
        self.q = queue.Queue()  # 新连接不接收上一连接的残留队列
        self.reader_stop = threading.Event()
        self.reset_stats()
        self.running = True
        self.t_start = time.time()
        self.btn_conn.config(text='断开')
        self.status.set(f'已连接 {port}')
        self.log_msg(f'--- 已连接 {port} @{self.baud_var.get()} ---')
        if self.save_var.get():
            self.open_csv()
        self.reader = threading.Thread(
            target=self.read_loop, args=(self.ser, self.reader_stop, self.q), daemon=True)
        self.reader.start()

    def disconnect(self):
        self.running = False
        if self.reader_stop:
            self.reader_stop.set()
        if self.ser:
            try:
                self.ser.close()
            except Exception as e:
                self.log_msg(f'串口关闭失败: {e}')
        if self.reader and self.reader.is_alive():
            self.reader.join(timeout=1)
        self.ser = None
        # 断开前已收齐的帧仍写入本次 CSV，不留到下一次连接。
        while True:
            try:
                raw, received_at = self.q.get_nowait()
            except queue.Empty:
                break
            if raw is not None:
                self.process_raw(raw, received_at)
        self.btn_conn.config(text='连接')
        self.status.set('未连接')
        self.log_msg('--- 已断开 ---')
        self.close_csv()

    def read_loop(self, port, stop, rx_queue):
        pending = b''
        discarding = False
        while not stop.is_set() and port.is_open:
            try:
                raw = port.readline(MAX_FRAME_BYTES + 1)
                if stop.is_set() or not raw:
                    continue
                if discarding:
                    discarding = not raw.endswith(b'\n')
                    continue
                pending += raw
                if len(pending) > MAX_FRAME_BYTES:
                    rx_queue.put((b'[oversized serial line discarded]\n', time.time()))
                    pending = b''
                    discarding = not raw.endswith(b'\n')
                    continue
                if pending.endswith(b'\n'):
                    rx_queue.put((pending, time.time()))
                    pending = b''
            except Exception as e:
                if not stop.is_set():
                    rx_queue.put((None, str(e)))
                break

    # ---------------- 数据处理 ----------------
    def poll(self):
        try:
            for _ in range(POLL_BATCH):
                raw, t_pc = self.q.get_nowait()
                if raw is None:
                    self.disconnect()
                    self.status.set('串口异常，已断开')
                    self.log_msg(f'串口读取失败: {t_pc}')
                    break
                self.process_raw(raw, t_pc)
        except queue.Empty:
            pass
        except Exception as e:
            self.log_msg(f'数据处理失败: {e}')
        try:
            self.check_rotate()
            self.update_info()
            if self.plot_dirty:
                self.redraw()
                self.plot_dirty = False
        finally:
            self.root.after(80, self.poll)

    def process_raw(self, raw, received_at):
        self.rx_bytes += len(raw)
        try:
            line = raw.decode('ascii').strip()
        except UnicodeDecodeError:
            self.log_msg('收到非 ASCII 数据，已跳过')
            return
        self.handle(line, received_at)

    def handle(self, line, t_pc):
        if not line:
            return
        if not line.startswith('$MIRA'):
            self.log_msg(line)
            return

        p = [x.strip() for x in line.split(',')]
        if p[0] != '$MIRA' or len(p) < 2:
            self.log_msg(f'帧格式错误，已跳过: {line}')
            return
        if p[1].isdigit():                         # $MIRA,seq,ts,ch4,h2o,c2h6[,lat,lon]
            if len(p) not in (6, 8):
                self.log_msg(f'帧字段数错误，已跳过: {line}')
                return
            seq = int(p[1]); ts = p[2]; vals = p[3:6]
            coords = p[6:8]
        elif len(p) in (5, 7):                       # $MIRA,ts,ch4,h2o,c2h6[,lat,lon]
            seq = None; ts = p[1]; vals = p[2:5]
            coords = p[5:7]
        else:
            self.log_msg(f'帧字段数错误，已跳过: {line}')
            return

        try:
            pico_time = sensor_time(ts)
            vals = [gas_value(v) for v in vals]
        except ValueError:
            self.log_msg(f'时间或浓度格式错误，已跳过: {line}')
            return

        if seq is not None:
            if (self.last_seq is not None and seq < self.last_seq
                    and self.last_pico_time is not None and pico_time > self.last_pico_time):
                self.first_seq = self.last_seq = None
                self.numbered_rx_count = 0
                self.log_msg('--- 发送端序号重新开始，丢包统计已重新计数 ---')
            if self.first_seq is None:
                self.first_seq = seq
            if self.last_seq is None or seq > self.last_seq:
                self.numbered_rx_count += 1
                self.last_seq = seq
                self.last_pico_time = pico_time

        lat = coordinate(coords[0]) if coords else 'nan'
        lon = coordinate(coords[1]) if len(coords) > 1 else 'nan'

        self.rx_count += 1
        self.timestamps.append(t_pc)
        self.log_msg(line)

        for (key, cn, unit, color), v in zip(GASES, vals):
            self.vals[key].set(v if v != 'nan' else 'No Data')
        self.info_vars['Pico时间'].set(ts)
        self.info_vars['纬度'].set(lat if lat != 'nan' else 'No Data')
        self.info_vars['经度'].set(lon if lon != 'nan' else 'No Data')

        # 曲线数据
        self.time_data.append(t_pc - (self.t_start or t_pc))
        for (key, _, _, _), v in zip(GASES, vals):
            try:
                self.series[key].append(float(v))
            except ValueError:
                self.series[key].append(float('nan'))
        if len(self.time_data) > MAX_POINTS:
            self.time_data.pop(0)
            for k in self.series:
                self.series[k].pop(0)
        self.plot_dirty = True

        if self.csv_writer:
            try:
                self.csv_writer.writerow(
                    [datetime.datetime.fromtimestamp(t_pc).isoformat(),
                     seq if seq is not None else '', ts] + vals + [lat, lon])
                self.csv_file.flush()
            except (OSError, csv.Error) as e:
                self.close_csv()
                self.save_var.set(False)
                self.log_msg(f'CSV 写入失败，已停止存盘，监测继续: {e}')
                messagebox.showerror('CSV 写入失败', str(e))

    def redraw(self):
        if not HAS_MPL:
            return
        for key, _, _, _ in GASES:
            self.lines[key].set_data(self.time_data, self.series[key])
            ax = self.axes[key]
            ax.relim()
            ax.autoscale_view()
        self.canvas.draw_idle()

    def update_info(self):
        self.info_vars['接收帧数'].set(str(self.rx_count))
        now = time.time()
        self.timestamps = [t for t in self.timestamps if now - t < 5]
        if self.t_start:
            self.info_vars['运行时长'].set(f'{now - self.t_start:.0f} s')
            if now - self.t_start > 0.5:
                self.info_vars['速率'].set(
                    f'{len(self.timestamps) / 5.0:.1f} 帧/秒  '
                    f'{self.rx_bytes / (now - self.t_start):.0f} B/s')
        if self.first_seq is not None and self.last_seq is not None:
            expected = self.last_seq - self.first_seq + 1
            if expected > 0:
                lost = max(0, expected - self.numbered_rx_count)
                self.info_vars['丢包率'].set(
                    f'{max(0.0, lost / expected * 100):.1f}%  (丢{lost}/{expected})')
        else:
            self.info_vars['丢包率'].set('需序号(升级机载端)')

    # ---------------- CSV（data 子文件夹 + 30 分钟轮转）----------------
    def data_dir(self):
        d = os.path.join(app_dir(), 'data')
        os.makedirs(d, exist_ok=True)
        return d

    def open_csv(self):
        if self.csv_file:
            return
        try:
            d = self.data_dir()
            name = f'mira_{datetime.datetime.now():%Y%m%d_%H%M%S_%f}.csv'
            path = os.path.join(d, name)
            self.csv_file = open(path, 'x', newline='', encoding='utf-8')
            self.csv_writer = csv.writer(self.csv_file)
            self.csv_writer.writerow(['pc_time', 'seq', 'pico_time',
                                      'CH4_ppm', 'H2O_ppm', 'C2H6_ppb',
                                      'Latitude', 'Longitude'])
            self.csv_file.flush()
        except Exception as e:
            self.close_csv()
            messagebox.showerror('无法写入', str(e))
            self.save_var.set(False)
            return
        self.csv_path = path
        self.csv_opened_at = time.monotonic()
        self.log_msg(f'CSV 记录中: {path}')
        self.info_vars['当前文件'].set(name)

    def close_csv(self):
        if self.csv_file:
            try:
                self.csv_file.close()
                self.log_msg(f'CSV 已保存: {self.csv_path}')
            except Exception:
                self.log_msg(f'CSV 关闭失败，文件可能未完整写入: {self.csv_path}')
        self.csv_file = None
        self.csv_writer = None
        self.csv_path = None
        self.info_vars['当前文件'].set('--')

    def check_rotate(self):
        """每 30 分钟切换到新文件"""
        if not self.running or not self.csv_writer:
            return
        if time.monotonic() - self.csv_opened_at >= ROTATE_SEC:
            self.close_csv()
            self.open_csv()

    def toggle_csv(self):
        if self.save_var.get() and self.running:
            self.open_csv()
        elif not self.save_var.get():
            self.close_csv()

    # ---------------- 杂项 ----------------
    def log_msg(self, s):
        ts = datetime.datetime.now().strftime('%H:%M:%S')
        self.log.insert(tk.END, f'[{ts}] {s}\n')
        self.log.see(tk.END)
        if int(self.log.index('end-1c').split('.')[0]) > 2000:
            self.log.delete('1.0', '500.0')

    def open_data_dir(self):
        d = self.data_dir()
        try:
            os.startfile(d)
        except Exception:
            self.log_msg(f'数据文件夹: {d}')

    def reset_stats(self):
        self.rx_count = 0
        self.rx_bytes = 0
        self.first_seq = None
        self.last_seq = None
        self.numbered_rx_count = 0
        self.last_pico_time = None
        self.t_start = time.time() if self.running else None
        self.timestamps.clear()
        self.time_data.clear()
        for k in self.series:
            self.series[k].clear()
        self.redraw()
        self.plot_dirty = False
        self.log_msg('--- 统计已重置 ---')

    def export_log(self):
        path = filedialog.asksaveasfilename(
            defaultextension='.txt',
            filetypes=[('Text', '*.txt'), ('All', '*.*')],
            initialfile=f'mira_log_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt')
        if path:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(self.log.get('1.0', tk.END))
            self.log_msg(f'已导出: {path}')

    def on_close(self):
        self.disconnect()
        self.terminal.close()
        for window, terminal in self.terminal_windows:
            terminal.close()
            window.destroy()
        self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    GroundStation(root)
    root.mainloop()
