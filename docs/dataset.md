# 数据、上传与 LeRobot 导出

## 文件从哪里找

| 目录 | 内容 |
| --- | --- |
| `data/raw/<任务>--<用臂>--<场次>/<轨迹 UUID>/` | 原始图像、CAN 报文、解码事件、质检、配置快照和 SHA-256 文件清单。 |
| `data/reviews/` | 离线 HTML。通过相对路径读取原始 JPEG，移动时保留原始目录结构。回看默认展示实录三路相机。 |
| `data/aligned_candidates/` | 两版对齐时间轴的中间结果，用于比较动作标注和诊断。 |
| `data/training_packages/` | 可独立传输的对齐包或原始归档包。对齐包带 JPEG、`steps.jsonl`、元数据和清单。 |
| `data/lerobot/` | 经导出和官方加载器复核的 LeRobot v3 数据集。 |
| `data/recorder_selftest/`、`data/lerobot_selftest/` | 模拟原始记录及模拟导出。包元数据中的 `source_simulated` 区分模拟和真实输入。 |

原始轨迹中的 `can/<侧>/frames.jsonl` 每行记录一帧 CAN 原始数据，包括 CAN ID、负载、接收时间与错误标记。`can/<侧>/signals.jsonl` 每行为一条解码事件，含字段、值和关联的原始帧索引。从臂反馈构成 `state`，主臂控制帧构成 `action`。`camera/<视角>/frames.jsonl` 每行记录图像文件名、帧号、设备时钟和本机接收时间。原始记录保留各路自己的频率。

## 对齐和裁剪

1. 对齐器用同一个本机单调接收时钟建立时间轴，默认跟随 30 Hz 的采集设置。每个时间步使用不晚于该时刻的最近相机帧与从臂反馈，分别使用 100 ms 和 50 ms 的新鲜度上限。视角之间的帧号独立。USB 接收时差与曝光时差可能不同。
2. 对每个关节组和夹爪分别更新动作。收到有效主臂目标时使用原值（`observed_target`）。通信与跟随检查通过时，之后沿用最后一个目标（`held_last_target`）。经过 Home 边界检查的关节可从 0 rad 开始（`verified_home_initial`）。夹爪需要已收到的有效目标。CAN 故障后清掉受影响侧的历史目标，等新的有效控制帧。
3. 尚未收到目标时，对齐包可保存首个未来目标的诊断性回填。导出器筛掉这类时间步，并要求保留时间步所有选中图像、状态和动作字段均有效。连续有效区间的开头或末尾可以裁掉。如有中间缺口，导出报错以保留实际间隔。开头静止时只要各字段有效就会保留，画面是否有动作本身不参与筛选。
4. 对齐包仅包含选中相机在时间轴中引用的 JPEG。JPEG 字节直接复制，原始数据仍保留。已闭合的轨迹无论人工标签都可打包。质检失败、跟随未确认或无有效时间步时保留原始归档包。主动中止或写入不完整的轨迹留在本机。

可以用离线回看后重新打包补标，原始标签和旧包都可追溯。

```powershell
$raw = "data/raw/<任务--用臂--场次>/<轨迹 UUID>"
.\.venv\Scripts\python.exe scripts/review_episode.py $raw
.\.venv\Scripts\python.exe scripts/collect_dataset.py prepare $raw --views left_wrist,overview --usability usable --review
```

`--confirm-follow` 仅用于能确认整段遥操作始终为主从跟随的补处理。旧版原始记录缺任务 ID 时可传 `--task-id`。导出时每个源 UUID 只选一个最终复核包。

## 可选的 SSH 上传

团队可以在同一数据盘汇总对齐包和归档包。先配置 OpenSSH 主机别名和密钥，确认连接到团队分配的目录。服务器需要 Python 3.12 和该目录写权限。上传会在该项目目录下写 `incoming/`、`data/training_packages/` 和 `receipts/`。代码版本通过 Git 管理。

```powershell
Copy-Item configs/upload.example.json configs/upload.json
# 将 host 改为已配置的 SSH 主机别名，project_root 改为受授权的服务器绝对路径
.\.venv\Scripts\python.exe scripts/collect_dataset.py upload "data/training_packages/<打印的 PACKAGE 目录名>"
```

用 `record --upload` 或 `prepare --upload` 可在打包后立即上传。自动上传前请确认实验室授权共享画面、配置快照和标注。服务器验证文件大小及 SHA-256 后写收据。同一包上传失败时，可用 `upload` 重试同一路径。真实的 `usable`、`unusable`、`unreviewed` 对齐包和归档包都可上传，模拟包保留在本机。

## LeRobot v3.0

使用独立环境，采集和导出可以在不同机器。Windows 本地安装和导出示例。

```powershell
py -3.12 -m venv .venv-lerobot
.\.venv-lerobot\Scripts\python.exe -m pip install -r requirements-export.txt
$package = "data/training_packages/<打印的 PACKAGE 目录名>"
.\.venv-lerobot\Scripts\python.exe scripts/collect_dataset.py export-lerobot $package --name v01
```

模拟验收把 `$package` 换成 README 模拟命令打印的包路径并加 `--allow-simulated`。在 Linux 可用 `python3.12 -m venv .venv-lerobot`，从 `.venv-lerobot/bin/python` 启动脚本。若包在共享数据盘，传入包路径和 `--data-root /团队项目目录/data`。

- 命令可按顺序传多个包。默认只接收质检通过、人工标为 `usable`、结果为 `success` 的轨迹。归档、`unusable` 和 `unreviewed` 包会跳过并列入报告。对已人工确认的特殊实验可用 `--allow-warning` 或 `--include-result failure` 等显式放宽。真实和模拟包分开导出。
- 同一数据集要求任务与指令、用臂、视角、分辨率、采样率、单位和对齐阈值一致。合并不同采集场次时保持这些设置相同。
- 每个时间步包含 `observation.state`、`action`、`observation.images.<视角>`，以及标准 `index`、`episode_index`、`frame_index`、`timestamp`、`task_index`。语言任务由 LeRobot 从 `task_index` 解析。每侧 7 维依次为六关节绝对角度（rad）及夹爪宽度（m）。双臂共 14 维，先左后右。
- 图像使用 LeRobot 的 image 特征。裁剪范围、源 UUID、人工标签和动作来源保存在 `piperx/provenance.json`、`piperx/rows.jsonl`。训练时按 episode 划分集合，随后由模型加载器从同一连续轨迹构造 action chunk。π 系列或 SmolVLA 的相机键、动作维度、归一化与时间窗口仍需各自配置。
