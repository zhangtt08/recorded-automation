# 录放台 · Agent API / MCP

把录放台当成一个带 schema 的工具对外暴露：本地 HTTP 服务 + MCP stdio 桥。
契约见 `personal-agent-hub/docs/AGENT_API_STANDARD.md`。**只监听 127.0.0.1**，只用标准库 + 项目已有依赖。

## 启动

```bash
python agent/server.py                 # 默认端口 8795（AGENT_API_STANDARD 给录放台分配的那一个）
AGENT_PORT=9100 python agent/server.py # 换端口
node agent/mcp-server.mjs              # MCP 客户端用法（stdio）
```

- `server.py`、`errors.py`、`mcp-server.mjs` 是标准模板的复制件。`server.py` 与模板唯一的差别是默认端口
  8790 → **8795**（外加一行说明注释），逻辑没动。
- 端口被占用时按标准自动 +1，并把实际地址写进 `agent/.endpoint`（MCP 桥优先读它）。
- 服务没起来时，MCP 桥会按 `agent/launch.json` 自己把 `python agent/server.py` 拉起来。

## HTTP 端点

```
GET  /api/health          项目名、版本、agent_api、工具数、uptime
GET  /api/agent/manifest  项目信息 + 全部工具描述
GET  /api/agent/tools     工具清单（name / description / input_schema / risk）
POST /api/agent/tool      {"tool":"ra.xxx","input":{...}} → {ok:true,data:...,tool,ms}
```

失败统一是 `{ok:false,error:{code,message}}`；缺参数与确认缺失是 HTTP 400 + `code:"bad_input"`，
未知工具带 `available` 列表。按标准实现，`bad_input` 之外的错误码（`not_found` / `needs_secret` /
`run_refused` / `unknown_tool` 除外的服务端判定）走 HTTP 500，但响应体仍是同一份 JSON 信封，
调用方读 `error.code` 与 `error.message` 就够，不需要靠状态码分类。

## 工具清单

| 工具 | risk | 需要什么 | 返回什么 | 真实落点 |
| --- | --- | --- | --- | --- |
| `ra.status` | read | — | 版本、数据目录、工作流/运行计数、秘密引用名、设置、浏览器缓存解析 | `ra/paths.py`、`ra/store.py`、`ra/journal.py`、`ra/secrets.py` |
| `ra.list_workflows` | read | 可选 `limit` | 每条工作流的 id/origin/步数/动作类型/未验证步数/用到的秘密引用名与缺失项 | `WorkflowStore.list()` |
| `ra.get_workflow` | read | `workflow_id` | 完整 Workflow v1（每步带编号与可读标签）+ Schema/执行语义双层校验 + 定位策略 | `store.load()` + `store.problems()` |
| `ra.validate_workflow` | read | `workflow` | 这份定义的校验结果（阻塞项 / 警告 / 还缺哪些秘密值），**不写盘** | `store.problems()` |
| `ra.save_workflow` | **write** | `workflow` + **`confirm:true`** | 是否写入、文件路径、校验结果 | `store.save()`（有阻塞项直接拒） |
| `ra.run_workflow` | **exec** | `workflow_id` + **`confirm:true`**（可选 `from_step` / `headless` / `wait_s`） | 真实步骤时间线、结果分类、错误码、停在的步、时长、日志路径 | `Api.run_workflow()` → `Session` → `ra/engine.py` → `core.Runner` |
| `ra.run_history` | read | 可选 `run_id` / `limit` | 最近运行摘要，或某次运行的完整时间线与阶段事件 | `FileJournal` + `Api.run_detail()` |
| `ra.list_secret_refs` | read | — | 引用名、长度、被哪些工作流用、缺哪些、哪些没人用；**永不返回秘密值** | `FileSecretStore.preview()` |

`agent/tools.py` 是本项目唯一需要写的文件；工具全部复用 `ra/api.py` 的 UI 门面与 `ra/store.py`、
`ra/secrets.py`、`ra/engine.py` 的真实能力，没有任何写死的假数据。

## 边界（有意不暴露的东西）

- **秘密值**：填值只能在应用界面里做。工具只报引用名，读不到值；缺值时运行会在打开浏览器之前被拦下，
  并把缺的引用名全部列出来。
- **删除类动作不暴露**：删工作流、删秘密引用、清空浏览器登录态都是应用界面里带确认框的人工操作。
- **不自动重试**：结果分类沿用 `ra/core.py`。`failed` = 动作之前停止；`uncertain` = 动作已发出且只发一次、
  无法确认页面是否生效 —— 此时工具返回人工检查清单，不会替你重跑。
- **续跑要显式**：`from_step` 只做「从失败/取消的那一步接着跑」，这一步之前没有发出过动作；
  已执行过的步骤绝不重跑，返回里会写 `resumed_from` 与 `skipped_steps`。
- **浏览器档案冲突**：`ra.run_workflow` 用的是与应用同一个数据目录里的受控浏览器档案。应用窗口开着时
  可能抢不到档案，此时结果按失败返回并附「先关闭应用窗口」的说明，不会静默改数据。
- **超时**：`wait_s`（默认 180，上限 900）到点按程序已有的取消路径处理，`timed_out:true` 会说出来，
  绝不写成「完成」。

## 自检

```bash
python -m unittest discover -s tests -t .      # 后端与契约
python -m ra.main --selfcheck                   # 录制→审阅→校验→回放整链（真实浏览器 + 本机站点）
python -m ra.main --verify                      # 窗口内界面自检
```
