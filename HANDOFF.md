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

## 2026-10-02 第二轮返工：默认改成可拖动缩放的窗口模式

上一轮把默认设成全屏无边框，实际用起来是倒退：一进来就占满屏幕、切不出去也没法拖动缩放。这一轮按
"窗口管理优先于无边框"重做：

| 改动 | 说明 |
| --- | --- |
| 默认窗口模式 | `Settings.window_fullscreen` 新增，默认 `False`；`main.build` 用它构造 Shell，标题栏留着，可拖动、可缩放、可关闭 |
| 全屏变成可选项 | 设置页新增「界面窗口模式」开关；`Api.win_fullscreen(on)` 切换并把选择写回 `settings.json`，切换没生效时提示"再点一次"而不是谎报引擎不支持 |
| 标题条跟随真实窗口状态 | 后端切完窗口状态直接点一下页面里的 `__raSyncFrame()`（不再等页面自己发现）；另留 resize 监听 + 2s 兜底轮询 |
| 启动时把窗口拉回屏幕内 | Chromium 的窗口档案会记住"最小化/上次位置"，实测本机的档案被记成最小化后，双击启动只剩任务栏一个按钮；`Shell._ensure_on_screen` 先让 Chromium 自己回到 `normal`（外部 `ShowWindow(SW_RESTORE)` 会被它的状态机改回去），再兜底 `SetWindowPos` 摆回工作区 |
| 窗口句柄缓存 + 进程过滤 | `_probe` 认出 HWND 后缓存，瞬时枚举不到会在下次探测补上；`find_window` 先按浏览器进程过滤再读标题，避免被上一次自检遗留的卡死同名进程拖住 |
| 关掉固定视口 | `no_viewport=True`，窗口模式下视口 = 客户区减去浏览器自带标题栏（实测 31px），全屏时 = 整个客户区 |

| 层次 | 命令 | 结果 |
| --- | --- | --- |
| 单元 + 契约 | `python -m unittest discover -s tests -t .` | 77 项全部通过（新增 `tests/test_window_mode.py` 4 项：默认窗口、切换会记住、不支持时不写、无窗口时如实报告） |
| 后端链路自检 | `./录放台.exe --selfcheck` | `passed=12/12 failed=0`，退出码 0 |
| 封装版界面自检（被污染的真实档案） | `./录放台.exe --verify` | 53 行 OK、0 FAIL、退出码 0——启动即 `rect=(120,60,1480,960)`、`iconic=False`，最小化档案被自动拉回 |
| 封装版界面自检（空 `LOCALAPPDATA`） | `LOCALAPPDATA=临时空目录 ./录放台.exe --verify` | 53 行 OK、0 FAIL、退出码 0，产出 4 张截图，`settings.json` 末尾 `window_fullscreen: false` |
| 窗口实测证据 | 同上 | 窗口模式 `rect=(120,60,1480,960) ≠ monitor=(0,0,1920,1080)`、`caption=True`；切全屏 `rect == monitor`、`caption=False`、`cls=frameless`；再切回窗口模式 |
| 用户路径证据 | 同上（点设置页开关，不碰内部接口） | 点一下 → `on=True, txt=全屏, bar=True, 视口 1920x1080`；再点一下 → `on=False, txt=窗口, bar=False, 视口 1344x861` |
| 视口实测证据 | 同上 | 窗口模式 `1344x861` vs 客户区 `1344x892`（顶部 31px = 浏览器标题栏）；全屏 `1920x1080` vs 客户区 `1920x1080` |

自检踩到的两个坑，记下来免得再犯：① 在页面里 `await` 一个需要窗口线程回包的调用会自锁（`evaluate` 占着泵，被排队的 `_remote` 永远等不到），所以点击与读取必须分成两次调用；② 依赖固定 `sleep` 的窗口断言会随上一次运行留下的档案状态漂移，改成"轮询到目标状态为止"。

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

## 2026-10-03 交付版：真无边框 + Agent 接口进程序本体

这一轮的目标是把它从"能用"推到"可以直接交给别人用"。三件事：窗口真的没有系统边框、
Agent 接口成为程序自身的一部分、以及把上一轮遗留的静默失效路径堵死。

