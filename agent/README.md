# 录放台 · Agent API / MCP

把录放台当成一个带 schema 的工具对外暴露：本地 HTTP 服务 + MCP stdio 桥。
契约见 `personal-agent-hub/docs/AGENT_API_STANDARD.md`。**只监听 127.0.0.1**，只用标准库 + 项目已有依赖。

## 谁能调：本机令牌（2026-10-05 补，评审 BLOCKER #1）

以前这两个入口对任何网页敞开：响应统一发 `access-control-allow-origin: *`，没有 Host / Origin / 令牌校验，
于是**用户访问的任意网页**都能驱动全部 25 个工具（`ra.record_start` 指任意 URL + `ra.draft` 对着装有真实
登录态的档案敲键）。现在闸门统一写在 `ra/localguard.py`，进程内服务与 `agent/server.py` 共用同一份判据：

| 闸门 | 判据 | 不过的话 |
| --- | --- | --- |
| 绑定 | 只 bind `127.0.0.1` | — |
| `Host` | 必须是 `127.0.0.1:<port>` / `localhost:<port>` / `[::1]:<port>`（带了端口就要对得上） | 403 `host_not_allowed` |
| `Origin` / `Referer` | 缺省放行（curl / node 不发）；带了就必须是回环 http(s) 源 | 403 `origin_not_allowed` |
| 令牌 | **所有非 GET** 必带 `x-ra-token`（也收 `authorization: bearer`），定时安全比较 | 401 `token_required` |
| CORS | 任何响应都不发 `access-control-allow-origin`；`OPTIONS` 预检也不给 ACAO/allow-methods/allow-headers | 浏览器页面在预检阶段就被拦下 |

⚠ `Origin` 只与「回环地址清单」这个常量比，**绝不与本次请求的 `Host` 比** —— DNS rebinding 时两者恰好相等，
比了等于没防。`tests/test_agent_guard.py::test_dns_rebinding_shape_is_refused_even_when_origin_matches_host`
钉的就是这一点。

- 令牌在服务启动时准备（`localguard.load_token()`）：已有就用已有的，没有就生成 32 字节随机值再落盘。
  两个入口读同一个文件，所以「录放台开着 + 又单独起一个 `agent/server.py`」是同一把令牌。
- 令牌文件默认 `%LOCALAPPDATA%\RecordedAutomation\agent-token`（非 Windows 走 `$XDG_DATA_HOME`）。
  **刻意不放在仓库里**：仓库会被同步、被打包、被 `git add -A`，而这个文件的全部价值就是「只有本机进程读得到」。
  Windows 上的边界来自 %LOCALAPPDATA% 的目录 ACL（当前用户 + 管理员组）；POSIX 上另外 `chmod 600`。
- `agent/.endpoint` 仍然只有**一行 URL**（personal-agent-hub 的脚本把整文件 trim 当 URL 读，别加第二行）。
  令牌不写在那里，也不从 `/api/health` 返回 —— 健康检查只报 `token_file` 这个**路径**。
- 想换位置：`RA_AGENT_TOKEN_FILE=<路径>`；想直接给定：`RA_AGENT_TOKEN=<值>`。两个都是给测试与编排器留的口。

**合法的本机 Agent 不需要任何手工步骤**：`agent/mcp-server.mjs` 每次调用现读那个文件（只有 `tools/call`
需要，`tools/list` 是 GET），读不到时把候选路径与出路一起写进错误。界面「Agent 接口」那一屏显示令牌文件
位置与被拦下的调用次数；`自检一次调用` 走同一道闸门（不带令牌就自检不过，这样它才证明得了事）。

手动调用写操作要自己带令牌（Git Bash 写法，抄过去就能用）：

```bash
TOKEN="$(cat "$LOCALAPPDATA/RecordedAutomation/agent-token")"
curl -s -X POST http://127.0.0.1:8795/api/agent/tool \
  -H 'content-type: application/json' -H "x-ra-token: $TOKEN" \
  -d '{"tool":"ra.status","input":{}}'
```

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
- `server.py`、`errors.py` 是标准模板的复制件。`server.py` **有意偏离模板两处**（文件头写清了）：
  默认端口 8790 → 8795，以及上面那套 `ra/localguard.py` 闸门。同步模板时别把它改回去。

## HTTP 端点

