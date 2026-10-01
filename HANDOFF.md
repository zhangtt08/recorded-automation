# 交付说明（原交接任务的实际完成情况）

共同契约：[ARCHITECTURE.md](ARCHITECTURE.md)、[ra/workflow.schema.json](ra/workflow.schema.json)、[ra/core.py](ra/core.py)。
执行 `python -m unittest discover -s tests -t .` 跑全部测试；`python -m ra.main --verify` 跑窗口内自检；
封装版在设置页点「运行端到端自检」。任何模块都不把密码明文写进工作流或日志，结果不确定时不自动重做 UI 动作。

## 四项交付的落位

| 顺序 | 负责人 | 交付物 | 实现位置 | 验收结果 |
| --- | --- | --- | --- | --- |
| 1 | Agent A：浏览器会话与录制 | 受控 Playwright 浏览器；`add_init_script` 录制脚本；`expose_binding` 事件桥；来源校验；录制开始/停止 | `ra/session.py`、`ra/recorder.py`、`ra/ui/record_script.js` | 单标签页 click/input/change/导航形成候选事件；其他 origin 被丢弃并列出原因；密码值不落盘；停止后重新录制可继续（绑定名复用，不重复注册） |
| 2 | Agent B：归并与工作流编辑 | 候选事件 → `Workflow v1`；候选定位器排序与唯一性检查；审阅/修复界面；JSON Schema 校验 | `ra/normalizer.py`、`ra/ui/index.html`、`ra/ui/app.js` | 录制 `click → fill → click` 输出可人工审阅的 JSON；多匹配标为待修复并保留候选；连续输入合并为一次 `fill`；普通文本与秘密值分开 |
| 3 | Agent C：回放适配器 | `Driver` 的 Playwright 实现；目标定位、frame/page 映射、动作执行、origin 再检查；`SecretStore` 与持久 `Journal` | `ra/driver.py`、`ra/secrets.py`、`ra/journal.py` | 跑通上面的工作流；目标消失/多匹配/跨 origin 时停在动作之前；动作失败后不重复点击；重启后不自动续跑 |
| 4 | Agent D：集成与打包 | 会话状态机、运行结果页、取消入口、独立用户数据目录与本地配置 | `ra/session.py`、`ra/shell.py`、`ra/api.py`、`ra/winframe.py`、`build/录放台.spec` | 录制与回放互斥；界面可发起、停止、查看失败步骤；单文件封装版双击可用，默认全屏无边框、窗口控制按钮内置在标题条里 |

## 验收证据（本机实测）

| 层次 | 命令 | 结果 |
| --- | --- | --- |
| 单元 + 契约 | `python -m unittest discover -s tests -t .` | 54 项全部通过 |
| 后端链路自检 | `python -m ra.main --selfcheck`，或设置页按钮 | 12 项全部通过 |
| 窗口内界面自检 | `python -m ra.main --verify` | 29 项全部通过（含保存 → 运行 → 时间线 → 日志 → 删除的完整用户路径） |
| 封装版 | `dist/录放台/录放台.exe --verify`，同时把 `LOCALAPPDATA` 指向空目录 | 35 行 OK、退出码 0：不依赖本机浏览器缓存，也不读写本机数据目录 |

封装版自带 Chromium（`ms-playwright/`，约 420MB）；该目录缺失时自动回退到本机 Edge，
两者都不可用时界面会给出修复指令。界面截图会写到
`%LOCALAPPDATA%\RecordedAutomation\ui-shots\ui-{deck,recording,review,run}.png`。

## 2026-10-02 复核（工程化整理之后重新量的数）

上表是首轮交付当天的记录，仍然有效；这一轮加界面与 Agent API 之后重新跑，数字如下（本机实测）：

| 层次 | 命令 | 结果 |
| --- | --- | --- |
| 单元 + 契约 | `python -m unittest discover -s tests -t .` | 73 项全部通过（新增 `tests/test_run_control.py`、`tests/test_agent_api.py`） |
| 后端链路自检 | `python -m ra.main --selfcheck`（`LOCALAPPDATA` 指向空目录） | `passed=12/12 failed=0` |
| 窗口内界面自检 | `python -m ra.main --verify`（同上隔离） | 45 行 OK、0 FAIL、退出码 0（含新增的「审阅页可试跑」「改定位器只重画该卡」「跑完不显示从失败步重试」三项） |
| Agent API | `python agent/server.py` + curl；`node agent/mcp-server.mjs` 三条握手 | 8 个 `ra.*` 工具；健康/清单/成功调用/缺参数 `bad_input`(400)/未知工具带 `available` 全部符合契约；MCP 的 initialize、tools/list、tools/call 均有响应 |
| Agent 真跑 | `ra.save_workflow` → `ra.run_workflow` → 失败 → `from_step` 续跑 | completed_unverified 三步时间线、failed/TargetTimeout 停在动作之前、续跑跳过 3 步只执行剩余步骤，全部为真实执行结果 |

## 2026-10-02 一体化窗口返工（针对"多了一层系统边框"）

首轮用 Win32 样式硬去掉 `WS_CAPTION`，实测无效：Chromium 的 `--app` 窗口自己画标题栏，
清样式后画面里那条标题栏仍在（`PrintWindow` 与屏幕截图都看得到），只是样式位变了 ——
所以旧自检里那条"无边框"检查是空断言，一路绿灯却骗过了人。这一轮改法与实测：