### 1. 无边框：从"只能全屏"改成"把浏览器标题条顶出屏幕"

上一轮的结论是"Chromium 的 `--app` 标题条只有进全屏才不画"，所以默认形态要么全屏、要么露着浏览器标题条。
这一轮实测推翻了"只能这样"：那条标题条画在 **Win32 客户区内部**（窗口 1100×760 → 客户区 1084×752 →
视口 1084×721），所以只要把窗口顶边放到屏幕外，它就不在屏幕上，而内容正好从屏幕第一行开始。

| 试过什么 | 实测结果 |
| --- | --- |
| 清 `WS_CAPTION`/`WS_THICKFRAME`/`WS_SYSMENU` + `SWP_FRAMECHANGED` | 样式位变了，画面里那条仍在（它是 Views 画的，不吃非客户区） |
| `SetWindowRgn` 裁掉顶部区域 | 返回成功但完全无效 —— Chrome 走 DirectComposition，窗口区域被合成路径忽略 |
| 找 Chromium 的去标题条开关 | 在 `chrome.dll` 里搜 `custom-frame` / `hide-titlebar` / `*-title-bar` 一类特征串，只有 `windows11-mica-titlebar` 与 `OpaqueBrowserFrameView`，没有可用开关 |
| CDP `Browser.setWindowBounds` 设负 top | **有效**：窗口顶边 -31 时屏幕第 0 行就是页面像素（用 GDI 逐行取色证明） |
| Win32 `SetWindowPos` 设负 top | 同样有效 —— 所以 CDP 不可用时还能兜底 |

据此实现的是**停靠无边框**：内容矩形是唯一事实，窗口边界 = 内容矩形按四边留白与标题条高度换算后
往上挪一条标题条。移动与缩放由界面自己画的手势驱动（标题条拖动、四条边与四角手势），
最小化/全屏走 CDP，关闭走退出确认框。三种形态：停靠无边框（默认）/ 全屏 / 自由窗口。

两个坑，都留了实测证据：

- **标题条高度不能只信几何差值。** 启动那几秒浏览器档案会连着改窗口尺寸，一次读到的
  「客户区高 − 视口高」会漂（实测把 76 读成 31 或反过来），差 45 像素就是把界面顶部切掉 45 像素。
  现在 `_measure()` 要连读到两次完全一致的样本才算数，并且加了一道**像素标定**：
  页面最上面画一条标记色横条（只在自检与启动时出现），看它在屏幕上出现在第几行就是真实高度，
  摆正之后再复核一次，标记条必须正好落在屏幕第一行（`drift=0`），有偏差就当场修正 `strip` 重摆。
  这条复核只走 GDI `GetPixel`，不引入第三方库，封装版里同样能跑。
- **Playwright 的对象只能在窗口线程上用。** 界面手势最初直接调 `_place()`，从 API 线程发 CDP 命令，
  现场是 `greenlet.error: Cannot switch to a different thread`，之后 CDP 会话被弄坏，
  切全屏静默失效。现在 `win_move` / `win_resize` / `win_gesture_end` / `win_remeasure` 全部排队到窗口线程，
  并且 `_send_bounds()` 一次失败会先重开 CDP 会话再退回 Win32；`win_mode()` 切换后按**实测**判断成没成，
  没成就返回 `ok:false` 与原因，不再谎报成功。

### 2. Agent 接口：跑在界面同一个进程里

新增 `ra/agentapi.py`：与 `agent/server.py` 同一份契约（`/api/health`、`/api/agent/tools`、
`/api/agent/manifest`、`POST /api/agent/tool`），但直接复用界面的 `Api`/`Session`。
好处是实打实的：Agent 发起的录制与运行会实时反映在界面上；也不会和界面抢同一个受控浏览器档案目录
（独立跑 `agent/server.py` 时抢不到档案只能报错）。工具表仍是 `agent/tools.py` 那一份，两种跑法共用。

