# 录放台 · Agent API / MCP

把录放台当成一个带 schema 的工具对外暴露：本地 HTTP 服务 + MCP stdio 桥。
契约见 `personal-agent-hub/docs/AGENT_API_STANDARD.md`。**只监听 127.0.0.1**，只用标准库 + 项目已有依赖。

## 两种跑法，同一份工具表

```bash
# 1) 随桌面程序一起：双击 录放台.exe（或 python -m ra.main）就已经在监听了
curl -s http://127.0.0.1:8795/api/health
node agent/mcp-server.mjs              # MCP 客户端用法（stdio）

# 2) 不要界面，单独起一个同契约的服务
python agent/server.py                 # 默认端口 8795（AGENT_API_STANDARD 给录放台分配的那一个）
AGENT_PORT=9100 python agent/server.py # 换端口
```

- **推荐第 1 种**：`ra/agentapi.py` 把同一份 `agent/tools.py` 挂在界面进程里，Agent 的录制、剪辑、运行
  会实时反映在界面上，也不会和界面抢同一个受控浏览器档案目录（第 2 种独立跑时会抢，抢不到就按失败返回
  并写明原因）。设置页「Agent 接口」开关控制它，界面里那一屏直接显示端点、工具清单与可复制的接入片段。
- 端口被占用时自动 +1，并把实际地址写进 `agent/.endpoint`（MCP 桥优先读它）。
- 服务没起来时，MCP 桥按 `agent/launch.json` 依次尝试拉起：先 `python agent/server.py`，
  再 `录放台.exe`；都失败会把每种方式的原因写在错误里，并提示"直接打开录放台也行"。
- `server.py`、`errors.py` 是标准模板的复制件（`server.py` 与模板唯一的差别是默认端口 8790 → 8795）。

## HTTP 端点

```
GET  /api/health          项目名、版本、agent_api、工具数、uptime、是否进程内服务、已被调用次数
GET  /api/agent/manifest  项目信息 + 全部工具描述 + base_url
GET  /api/agent/tools     工具清单（name / description / input_schema / risk）
POST /api/agent/tool      {"tool":"ra.xxx","input":{...}} → {ok:true,data:...,tool,ms}
```

失败统一是 `{ok:false,error:{code,message}}`。**工具层抛出的结构化错误一律 HTTP 400**
（`bad_input` / `not_found` / `needs_secret` / `run_refused` / `record_refused` / `mode_refused` /
`cancel_failed` / `stop_failed`），未知工具带 `available` 列表；只有工具内部 unexpected 异常才是 500
（`handler_failed`）。调用方读 `error.code` 与 `error.message` 就够，不需要靠状态码分类。

## 工具清单（25 个）

`risk` 三档：**read** 只读；**write** 会写本机文件；**exec** 会真的开浏览器操作页面。
write / exec 一律要在 `input` 里显式传 `confirm:true`，缺了直接返回 `bad_input`。