| 改动 | 依据 |
| --- | --- |
| 窗口状态改由 CDP `Browser.setWindowBounds` 控制，默认进入 `fullscreen` | 只有 Chromium 自己处于全屏时才不画标题栏；实测切全屏后 `WS_CAPTION=False` 且窗口矩形 = 显示器矩形 |
| 关掉 Playwright 的固定视口（`no_viewport`） | 之前页面被锁在 1280×720，全屏后四周留白；现在视口 = 1920×1080 |
| `ra/winframe.py` 只保留只读探测（找窗口、读样式、读窗口/显示器矩形） | 删掉 `strip_frame/place/drag/toggle_maximize` 等已被证明无效的写法，避免下一个人再踩 |
| 自检改成不依赖被检对象的黑盒断言 | 新增「Win32 实测：窗口没有系统标题栏」「窗口铺满整块显示器」「页面按真实窗口尺寸渲染」三项，直接读 Win32 与 `innerWidth×dpr` |

| 层次 | 命令 | 结果 |
| --- | --- | --- |
| 单元 + 契约 | `python -m unittest discover -s tests -t .` | 73 项全部通过 |
| 后端链路自检 | `./录放台.exe --selfcheck` | `passed=12/12 failed=0`，退出码 0 |
| 封装版界面自检 | `./录放台.exe --verify`（`LOCALAPPDATA` 指向空目录） | 48 行 OK、0 FAIL、退出码 0；三项窗口实测证据为 `caption=False`、`rect=(0,0,1920,1080)=monitor`、视口 1920×1080@1 |

窗口按钮语义：`最小化` → CDP `minimized`；`窗口/全屏` → 在 `fullscreen` 与 `normal` 之间切换（切到
窗口模式时浏览器会带回它自带的标题栏，这是 Chromium 的限制，界面按钮的提示文字已写明）；`关闭` → 退出整个程序。

## 首轮集成用例的实测结果

| 用例 | 结果 | 证据 |
| --- | --- | --- |
| 1 同来源表单：填普通文本并提交，等待成功提示 | 通过 | 设置页自检：录到 5 步、回放执行到结束、提交成功提示出现 |
| 2 目标匹配 0 个或 2 个元素 | 通过 | 自检：多匹配 → `failed / AmbiguousTarget` 且没有 `action_started`；缺失目标 → `failed / TargetTimeout` |
| 3 点击后页面卡住或导航 | 部分 | 单元层已证明「动作异常/完成条件超时 → `uncertain` 且动作只发一次」（`tests/test_core.py`）；真实站点上"点完就挂住页面"的场景未构造，未执行 |
| 4 录制焦点切到其他 origin | 通过 | 自检：候选 0 条、丢弃 ≥1 且原因记为来源不符 |
| 5 输入密码 | 通过 | 自检：步骤只含 `secret_ref`；工作流文件与 `journal.jsonl` 中不含明文 |

界面侧另有 20 项窗口内检查（结构、导航、确认框、桥接调用、录制开始/停止），见 `ra/uiverify.py`。
说明：界面检查是程序化地在真实应用窗口里读写 DOM 完成的，不是人工点击逐页走查。

## 契约注意事项的落实

- `Driver.resolve` 只定位不动作，返回可再次解析的 Locator（Playwright Locator 本身惰性），要求唯一且可见；`resolve(target, timeout_s)` 把每步的超时预算带到动作调用上，每个 Playwright 调用都有上限。
- `Driver.act` 只执行一次，动作前再次核对页面 origin 与目标数量；origin 用解析后的 scheme/主机/端口比较，不用字符串前缀。`<select>` 用 `select_option`，`fill` 会拒绝它。
- `Journal.append` 对 `action_started` 先落盘（`flush` + `fsync`）再返回；日志只含运行/工作流/步骤 ID、阶段、错误码和时间，不含用户输入。
- `SecretStore.get` 在动作即将执行前取值；值用当前 Windows 账户（DPAPI + 附加熵）加密存储，界面只显示引用名与长度；缺引用时运行被直接拦下。
- `expected` 是执行后的可观察完成条件。审阅页可对任意步骤选「完成条件」：引用另一步骤的目标，或按页面文本判断；不设则结果记为「完成 · 未验证」。

## 契约变更与兼容策略

`Workflow v1` 新增可选字段 `name`（界面显示名），并把 `id` 收紧为 `[A-Za-z0-9_-]{1,64}` 以便作为文件名。
旧文件缺 `name` 仍通过校验，读取时回退为 `id`；其余字段未变，已保存的工作流无需迁移。

## 已知边界与后续

- 单标签页、单 origin；跨来源 iframe 只记录主框架之外的显式 `frame` 选择器，未在真实跨源站上验证。
- 未支持：文件上传、拖拽、验证码、下载、新标签页/弹窗、浏览器工具栏操作。
- 停止按钮在**当前动作返回后**生效：阻塞在浏览器内部的调用无法被策略层打断（每步都有超时上限兜底）。
- 定位质量依赖目标站 DOM 稳定性；多匹配一律交给人工，不退化为坐标。
- 可用性验证还差一步：按架构文档要求选 3 个真实流程各跑 10 次并统计成功/失败/`uncertain` 原因。