工具从 8 个扩到 25 个，覆盖界面能做的每件事：`ra.workflow_schema`（把 Schema、动作目录、最小示例与
全部规则直接给 Agent，不用读源码）、`ra.app_state` / `ra.window_state` / `ra.window_mode`、
`ra.record_start` / `ra.record_stop` / `ra.draft` / `ra.edit_step` / `ra.move_step` / `ra.remove_step` /
`ra.add_step` / `ra.save_draft`（剪辑那一段全流程）、`ra.run_status` / `ra.cancel_run`、
`ra.delete_workflow` / `ra.set_secret` / `ra.delete_secret`。`ra.run_workflow` 支持 `wait_s:0` 只发起不等待。

上一轮"删除类不暴露、秘密值只能界面里填"是那个阶段的保守选择。这一轮按交付要求放开：
删除与写秘密值都开放给 Agent，但**每一个 write/exec 都必须显式 `confirm:true`**，
秘密值仍然**只进不出**（返回里只有引用名与长度）。清空浏览器登录态仍然只在界面里做。

界面里新增「Agent 接口」一屏（`Alt+4`）：端点、25 个工具的名字/风险档/干什么、必填参数、
可直接复制的 curl 与 MCP 配置片段，以及一个「自检一次调用」按钮 —— 它真的对自己发一次 HTTP 请求，
把 HTTP 状态、耗时和数出来的工作流条数显示出来，而不是报告"服务应该起来了"。

`agent/launch.json` 与 MCP 桥跟着改了：桥优先连已经在跑的服务，连不上才按
`python agent/server.py` → `录放台.exe` 的顺序尝试拉起，每种方式的失败原因都会写进错误里。

### 3. 上手体验

- 工作台顶部新增「三步就能用起来」：按真实进度标出已完成/下一步，每步一句话说明它在干什么，
  按钮直接把人带到该去的地方；收起了在设置页可以随时放回来。
- 设置页「界面窗口形态」改成三档分段选择，并新增「当前窗口实测」一行：模式、标题条高度、
  内容区与窗口矩形、像素复核结论都写成人话，不达标时当场写明偏差。
- 标题条四个按钮（最小化 / 铺满工作区 / 全屏 / 关闭）的提示文字与实际语义一致；
  关闭先弹应用内确认框，正在录制或运行时会说明"会停在当前步骤、不会自动续跑"。
- 侧栏与统计卡显示 Agent 接口是否在线与工具数。

### 4. 这一轮踩到的两个静默失效

- **`Shell._probe()` 读了一个只在成功路径里才初始化的字段**（`self._cdp_error`），
  于是 `win_state()` 每次都抛 AttributeError，界面拿不到窗口状态、自检只看到一片 None。
  现在所有诊断字段都在构造函数里初始化，并加了一条不需要浏览器的单测：
  直接 `Shell(...)._probe()` / `_place()` 必须返回完整字段而不是抛错。
- **app.js 里一个字符串提前闭合**（`class="k"` 的双引号没转义）让整屏界面静默不初始化：
  Python 侧一切正常，界面停在"正在连接后端…"。已修，并加了 `tests/test_ui_scripts.py`
  这道语法门（用 node 解析 `app.js` / `record_script.js`）+ 界面关键结构断言，
  同类错误以后在打包前就会被拦住。

### 5. 一条没被修掉的引擎边界（如实记录）

无边框停靠要求窗口顶边贴住工作区顶边，因此标题条拖动只能改水平位置；且 Chromium 会不定期把自己
记住的窗口位置刷回来。实测：`win_move` 把窗口从 x=280 摆到 x=340 稳定保持；界面上的真实
pointer 拖动把 x=340 摆到 x=400，`_place` 的核对循环当场看到过 400（`place_error` 为空），
但随后窗口又回到 340，且第二次、第三次摆放请求不再到达后端（被第一次的核对循环占住窗口线程）。
试过并排除的方向：CDP 与 `SetWindowPos` 两条通路都试过、每次摆放前重取 `windowId` 也试过、
收尾时反复重摆会把后续桥调用全堵死（已撤掉）。最终选择：
**不与它抢水平位置** —— 自愈只在「浏览器标题条重新露出来」时补位；
自检断言也改成可保证且可失败的那件事（手势必须到达后端并发出摆放），不再断言位置一定保持。

### 6. 本轮实测数字

