# 设备接线、配置与上机检查

## 硬件与系统

| 设备 | 连接与用途 |
| --- | --- |
| 左侧 Piper X 主臂、从臂 | 使用实验室已标定并验证过的左侧主从 CAN 接线。左侧 USB-CAN 接采集机。 |
| 右侧 Piper X 主臂、从臂 | 同样使用一组独立的主从 CAN 接线。右侧 USB-CAN 接采集机。 |
| 三台 RealSense D435 | 分别安装在左从臂腕部、右从臂腕部和固定全景位置。RGB 默认 640×480、30 FPS。 |
| Windows 采集机 | PowerShell、Python 3.12、WinUSB 可访问的 candleLight/gs_usb 适配器、本地磁盘。GPU 服务器可在采集结束后处理数据。 |

两组主从臂由各自的电源供电。USB-CAN 默认采用 VID/PID `1d50:606f`、1 Mbps。换 USB 口后按设备序列号重新识别。交换物理适配器在左右两侧的 CAN 接线后，需要重新核对角色映射。优先让多台相机走稳定的 USB 3 链路。若所有设备都经过同一扩展坞，正式批量采集前先采一条短轨迹，检查三路丢帧、超时和 CAN 错误。

## 确定角色并填写本机配置

从仓库根目录运行。

```powershell
Copy-Item configs/devices.example.json configs/devices.json
Copy-Item configs/robot_firmware.example.json configs/robot_firmware.json
.\.venv\Scripts\python.exe scripts/check_devices.py --list
.\.venv\Scripts\python.exe scripts/identify_cameras.py
```

1. 通过逐个核对电脑侧 USB-CAN 插头，确定左侧和右侧适配器的序列号。在停机状态下只操作电脑侧 USB，保留机械臂间的 CAN 接线。
2. 查看 `data/device_identification/camera_contact_sheet.jpg` 中各画面的标签，找出左腕、右腕和全景。把两路适配器和三台相机的序列号以 JSON 字符串填入 `configs/devices.json`。
3. 向设备负责人核对每台主臂和从臂的实际固件版本，把它们写入 `configs/robot_firmware.json`。当前 Home 配置记录已验证的固件 `S-V1.9-1`。新固件或其它机型应先由设备负责人核对协议、关节零位和回零空间，再确认是否可沿用此配置。
4. 运行 `scripts/check_devices.py`，确认两个 CAN 和三个相机均显示 `PRESENT`。该检查只枚举设备。开始实机采集前还需现场验证供电、零点、夹爪和主从跟随。
5. 若已有与这套相机及分辨率对应的标定文件，可放在 `configs/camera_calibration/`，录制时会保存快照。厂家相机内参与 RGB-D 外参只描述相机本身，夹爪或工作台坐标系的外参需另行标定。

```powershell
.\.venv\Scripts\python.exe scripts/check_devices.py
```

## 实机首条

让有权限操作设备的同门在场，确认台面、物体和人员都在回零路径之外，并能触及现场急停。先分侧验证空场回零和恢复拖动。发出回零后，程序用从臂反馈验证位置、稳定性和控制状态，同时由现场人员确认主臂也已到零位。回零完成后软件恢复拖动，再进入被动监听。若有主从姿态明显不一致、拖不动、碰撞风险或异常动作，停止流程并查阅 Home 日志。Ctrl+C 结束脚本，现场急停用于停止已经发出的运动。

USB 序列号报错（如 `no langid`）时，先关闭占用设备的上位机或其他录制器，在设备静止时检查电脑侧连接、WinUSB 和扩展坞，再运行枚举。列出了序列号只能证明 USB 可访问，连续 CAN 与相机采集质量仍以短轨迹的 `quality.json` 为准。
