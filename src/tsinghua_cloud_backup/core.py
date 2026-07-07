from __future__ import annotations

import concurrent.futures
import csv
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable


BASE_URL = "https://cloud.tsinghua.edu.cn"
ALL_CATEGORIES = ("我的资料库", "群组共享内容", "共享给我的")
MAX_WINDOWS_PATH = 240
INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def utc_iso(ts: int | str | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except Exception:
        return ""


def human_size(value: int | float | None) -> str:
    value = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"


def safe_name(name: str | None, fallback: str = "unnamed") -> str:
    name = str(name or fallback)
    name = INVALID_CHARS.sub("_", name).strip().rstrip(".")
    if not name:
        name = fallback
    if name.upper() in RESERVED_NAMES:
        name = f"{name}_"
    return name


def shorten_component(name: str, max_len: int = 110) -> str:
    if len(name) <= max_len:
        return name
    stem, suffix = os.path.splitext(name)
    suffix_len = min(len(suffix), 20)
    suffix = suffix[-suffix_len:] if suffix_len else ""
    keep = max_len - len(suffix) - 9
    if keep < 20:
        keep = max_len - 9
        suffix = ""
    return f"{stem[:keep]}__trimmed{suffix}"


def fit_path(path: Path, key: str) -> Path:
    path = Path(path)
    if len(str(path)) <= MAX_WINDOWS_PATH:
        return path
    parent = path.parent
    name = path.name
    stem, suffix = os.path.splitext(name)
    reserve = len(str(parent)) + len(os.sep) + len(suffix) + 14
    keep = max(MAX_WINDOWS_PATH - reserve, 12)
    return parent / f"{stem[:keep]}__{key[:8]}{suffix}"


def posix_join(parent: str, child: str) -> str:
    if parent in ("", "/"):
        return "/" + child
    return str(PurePosixPath(parent) / child)


def category_for(repo: dict) -> str:
    types = set(repo.get("types") or [])
    if "srepo" in types or "personal" in types:
        return "共享给我的"
    if "grepo" in types or "group" in types or repo.get("group_names"):
        return "群组共享内容"
    return "我的资料库"


class CancelledBackup(Exception):
    pass


class SeafileClient:
    def __init__(self, token: str, base_url: str = BASE_URL, timeout: int = 90, retries: int = 5):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.opener = urllib.request.build_opener()
        self.headers = {
            "Authorization": f"Token {token}",
            "User-Agent": "TsinghuaCloudBackupGUI/0.1",
        }

    def open(self, url: str, headers: dict | None = None, timeout: int | None = None):
        merged = dict(self.headers)
        if headers:
            merged.update(headers)
        request = urllib.request.Request(url, headers=merged)
        delay = 2
        last_error = None
        for attempt in range(1, self.retries + 1):
            try:
                return self.opener.open(request, timeout=timeout or self.timeout)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in (429, 500, 502, 503, 504):
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last_error = exc
                if attempt == self.retries:
                    break
                time.sleep(delay)
                delay = min(delay * 2, 60)
        raise last_error

    def get_json(self, path: str, params: dict | None = None):
        query = urllib.parse.urlencode(params or {})
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        with self.open(url) as response:
            return json.load(response)

    def get_text(self, path: str, params: dict | None = None) -> str:
        query = urllib.parse.urlencode(params or {})
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        with self.open(url) as response:
            return response.read().decode("utf-8")

    def ping(self) -> bool:
        return self.get_text("/api2/auth/ping/").strip().strip('"') == "pong"

    def account_info(self) -> dict:
        return self.get_json("/api2/account/info/")

    def repos(self) -> list[dict]:
        data = self.get_json("/api2/repos/")
        return data if isinstance(data, list) else data.get("value", [])

    def shared_repos(self) -> list[dict]:
        return self.get_json("/api2/shared-repos/")

    def beshared_repos(self) -> list[dict]:
        return self.get_json("/api2/beshared-repos/")

    def list_dir(self, repo_id: str, path: str) -> list[dict]:
        data = self.get_json(f"/api2/repos/{repo_id}/dir/", {"p": path})
        if isinstance(data, dict) and "dirent_list" in data:
            return data["dirent_list"]
        return data

    def download_url(self, repo_id: str, path: str) -> str:
        text = self.get_text(f"/api2/repos/{repo_id}/file/", {"p": path}).strip()
        return json.loads(text)


def discover_repositories(client: SeafileClient) -> list[dict]:
    repo_meta: dict[str, dict] = {}

    def add(item: dict, source: str) -> None:
        repo_id = item.get("id") or item.get("repo_id")
        if not repo_id:
            return
        current = repo_meta.setdefault(
            repo_id,
            {
                "id": repo_id,
                "name": item.get("name") or item.get("repo_name") or repo_id,
                "types": set(),
                "sources": set(),
                "group_names": set(),
                "owner_names": set(),
                "share_from_names": set(),
                "permissions": set(),
                "size": int(item.get("size") or 0),
                "mtime": item.get("mtime") or item.get("last_modify") or 0,
                "encrypted": bool(item.get("encrypted")),
                "root": item.get("root") or "",
                "version": item.get("version"),
                "head_commit_id": item.get("head_commit_id") or item.get("head_cmmt_id") or "",
            },
        )
        current["sources"].add(source)
        if item.get("type"):
            current["types"].add(item["type"])
        if item.get("share_type"):
            current["types"].add(item["share_type"])
        if item.get("group_name"):
            current["group_names"].add(item["group_name"])
        owner_name = item.get("owner_name") or item.get("owner")
        if owner_name:
            current["owner_names"].add(owner_name)
        if item.get("share_from_name"):
            current["share_from_names"].add(item["share_from_name"])
        if item.get("permission"):
            current["permissions"].add(item["permission"])
        current["size"] = max(current["size"], int(item.get("size") or 0))
        current["mtime"] = max(int(current["mtime"] or 0), int(item.get("mtime") or item.get("last_modify") or 0))
        current["encrypted"] = current["encrypted"] or bool(item.get("encrypted"))
        current["root"] = current["root"] or item.get("root") or ""
        current["head_commit_id"] = current["head_commit_id"] or item.get("head_commit_id") or item.get("head_cmmt_id") or ""

    for item in client.repos():
        add(item, "api2/repos")
    for item in client.shared_repos():
        add(item, "api2/shared-repos")
    for item in client.beshared_repos():
        add(item, "api2/beshared-repos")

    for meta in repo_meta.values():
        for key in ("types", "sources", "group_names", "owner_names", "share_from_names", "permissions"):
            meta[key] = sorted(meta[key])
    return sorted(repo_meta.values(), key=lambda repo: (category_for(repo), repo["name"], repo["id"]))


def repo_folder(destination: Path, repo: dict) -> Path:
    category = category_for(repo)
    category_dir = destination / category
    owner = repo["owner_names"][0] if repo.get("owner_names") else ""
    group = repo["group_names"][0] if repo.get("group_names") else ""
    if category == "群组共享内容" and group:
        preferred = f"{group} - {repo['name']} [{repo['id'][:8]}]"
    elif category == "共享给我的":
        prefix = owner or (repo["share_from_names"][0] if repo.get("share_from_names") else "")
        preferred = f"{prefix} - {repo['name']} [{repo['id'][:8]}]" if prefix else f"{repo['name']} [{repo['id'][:8]}]"
    else:
        preferred = f"{repo['name']} [{repo['id'][:8]}]"
    return category_dir / shorten_component(safe_name(preferred))


@dataclass
class BackupOptions:
    destination: Path
    categories: set[str]
    workers: int = 4
    overwrite_same_size: bool = False
    dry_run: bool = False


@dataclass
class BackupStats:
    repositories_total: int = 0
    repositories_done: int = 0
    files_seen: int = 0
    downloaded: int = 0
    skipped: int = 0
    would_download: int = 0
    failed: int = 0
    bytes_downloaded: int = 0
    current_repo: str = ""


class BackupRunner:
    def __init__(
        self,
        client: SeafileClient,
        options: BackupOptions,
        event_callback: Callable[[dict], None] | None = None,
        cancel_event: threading.Event | None = None,
    ):
        self.client = client
        self.options = options
        self.event_callback = event_callback or (lambda event: None)
        self.cancel_event = cancel_event or threading.Event()
        self.destination = Path(options.destination)
        self.meta_dir = self.destination / "_backup_metadata"
        self.log_path = self.meta_dir / "backup.log"
        self.failures_path = self.meta_dir / "failures.jsonl"
        self.manifest_path = self.meta_dir / "manifest.json"
        self.repos_csv_path = self.meta_dir / "repositories.csv"
        self.files_csv_path = self.meta_dir / "files.csv"
        if self.options.dry_run:
            self.log_path = self.meta_dir / "dry-run.log"
            self.failures_path = self.meta_dir / "dry-run-failures.jsonl"
            self.manifest_path = self.meta_dir / "dry-run-manifest.json"
            self.repos_csv_path = self.meta_dir / "dry-run-repositories.csv"
            self.files_csv_path = self.meta_dir / "dry-run-files.csv"
        self.lock = threading.Lock()
        self.writer_lock = threading.Lock()
        self.stats = BackupStats()
        self.executor: concurrent.futures.ThreadPoolExecutor | None = None
        self.pending: set[concurrent.futures.Future] = set()

    def emit(self, kind: str, **payload) -> None:
        event = {"kind": kind, "time": now_text(), **payload}
        self.event_callback(event)

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise CancelledBackup()

    def log(self, message: str) -> None:
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        line = f"[{now_text()}] {message}"
        with self.lock:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        self.emit("log", message=message)

    def record_failure(self, payload: dict) -> None:
        with self.lock:
            self.stats.failed += 1
        payload = dict(payload)
        payload["time"] = now_text()
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        with self.lock:
            with self.failures_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def write_repository_index(self, repos: list[dict]) -> None:
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "created_at": now_text(),
            "base_url": self.client.base_url,
            "destination": str(self.destination),
            "repository_count": len(repos),
            "total_declared_size": sum(int(repo.get("size") or 0) for repo in repos),
            "repositories": repos,
        }
        self.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        with self.repos_csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "selected",
                    "category",
                    "id",
                    "name",
                    "size",
                    "mtime_utc",
                    "types",
                    "sources",
                    "owners",
                    "groups",
                    "permissions",
                    "local_path",
                ],
            )
            writer.writeheader()
            for repo in repos:
                writer.writerow(
                    {
                        "selected": category_for(repo) in self.options.categories,
                        "category": category_for(repo),
                        "id": repo["id"],
                        "name": repo["name"],
                        "size": repo["size"],
                        "mtime_utc": utc_iso(repo.get("mtime")),
                        "types": ";".join(repo.get("types") or []),
                        "sources": ";".join(repo.get("sources") or []),
                        "owners": ";".join(repo.get("owner_names") or []),
                        "groups": ";".join(repo.get("group_names") or []),
                        "permissions": ";".join(repo.get("permissions") or []),
                        "local_path": str(repo_folder(self.destination, repo)),
                    }
                )

    def run(self) -> BackupStats:
        start = time.time()
        self.destination.mkdir(parents=True, exist_ok=True)
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        self.log("开始连接清华云盘。")
        repos = discover_repositories(self.client)
        self.write_repository_index(repos)
        selected = [repo for repo in repos if category_for(repo) in self.options.categories]
        self.stats.repositories_total = len(selected)
        self.log(f"发现 {len(repos)} 个唯一资料库，选中 {len(selected)} 个。")

        with self.files_csv_path.open("a", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["time", "repo_id", "repo_name", "remote_path", "local_path", "size", "mtime_utc", "status"],
            )
            if handle.tell() == 0:
                writer.writeheader()
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, self.options.workers)) as executor:
                self.executor = executor
                for index, repo in enumerate(selected, start=1):
                    self.check_cancelled()
                    self.backup_repository(repo, index, len(selected), writer, handle)
                    self.drain_pending()
                    with self.lock:
                        self.stats.repositories_done += 1
                    self.emit_progress()
                self.drain_pending()
        elapsed = time.time() - start
        self.log(
            "完成。"
            f"文件={self.stats.files_seen}，下载={self.stats.downloaded}，跳过={self.stats.skipped}，"
            f"试运行待下载={self.stats.would_download}，失败={self.stats.failed}，"
            f"新增={human_size(self.stats.bytes_downloaded)}，耗时={elapsed/60:.1f} 分钟。"
        )
        self.emit("done", stats=self.stats.__dict__)
        return self.stats

    def backup_repository(self, repo: dict, index: int, total: int, writer: csv.DictWriter, handle) -> None:
        repo_dir = repo_folder(self.destination, repo)
        repo_dir.mkdir(parents=True, exist_ok=True)
        (repo_dir / "_repository_info.json").write_text(json.dumps(repo, ensure_ascii=False, indent=2), encoding="utf-8")
        with self.lock:
            self.stats.current_repo = repo["name"]
        self.log(f"[{index}/{total}] {category_for(repo)} | {repo['name']} ({repo['id'][:8]}, {human_size(repo.get('size'))})")
        if repo.get("encrypted"):
            self.log(f"跳过加密资料库（未提供密码）：{repo['name']}")
            self.record_failure({"repo_id": repo["id"], "repo_name": repo["name"], "error": "encrypted repository"})
            return
        self.walk_dir(repo, "/", repo_dir, writer, handle)

    def walk_dir(self, repo: dict, remote_dir: str, local_dir: Path, writer: csv.DictWriter, handle) -> None:
        self.check_cancelled()
        local_dir.mkdir(parents=True, exist_ok=True)
        try:
            entries = self.client.list_dir(repo["id"], remote_dir)
        except Exception as exc:
            self.log(f"列目录失败 {repo['name']}:{remote_dir} | {exc}")
            self.record_failure({"repo_id": repo["id"], "repo_name": repo["name"], "remote_path": remote_dir, "error": repr(exc), "operation": "list_dir"})
            return
        for entry in entries or []:
            self.check_cancelled()
            entry_name = entry.get("name") or entry.get("obj_name") or ""
            remote_path = posix_join(remote_dir, entry_name)
            local_name = shorten_component(safe_name(entry_name, fallback=entry.get("id") or "unnamed"))
            local_path = fit_path(local_dir / local_name, entry.get("id") or repo["id"])
            if entry.get("type") == "dir":
                self.walk_dir(repo, remote_path, local_path, writer, handle)
            elif entry.get("type") == "file":
                with self.lock:
                    self.stats.files_seen += 1
                self.submit_download(repo, entry, remote_path, local_path, writer, handle)

    def submit_download(self, repo: dict, entry: dict, remote_path: str, local_path: Path, writer: csv.DictWriter, handle) -> None:
        while len(self.pending) >= max(1, self.options.workers) * 8:
            self.drain_pending(limit=1)
        assert self.executor is not None
        self.pending.add(self.executor.submit(self.download_file, repo, entry, remote_path, local_path, writer, handle))

    def drain_pending(self, limit: int | None = None) -> None:
        done_count = 0
        while self.pending and (limit is None or done_count < limit):
            done, self.pending = concurrent.futures.wait(self.pending, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                done_count += 1
                try:
                    future.result()
                except CancelledBackup:
                    self.cancel_event.set()
                    raise
                except Exception as exc:
                    self.log(f"下载线程异常：{exc}")
                    self.record_failure({"error": repr(exc), "operation": "worker"})
                if limit is not None and done_count >= limit:
                    break

    def download_file(self, repo: dict, entry: dict, remote_path: str, local_path: Path, writer: csv.DictWriter, handle) -> None:
        self.check_cancelled()
        expected_size = int(entry.get("size") or 0)
        mtime = int(entry.get("mtime") or 0)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if self.local_file_is_current(local_path, expected_size, mtime) and not self.options.overwrite_same_size:
            if mtime:
                set_mtime(local_path, mtime)
            with self.lock:
                self.stats.skipped += 1
            self.write_file_row(writer, handle, repo, remote_path, local_path, expected_size, mtime, "skipped")
            return

        if self.options.dry_run:
            with self.lock:
                self.stats.would_download += 1
            self.write_file_row(writer, handle, repo, remote_path, local_path, expected_size, mtime, "would_download")
            self.emit_progress()
            return

        temp_path = local_path.with_name(f".{local_path.name}.part")
        last_error = None
        for attempt in range(1, 4):
            self.check_cancelled()
            try:
                download_url = self.client.download_url(repo["id"], remote_path)
                bytes_written = self.stream_to_file(download_url, temp_path)
                actual_size = temp_path.stat().st_size
                if expected_size and actual_size != expected_size:
                    raise IOError(f"size mismatch: expected {expected_size}, got {actual_size}")
                os.replace(temp_path, local_path)
                if mtime:
                    set_mtime(local_path, mtime)
                with self.lock:
                    self.stats.downloaded += 1
                    self.stats.bytes_downloaded += bytes_written
                self.write_file_row(writer, handle, repo, remote_path, local_path, expected_size, mtime, "downloaded")
                self.emit_progress()
                return
            except Exception as exc:
                last_error = exc
                keep_part = isinstance(exc, IOError) and str(exc).startswith("size mismatch:")
                if temp_path.exists() and not keep_part:
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
                if attempt < 3:
                    time.sleep(2 * attempt)
        self.log(f"文件失败 {repo['name']}:{remote_path} | {last_error}")
        self.record_failure(
            {
                "repo_id": repo["id"],
                "repo_name": repo["name"],
                "remote_path": remote_path,
                "local_path": str(local_path),
                "size": expected_size,
                "error": repr(last_error),
                "operation": "download_file",
            }
        )
        self.write_file_row(writer, handle, repo, remote_path, local_path, expected_size, mtime, "failed")
        self.emit_progress()

    @staticmethod
    def local_file_is_current(local_path: Path, expected_size: int, mtime: int) -> bool:
        if not local_path.exists():
            return False
        try:
            stat = local_path.stat()
        except OSError:
            return False
        if stat.st_size != expected_size:
            return False
        if not mtime:
            return True
        return abs(stat.st_mtime - mtime) <= 2

    def stream_to_file(self, download_url: str, temp_path: Path) -> int:
        headers = {}
        resume_at = 0
        if temp_path.exists():
            resume_at = temp_path.stat().st_size
            if resume_at:
                headers["Range"] = f"bytes={resume_at}-"
        mode = "ab" if resume_at else "wb"
        bytes_written = 0
        with self.client.open(download_url, headers=headers, timeout=1800) as response:
            if resume_at and getattr(response, "status", 200) == 200:
                mode = "wb"
                resume_at = 0
            with temp_path.open(mode) as handle:
                while True:
                    self.check_cancelled()
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    bytes_written += len(chunk)
        return resume_at + bytes_written

    def write_file_row(self, writer: csv.DictWriter, handle, repo: dict, remote_path: str, local_path: Path, size: int, mtime: int, status: str) -> None:
        with self.writer_lock:
            writer.writerow(
                {
                    "time": now_text(),
                    "repo_id": repo["id"],
                    "repo_name": repo["name"],
                    "remote_path": remote_path,
                    "local_path": str(local_path),
                    "size": size,
                    "mtime_utc": utc_iso(mtime),
                    "status": status,
                }
            )
            handle.flush()

    def emit_progress(self) -> None:
        with self.lock:
            stats = dict(self.stats.__dict__)
        stats["bytes_downloaded_text"] = human_size(stats["bytes_downloaded"])
        self.emit("progress", stats=stats)


def set_mtime(path: Path, mtime: int) -> None:
    try:
        os.utime(path, (mtime, mtime))
    except OSError:
        pass


def enqueue_callback(q: queue.Queue) -> Callable[[dict], None]:
    return q.put