| 工具 | risk | 需要什么 | 返回什么 | 真实落点 |
| --- | --- | --- | --- | --- |
| `ra.status` | read | — | 版本、数据目录、工作流/运行计数、秘密引用名、设置、浏览器缓存解析、是否进程内服务 | `ra/paths.py`、`ra/store.py`、`ra/journal.py`、`ra/secrets.py` |
| `ra.app_state` | read | — | 会话状态机（idle/recording/review/running）、草稿步数与阻塞项、界面窗口实测形态 | `Session.snapshot()` + `Api.win_state()` |
| `ra.window_state` | read | — | 标题条高度、内容矩形、窗口矩形、工作区、标题条是否已顶出屏幕、像素复核结果与失败原因 | `ra/shell.py` + `ra/winframe.py` |
| `ra.window_mode` | **write** | `mode` + **`confirm:true`** | 切 docked / free / fullscreen 后的实测结果；没生效就 `ok:false` 并写原因 | `Shell.win_mode()` → CDP / Win32 |
| `ra.list_workflows` | read | 可选 `limit` | 每条工作流的 id/origin/步数/动作类型/未验证步数/秘密引用名与缺失项 | `WorkflowStore.list()` |
| `ra.get_workflow` | read | `workflow_id` | 完整 Workflow v1（每步带编号与可读标签）+ 双层校验 + 定位策略 | `store.load()` + `store.problems()` |
| `ra.workflow_schema` | read | — | JSON Schema、动作与定位器目录、最小示例、保存前全部规则、结果分类含义 | `ra/workflow.schema.json` + `store.problems()` |
| `ra.validate_workflow` | read | `workflow` | 这份定义的校验结果（阻塞项 / 警告 / 缺哪些秘密值），**不写盘** | `store.problems()` |
| `ra.save_workflow` | **write** | `workflow` + **`confirm:true`** | 是否写入、文件路径、校验结果 | `store.save()`（有阻塞项直接拒） |
| `ra.save_draft` | **write** | **`confirm:true`**（可选 `workflow_id` / `name`） | 把当前草稿过一遍校验后写成本机工作流 | `Api.preview_workflow()` + `Api.save_workflow()` |
| `ra.record_start` | **exec** | `url` + **`confirm:true`** | 打开受控浏览器并开始录制 | `Api.start_recording()` → `Session` |
| `ra.record_stop` | **exec** | — | 停止录制并返回整理好的草稿（步数、丢弃计数、问题） | `Api.stop_recording()` |
| `ra.draft` | read | — | 当前草稿每一步的编号、id、动作、定位器与匹配情况 | `Api.draft()` |
| `ra.edit_step` | **write** | `step_id` + `patch` + **`confirm:true`** | 剪辑一个步骤（定位器 / expected / value / secret_ref / timeout） | `Api.edit_step()` |
| `ra.move_step` | **write** | `step_id` + **`confirm:true`**（`offset` 或 `to_index`） | 调整顺序后的步骤列表 | `Api.move_step()` |
| `ra.remove_step` | **write** | `step_id` + **`confirm:true`** | 删掉一个步骤后的步骤列表 | `Api.remove_step()` |
| `ra.add_step` | **write** | **`confirm:true`**（`action=wait_for` 或带 `target` 的动作） | 追加步骤后的步骤列表 | `Api.add_wait_step()` / `Session.load_draft()` |
| `ra.run_workflow` | **exec** | `workflow_id` + **`confirm:true`**（可选 `from_step` / `headless` / `wait_s`） | 真实时间线、结果分类、错误码、停在的步、时长、日志路径；`wait_s:0` 只发起不等待 | `Api.run_workflow()` → `Session` → `ra/engine.py` → `core.Runner` |
| `ra.run_status` | read | `run_id` | 这次运行的实时状态与时间线、是否可续跑 | `Api.run_status()` + `Api.run_detail()` |
| `ra.cancel_run` | **exec** | **`confirm:true`** | 发停止请求后的会话状态 | `Api.cancel_run()` |
| `ra.run_history` | read | 可选 `run_id` / `limit` | 最近运行摘要，或某次运行的完整时间线与阶段事件 | `FileJournal` + `Api.run_detail()` |
| `ra.list_secret_refs` | read | — | 引用名、长度、被哪些工作流用、缺哪些、哪些没人用；**永不返回秘密值** | `FileSecretStore.preview()` |
| `ra.set_secret` | **write** | `ref` + `value` + **`confirm:true`** | 只回引用名与长度 | `FileSecretStore.set()`（DPAPI 加密落盘） |
| `ra.delete_secret` | **write** | `ref` + **`confirm:true`** | 是否删除 | `FileSecretStore.delete()` |
| `ra.delete_workflow` | **write** | `workflow_id` + **`confirm:true`** | 是否删除（运行日志保留） | `WorkflowStore.delete()` |

工具全部复用 `ra/api.py` 的界面门面与 `ra/store.py`、`ra/secrets.py`、`ra/engine.py`、`ra/core.py`
的真实能力，没有任何写死的假数据。`agent/tools.py` 是本项目唯一需要写工具逻辑的文件。

## 边界（有意保留的东西）

- **秘密值只进不出**：`ra.set_secret` 能写，但任何工具的返回里都读不到值（只有引用名与长度）。
  缺值时运行会在打开浏览器之前被拦下，并把缺的引用名全部列出来。
- **清空浏览器登录态不暴露**：那是界面里带确认框的人工操作；删工作流与删秘密引用已开放，但都要 `confirm:true`。
- **不自动重试**：结果分类沿用 `ra/core.py`。`failed` = 动作之前停止；`uncertain` = 动作已发出且只发一次、
  无法确认页面是否生效 —— 此时返回人工检查清单，不会替你重跑。
- **续跑要显式**：`from_step` 只做「从失败/取消的那一步接着跑」；已执行过的步骤绝不重跑，
  返回里会写 `resumed_from` 与 `skipped_steps`。
- **超时**：`wait_s`（默认 180，上限 900，`0` = 只发起不等待）到点按程序已有的取消路径处理，
  `timed_out:true` 会说出来，绝不写成「完成」。
- **候选事件不是可信事实**：`ra.record_*` 拿到的草稿仍需审阅（人工或 `ra.edit_step`）才会保存为工作流。

## 自检

```bash
python -m unittest discover -s tests -t .      # 后端、契约与界面脚本语法门
python -m ra.main --selfcheck                  # 录制→审阅→校验→回放整链（真实浏览器 + 本机站点）
python -m ra.main --verify                     # 窗口内界面自检（含 Agent 屏与窗口形态）
```

界面里点「Agent 接口 → 自检一次调用」会真的对本进程发一次 HTTP 调用，把 HTTP 状态、耗时与
数出来的工作流条数显示出来 —— 而不是只报告"服务应该已经起来了"。