| 层次 | 命令 | 结果（本机实测） |
| --- | --- | --- |
| 单元 + 契约 + 界面脚本门 | `python -m unittest discover -s tests -t .` | **91 项全部通过**（新增 `tests/test_ui_scripts.py`、`tests/test_window_mode.py` 的几何与自愈断言） |
| 后端链路自检（源码） | `python -m ra.main --selfcheck`（`LOCALAPPDATA` 指向空目录） | `passed=12/12 failed=0`，退出码 0 |
| 窗口内界面自检（源码） | `python -m ra.main --verify`（同上隔离） | 72 项通过、1 项未通过（拖动手势后被 Chromium 刷回原位，已如实记为引擎边界），退出码 1 |
| 封装版后端链路 | `./录放台.exe --selfcheck`（`LOCALAPPDATA` 指向空目录） | `passed=12/12 failed=0`，退出码 0 |
| 封装版界面自检 | `./录放台.exe --verify`（`LOCALAPPDATA` 指向空目录） | **73 项全部通过、0 失败、退出码 0**；产出 5 张界面截图（含 ui-agent.png） |
| Agent 真跑 | 界面进程内 `POST /api/agent/tool` 全链路 | **15/15 通过**：`ra.workflow_schema` → `ra.save_workflow`（缺 confirm 被拒）→ `ra.run_workflow` 真回放 4 步 `completed_unverified` → `ra.run_status` → `ra.set_secret`（返回不含秘密值）→ `ra.delete_workflow` / `ra.delete_secret` → `ra.window_state` 报告 `titlebar_hidden=true, pixel_ok=true` |
| 窗口形态切换 | 连续六次 `free → docked → fullscreen → docked → free → docked` | 每次都 `ok:true` 且实测形态正确（自由窗口如实露出浏览器标题条，其余两种顶出屏幕） |

这一轮修掉的静默失效（都有断言兜着）：`_probe()` 读未初始化字段导致窗口状态全 None；
app.js 一处引号未转义让整屏界面停在"正在连接后端"；界面手势从 API 线程直接调 Playwright
触发 greenlet 跨线程错误并弄坏 CDP 会话（全屏因此失效）；`_place()` 不带参数时不按新模式重新
归一化，导致自由窗口切回停靠时沿用旧顶边；`ra.delete_secret` 因为 `delete()` 无返回值而谎报失败；
`ra.add_step` 把 `fill` 的内容写成 `value`（Schema 要 `text`）会被自己的校验层拒掉。

## 2026-10-04 交互修复轮：「很多按钮点不了 + 排版不好看」

上一版交付时全部绿灯，用户一上手就说按钮点不动、Agent 屏一片空白。真实原因有四个，
每一个都是绿色检查看不见的（它们只数 DOM 里有没有这个节点，不看这个节点在屏幕上是什么形状）：

| 原因 | 现象 | 修法 |
| --- | --- | --- |
| `.view` 是纵向 flex，卡片默认 `flex-shrink:1` | 内容多一屏就装不下：每张卡被压成 ~28px 高的空壳，内容溢出盖到下一张卡片上面 —— 按钮看着在，鼠标点到的是别人；更靠下的按钮直接被推出视口（体检报 `covered: null`） | `.view.scroll > * { flex: none; }`，让卡片按自然高度排、整屏滚动 |
| Agent 屏两张卡只有 `class="card"`，而 `.card` 本来不带内边距；`<p class="s">` 也没有独立规则 | 整屏像没样式的裸文本，顶在边框上 | 卡片补 `agent-card` 内边距、补 `.s` 字号层级、给工具清单加表头 |
| 4 个设置页按钮的处理器没有 `catch`，端到端自检按钮先 `disabled=true` 再 `await` | 后端一拒绝（比如正在录制）就是「点了没反应」，自检按钮还会永远停在「自检运行中…」 | 全局 `unhandledrejection` 兜底提示（`call()` 已提示过的用 `error.notified` 去重），自检按钮改 `try/finally` |
| 缩放手势 `z-index:95` 压在内容上、`navigator.clipboard` 无焦点时抛错 | 底部一行按钮被 `#gs` 接走、复制按钮弹出文本框 | 手势容器 `pointer-events:none` 只留把手、标题条按钮抬到 `z-index:120`、设置区留底部内边距、复制先退到 `execCommand` |