```
GET  /api/health          项目名、版本、agent_api、工具数、uptime、是否进程内服务、已被调用次数，
                          外加 auth / token_header / token_file / denied（只报令牌文件的路径，绝不报令牌值）
GET  /api/agent/manifest  项目信息 + 全部工具描述 + base_url + auth
GET  /api/agent/tools     工具清单（name / description / input_schema / risk）
POST /api/agent/tool      {"tool":"ra.xxx","input":{...}} → {ok:true,data:...,tool,ms}
```

失败统一是 `{ok:false,error:{code,message}}`。**工具层抛出的结构化错误一律 HTTP 400**
（`bad_input` / `not_found` / `needs_secret` / `run_refused` / `record_refused` / `mode_refused` /
`cancel_failed` / `stop_failed`），未知工具带 `available` 列表；只有工具内部 unexpected 异常才是 500
（`handler_failed`）。调用方读 `error.code` 与 `error.message` 就够，不需要靠状态码分类。

## 工具清单（25 个）

`risk` 三档按「这件事真的动了什么」判：**read** 只读，什么都不改；**write** 改本机状态（内存里的会话
或磁盘上的文件），但不驱动页面；**exec** 真的跑活 —— 开浏览器、驱动页面、起进程、把一份定义执行出去。
`confirm` 的要求照这个判据收窄（`AGENT_API_STANDARD.md` 第 4 条）：**exec 必须有 `confirm` 且缺省拒绝**；
**write 只在名字或描述含破坏性动词（删除 / 清空 / 覆盖 / 重置）时才强制**；对自己那一场操作的收尾
（`ra.record_stop`）与「取消一个自己建的东西」这类可逆小写入不强制 —— 把 `confirm` 塞给每一个写入，
只会把调用方训练成无脑传 `true`，那才是标准点名的真退化。这一条判据由
`tests/test_agent_guard.py::test_the_published_manifest_satisfies_standard_rule4` 在公示清单上逐条复查，
`tests/test_agent_api.py` 在注册表上复查同一件事。

⚠ `confirm` 是**给 Agent 的二次确认**（防手滑），**不是安全边界**：它就在请求体里，任何能发出这个请求的人
都能自己写 `true`。真正决定「谁能发出这个请求」的是上面那一节的本机令牌 —— 评审原话「唯一的闸门就是
`confirm:true`，而这个值是跨源攻击者可控的」，补的就是这一层。两道都在，顺序是：Host → Origin → 令牌 →
schema 校验 → confirm。

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
| `ra.record_stop` | write | —（不需要 confirm：不写本机文件、不起进程、不驱动页面，只是收尾） | 结束自己那一场录制并把草稿原样返回（步数、丢弃计数、问题）；没有进行中的录制时如实报 `stop_failed` | `Api.stop_recording()` → `Session.stop_recording()` |
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
- **运行日志有上界，且删过档会说出来**：`journal.jsonl` 单份 2MB 到线就整份轮转、留 2 份归档，
  被挤出去的最旧一份会删除。`ra.status` 的返回里总带 `journal`
  （`bytes / files / rotations / dropped_bytes / trimmed / note`）；`ra.run_history` 查不到某个
  `run_id` 时错误信息带同一句话，不带 `run_id` 的列表调用只在真的轮转过（`trimmed = true`）时才附上
  `journal` —— 没轮转过时那份列表本来就是全部，附它只是多一屏噪声。
  看到 `journal.trimmed = true` 就要知道「这份列表不是全部历史」。

## 自检

```bash
python -m unittest discover -s tests -t .      # 后端、契约、防护闸门与界面脚本语法门
python -m ra.main --selfcheck                  # 录制→审阅→校验→回放整链（真实浏览器 + 本机站点）
python -m ra.main --verify                     # 窗口内界面自检（含 Agent 屏与窗口形态）
```

没有单独的 tools/verify.py 一键门禁——门禁就是上面三条命令按顺序跑（先单元/契约，再真实浏览器整链，再界面自检）。

`tests/test_agent_guard.py` 用真实 HTTP 请求打这两个服务（独立那一个真起子进程），逐条验：
伪造 `Host` 拒、`Origin: http://evil.test` 的跨源 POST 拒、没带令牌拒、从令牌文件取值的合法本机调用成功、
`OPTIONS` 预检与所有 mutating 响应都不带 `access-control-allow-origin`。

界面里点「Agent 接口 → 自检一次调用」会真的对本进程发一次 HTTP 调用，把 HTTP 状态、耗时与
数出来的工作流条数显示出来 —— 而不是只报告"服务应该已经起来了"。它带的是同一个令牌，所以这道自检
证明的是「合法本机路径确实走得通」，而不只是端口开着。
