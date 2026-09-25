# UAV-Methane-Telemetry

无人机甲烷浓度实时无线回传系统。基于大疆 M400 无人机 + Aeris Mira Pico 气体监测仪 + 亿佰特 E28 LoRa 无线串口模块，将空中采集的甲烷浓度实时传回地面电脑并可视化记录。

---

## 工作原理

E28 LoRa 模块为**透明传输**——对两端程序而言它就是一根串口线，无线电部分完全由模块完成。因此机载端与地面端的代码都只需读写串口，无需任何网络协议。

```
Mira Pico（采集 + 运行桥接程序）
        │ USB
        ↓
   E28 机载端  ～～～ 2.4GHz 无线电 ～～～  E28 地面端
                                              │ USB
                                              ↓
                                      Windows 电脑（地面站）
```

一帧数据的完整旅程：传感器采集 → Pico 输出原始行（约 134 字节）→ 桥接程序提取时间戳/CH4/H2O/C2H6 并加序号 → 写入 LoRa 串口 → 调制发射 → 地面解调还原 → 地面站解析、显示、存盘。

> 帧格式：`$MIRA,<序号>,<时间戳>,<CH4>,<H2O>,<C2H6>`（约 45 字节，1 Hz）
>
> 加序号的原因：LoRa 广播式传输无 ACK 确认，只有靠序号连续性才能统计丢包率——这是量化无人机图传干扰程度的唯一依据。

完整原理说明见 [当前方案说明与操作手册](./当前方案说明与操作手册.md)。

---

## 硬件清单

| 设备 | 说明 |
|---|---|
| 大疆 Matrice 400 | 无人机平台 |
| Aeris Mira Pico | 甲烷气体监测仪（Debian 系统，可 SSH、可运行 Python） |
| 亿佰特 E28-2G4TBM-01 × 2 | 2.4GHz LoRa 无线串口模块评估板，**必须成对使用** |
| Windows 电脑 | 运行地面站 |

---

## 文件说明

| 文件 | 运行位置 | 说明 |
|---|---|---|
| `pico_ch4_bridge.py` | Pico | 读数据口 → 提取字段 → 加序号 → 写 LoRa 串口 |
| `mira_ground_station.py` | Windows | 地面站 GUI：三气体实时曲线 / 数值卡 / CSV 存盘 / 丢包统计 / 终端遥控 |
| `启动地面站.bat` | Windows | 一键启动（自动探测 Python、自动安装依赖） |

文档：[操作手册](./当前方案说明与操作手册.md) · [开发日志](./甲烷监测无人机无线传输开发日志.md) · [项目背景](./甲烷监测无人机无线传输项目背景说明.md)

---

## 快速开始

### 1. 硬件连接

**两块 E28 都必须先拧好天线，再插 USB。** 天线未接时发射会烧毁功放，且损坏是隐性的（模块仍能识别，只是发不出去）。

地面端建议插黑色 USB 2.0 口（USB 3.0 噪声落在 2.4GHz 会干扰接收），并用延长线拉离主机。

### 2. 电脑端依赖

```powershell
pip install pyserial matplotlib
```

### 3. Pico 端部署

将 `pico_ch4_bridge.py` 放到 `/home/debian/`，用 systemd 设置开机自启：

```ini
# /etc/systemd/system/pico-lora.service
[Unit]
Description=Pico to LoRa Bridge
After=multi-user.target

[Service]
Type=simple
User=debian
ExecStart=/usr/bin/python3 /home/debian/pico_ch4_bridge.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable pico-lora.service
sudo systemctl start pico-lora.service
```

### 4. SSH 免密（供地面站远程控制使用）

```powershell
ssh-keygen
type $env:USERPROFILE\.ssh\id_rsa.pub | ssh debian@<Pico的IP> "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
```

Pico 上执行：

```bash
echo "debian ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/debian
```

### 5. 启动地面站

双击 `启动地面站.bat`。**运行前请关闭 PuTTY**——串口不能同时被两个程序占用。

选择 E28 对应的 COM 口，波特率保持 **9600**，点击「连接」。

数据会自动存入程序目录下 `data/` 文件夹，每 30 分钟新建一个 CSV 文件。

### 6. 结束工作

切到「终端 / 远程控制」标签页 → 点「停止采集」停止发送，点「正常关机」安全关闭 Pico。

> **切勿长按电源键强制断电。** Pico 是精密仪器且存有采集数据，强制断电可能损坏文件系统。

---

## 关键参数

| 项目 | 值 |
|---|---|
| 串口波特率 | **9600（不要修改）** |
| 空中速率 | 10 kbps |
| 单帧上限 | 121 字节（实际约 45 字节） |
| 工作频段 | 2400–2500 MHz（默认信道 2413 MHz） |
| 发射功率 | 20 dBm |
| Pico 数据口 | `/dev/ttyUSB_RS232` |
| Pico LoRa 口 | `/dev/ttyUSB_LORA` |

> **不要提高波特率。** 空中速率仅约 1250 字节/秒，串口速率超过它会导致模块缓冲区溢出，造成疯狂丢包，且症状与"被干扰"完全相同，极易误导排查。

---

## 注意事项

- **天线必须先拧再上电**，换天线前先断电
- 两块模块测试时保持 **2~3 米以上**，过近会因接收机饱和导致"近距堵死"
- 串口设备一律使用别名（`ttyUSB_RS232` / `ttyUSB_LORA`），**不要写 `ttyUSB0/1/2`**，端口号会随插拔顺序漂移
- Pico 设置开机自启后为**开机即连续发射**，每次开机前务必目视检查天线

---

## 待办

- [ ] **干扰测试**：M400 的 O4 图传工作频段（2.400–2.4835 GHz）与 E28 完全重叠，需实测飞机开机 + 图传工作时的丢包率。判读标准见[操作手册](./当前方案说明与操作手册.md)
- [ ] 机载天线安装位置对比（贴机身 / 伸出机臂 / 朝下）
- [ ] Pico 供电续航实测（LoRa 由 Pico 取电）
- [ ] 实际拉距测试