新加的门（都能失败，改坏就红）：

- `ra/uiaudit.py` + `python -m ra.main --audit`：五个界面逐个滚到眼前 →
  `elementFromPoint` 命中测试 → 真点一次 → 要求可观察变化；`covered / dead / noeffect / throws`
  都算失败。会改数据、开浏览器、动窗口的 25 个后端调用由体检层拦下，可以在真实数据目录上反复跑。
- `ra/uiverify.py` 新增一条「每张卡片都装得下自己的内容」（`scrollHeight <= clientHeight`）——
  这一条单独就能抓住整场 flex 压扁。
- `tests/test_ui_layout.py` 14 条：flex 不许收回、`.s` 必须有规则、把手不许吃点击、
  窗口形态文案必须说人话、剪贴板必须有退路、自检按钮不许卡住、体检工具不许被悄悄削弱。
  删掉 `.view.scroll > * { flex: none; }` 验证过：门会变红。

排版另两处：`.eyebrow` 的 `letter-spacing:2px` 会把中文拉开成「工 作 台」，中文部分单独收回；
设置页「当前窗口实测」原来把 `-31992×-32000×144×20` 这种数字直接甩给用户，现在一句人话 +
一行小字放实测数字，窗口不在屏幕上时明说并给「重新测量」这个出口。

### 本轮实测数字（本机，2026-10-04）

| 层次 | 命令 | 结果 |
| --- | --- | --- |
| 单元 + 契约 + 界面脚本 + 排版门 | `python -m unittest discover -s tests -t .` | **106 项全部通过** |
| 按钮体检（源码 + 封装版） | `python -m ra.main --audit`、`./录放台.exe --audit` | **71 个按钮、71 个点了有反应、0 个问题**，退出码 0（明细 `%LOCALAPPDATA%\RecordedAutomation\audit-report.json`） |
| 窗口内界面自检（源码） | `python -m ra.main --verify` | 72 项通过、2 项未通过（见下） |
| 封装版后端链路 | `./录放台.exe --selfcheck` | `passed=12/12 failed=0`，退出码 0 |
| 封装版界面自检 | `./录放台.exe --verify`，连跑两次 | 两次都是 **72 项通过、同样的 2 项未通过** |
| 交付 exe | `录放台.exe` | 92,339,092 字节，sha256 前缀 `e0287f960b20ddd4`，与 `exe-dist/` 内构建产物逐字节一致 |

那 2 项未通过是同一条链路上的两个探针，都不是用户在屏幕上看到的缺陷：

- 「像素复核 drift=0」要往页面最上面画一条标记色横条，再用 `GetPixel` 反查它出现在第几行 ——
  前提是应用窗口没被别的窗口盖住。这台机器上多个 AI 应用会抢前台，被盖住时取到的是别人的像素，
  这次 `window-debug.txt` 记的就是 `calibrate=屏幕上没找到标定标记条` → `drift=-1` → strip 从 31 追到 33。
  不依赖屏幕取色的三条几何实测全部通过：`titlebar_hidden=True`、`窗口顶边+标题条高度==内容顶边`、
  `内容顶边贴住工作区顶边`。
- 「拖标题条 = 横向移动窗口」是上一轮就记下的引擎边界（Chromium 会把自己记住的位置刷回来）；
  界面保证的是浏览器标题条绝不重新露出来，不是水平位置一定保持。

半路试过并已撤掉的改法：把像素校正改成「按新的实际内容矩形重画重测」的收敛循环。它把 `drift`
当成 1 像素级偏移，而 `_verify_marker` 的 `-1` 其实是「找不到标记条」的哨兵值，于是循环把 strip
从 33 推到 37，反而弄坏了原本通过的检查 —— 已回退成单次校正。**下一轮该修的是这个返回值本身**：
把「找不到」和「差几像素」分成两种结果，找不到就明说「窗口被遮住，这条测不了」，而不是伪装成 1 像素偏差。

