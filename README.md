# Piper X 方块入盘数据采集

用松灵 Piper X 的主从遥操作采集“把方块放到盘子里”的示教。采集机用 Windows 本地接入两组 USB-CAN 和三台 RealSense D435，录制完成后生成可回看的原始轨迹与可独立传输的对齐包。经过人工复核的轨迹可用官方 LeRobot SDK 写成 v3.0 数据集。

- [设备接线、配置与上机检查](docs/setup.md)
- [任务定义、采集流程与故障恢复](docs/collection.md)
- [数据结构、时间对齐、上传与 LeRobot 导出](docs/dataset.md)

## 安装与模拟验收

在 Windows PowerShell 中进入仓库根目录。采集环境使用 64 位 Python 3.12、Git 和主机的 USB-CAN WinUSB 驱动。安装 Python 依赖需要访问 PyPI（或已同步的镜像）及 GitHub 的 codeload.github.com。先用模拟跑通流程，再配置实机。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-capture.txt
.\.venv\Scripts\python.exe .\scripts\collect_dataset.py record --simulate --seconds 2 --arms left --session smoke-left --views left_wrist,overview --once --review
```

命令打印 `PASSED`、`PACKAGE` 和 `REVIEW` 时，分别可在 `data/recorder_selftest/`、`data/training_packages/`、`data/reviews/` 找到产物。模拟数据无需连接设备或填写实验室配置，并带独立的模拟标记。把 `--arms left` 改为 `--arms both`，用 `--session smoke-both` 并去掉 `--views`，即可验证双臂、三视角的数据结构。

## 上机采集

完成[设备配置和上机检查](docs/setup.md)后，在机器人旁运行。启用 `--enable-homing` 时，只有现场输入 `HOME` 后才会向选中侧的主从臂发出回零请求。

```powershell
.\.venv\Scripts\python.exe .\scripts\collect_dataset.py record --arms left --session cube-pilot01 --views left_wrist,overview --enable-homing --once --review
```

这条命令只让左侧主从回零，原始轨迹仍录制双 CAN 与三路 RGB。包中包含左侧动作与状态、左腕和全景。去掉 `--once` 可在同一场次多次采集。新 UUID 自动追加到场次目录。流程默认为本地打包，可配置[团队内部 SSH 上传](docs/dataset.md#可选的-ssh-上传)。

## 仓库内容

| 路径 | 用途 |
| --- | --- |
| `scripts/collect_dataset.py` | 采集、补打包、上传与 LeRobot 导出的统一入口 |
| `scripts/check_devices.py`、`scripts/identify_cameras.py` | USB-CAN 和相机枚举、相机视角预览 |
| `scripts/home_reset.py` | 由现场操作员确认的回零与拖动恢复 |
| `scripts/review_episode.py` | 生成图像和机器人曲线的离线回看 |
| `configs/*.example.json` | 设备、固件和 SSH 上传的配置模板 |
| `configs/capture_v0_1.json`、`configs/home_reset.json` | 采集规格与经验证的 Home 配置 |
| `requirements-capture.txt`、`requirements-export.txt` | 采集与导出各自的 Python 依赖 |

Git 忽略采集数据、运行日志、解释器环境、实际设备序列号、标定文件和上传配置。采集包本身可能含设备配置副本、画面和操作者备注，按实验室的数据权限共享。采集依赖固定到 [AgileX pyAgxArm 官方提交](https://github.com/agilexrobotics/pyAgxArm/commit/d90e234ba8e408cfa964b38aa9120b3131fc9f85)，导出使用 [LeRobot v0.4.4](https://github.com/huggingface/lerobot/releases/tag/v0.4.4)。依赖由安装命令获取。
