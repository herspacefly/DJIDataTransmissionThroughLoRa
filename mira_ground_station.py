#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MIRA Pico 甲烷监测地面站 (Windows)
替代 PuTTY 的专用接收软件：三气体实时曲线 / 数值卡 / 自动存 CSV / 丢包统计

【数据输出】
  自动在程序所在文件夹下创建 data/ 子目录
  每 30 分钟自动切换到新文件，文件名带起始时刻，例如：
      data/mira_20260925_183000.csv

【运行前】
  pip install pyserial matplotlib
  注意：运行本程序前请先关闭 PuTTY，串口不能被两个程序同时占用
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
import serial
import subprocess
import serial.tools.list_ports
import threading
import queue
import time
import csv
import os
import sys
import datetime

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


class GroundStation:
    def __init__(self, root):
        self.root = root
        self.root.title('MIRA Pico 甲烷监测地面站')
        self.root.geometry('1040x760')

        self.ser = None
        self.reader = None
        self.q = queue.Queue()
        self.running = False

        # 统计
        self.rx_count = 0
        self.rx_bytes = 0
        self.first_seq = None
        self.last_seq = None
        self.t_start = None
        self.timestamps = []
        self.series = {k: [] for k, _, _, _ in GASES}   # 各气体数值序列
        self.time_data = []

        # CSV
        self.csv_file = None
        self.csv_writer = None
        self.csv_path = None
        self.csv_opened_at = 0

        # 终端 / 远程控制
        self.proc = None            # 本地 shell 进程
        self.proc_q = queue.Queue()
        self.remote_busy = False

        # 标签页：监测 / 终端
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill=tk.BOTH, expand=True)

        self.tab_main = ttk.Frame(self.nb)
        self.tab_term = ttk.Frame(self.nb)
        self.nb.add(self.tab_main, text='  监测  ')
        self.nb.add(self.tab_term, text='  终端 / 远程控制  ')

        self.build_ui(self.tab_main)
        self.build_terminal(self.tab_term)

        self.refresh_ports()
        self.root.after(100, self.poll)
        self.root.after(120, self.poll_terminal)
        self.root.protocol('WM_DELETE_WINDOW', self.on_close)

    # ---------------- UI ----------------
    def build_ui(self, parent):
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
        for k in ('Pico时间', '接收帧数', '丢包率', '速率', '运行时长', '当前文件'):
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
            self.axes[GASES[-1][0]].set_xlabel('时间 (s)', fontsize=8)
            self.fig.tight_layout()
            self.canvas = FigureCanvasTkAgg(self.fig, master=right)
            self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        else:
            tk.Label(right, text='未安装 matplotlib\n\n如需曲线请运行:\npip install matplotlib',
                     font=FONT, fg='gray', justify=tk.CENTER).pack(expand=True)

        bottom = ttk.LabelFrame(parent, text='原始数据', padding=4)
        bottom.pack(fill=tk.BOTH, expand=False)
        self.log = scrolledtext.ScrolledText(bottom, height=9, font=('Consolas', 9))
        self.log.pack(fill=tk.BOTH, expand=True)

        bar = ttk.Frame(parent, padding=4)
        bar.pack(fill=tk.X)
        ttk.Button(bar, text='清空日志',
                   command=lambda: self.log.delete('1.0', tk.END)).pack(side=tk.LEFT)
        ttk.Button(bar, text='导出日志', command=self.export_log).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text='打开数据文件夹',
                   command=self.open_data_dir).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text='重置统计', command=self.reset_stats).pack(side=tk.LEFT)

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
        for label, cmd in [
            ('停止采集', 'sudo systemctl stop pico-lora'),
            ('启动采集', 'sudo systemctl start pico-lora'),
            ('查看状态', 'systemctl status pico-lora'),
            ('实时日志', 'journalctl -u pico-lora -n 30'),
            ('重启 Pico', 'sudo reboot'),
            ('正常关机', 'sudo shutdown -h now'),
        ]:
            ttk.Button(acts, text=label,
                       command=lambda c=cmd: self.run_remote(c)).pack(side=tk.LEFT, padx=3)
        ttk.Button(acts, text='SSH 登录',
                   command=self.ssh_login).pack(side=tk.LEFT, padx=(14, 3))
        ttk.Button(acts, text='查网关 IP',
                   command=self.run_local_ipconfig).pack(side=tk.LEFT, padx=3)

        term = ttk.LabelFrame(parent, text='终端', padding=4)
        term.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        self.term_out = scrolledtext.ScrolledText(term, font=('Consolas', 9),
                                                  height=16, bg='#1e1e1e',
                                                  fg='#d4d4d4',
                                                  insertbackground='#d4d4d4')
        self.term_out.pack(fill=tk.BOTH, expand=True)

        inp = ttk.Frame(parent, padding=(8, 0, 8, 8))
        inp.pack(fill=tk.X)
        self.term_in = ttk.Entry(inp, font=('Consolas', 10))
        self.term_in.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        self.term_in.bind('<Return>', self.send_cmd)
        ttk.Button(inp, text='发送', command=self.send_cmd).pack(side=tk.LEFT)

        self.term_put('终端未启动。点「发送」或回车即自动启动本地终端。\n')
        self.term_put('提示：远程命令依赖 Windows 自带 ssh；建议先配置免密登录（见「免密设置说明」）。\n')

    # ---- 终端基础 ----
    def term_put(self, text, autoscroll=True):
        try:
            self.term_out.insert(tk.END, text)
            if int(self.term_out.index('end-1c').split('.')[0]) > 1500:
                self.term_out.delete('1.0', '400.0')
            if autoscroll:
                self.term_out.see(tk.END)
        except Exception:
            pass

    def start_shell(self):
        if self.proc and self.proc.poll() is None:
            return
        kw = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                  stderr=subprocess.STDOUT, text=True,
                  encoding='utf-8', errors='replace', bufsize=1)
        if os.name == 'nt':
            kw['creationflags'] = subprocess.CREATE_NO_WINDOW
        try:
            self.proc = subprocess.Popen(['cmd.exe'] if os.name == 'nt'
                                         else ['/bin/bash'], **kw)
        except Exception as e:
            self.term_put(f'终端启动失败: {e}\n')
            return
        threading.Thread(target=self._shell_reader, daemon=True).start()
        self.term_put('--- 本地终端已启动 ---\n')

    def _shell_reader(self):
        while self.proc and self.proc.poll() is None:
            try:
                line = self.proc.stdout.readline()
            except Exception:
                break
            if not line:
                break
            self.proc_q.put(line)

    def poll_terminal(self):
        try:
            while True:
                self.term_put(self.proc_q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(120, self.poll_terminal)

    def send_cmd(self, event=None):
        cmd = self.term_in.get().strip()
        if not cmd:
            return
        self.term_in.delete(0, tk.END)
        self.term_put(f'> {cmd}\n')
        self.start_shell()
        if not self.proc:
            return
        try:
            self.proc.stdin.write(cmd + '\n')
            self.proc.stdin.flush()
        except Exception as e:
            self.term_put(f'写入失败: {e}\n')

    # ---- 远程 ----
    def host(self):
        h = self.pico_host.get().strip()
        if not h:
            messagebox.showwarning('提示', '请先填写 Pico 地址，如 debian@192.168.1.1')
            return None
        return h

    def run_remote(self, remote_cmd):
        h = self.host()
        if not h:
            return
        self.term_put(f'\n$ ssh {h} "{remote_cmd}"\n')
        threading.Thread(target=self._ssh_thread, args=(h, remote_cmd),
                         daemon=True).start()

    def _ssh_thread(self, host, remote_cmd):
        kw = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                  encoding='utf-8', errors='replace')
        if os.name == 'nt':
            kw['creationflags'] = subprocess.CREATE_NO_WINDOW
        args = ['ssh', '-o', 'StrictHostKeyChecking=no',
                '-o', 'ConnectTimeout=10', host, remote_cmd]
        try:
            p = subprocess.Popen(args, **kw)
            for line in p.stdout:
                self.proc_q.put(line)
            p.wait()
            self.proc_q.put(f'[退出码 {p.returncode}]\n')
        except FileNotFoundError:
            self.proc_q.put(
                '错误：未找到 ssh 命令。\n'
                '请在 Windows「设置 → 应用 → 可选功能」中安装 OpenSSH 客户端，\n'
                '或在 PowerShell 中直接手动 ssh 登录。\n')
        except Exception as e:
            self.proc_q.put(f'错误：{e}\n')

    def ssh_login(self):
        h = self.host()
        if not h:
            return
        self.term_put(f'> ssh {h}\n')
        self.start_shell()
        if self.proc:
            try:
                self.proc.stdin.write(f'ssh {h}\n')
                self.proc.stdin.flush()
                self.term_put('（若提示输入密码，请确保焦点在本窗口，或先配免密登录）\n')
            except Exception as e:
                self.term_put(f'写入失败: {e}\n')

    def run_local_ipconfig(self):
        """查本机默认网关 —— 电脑连 Pico WiFi 时，网关通常就是 Pico 自身 IP"""
        cmd = ("powershell -NoProfile -Command \"Get-NetRoute -DestinationPrefix "
               "'0.0.0.0/0' | Sort-Object RouteMetric | Select-Object -First 1 "
               "-ExpandProperty NextHop\"")
        self.term_put('\n> ' + cmd + '\n')
        self.term_put('（输出的 IP 通常就是 Pico 的地址，前面加上 debian@ 填入上方）\n')
        self.term_put('（若电脑同时插着网线，可能显示的是路由器网关，请以 ipconfig 为准）\n')
        self.start_shell()
        if self.proc:
            try:
                self.proc.stdin.write(cmd + '\n')
                self.proc.stdin.flush()
            except Exception as e:
                self.term_put(f'写入失败: {e}\n')

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
            '在 Windows PowerShell 中执行：\n\n'
            '1) 生成密钥（一路回车）\n'
            '   ssh-keygen\n\n'
            '2) 把公钥传到 Pico（把地址换成你的）\n'
            '   type $env:USERPROFILE\\.ssh\\id_rsa.pub | ssh debian@192.168.1.1 '
            '"mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"\n\n'
            '3) 让 systemctl 不再要 sudo 密码（在 Pico 上执行）\n'
            '   echo "debian ALL=(ALL) NOPASSWD:ALL" | sudo tee '
            '/etc/sudoers.d/debian\n\n'
            '完成后，本窗口的「停止采集 / 启动采集 / 关机」等按钮\n'
            '就能一键执行，无需输入密码。\n\n'
            '注：Pico 是内网设备，NOPASSWD:ALL 风险可接受；\n'
            '若在意安全，可只放行 systemctl 与 shutdown 两个命令。')

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
        self.running = True
        self.t_start = time.time()
        self.btn_conn.config(text='断开')
        self.status.set(f'已连接 {port}')
        self.log_msg(f'--- 已连接 {port} @{self.baud_var.get()} ---')
        if self.save_var.get():
            self.open_csv()
        self.reader = threading.Thread(target=self.read_loop, daemon=True)
        self.reader.start()

    def disconnect(self):
        self.running = False
        time.sleep(0.3)
        if self.ser and self.ser.is_open:
            self.ser.close()
        self.btn_conn.config(text='连接')
        self.status.set('未连接')
        self.log_msg('--- 已断开 ---')
        self.close_csv()

    def read_loop(self):
        while self.running and self.ser and self.ser.is_open:
            try:
                raw = self.ser.readline()
                if raw:
                    self.q.put((raw, time.time()))
            except Exception:
                break

    # ---------------- 数据处理 ----------------
    def poll(self):
        try:
            while True:
                raw, t_pc = self.q.get_nowait()
                self.rx_bytes += len(raw)
                self.handle(raw.decode('ascii', errors='ignore').strip(), t_pc)
        except queue.Empty:
            pass
        self.check_rotate()
        self.update_info()
        self.root.after(80, self.poll)

    def handle(self, line, t_pc):
        if not line:
            return
        if not line.startswith('$MIRA'):
            self.log_msg(line)
            return

        p = [x.strip() for x in line.split(',')]
        if len(p) >= 6 and p[1].isdigit():          # $MIRA,seq,ts,ch4,h2o,c2h6
            seq = int(p[1]); ts = p[2]; vals = p[3:6]
            if self.first_seq is None:
                self.first_seq = seq
            self.last_seq = seq
        elif len(p) >= 5:                            # $MIRA,ts,ch4,h2o,c2h6
            seq = None; ts = p[1]; vals = p[2:5]
        else:
            return

        self.rx_count += 1
        self.timestamps.append(t_pc)
        self.log_msg(line)

        for (key, cn, unit, color), v in zip(GASES, vals):
            self.vals[key].set(v)
        self.info_vars['Pico时间'].set(ts)

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
        self.redraw()

        if self.csv_writer:
            self.csv_writer.writerow(
                [datetime.datetime.now().isoformat(),
                 seq if seq is not None else '', ts] + vals)
            self.csv_file.flush()

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
                lost = expected - self.rx_count
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
        d = self.data_dir()
        name = f'mira_{datetime.datetime.now():%Y%m%d_%H%M%S}.csv'
        path = os.path.join(d, name)
        try:
            self.csv_file = open(path, 'a', newline='', encoding='utf-8')
        except Exception as e:
            messagebox.showerror('无法写入', f'{path}\n{e}')
            self.save_var.set(False)
            return
        self.csv_path = path
        self.csv_opened_at = time.time()
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(['pc_time', 'seq', 'pico_time',
                                  'CH4_ppm', 'H2O_ppm', 'C2H6_ppb'])
        self.log_msg(f'CSV 记录中: {path}')
        self.info_vars['当前文件'].set(name)

    def close_csv(self):
        if self.csv_file:
            try:
                self.csv_file.close()
                self.log_msg(f'CSV 已保存: {self.csv_path}')
            except Exception:
                pass
        self.csv_file = None
        self.csv_writer = None
        self.csv_path = None
        self.info_vars['当前文件'].set('--')

    def check_rotate(self):
        """每 30 分钟切换到新文件"""
        if not self.running or not self.csv_writer:
            return
        if time.time() - self.csv_opened_at >= ROTATE_SEC:
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
        self.timestamps.clear()
        self.time_data.clear()
        for k in self.series:
            self.series[k].clear()
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
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass
        self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    GroundStation(root)
    root.mainloop()