自检工具链自己的两处不可靠也一并修了：`--verify` 过去会顺手把 `headless` 写进用户设置
（现在测完写回原值；本机被之前几次运行留在 `headless=true`，已改回 `false`，否则录制时看不见页面）；
窗口形态那一段用固定 sleep 读界面，读到的是上一次切换的状态，整套检查会整体错一格——
现在改成等到页面自己说出目标形态再断言，并在测量前把形态切回默认（用户可能把窗口留在自由模式）。

## 2026-10-05 验收返工第一轮：Agent 接口从「本机任意进程」收紧成「本机带令牌的进程」

评审给的是 BLOCKER，形状很清楚：**用户访问的任意网页都能驱动全部 25 个工具**。
`ra/agentapi.py` 与 `agent/server.py` 每个响应都发 `access-control-allow-origin: *`，
没有 Host / Origin / 令牌校验，服务还默认开着（`store.py` 的 `"agent_api": True`、`main.py` 起它）。
最要命的是 `ra.record_start`（任意 URL）+ `ra.draft` 这一对 —— 对着那份装有真实登录态的
受控档案敲键盘；`ra.set_secret` / `ra.run_workflow` / `ra.delete_workflow` 同理。
唯一的"闸门" `_require_confirm` 只看请求体里的 `"confirm": true`，而那个值由发起请求的人写。

### 判据只写一份：`ra/localguard.py`

两个入口必须过同一道门，否则攻击者挑没设防的那一个就行，所以判据收在新模块里，
`ra/agentapi.py` 与 `agent/server.py` 都 import 它（`server.py` 从此不再是"与模板逐字相同"的复制件，
文件头写明了偏离的两条，别在同步模板时改回去）：

| 闸门 | 判据 | 拒绝时 |
| --- | --- | --- |
| 绑定 | 只 bind `127.0.0.1`（原来就是） | — |
| `Host` | `127.0.0.1:<port>` / `localhost:<port>` / `[::1]:<port>`，端口带了必须等于自己这一台 | 403 `host_not_allowed` |
| `Origin` / `Referer` | 不发 = 非浏览器客户端，放行；发了必须是回环 http(s) 源 | 403 `origin_not_allowed` |
| 令牌 | 所有非 GET 必带 `x-agent-token`（`authorization: bearer` 也收），`hmac.compare_digest` | 401 `token_required` |
| CORS | 任何响应都不发 `access-control-allow-origin`；`OPTIONS` 不给 ACAO / allow-methods / allow-headers | 网页卡在预检 |

**`Origin` 只与回环清单这个常量比，绝不与本次请求的 `Host` 比。** 评审点名的 DNS rebinding 就是这个形状：
攻击者把域名解析到 127.0.0.1 之后，浏览器发来的 `Host` 与 `Origin` 恰好都是那个域名，
"两者一致就放行"等于把门开着。回归 `test_dns_rebinding_shape_is_refused_even_when_origin_matches_host`
先断言这两个值确实相等，再断言服务拒绝 —— 谁将来"顺手加个 Origin==Host 的校验"都会红在这一条上。

### 令牌存哪、为什么不是别的地方

`localguard.load_token()`：文件已有且形状对就用，否则生成 32 字节随机值原子落盘（`os.open(..., 0o600)`）。
默认位置 `%LOCALAPPDATA%\RecordedAutomation\agent-token`（非 Windows 走 `$XDG_DATA_HOME`）。
两个入口读同一个文件，所以"录放台开着 + 又单独起 `agent/server.py`"是同一把令牌，不需要谁通知谁。

- **不放仓库里**（`agent/.endpoint` 旁边那种做法）：仓库会被同步、被打包进 exe、被 `git add -A`，
  而这个文件的全部价值就是"只有本机进程读得到"。评审给的另一个例子（`%LOCALAPPDATA%`）才是对的。
- **`agent/.endpoint` 保持一行 URL**：personal-agent-hub 的 `register-agent-apis.mjs` 把整个文件 trim
  之后当 URL 用，加第二行就把它读坏了。`/api/health` 只报 `token_file` 这个**路径**，不报值。
- Windows 上这份"限制"来自 %LOCALAPPDATA% 的目录 ACL（当前用户 + 管理员组），不是 `chmod`
  （NTFS 上 `chmod` 只动只读位）。这条如实写进模块 docstring，不夸大成"加密"。
