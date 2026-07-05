from __future__ import annotations

import argparse
import json
import socket
import threading
import time
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from .baidu import BaiduAuthClient, BaiduNetdiskClient, baidu_auth_url
from .core import BackupOptions, BackupRunner, CancelledBackup, SeafileClient, category_for, discover_repositories, human_size
from .migration import MigrationOptions, MigrationRunner


CATEGORIES = ("我的资料库", "群组共享内容", "共享给我的")
TOKEN_PROFILE_URL = "https://cloud.tsinghua.edu.cn/profile/"
DEFAULT_PORT = 8765


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def format_rate(value: int | float | None) -> str:
    value = float(value or 0)
    return f"{human_size(value)}/s" if value > 0 else "-"


def format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "-"
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def selected_summary(repositories: list[dict], categories: set[str]) -> dict:
    selected = [repo for repo in repositories if category_for(repo) in categories]
    return {
        "count": len(selected),
        "size": sum(int(repo.get("size") or 0) for repo in selected),
        "size_text": human_size(sum(int(repo.get("size") or 0) for repo in selected)),
    }


class ConsoleState:
    def __init__(self):
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.event_id = 0
        self.status = "等待连接清华云盘。"
        self.error = ""
        self.logs: list[str] = []
        self.repositories: list[dict] = []
        self.account: dict = {}
        self.baidu_account: dict = {}
        self.baidu_quota: dict = {}
        self.baidu_device_data: dict = {}
        self.baidu_access_ready = False
        self.tsinghua_token = ""
        self.baidu_access_token = ""
        self.running = False
        self.operation = ""
        self.mode = ""
        self.stats: dict = {}
        self.cancel_event = threading.Event()
        self.worker_thread: threading.Thread | None = None

    def notify_locked(self) -> None:
        self.event_id += 1
        self.condition.notify_all()

    def update(self, **values) -> None:
        with self.condition:
            for key, value in values.items():
                setattr(self, key, value)
            self.notify_locked()

    def append_log(self, message: str) -> None:
        message = str(message or "").strip()
        if not message:
            return
        with self.condition:
            self.logs.append(f"[{time.strftime('%H:%M:%S')}] {message}")
            if len(self.logs) > 1000:
                self.logs = self.logs[-1000:]
            self.notify_locked()

    def start_worker(self, operation: str, mode: str, target: Callable[[], None], clear_logs: bool = False) -> None:
        with self.condition:
            if self.running:
                raise ApiError(HTTPStatus.CONFLICT, "当前已有任务在运行。")
            self.running = True
            self.operation = operation
            self.mode = mode
            self.error = ""
            self.cancel_event.clear()
            if clear_logs:
                self.logs = []
            self.status = operation
            self.notify_locked()

        def run() -> None:
            try:
                target()
            except CancelledBackup:
                self.append_log("任务已停止。")
                self.update(status="已停止。")
            except Exception as exc:
                self.append_log(f"{operation}失败：{exc}")
                self.update(status=f"{operation}失败。", error=str(exc))
            finally:
                with self.condition:
                    self.running = False
                    self.operation = ""
                    self.notify_locked()

        thread = threading.Thread(target=run, daemon=True)
        with self.condition:
            self.worker_thread = thread
        thread.start()

    def event_callback(self, event: dict) -> None:
        kind = event.get("kind")
        mode = event.get("mode") or self.mode
        if kind == "log":
            self.append_log(event.get("message", ""))
            return
        if kind == "progress":
            stats = dict(event.get("stats") or {})
            with self.condition:
                self.stats = stats
                self.mode = mode
                self.status = progress_status(stats, mode)
                self.notify_locked()
            return
        if kind == "done":
            stats = dict(event.get("stats") or {})
            with self.condition:
                self.stats = stats
                self.mode = mode
                self.status = "迁移完成。" if mode == "migration" else "备份完成。"
                self.notify_locked()
            return
        if kind == "status":
            self.append_log(event.get("message", ""))
            self.update(status=event.get("message", ""))
            return
        if kind == "error":
            message = event.get("message", "发生错误。")
            self.append_log(message)
            self.update(status=message, error=message)

    def snapshot_locked(self) -> dict:
        categories = set(CATEGORIES)
        selected = selected_summary(self.repositories, categories)
        stats = dict(self.stats or {})
        return {
            "eventId": self.event_id,
            "status": self.status,
            "error": self.error,
            "logs": list(self.logs),
            "repositories": list(self.repositories),
            "account": dict(self.account or {}),
            "baiduAccount": dict(self.baidu_account or {}),
            "baiduQuota": dict(self.baidu_quota or {}),
            "baiduAccessReady": self.baidu_access_ready,
            "baiduDeviceData": dict(self.baidu_device_data or {}),
            "running": self.running,
            "operation": self.operation,
            "mode": self.mode,
            "stats": stats,
            "categories": list(CATEGORIES),
            "selectedSummary": selected,
            "tokenReady": bool(self.tsinghua_token),
            "defaults": {
                "destination": str(Path.home() / "Desktop" / "清华云盘备份"),
                "migrationRoot": "清华云盘迁移",
                "migrationTempDir": str(Path.home() / "Desktop" / "清华云盘迁移临时"),
                "backupWorkers": 4,
                "migrationWorkers": 4,
            },
        }

    def snapshot(self) -> dict:
        with self.condition:
            return self.snapshot_locked()


