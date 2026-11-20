# 方块入盘与现场采集

## 任务定义

任务 ID 默认 `cube_to_plate`，自然语言指令为“把方块放到盘子里”。开场让方块位于盘外，夹爪未夹住方块，主从臂回到已标定的 Home 后停稳。操作者用选定主臂引导从臂抓起方块、移到盘内、释放。结束时方块稳定留在盘内且夹爪已放开，才标记 `success`。仅抓起、放在盘外或掉落后无法完成，标记 `failure`。双臂模式用于需要记录两侧协作的协议，单臂采集和双臂采集分别组建数据集。

同一批次先由负责人约定方块与盘子的可达、安全摆位范围及用臂策略。用几组可复现的摆位做试采，再在工作台标出的范围内变换物体位置。初始机器人关节位姿用相同的 Home，保留场次、任务指令和视角设置；变更任务定义或相机方案时用新场次或分开导出。可在结束备注写摆位编号或影响复核的异常。

## 每条怎么采

1. 启动 `record`，先准备好清空的回零路径。在提示时输入 `HOME`。左、右模式分别操作选中侧的主从对，`both` 按左、右顺序分别回零。
2. 等待 Home 确认，现场确认主臂也已到零位且两臂静止。脚本恢复拖动后等待 `READY`。在现场设备流程允许且两臂已停稳的情况下布置或核对任务物体。保持选中侧关节在 Home，可在 `READY` 后且开录前单独调整夹爪并等它停稳，让预录缓存有机会取得有效夹爪目标。
3. 按 Enter 后等 `RECORDING` 再开始示教。完成后按 Enter 停止，依次填任务结果、备注、是否全程主从跟随、整段轨迹可用性。
4. 等质检、对齐和打包完成。本地可打开 `REVIEW` 打印的 HTML 回看。用 `y` 继续时，下一条重新走回零和人工确认。输入 `n` 或直接回车结束。下次用同一个 `--session` 重跑会创建新 UUID。
5. Esc 或 Ctrl+C 可中止当前录制。已完整关闭的旧轨迹仍保留。中断的本条可能留在 `.incomplete`，强制关闭进程可能留下 `.inprogress`。

| 提示 | 填什么 |
| --- | --- |
| `Result` | `success` 任务完成，`failure` 完成了本次尝试但结果不符合定义，`aborted` 主动中止，`unlabeled` 暂不能判断。 |
| `Termination note` | 可填摆位编号、碰撞、掉落、重抓、相机遮挡等复核信息，也可留空。 |
| `Was leader-follower mode maintained throughout?` | 仅在现场确认整段始终正常跟随时填 `y`。未确认的已关闭轨迹会生成归档包。 |
| `Trajectory usability` | `usable` 示教清楚且适合计划用途，`unusable` 明显抖动、误操作等影响使用，`unreviewed` 需复核，也是回车默认值。三个标签都保留。 |

程序打印的 `PASSED` 表示采集质量检查通过。质检、人为可用性与任务结果是三项独立的信息。一条真正完成入盘的轨迹如果示教过于抖动，仍可标记 `success` 和 `unusable`。

## 常用参数

| 参数 | 说明 |
| --- | --- |
| `--arms left\|right\|both` | 活动臂和回零范围。实机仍要求双 CAN、三相机在线。 |
| `--task-id`、`--instruction` | 任务目录 ID、训练时读取的语言指令。更改任务时两者一起核对。 |
| `--session` | 场次 ID，默认本机日期。多人合并时为操作者分配不同场次 ID。 |
| `--views left_wrist,overview` | 对齐包与 LeRobot 导出的相机。默认三视角。右臂可传 `right_wrist,overview`。 |
| `--depth` | 原始记录中额外保存深度。对齐包和 LeRobot 导出中的图像仍为 RGB。 |
| `--once`、`--review` | 只采一段、生成回看页面。 |
| `--upload` | 显式开启 SSH 上传。`--no-upload` 可兼容旧命令，默认本地打包。 |
| `--simulate --seconds 2` | 仅验证软件链路。真实录制由 Enter 开始及结束。 |

任务 ID 和场次 ID 可用最多 32 个小写 ASCII 字母、数字、下划线或单个连字符，以字母或数字开头。

## Home 后录制未启动

先看 `logs/home-*.json` 与 `quality.json`。若 Home 日志显示 `ready_for_passive_verification`、`restore_write_returned` 为真，且同一对主从臂仍停稳在 Home 并已恢复拖动，可把原命令的 `--enable-homing` 换成 `--resume-home "logs/<成功的 Home 日志>.json"`。该入口只录一条，按 Enter 时重新验证从臂位置和状态。

若回零中断后主臂拖不动，由现场操作员确认运动已停止并清空范围后，使用对应侧的中断日志恢复拖动，再现场检查主从跟随。

```powershell
.\.venv\Scripts\python.exe scripts/home_reset.py --restore-drag left --report "logs/<中断的 Home 日志>.json"
```