- 出口：`RA_AGENT_TOKEN_FILE` 换位置、`RA_AGENT_TOKEN` 直接给定，测试与编排器用；正常运行不需要。

### 合法路径不许多一步

`agent/mcp-server.mjs` 每次调用现读令牌文件（`tools/call` 带、`tools/list` 是 GET 不带），
读不到就把候选路径与出路写进错误里。桌面程序的界面也照同一口径改了：
「Agent 接口」那一屏多一格"鉴权"（显示令牌文件位置 + 已拦下多少次外部调用），
可复制的 curl 片段按 `$LOCALAPPDATA/.../agent-token` 现读、绝不把令牌原文写进界面或剪贴板，
而 `自检一次调用` 现在带令牌 —— 它证明的是"合法本机路径走得通"，不再只是"端口开着"。

`confirm:true` 保留，但按标准的口径说清楚它的身份：那是给 Agent 的**二次确认（防手滑）**，
不是安全边界；顺序是 Host → Origin → 令牌 → schema → confirm。README 与 `agent/README.md` 都改了这句。

### 本轮实测数字

| 层次 | 命令 | 结果 |
| --- | --- | --- |
| 单元 + 契约 + 防护闸门 | `python -m unittest discover -s tests -t .` | 见文末汇总 |
| 防护闸门单跑 | `python -m unittest tests.test_agent_guard` | **23 项全部通过** |
| 红→绿对照（评审要求的"退回修复前"实验） | `git checkout HEAD -- ra/agentapi.py agent/server.py` 后重跑那一组 | **23 项红 15 项**（11 FAIL + 4 ERROR），下面逐条列 |
| 整链自检 | `python -m ra.main --selfcheck` | 见文末汇总 |

退回去之后每一条是怎么红的（不是推理，是把服务起起来真打的）：

- `test_spoofed_host_is_refused` → `AssertionError: 200 != 403 : 伪造 Host evil.test 居然过了`
- `test_rebinding_pair_of_host_and_origin_is_refused` → `200 != 403`
- `test_cross_origin_post_is_refused` / `test_cross_origin_get_is_refused_too` → `200 != 403 : http://evil.test 的跨源 POST 居然过了`
- `test_post_without_token_is_refused` / `test_post_with_wrong_token_is_refused_the_same_way` → `200 != 401`
- `test_every_registered_write_tool_is_unreachable_without_a_token` → `(400, 'bad_input') != (401, 'token_required')`
  —— 这一条最能说明问题：没带令牌的请求**已经打到工具层**了，只差参数不对。
  （`input` 刻意传空对象：万一防护不在，`ra.record_start` 也不会真的开一个浏览器去录陌生 URL，
  这台机器上那会抢前台。带修复的版本回 401，证明闸门排在参数校验之前。）
- `test_no_wildcard_acao_on_mutating_routes` → 响应头里当场抓到 `'access-control-allow-origin': '*'`
- `test_browser_preflight_cannot_open_the_door` → 预检回的是 `access-control-allow-headers: content-type`
- `test_standalone_*` 三条 → 独立进程同样 `200 != 403 / 401`，证明 `agent/server.py` 那半个洞原来也开着
- `test_denied_requests_are_counted_...` / `test_legit_get_needs_no_token` → `AttributeError: denied`、
  `KeyError: 'auth'`（那一版根本不记账、也不宣告自己的鉴权方式）
- `test_endpoint_file_stays_a_single_url_line` → 令牌文件不存在（那一版不写它）

跑法上的两个坑，下一个人别再踩：

1. 服务只在临时目录里跑：`LOCALAPPDATA`、`RA_AGENT_TOKEN_FILE`、`RA_AGENT_ENDPOINT_FILE` 全部指到 tempdir。
   上面那次"退回修复前"的实验把仓库里的 `agent/.endpoint` 覆盖成了测试端口（旧版不认这个 env），
   已改回 `http://127.0.0.1:8795`。**新代码认这个 env，所以测试不会再碰仓库里那一份。**
2. 断言一律写成 `body.get("error", {}).get("code")` 而不是 `body["error"]["code"]`：
   防护不在时响应是 200 + data，直接下标会让测试在夹具里炸掉，评审要的"每条都能红"就变成"整组 ERROR"。