def progress_status(stats: dict, mode: str) -> str:
    total = int(stats.get("repositories_total") or 0)
    done = int(stats.get("repositories_done") or 0)
    action = "迁移" if mode == "migration" else "下载"
    return (
        f"资料库 {done}/{total} | 文件 {stats.get('files_seen', 0)} | "
        f"{action} {stats.get('downloaded', 0)} | 跳过 {stats.get('skipped', 0)} | "
        f"失败 {stats.get('failed', 0)} | 下载 {format_rate(stats.get('download_speed_bps'))} | "
        f"上传 {format_rate(stats.get('upload_speed_bps')) if mode == 'migration' else '-'} | "
        f"剩余 {format_duration(stats.get('eta_seconds'))}"
    )


class WebConsoleHandler(BaseHTTPRequestHandler):
    server_version = "THUCloudKeeperWeb/0.1"

    @property
    def state(self) -> ConsoleState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, format: str, *args) -> None:
        return

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self.send_html(WEB_CONSOLE_HTML)
        elif parsed.path == "/api/state":
            self.send_json(self.state.snapshot())
        elif parsed.path == "/api/events":
            self.stream_events()
        elif parsed.path == "/api/token-page":
            self.send_json({"url": TOKEN_PROFILE_URL})
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            payload = self.read_json()
            if parsed.path == "/api/tsinghua/connect":
                self.api_tsinghua_connect(payload)
            elif parsed.path == "/api/baidu/device-code":
                self.api_baidu_device_code(payload)
            elif parsed.path == "/api/baidu/finish-auth":
                self.api_baidu_finish_auth(payload)
            elif parsed.path == "/api/baidu/validate-token":
                self.api_baidu_validate_token(payload)
            elif parsed.path == "/api/backup/start":
                self.api_backup_start(payload)
            elif parsed.path == "/api/migration/start":
                self.api_migration_start(payload)
            elif parsed.path == "/api/task/stop":
                self.api_task_stop()
            else:
                raise ApiError(HTTPStatus.NOT_FOUND, "未知接口。")
        except ApiError as exc:
            self.send_json({"ok": False, "error": exc.message}, status=exc.status)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def stream_events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_event_id = -1
        while True:
            with self.state.condition:
                if self.state.event_id == last_event_id:
                    self.state.condition.wait(timeout=15)
                snapshot = self.state.snapshot_locked()
                last_event_id = self.state.event_id
            data = json.dumps(snapshot, ensure_ascii=False)
            try:
                self.wfile.write(f"id: {last_event_id}\nevent: state\ndata: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

    def api_tsinghua_connect(self, payload: dict) -> None:
        token = str(payload.get("token") or "").strip()
        if not token:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请填写清华云盘个人 Token。")

        def worker() -> None:
            self.state.append_log("正在验证清华云盘 Token。")
            client = SeafileClient(token)
            info = client.account_info()
            self.state.append_log("Token 验证通过，正在读取资料库。")
            repos = discover_repositories(client)
            with self.state.condition:
                self.state.tsinghua_token = token
                self.state.account = info
                self.state.repositories = repos
                self.state.status = f"已读取 {len(repos)} 个资料库。"
                self.state.notify_locked()
            self.state.append_log(f"登录账号：{info.get('name') or info.get('email') or '未知'}")

        self.state.start_worker("连接清华云盘", "connect", worker)
        self.send_json({"ok": True})

    def api_baidu_device_code(self, payload: dict) -> None:
        app_key = str(payload.get("appKey") or "").strip()
        if not app_key:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请填写百度 App Key。")
        data = BaiduAuthClient().get_device_code(app_key)
        user_code = data.get("user_code") or ""
        url = baidu_auth_url(user_code, data.get("verification_url") or "https://openapi.baidu.com/device")
        with self.state.condition:
            self.state.baidu_device_data = data
            self.state.status = f"百度授权用户码：{user_code}。授权完成后点击“完成授权”。"
            self.state.notify_locked()
        self.state.append_log(f"百度授权用户码：{user_code}")
        self.send_json({"ok": True, "data": data, "authUrl": url})

    def api_baidu_finish_auth(self, payload: dict) -> None:
        app_key = str(payload.get("appKey") or "").strip()
        app_secret = str(payload.get("appSecret") or "").strip()
        if not app_key or not app_secret:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请填写百度 App Key 和 Secret Key。")
        with self.state.lock:
            device_data = dict(self.state.baidu_device_data or {})
        if not device_data.get("device_code"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "请先获取百度授权用户码。")

        def worker() -> None:
            self.state.append_log("等待百度授权完成并换取 Access Token。")
            token_data = BaiduAuthClient().poll_device_token(
                app_key,
                app_secret,
                device_data["device_code"],
                interval=int(device_data.get("interval") or 5),
                expires_in=int(device_data.get("expires_in") or 300),
            )
            self.load_baidu_account(token_data["access_token"])

        self.state.start_worker("完成百度授权", "auth", worker)
        self.send_json({"ok": True})

    def api_baidu_validate_token(self, payload: dict) -> None:
        access_token = str(payload.get("accessToken") or "").strip()
        if not access_token:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请填写百度 Access Token。")

        def worker() -> None:
            self.state.append_log("正在验证百度网盘账号。")
            self.load_baidu_account(access_token)

        self.state.start_worker("验证百度账号", "auth", worker)
        self.send_json({"ok": True})

    def load_baidu_account(self, access_token: str) -> None:
        client = BaiduNetdiskClient(access_token)
        info = client.user_info()
        quota = client.quota()
        with self.state.condition:
            self.state.baidu_access_token = access_token
            self.state.baidu_access_ready = True
            self.state.baidu_account = info
            self.state.baidu_quota = quota
            name = info.get("netdisk_name") or info.get("baidu_name") or info.get("uk") or "未知账号"
            used = int(quota.get("used") or 0)
            total = int(quota.get("total") or 0)
            free = int(quota.get("free") or max(total - used, 0))
            quota_text = f"{human_size(used)} / {human_size(total)}，可用 {human_size(free)}" if total else f"可用 {human_size(free)}"
            self.state.status = f"百度网盘验证通过：{name}，{quota_text}"
            self.state.notify_locked()
        self.state.append_log(self.state.status)

    def api_backup_start(self, payload: dict) -> None:
        with self.state.lock:
            token = self.state.tsinghua_token
        if not token:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请先连接清华云盘。")
        categories = validate_categories(payload.get("categories"))
        destination = Path(str(payload.get("destination") or "").strip())
        if not destination:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请填写下载目录。")
        workers = max(1, min(int(payload.get("workers") or 4), 16))

        def worker() -> None:
            client = SeafileClient(token)
            options = BackupOptions(destination=destination, categories=categories, workers=workers)
            runner = BackupRunner(client, options, event_callback=self.state.event_callback, cancel_event=self.state.cancel_event)
            runner.run()

        self.state.start_worker("开始本地备份", "backup", worker, clear_logs=True)
        self.send_json({"ok": True})

    def api_migration_start(self, payload: dict) -> None:
        with self.state.lock:
            tsinghua_token = self.state.tsinghua_token
            baidu_access_token = self.state.baidu_access_token
        if not tsinghua_token:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请先连接清华云盘。")
        if not baidu_access_token:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请先完成百度授权或验证百度 Token。")
        categories = validate_categories(payload.get("categories"))
        app_dir = str(payload.get("appDir") or "").strip()
        if not app_dir:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请填写百度应用产品名称。")
        target_root = str(payload.get("targetRoot") or "清华云盘迁移").strip()
        temp_dir = Path(str(payload.get("tempDir") or "").strip())
        if not temp_dir:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请填写迁移临时目录。")
        workers = max(1, min(int(payload.get("workers") or 4), 16))
        verify_remote = bool(payload.get("verifyRemote"))

        def worker() -> None:
            seafile_client = SeafileClient(tsinghua_token)
            baidu_client = BaiduNetdiskClient(baidu_access_token)
            options = MigrationOptions(
                categories=categories,
                app_dir_name=app_dir,
                target_root=target_root,
                temp_dir=temp_dir,
                workers=workers,
                verify_remote_resume=verify_remote,
            )
            runner = MigrationRunner(seafile_client, baidu_client, options, event_callback=self.state.event_callback, cancel_event=self.state.cancel_event)
            runner.run()

        self.state.start_worker("开始迁移到百度网盘", "migration", worker, clear_logs=True)
        self.send_json({"ok": True})

    def api_task_stop(self) -> None:
        self.state.cancel_event.set()
        self.state.update(status="正在停止，当前文件完成或中断后会退出。")
        self.send_json({"ok": True})


def validate_categories(raw_categories) -> set[str]:
    categories = {str(item) for item in (raw_categories or []) if str(item) in CATEGORIES}
    if not categories:
        raise ApiError(HTTPStatus.BAD_REQUEST, "请至少选择一个资料库分类。")
    return categories


class WebConsoleServer(ThreadingHTTPServer):
    def __init__(self, server_address, RequestHandlerClass, state: ConsoleState):
        super().__init__(server_address, RequestHandlerClass)
        self.state = state


def bind_server(host: str, port: int) -> WebConsoleServer:
    last_error: OSError | None = None
    for candidate in range(port, port + 50):
        try:
            return WebConsoleServer((host, candidate), WebConsoleHandler, ConsoleState())
        except OSError as exc:
            last_error = exc
    raise RuntimeError(f"无法绑定本地端口：{last_error}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="THU Cloud Keeper Web console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    server = bind_server(args.host, args.port)
    host, port = server.server_address
    url = f"http://{host}:{port}/"
    print(f"THU Cloud Keeper Web 控制台已启动：{url}")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


WEB_CONSOLE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>THU Cloud Keeper</title>
  <style>
    :root {
      --bg: #eef2f7;
      --panel: rgba(255, 255, 255, 0.86);
      --panel-strong: #ffffff;
      --line: rgba(15, 23, 42, 0.10);
      --text: #0f172a;
      --muted: #64748b;
      --primary: #2563eb;
      --primary-dark: #1d4ed8;
      --danger: #dc2626;
      --success: #059669;
      --shadow: 0 20px 50px rgba(15, 23, 42, 0.10);
      --radius: 18px;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(37, 99, 235, 0.16), transparent 32rem),
        linear-gradient(135deg, #f8fafc 0%, #eef2f7 54%, #e2e8f0 100%);
      min-height: 100vh;
    }
    button, input { font: inherit; }
    .shell { max-width: 1440px; margin: 0 auto; padding: 28px; }
    .hero {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 18px;
    }
    .brand h1 { margin: 0; font-size: 28px; letter-spacing: 0; }
    .brand p { margin: 8px 0 0; color: var(--muted); }
    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 10px 14px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--panel);
      box-shadow: 0 10px 30px rgba(15, 23, 42, 0.08);
      color: var(--muted);
      max-width: 560px;
    }
    .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--success); box-shadow: 0 0 0 5px rgba(5, 150, 105, 0.12); flex: 0 0 auto; }
    .dot.running { background: var(--primary); box-shadow: 0 0 0 5px rgba(37, 99, 235, 0.14); }
    .layout {
      display: grid;
      grid-template-columns: minmax(340px, 420px) minmax(0, 1fr);
      gap: 18px;
      align-items: start;
    }
    .stack { display: grid; gap: 18px; }
    .card {
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--panel);
      box-shadow: var(--shadow);
      backdrop-filter: blur(14px);
      overflow: hidden;
    }
    .card-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 16px 18px 0;
    }
    .card-title { font-size: 16px; font-weight: 750; }
    .card-body { padding: 16px 18px 18px; }
    .form-grid { display: grid; gap: 12px; }
    label { display: grid; gap: 7px; color: var(--muted); font-size: 13px; }
    input {
      width: 100%;
      border: 1px solid rgba(100, 116, 139, 0.25);
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.92);
      color: var(--text);
      padding: 11px 12px;
      outline: none;
      transition: border-color 0.15s, box-shadow 0.15s;
    }
    input:focus { border-color: rgba(37, 99, 235, 0.70); box-shadow: 0 0 0 4px rgba(37, 99, 235, 0.11); }
    .row { display: flex; gap: 10px; flex-wrap: wrap; }
    .row > * { flex: 1 1 auto; }
    .btn {
      border: 0;
      border-radius: 12px;
      padding: 11px 14px;
      background: #e2e8f0;
      color: #0f172a;
      cursor: pointer;
      transition: transform 0.15s, box-shadow 0.15s, background 0.15s;
      white-space: nowrap;
    }
    .btn:hover { transform: translateY(-1px); box-shadow: 0 10px 22px rgba(15, 23, 42, 0.12); }
    .btn.primary { background: linear-gradient(135deg, var(--primary), var(--primary-dark)); color: white; }
    .btn.danger { background: var(--danger); color: white; }
    .btn.ghost { background: rgba(255, 255, 255, 0.65); border: 1px solid var(--line); }
    .checks { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
    .check {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.70);
      color: #334155;
      cursor: pointer;
    }
    .check input { width: auto; accent-color: var(--primary); }
    .metrics {
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 12px;
    }
    .metric {
      padding: 14px;
      border-radius: 16px;
      background: var(--panel-strong);
      border: 1px solid var(--line);
      min-height: 86px;
    }
    .metric .label { color: var(--muted); font-size: 12px; margin-bottom: 8px; }
    .metric .value { font-size: 18px; font-weight: 760; overflow-wrap: anywhere; }
    .progress-wrap {
      height: 12px;
      border-radius: 999px;
      background: #dbe3ef;
      overflow: hidden;
      margin-top: 14px;
    }
    .progress-bar {
      height: 100%;
      width: 0%;
      border-radius: 999px;
      background: linear-gradient(90deg, #2563eb, #06b6d4);
      transition: width 0.25s ease;
    }
    .summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
    .summary-item {
      padding: 12px;
      border-radius: 14px;
      background: rgba(248, 250, 252, 0.95);
      border: 1px solid var(--line);
    }
    .summary-item span { display: block; color: var(--muted); font-size: 12px; }
    .summary-item strong { display: block; margin-top: 6px; font-size: 15px; overflow-wrap: anywhere; }
    .table-wrap { overflow: auto; max-height: 380px; border-radius: 14px; border: 1px solid var(--line); }
    table { width: 100%; border-collapse: collapse; background: white; min-width: 860px; }
    th, td { text-align: left; padding: 11px 12px; border-bottom: 1px solid #edf2f7; font-size: 13px; }
    th { position: sticky; top: 0; background: #f8fafc; color: #475569; z-index: 1; }
    td.size { text-align: right; white-space: nowrap; }
    .logs {
      height: 300px;
      overflow: auto;
      border-radius: 14px;
      background: #0f172a;
      color: #dbeafe;
      padding: 12px;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 12px;
      line-height: 1.65;
    }
    .muted { color: var(--muted); }
    .two { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .footer-status { margin-top: 14px; color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
    @media (max-width: 1100px) {
      .layout { grid-template-columns: 1fr; }
      .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="brand">
        <h1>THU Cloud Keeper</h1>
        <p>清华云盘本地备份与百度网盘迁移控制台</p>
      </div>
      <div class="status-pill"><span id="statusDot" class="dot"></span><span id="statusText">正在连接控制台...</span></div>
    </section>

    <section class="layout">
      <aside class="stack">
        <div class="card">
          <div class="card-header"><div class="card-title">清华云盘</div><button class="btn ghost" id="openToken">打开 Token 页面</button></div>
          <div class="card-body form-grid">
            <label>个人 Token <input id="token" type="password" autocomplete="off" placeholder="粘贴清华云盘个人 Token" /></label>
            <div class="row">
              <button class="btn" id="pasteToken">粘贴</button>
              <button class="btn primary" id="connectTsinghua">连接并读取资料库</button>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-header"><div class="card-title">百度网盘授权</div></div>
          <div class="card-body form-grid">
            <div class="two">
              <label>App Key <input id="appKey" placeholder="百度开放平台 App Key" /></label>
              <label>Secret Key <input id="appSecret" type="password" placeholder="Secret Key" /></label>
            </div>
            <div class="row">
              <button class="btn" id="getDeviceCode">获取授权码</button>
              <button class="btn primary" id="finishAuth">完成授权</button>
            </div>
            <label>Access Token <input id="baiduToken" type="password" placeholder="也可以直接粘贴百度 Access Token" /></label>
            <div class="row">
              <button class="btn" id="pasteBaiduToken">粘贴</button>
              <button class="btn" id="validateBaidu">验证百度账号</button>
            </div>
            <div class="muted" id="baiduHint">尚未验证百度账号。</div>
          </div>
        </div>

        <div class="card">
          <div class="card-header"><div class="card-title">任务配置</div></div>
          <div class="card-body form-grid">
            <div class="checks" id="categoryChecks"></div>
            <label>本地备份目录 <input id="destination" /></label>
            <div class="two">
              <label>本地备份并发 <input id="workers" type="number" min="1" max="16" value="4" /></label>
              <label>迁移并发 <input id="migrationWorkers" type="number" min="1" max="16" value="4" /></label>
            </div>
            <div class="two">
              <label>应用产品名称 <input id="appDir" placeholder="例如 thucs" /></label>
              <label class="check"><input id="verifyRemote" type="checkbox"><span>严格远端校验</span></label>
            </div>
            <label>百度迁移根目录 <input id="targetRoot" value="清华云盘迁移" /></label>
            <label>迁移临时目录 <input id="tempDir" /></label>
            <div class="row">
              <button class="btn primary" id="startBackup">开始本地备份</button>
              <button class="btn primary" id="startMigration">开始迁移到百度网盘</button>
              <button class="btn danger" id="stopTask">停止</button>
            </div>
          </div>
        </div>
      </aside>

      <section class="stack">
        <div class="card">
          <div class="card-header"><div class="card-title">运行状态</div><span class="muted" id="operationText">空闲</span></div>
          <div class="card-body">
            <div class="metrics">
              <div class="metric"><div class="label">当前资料库</div><div class="value" id="mRepo">-</div></div>
              <div class="metric"><div class="label">完成进度</div><div class="value" id="mProgress">-</div></div>
              <div class="metric"><div class="label">下载速率</div><div class="value" id="mDown">-</div></div>
              <div class="metric"><div class="label">上传速率</div><div class="value" id="mUp">-</div></div>
              <div class="metric"><div class="label">剩余时间</div><div class="value" id="mEta">-</div></div>
              <div class="metric"><div class="label">本次传输</div><div class="value" id="mTransfer">-</div></div>
            </div>
            <div class="progress-wrap"><div id="progressBar" class="progress-bar"></div></div>
            <div class="footer-status" id="footerStatus">等待任务开始。</div>
          </div>
        </div>

        <div class="card">
          <div class="card-header"><div class="card-title">云盘概览</div></div>
          <div class="card-body">
            <div class="summary-grid">
              <div class="summary-item"><span>清华账号</span><strong id="sAccount">未连接</strong></div>
              <div class="summary-item"><span>全部资料库</span><strong id="sAll">-</strong></div>
              <div class="summary-item"><span>当前选中</span><strong id="sSelected">-</strong></div>
              <div class="summary-item"><span>百度账号</span><strong id="sBaidu">未验证</strong></div>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-header"><div class="card-title">资料库</div><span class="muted" id="repoCount">0 个</span></div>
          <div class="card-body">
            <div class="table-wrap">
              <table>
                <thead><tr><th>分类</th><th>名称</th><th>大小</th><th>所有者/群组</th><th>权限</th></tr></thead>
                <tbody id="repoRows"><tr><td colspan="5" class="muted">连接清华云盘后显示资料库。</td></tr></tbody>
              </table>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-header"><div class="card-title">日志</div></div>
          <div class="card-body"><div class="logs" id="logs"></div></div>
        </div>
      </section>
    </section>
  </main>

  <script>
    const CATEGORIES = ["我的资料库", "群组共享内容", "共享给我的"];
    const state = { data: null };
    const $ = (id) => document.getElementById(id);
    const fmtSize = (value) => {
      value = Number(value || 0);
      const units = ["B", "KiB", "MiB", "GiB", "TiB"];
      let index = 0;
      while (value >= 1024 && index < units.length - 1) { value /= 1024; index++; }
      return index === 0 ? `${Math.round(value)} B` : `${value.toFixed(1)} ${units[index]}`;
    };
    const fmtRate = (value) => Number(value || 0) > 0 ? `${fmtSize(value)}/s` : "-";
    const fmtDuration = (seconds) => {
      if (seconds === null || seconds === undefined) return "-";
      seconds = Math.max(0, Math.floor(Number(seconds) || 0));
      const h = Math.floor(seconds / 3600);
      const m = Math.floor((seconds % 3600) / 60);
      const s = seconds % 60;
      if (h) return `${h} 小时 ${m} 分`;
      if (m) return `${m} 分 ${s} 秒`;
      return `${s} 秒`;
    };
    const selectedCategories = () => [...document.querySelectorAll(".category-check:checked")].map((el) => el.value);
    const postJSON = async (url, body = {}) => {
      const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const payload = await response.json();
      if (!response.ok || payload.ok === false) throw new Error(payload.error || response.statusText);
      return payload;
    };
    const setStatus = (message) => { $("footerStatus").textContent = message; };
    const bind = () => {
      $("openToken").onclick = () => window.open("https://cloud.tsinghua.edu.cn/profile/", "_blank");
      $("pasteToken").onclick = async () => { $("token").value = await navigator.clipboard.readText(); };
      $("connectTsinghua").onclick = async () => runAction(() => postJSON("/api/tsinghua/connect", { token: $("token").value }), "已开始连接清华云盘。");
      $("getDeviceCode").onclick = async () => runAction(async () => {
        const payload = await postJSON("/api/baidu/device-code", { appKey: $("appKey").value });
        if (payload.authUrl) window.open(payload.authUrl, "_blank");
      }, "已打开百度授权页面。");
      $("finishAuth").onclick = async () => runAction(() => postJSON("/api/baidu/finish-auth", { appKey: $("appKey").value, appSecret: $("appSecret").value }), "正在等待百度授权完成。");
      $("pasteBaiduToken").onclick = async () => { $("baiduToken").value = await navigator.clipboard.readText(); };
      $("validateBaidu").onclick = async () => runAction(() => postJSON("/api/baidu/validate-token", { accessToken: $("baiduToken").value }), "正在验证百度账号。");
      $("startBackup").onclick = async () => runAction(() => postJSON("/api/backup/start", {
        categories: selectedCategories(),
        destination: $("destination").value,
        workers: Number($("workers").value || 4)
      }), "已开始本地备份。");
      $("startMigration").onclick = async () => runAction(() => postJSON("/api/migration/start", {
        categories: selectedCategories(),
        appDir: $("appDir").value,
        targetRoot: $("targetRoot").value,
        tempDir: $("tempDir").value,
        workers: Number($("migrationWorkers").value || 4),
        verifyRemote: $("verifyRemote").checked
      }), "已开始迁移到百度网盘。");
      $("stopTask").onclick = async () => runAction(() => postJSON("/api/task/stop", {}), "正在停止任务。");
    };
    const runAction = async (fn, okMessage) => {
      try {
        await fn();
        setStatus(okMessage);
      } catch (error) {
        setStatus(error.message);
      }
    };
    const initCategories = () => {
      $("categoryChecks").innerHTML = "";
      for (const category of CATEGORIES) {
        const label = document.createElement("label");
        label.className = "check";
        label.innerHTML = `<input class="category-check" type="checkbox" checked value="${category}"><span>${category}</span>`;
        $("categoryChecks").appendChild(label);
      }
    };
    const render = (data) => {
      state.data = data;
      $("statusText").textContent = data.status || "空闲";
      $("statusDot").className = `dot${data.running ? " running" : ""}`;
      $("operationText").textContent = data.running ? (data.operation || "运行中") : "空闲";
      $("footerStatus").textContent = data.status || "";
      if (!$("destination").value) $("destination").value = data.defaults.destination;
      if (!$("targetRoot").value) $("targetRoot").value = data.defaults.migrationRoot;
      if (!$("tempDir").value) $("tempDir").value = data.defaults.migrationTempDir;
      if (!$("workers").value) $("workers").value = data.defaults.backupWorkers;
      if (!$("migrationWorkers").value) $("migrationWorkers").value = data.defaults.migrationWorkers;
      renderMetrics(data);
      renderSummary(data);
      renderRepos(data.repositories || []);
      renderLogs(data.logs || []);
      renderBaiduHint(data);
    };
    const renderMetrics = (data) => {
      const stats = data.stats || {};
      const total = Number(stats.bytes_total || 0);
      const completed = Number(stats.bytes_completed || 0);
      const repoTotal = Number(stats.repositories_total || 0);
      const repoDone = Number(stats.repositories_done || 0);
      let percent = 0;
      let progressText = "-";
      if (total > 0) {
        percent = Math.min(completed / total * 100, 100);
        progressText = `${fmtSize(completed)} / ${fmtSize(total)} (${percent.toFixed(1)}%)`;
      } else if (repoTotal > 0) {
        percent = Math.min(repoDone / repoTotal * 100, 100);
        progressText = `资料库 ${repoDone}/${repoTotal} (${percent.toFixed(1)}%)`;
      }
      $("progressBar").style.width = `${percent}%`;
      $("mRepo").textContent = stats.current_repo || "-";
      $("mProgress").textContent = progressText;
      $("mDown").textContent = fmtRate(stats.download_speed_bps);
      $("mUp").textContent = data.mode === "migration" ? fmtRate(stats.upload_speed_bps) : "-";
      $("mEta").textContent = fmtDuration(stats.eta_seconds);
      $("mTransfer").textContent = data.mode === "migration"
        ? `读 ${fmtSize(stats.bytes_source_read)} / 传 ${fmtSize(stats.bytes_uploaded)}`
        : fmtSize(stats.bytes_source_read);
    };
    const renderSummary = (data) => {
      const account = data.account || {};
      const repos = data.repositories || [];
      const allSize = repos.reduce((sum, repo) => sum + Number(repo.size || 0), 0);
      const selected = repos.filter((repo) => selectedCategories().includes(categoryOf(repo)));
      const selectedSize = selected.reduce((sum, repo) => sum + Number(repo.size || 0), 0);
      $("sAccount").textContent = account.name || account.email || (data.tokenReady ? "已连接" : "未连接");
      $("sAll").textContent = `${repos.length} 个，${fmtSize(allSize)}`;
      $("sSelected").textContent = `${selected.length} 个，${fmtSize(selectedSize)}`;
      const baidu = data.baiduAccount || {};
      $("sBaidu").textContent = data.baiduAccessReady ? (baidu.netdisk_name || baidu.baidu_name || baidu.uk || "已验证") : "未验证";
      $("repoCount").textContent = `${repos.length} 个`;
    };
    const categoryOf = (repo) => {
      const types = new Set(repo.types || []);
      if (types.has("srepo") || types.has("personal")) return "共享给我的";
      if (types.has("grepo") || types.has("group") || (repo.group_names || []).length) return "群组共享内容";
      return "我的资料库";
    };
    const renderRepos = (repos) => {
      const selected = selectedCategories();
      const rows = repos.filter((repo) => selected.includes(categoryOf(repo)));
      $("repoRows").innerHTML = "";
      if (!rows.length) {
        $("repoRows").innerHTML = `<tr><td colspan="5" class="muted">没有匹配当前范围的资料库。</td></tr>`;
        return;
      }
      for (const repo of rows) {
        const tr = document.createElement("tr");
        const owner = [...(repo.group_names || []), ...(repo.owner_names || []), ...(repo.share_from_names || [])].filter(Boolean).join(", ");
        const cells = [categoryOf(repo), repo.name || repo.id, fmtSize(repo.size), owner, (repo.permissions || []).join(",")];
        cells.forEach((value, index) => {
          const td = document.createElement("td");
          td.textContent = value || "-";
          if (index === 2) td.className = "size";
          tr.appendChild(td);
        });
        $("repoRows").appendChild(tr);
      }
    };
    const renderLogs = (logs) => {
      $("logs").textContent = logs.join("\n");
      $("logs").scrollTop = $("logs").scrollHeight;
    };
    const renderBaiduHint = (data) => {
      if (!data.baiduAccessReady) {
        const device = data.baiduDeviceData || {};
        $("baiduHint").textContent = device.user_code ? `用户码：${device.user_code}，授权后点击“完成授权”。` : "尚未验证百度账号。";
        return;
      }
      const quota = data.baiduQuota || {};
      const used = Number(quota.used || 0);
      const total = Number(quota.total || 0);
      const free = Number(quota.free || Math.max(total - used, 0));
      $("baiduHint").textContent = total ? `空间 ${fmtSize(used)} / ${fmtSize(total)}，可用 ${fmtSize(free)}` : `可用 ${fmtSize(free)}`;
    };
    initCategories();
    bind();
    document.addEventListener("change", (event) => {
      if (event.target.classList.contains("category-check") && state.data) {
        renderSummary(state.data);
        renderRepos(state.data.repositories || []);
      }
    });
    const events = new EventSource("/api/events");
    events.addEventListener("state", (event) => render(JSON.parse(event.data)));
    events.onerror = () => setStatus("控制台连接中断，正在等待自动重连。");
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
