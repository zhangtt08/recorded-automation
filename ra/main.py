"""录放台桌面入口：界面与后端在同一个进程、同一个应用窗口内运行。"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from .agentapi import AgentServer
from .api import Api
from .diagnostics import RunDiagnostics
from .journal import FileJournal
from .paths import APP_TITLE, browsers_path_env, data_root, resource_root
from .secrets import FileSecretStore
from .session import Session
from .shell import Shell
from .store import Settings, WorkflowStore


def start_agent_server(api: Api, session: Session, port: int = 8795) -> AgentServer:
    """在同一个进程里起 Agent 接口：Agent 与界面共用一套会话，界面上能实时看到它做了什么。"""
    server = AgentServer(api=api, session=session, port=port).start()
    if server.error:
        print(f"[agent-api] 未能启动：{server.error}", file=sys.stderr)
    return server


def build(data_dir: str | Path | None = None):
    root = Path(data_dir) if data_dir else data_root()
    root.mkdir(parents=True, exist_ok=True)
    resources = resource_root()
    journal = FileJournal(root / "journal.jsonl")
    secrets = FileSecretStore(root / "secrets.json")
    settings = Settings(root / "settings.json")
    store = WorkflowStore(root / "workflows", resources / "workflow.schema.json")
    session = Session(
        journal=journal,
        secrets=secrets,
        settings=settings,
        script_path=resources / "ui" / "record_script.js",
        profile_dir=root / "profile",
    )
    # 失败/未确认/取消的真实原因单独落盘（秘密值已脱敏），历史运行重启后仍答得出为什么。
    session.diagnostics = RunDiagnostics(root / "run-diagnostics.jsonl")
    api = Api(session, store, secrets, settings, journal)
    session.notify = api.push
    return api, session, root


def dispatcher(api: Api):
    """只暴露 Api 上的公开方法给窗口里的脚本。"""
    allowed = {name for name in dir(api) if not name.startswith("_") and callable(getattr(api, name))}

    def invoke(method: str, args: list):
        if method not in allowed:
            raise ValueError(f"未知接口：{method}")
        return getattr(api, method)(*args)

    return invoke


def _verify_url(argv: list[str]) -> tuple[str, object]:
    """--verify 可以指定地址；不给地址时用程序自带的本地站点。"""
    from .fixture import FixtureSite

    index = argv.index("--verify")
    if index + 1 < len(argv) and not argv[index + 1].startswith("--"):
        return argv[index + 1], None
    site = FixtureSite().start()
    return site.url, site


def _selfcheck_only(root: Path) -> int:
    """无界面跑一遍端到端自检，结果写成文件便于排查。"""
    from .selfcheck import run as run_selfcheck

    try:
        result = run_selfcheck(root / "selfcheck")
    except Exception as exc:  # noqa: BLE001 - 诊断模式要把异常留在文件里
        (root / "selfcheck-report.txt").write_text(
            f"passed=0/1 failed=1\n自检异常: {type(exc).__name__}: {exc}\n", encoding="utf-8")
        return 1
    lines = [f"passed={result['passed']}/{len(result['results'])} failed={result['failed']}"]
    lines += [f"{'通过  ' if item['ok'] else '未通过'} {item['case']} | {item['detail']}" for item in result["results"]]
    body = "\n".join(lines) + "\n"
    (root / "selfcheck-report.txt").write_text(body, encoding="utf-8")
    print(body)
    return 0 if not result["failed"] else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    verify = "--verify" in argv
    audit = "--audit" in argv
    if audit:
        import os

        os.environ["RA_UI_AUDIT"] = "1"        # 让桥接记录每次点击调了哪个后端方法
    browsers_path_env()                      # 应用窗口与受控浏览器都要用同一个引擎解析
    api, session, root = build()
    resources = resource_root()
    if not (resources / "ui" / "index.html").exists():
        print(f"缺少界面资源：{resources / 'ui' / 'index.html'}", file=sys.stderr)
        return 2

    if "--selfcheck" in argv:
        code = _selfcheck_only(root)
        session.shutdown()
        return code

    settings = api.settings.all()
    mode = str(settings.get("window_mode") or "docked")
    if settings.get("window_fullscreen"):        # 兼容旧设置：只有全屏一个开关
        mode = "fullscreen"
    shell = Shell(resources / "ui", root / "shell-profile", dispatcher(api), on_close=session.shutdown,
                  mode=mode, geometry=settings.get("window_geom") or {},
                  on_geometry=lambda content, chosen: api.settings.update({
                      "window_geom": {"x": int(content[0]), "y": int(content[1]),
                                      "w": int(content[2]), "h": int(content[3])},
                      "window_mode": chosen}))
    api.attach(shell)
    agent_server = None
    if bool(settings.get("agent_api")):
        agent_server = start_agent_server(api, session, int(settings.get("agent_port") or 8795))
        api.agent_server = agent_server
    outcome = {"code": 0}

    def boot() -> None:
        site = None
        target = ""
        headless_before = None
        try:
            if verify:
                target, site = _verify_url(argv)
                headless_before = bool(api.settings.all().get("headless"))
                api.settings.update({"headless": True})
            api.push("ready", api.app_info())
            if verify:
                from . import uiverify

                outcome["code"] = uiverify.run(shell, target, root / "ui-shots", session)
                (root / "verify-report.txt").write_text(f"exit={outcome['code']}\nurl={target}\n", encoding="utf-8")
            if audit:
                from . import uiaudit

                outcome["code"] = uiaudit.run(shell, root)
                (root / "audit-exit.txt").write_text(f"exit={outcome['code']}\n", encoding="utf-8")
        except Exception as exc:  # 自检异常也要报告
            outcome["code"] = 1
            (root / "verify-report.txt").write_text(f"exit=1\nerror={type(exc).__name__}: {exc}\n", encoding="utf-8")
        finally:
            if site is not None:
                site.stop()
            if headless_before is not None:
                # 自检借用的是用户真实的数据目录：不能把 headless 留在自检要的值上
                api.settings.update({"headless": headless_before})
            if verify or audit:
                shell.close()

    try:
        shell.start()
    except RuntimeError as exc:
        message = f"无法启动应用窗口：{exc}"
        print(message, file=sys.stderr)
        try:  # 封装版没有控制台，把原因留成文件便于排查
            (root / "startup-error.txt").write_text(message + "\n", encoding="utf-8")
        except OSError:
            pass
        return 1
    threading.Thread(target=boot, name="ra-boot", daemon=True).start()
    shell.wait()
    if agent_server is not None:
        agent_server.stop()
    session.shutdown()
    return int(outcome["code"])


if __name__ == "__main__":
    raise SystemExit(main())
