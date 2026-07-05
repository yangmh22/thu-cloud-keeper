from __future__ import annotations

import csv
import concurrent.futures
import hashlib
import json
import shutil
import threading
import time
import urllib.error
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from .baidu import BaiduNetdiskClient
from .core import (
    BackupStats,
    CancelledBackup,
    ProgressMeter,
    SeafileClient,
    category_for,
    discover_repositories,
    human_size,
    now_text,
    posix_join,
    safe_name,
    shorten_component,
    utc_iso,
)


BAIDU_CHUNK_SIZE = 4 * 1024 * 1024


class RangeUnsupported(Exception):
    pass


@dataclass
class MigrationOptions:
    categories: set[str]
    app_dir_name: str
    target_root: str = "清华云盘迁移"
    temp_dir: Path = Path.home() / "Desktop" / "清华云盘迁移临时"
    rtype: int = 2
    workers: int = 4
    verify_remote_resume: bool = False


class MigrationRunner:
    def __init__(
        self,
        seafile_client: SeafileClient,
        baidu_client: BaiduNetdiskClient,
        options: MigrationOptions,
        event_callback: Callable[[dict], None] | None = None,
        cancel_event: threading.Event | None = None,
    ):
        self.seafile_client = seafile_client
        self.baidu_client = baidu_client
        self.options = options
        self.event_callback = event_callback or (lambda event: None)
        self.cancel_event = cancel_event or threading.Event()
        self.temp_dir = Path(options.temp_dir)
        self.parts_dir = self.temp_dir / "_parts"
        self.meta_dir = self.temp_dir / "_migration_metadata"
        self.log_path = self.meta_dir / "migration.log"
        self.failures_path = self.meta_dir / "failures.jsonl"
        self.repos_csv_path = self.meta_dir / "repositories.csv"
        self.files_csv_path = self.meta_dir / "files.csv"
        self.lock = threading.Lock()
        self.writer_lock = threading.Lock()
        self.dir_lock = threading.Lock()
        self.index_lock = threading.Lock()
        self.remote_lock = threading.Lock()
        self.stats = BackupStats()
        self.progress_meter = ProgressMeter(self.stats, self.lock)
        self.executor: concurrent.futures.ThreadPoolExecutor | None = None
        self.pending: set[concurrent.futures.Future] = set()
        self.created_dirs: set[str] = set()
        self.success_index: set[tuple[str, str, str, int, str]] = set()
        self.remote_dir_cache: dict[str, dict[str, dict] | None] = {}
        self.remote_check_errors: set[str] = set()

    def emit(self, kind: str, **payload) -> None:
        self.event_callback({"kind": kind, "time": now_text(), "mode": "migration", **payload})

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

    def run(self) -> BackupStats:
        start = time.time()
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.parts_dir.mkdir(parents=True, exist_ok=True)
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        self.success_index = self.load_success_index()
        self.log("开始连接清华云盘并迁移到百度网盘。")
        if self.success_index:
            self.log(f"已加载 {len(self.success_index)} 条本地成功迁移记录用于续跑判断。")
        if self.options.verify_remote_resume:
            self.log("续跑模式：严格校验，会查询百度目标目录确认已迁移文件。")
        else:
            self.log("续跑模式：快速续跑，优先信任本地成功清单，避免逐文件查询百度目录。")
        repos = discover_repositories(self.seafile_client)
        selected = [repo for repo in repos if category_for(repo) in self.options.categories]
        self.stats.repositories_total = len(selected)
        self.progress_meter.set_total(sum(int(repo.get("size") or 0) for repo in selected))
        self.write_repository_index(repos)
        self.log(f"发现 {len(repos)} 个唯一资料库，选中 {len(selected)} 个。")
        self.emit_progress()

        with self.files_csv_path.open("a", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "time",
                    "repo_id",
                    "repo_name",
                    "remote_path",
                    "baidu_path",
                    "size",
                    "mtime_utc",
                    "status",
                ],
            )
            if handle.tell() == 0:
                writer.writeheader()
            self.ensure_dir(self.target_base_path())
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(self.options.workers or 1))) as executor:
                self.executor = executor
                for index, repo in enumerate(selected, start=1):
                    self.check_cancelled()
                    self.migrate_repository(repo, index, len(selected), writer, handle)
                    self.drain_pending()
                    with self.lock:
                        self.stats.repositories_done += 1
                    self.emit_progress()
                self.drain_pending()

        elapsed = time.time() - start
        self.log(
            "迁移完成。"
            f"文件={self.stats.files_seen}，迁移={self.stats.downloaded}，跳过={self.stats.skipped}，"
            f"失败={self.stats.failed}，传输={human_size(self.stats.bytes_downloaded)}，耗时={elapsed/60:.1f} 分钟。"
        )
        self.emit("done", stats=self.stats.__dict__)
        return self.stats

    def write_repository_index(self, repos: list[dict]) -> None:
        manifest = {
            "created_at": now_text(),
            "target": "baidu-netdisk",
            "target_root": self.target_base_path(),
            "repository_count": len(repos),
            "total_declared_size": sum(int(repo.get("size") or 0) for repo in repos),
            "repositories": repos,
        }
        (self.meta_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        with self.repos_csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["selected", "category", "id", "name", "size", "mtime_utc", "target_path"],
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
                        "target_path": self.repo_target_path(repo),
                    }
                )

    def migrate_repository(self, repo: dict, index: int, total: int, writer: csv.DictWriter, handle) -> None:
        with self.lock:
            self.stats.current_repo = repo["name"]
        target_dir = self.repo_target_path(repo)
        self.ensure_dir(target_dir)
        self.log(f"[{index}/{total}] {category_for(repo)} | {repo['name']} -> {target_dir}")
        if repo.get("encrypted"):
            self.log(f"跳过加密资料库（未提供密码）：{repo['name']}")
            self.record_failure({"repo_id": repo["id"], "repo_name": repo["name"], "error": "encrypted repository"})
            return
        self.walk_dir(repo, "/", target_dir, writer, handle)

    def walk_dir(self, repo: dict, remote_dir: str, target_dir: str, writer: csv.DictWriter, handle) -> None:
        self.check_cancelled()
        self.ensure_dir(target_dir)
        try:
            entries = self.seafile_client.list_dir(repo["id"], remote_dir)
        except CancelledBackup:
            raise
        except Exception as exc:
            self.log(f"列目录失败 {repo['name']}:{remote_dir} | {exc}")
            self.record_failure({"repo_id": repo["id"], "repo_name": repo["name"], "remote_path": remote_dir, "error": repr(exc), "operation": "list_dir"})
            return

        for entry in entries or []:
            self.check_cancelled()
            entry_name = entry.get("name") or entry.get("obj_name") or ""
            remote_path = posix_join(remote_dir, entry_name)
            target_name = remote_component(entry_name, fallback=entry.get("id") or "unnamed")
            target_path = baidu_join(target_dir, target_name)
            if entry.get("type") == "dir":
                self.walk_dir(repo, remote_path, target_path, writer, handle)
            elif entry.get("type") == "file":
                with self.lock:
                    self.stats.files_seen += 1
                self.submit_migration(repo, entry, remote_path, target_path, writer, handle)

    def submit_migration(self, repo: dict, entry: dict, remote_path: str, target_path: str, writer: csv.DictWriter, handle) -> None:
        while len(self.pending) >= max(1, int(self.options.workers or 1)) * 4:
            self.drain_pending(limit=1)
        assert self.executor is not None
        self.pending.add(self.executor.submit(self.migrate_file, repo, entry, remote_path, target_path, writer, handle))

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
                    self.log(f"迁移线程异常：{exc}")
                    self.record_failure({"error": repr(exc), "operation": "worker"})
                if limit is not None and done_count >= limit:
                    break

    def migrate_file(self, repo: dict, entry: dict, remote_path: str, target_path: str, writer: csv.DictWriter, handle) -> None:
        expected_size = int(entry.get("size") or 0)
        mtime = int(entry.get("mtime") or 0)
        file_key = f"{repo['id']}:{remote_path}:{target_path}"
        resume_status = self.resume_status(repo, remote_path, target_path, expected_size, mtime)
        if resume_status:
            with self.lock:
                self.stats.skipped += 1
            self.write_file_row(writer, handle, repo, remote_path, target_path, expected_size, mtime, resume_status)
            if resume_status in {"skipped_remote", "skipped_manifest"}:
                self.remember_success(repo, remote_path, target_path, expected_size, mtime)
            self.progress_meter.finish_progress(file_key, expected_size)
            self.emit_progress()
            return
        if expected_size == 0:
            self.log(f"跳过空文件（百度开放平台上传接口不支持空文件）：{repo['name']}:{remote_path}")
            with self.lock:
                self.stats.skipped += 1
            self.write_file_row(writer, handle, repo, remote_path, target_path, expected_size, mtime, "skipped_empty")
            self.progress_meter.finish_progress(file_key, expected_size)
            self.emit_progress()
            return
        last_error = None
        for attempt in range(1, 4):
            self.check_cancelled()
            try:
                self.upload_remote_file(repo["id"], remote_path, target_path, expected_size, file_key)
                with self.lock:
                    self.stats.downloaded += 1
                    self.stats.bytes_downloaded += expected_size
                self.write_file_row(writer, handle, repo, remote_path, target_path, expected_size, mtime, "migrated")
                self.remember_success(repo, remote_path, target_path, expected_size, mtime)
                self.update_remote_file_cache(target_path, expected_size)
                self.progress_meter.finish_progress(file_key, expected_size)
                self.emit_progress()
                return
            except CancelledBackup:
                raise
            except Exception as exc:
                last_error = exc
                self.cleanup_file_parts(file_key)
                if attempt < 3:
                    time.sleep(2 * attempt)
        self.log(f"迁移文件失败 {repo['name']}:{remote_path} -> {target_path} | {last_error}")
        self.record_failure(
            {
                "repo_id": repo["id"],
                "repo_name": repo["name"],
                "remote_path": remote_path,
                "baidu_path": target_path,
                "size": expected_size,
                "error": repr(last_error),
                "operation": "migrate_file",
            }
        )
        self.write_file_row(writer, handle, repo, remote_path, target_path, expected_size, mtime, "failed")
        self.progress_meter.finish_progress(file_key, expected_size)
        self.emit_progress()

    def resume_status(self, repo: dict, remote_path: str, target_path: str, size: int, mtime: int) -> str:
        with self.index_lock:
            local_success = self.success_key(repo, remote_path, target_path, size, mtime) in self.success_index
        if local_success and not self.options.verify_remote_resume:
            return "skipped_manifest"
        if not self.options.verify_remote_resume:
            return ""
        remote_state = self.remote_file_state(target_path)
        if remote_state is not None:
            if int(remote_state.get("size") or 0) == size and not int(remote_state.get("isdir") or 0):
                status = "skipped_manifest" if local_success else "skipped_remote"
                self.log(f"跳过已存在文件：{target_path}（{human_size(size)}，{status}）")
                return status
            if local_success:
                self.log(f"本地清单显示已迁移，但百度目标文件大小不一致，将重新迁移：{target_path}")
            return ""
        if local_success:
            parent = normalize_baidu_path(str(PurePosixPath(target_path).parent))
            if self.remote_dir_cache.get(parent) is None:
                self.log(f"百度远端校验不可用，按本地成功清单跳过：{target_path}")
                return "skipped_manifest_unverified"
        return ""

    def load_success_index(self) -> set[tuple[str, str, str, int, str]]:
        if not self.files_csv_path.exists():
            return set()
        success_statuses = {"migrated", "skipped_remote", "skipped_manifest"}
        index: set[tuple[str, str, str, int, str]] = set()
        try:
            with self.files_csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    if (row.get("status") or "") not in success_statuses:
                        continue
                    try:
                        size = int(row.get("size") or 0)
                    except ValueError:
                        continue
                    key = (
                        row.get("repo_id") or "",
                        row.get("remote_path") or "",
                        normalize_baidu_path(row.get("baidu_path") or ""),
                        size,
                        row.get("mtime_utc") or "",
                    )
                    if key[0] and key[1] and key[2]:
                        index.add(key)
        except OSError as exc:
            self.log(f"读取历史迁移清单失败，将不使用本地清单续跑：{exc}")
        return index

    def success_key(self, repo: dict, remote_path: str, target_path: str, size: int, mtime: int) -> tuple[str, str, str, int, str]:
        return (repo["id"], remote_path, normalize_baidu_path(target_path), int(size), utc_iso(mtime))

    def remember_success(self, repo: dict, remote_path: str, target_path: str, size: int, mtime: int) -> None:
        with self.index_lock:
            self.success_index.add(self.success_key(repo, remote_path, target_path, size, mtime))

    def remote_file_state(self, target_path: str) -> dict | None:
        target_path = normalize_baidu_path(target_path)
        parent = normalize_baidu_path(str(PurePosixPath(target_path).parent))
        name = PurePosixPath(target_path).name
        listing = self.remote_dir_listing(parent)
        if listing is None:
            return None
        item = listing.get(name)
        if item and normalize_baidu_path(item.get("path") or target_path) == target_path:
            return item
        return None

    def remote_dir_listing(self, remote_dir: str) -> dict[str, dict] | None:
        remote_dir = normalize_baidu_path(remote_dir)
        with self.remote_lock:
            if remote_dir in self.remote_dir_cache:
                return self.remote_dir_cache[remote_dir]
        items: dict[str, dict] = {}
        start = 0
        limit = 1000
        try:
            while True:
                payload = self.baidu_client.list_dir(remote_dir, start=start, limit=limit)
                page = payload.get("list") or []
                if not isinstance(page, list):
                    break
                for item in page:
                    if not isinstance(item, dict):
                        continue
                    filename = item.get("server_filename") or PurePosixPath(str(item.get("path") or "")).name
                    if filename:
                        items[str(filename)] = item
                if len(page) < limit:
                    break
                start += limit
        except CancelledBackup:
            raise
        except Exception as exc:
            should_log = False
            with self.remote_lock:
                if remote_dir not in self.remote_check_errors:
                    self.remote_check_errors.add(remote_dir)
                    should_log = True
                self.remote_dir_cache[remote_dir] = None
            if should_log:
                self.log(f"百度远端目录查询失败，目录内文件将按本地清单或重新上传处理：{remote_dir} | {exc}")
            return None
        with self.remote_lock:
            self.remote_dir_cache[remote_dir] = items
        return items

    def update_remote_file_cache(self, target_path: str, size: int) -> None:
        target_path = normalize_baidu_path(target_path)
        parent = normalize_baidu_path(str(PurePosixPath(target_path).parent))
        with self.remote_lock:
            listing = self.remote_dir_cache.get(parent)
            if listing is None:
                return
            listing[PurePosixPath(target_path).name] = {"path": target_path, "size": int(size), "isdir": 0}

    def upload_remote_file(self, repo_id: str, remote_path: str, target_path: str, expected_size: int, file_key: str) -> None:
        block_md5s = self.compute_remote_block_md5s(self.fresh_download_url(repo_id, remote_path), expected_size)
        parent = str(PurePosixPath(target_path).parent)
        self.ensure_dir(parent)
        precreate = self.baidu_client.precreate(target_path, expected_size, block_md5s, rtype=self.options.rtype)
        uploadid = precreate["uploadid"]
        upload_base = self.baidu_client.locate_upload(target_path, uploadid)
        wanted_parts = normalize_wanted_parts(precreate.get("block_list"), len(block_md5s))
        if len(wanted_parts) < len(block_md5s):
            self.log(f"百度预上传返回待上传分片 {len(wanted_parts)}/{len(block_md5s)}，将续传缺失分片：{target_path}")
        if wanted_parts:
            part_dir = self.file_parts_dir(file_key)
            try:
                for partseq in wanted_parts:
                    self.check_cancelled()
                    part_path = part_dir / f"part-{partseq:06d}.bin"
                    start = partseq * BAIDU_CHUNK_SIZE
                    end = min(expected_size, start + BAIDU_CHUNK_SIZE) - 1
                    self.download_range_to_file(self.fresh_download_url(repo_id, remote_path), part_path, start, end, expected_size)
                    self.upload_part_with_progress(upload_base, target_path, uploadid, partseq, part_path, file_key, expected_size)
                    safe_unlink(part_path)
            except RangeUnsupported as exc:
                self.cleanup_file_parts(file_key)
                reason = f"（{exc}）" if str(exc) else ""
                self.log(f"清华云盘下载地址不支持分片读取{reason}，改为顺序读取并逐片上传：{target_path}")
                self.upload_via_sequential_stream(
                    self.fresh_download_url(repo_id, remote_path),
                    target_path,
                    expected_size,
                    uploadid,
                    upload_base,
                    wanted_parts,
                    file_key,
                )
        self.baidu_client.create_file(target_path, expected_size, block_md5s, uploadid, rtype=self.options.rtype)
        self.cleanup_file_parts(file_key)

    def fresh_download_url(self, repo_id: str, remote_path: str) -> str:
        return self.seafile_client.download_url(repo_id, remote_path)

    def compute_remote_block_md5s(self, download_url: str, expected_size: int) -> list[str]:
        md5s: list[str] = []
        current = hashlib.md5()
        remaining = BAIDU_CHUNK_SIZE
        total_read = 0
        try:
            with self.seafile_client.open(download_url, timeout=1800) as response:
                while True:
                    self.check_cancelled()
                    chunk = response.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    total_read += len(chunk)
                    if self.progress_meter.add_transfer("download", len(chunk)):
                        self.emit_progress()
                    current.update(chunk)
                    remaining -= len(chunk)
                    if remaining == 0:
                        md5s.append(current.hexdigest())
                        current = hashlib.md5()
                        remaining = BAIDU_CHUNK_SIZE
        except urllib.error.HTTPError as exc:
            raise tsinghua_http_error("计算分片 MD5", exc) from exc
        if remaining != BAIDU_CHUNK_SIZE:
            md5s.append(current.hexdigest())
        if total_read != expected_size:
            raise IOError(f"remote file size mismatch while hashing: expected {expected_size}, got {total_read}")
        return md5s

    def download_range_to_file(self, download_url: str, part_path: Path, start: int, end: int, expected_size: int) -> None:
        self.parts_dir.mkdir(parents=True, exist_ok=True)
        headers = {"Range": f"bytes={start}-{end}"}
        try:
            with self.seafile_client.open(download_url, headers=headers, timeout=1800) as response:
                status = getattr(response, "status", 200)
                if status == 200 and (start != 0 or end + 1 < expected_size):
                    raise RangeUnsupported()
                with part_path.open("wb") as handle:
                    written = 0
                    while True:
                        self.check_cancelled()
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        written += len(chunk)
                        if self.progress_meter.add_transfer("download", len(chunk)):
                            self.emit_progress()
        except urllib.error.HTTPError as exc:
            body = http_error_body(exc)
            if exc.code == 403 and "Access token not found" in body:
                raise RangeUnsupported("HTTP 403 Access token not found") from exc
            raise tsinghua_http_error("读取文件分片", exc, body=body) from exc
        expected = end - start + 1
        if written != expected:
            raise IOError(f"range size mismatch: expected {expected}, got {written}")

    def upload_via_sequential_stream(
        self,
        download_url: str,
        target_path: str,
        expected_size: int,
        uploadid: str,
        upload_base: str,
        wanted_parts: list[int],
        file_key: str,
    ) -> None:
        part_dir = self.file_parts_dir(file_key)
        wanted_set = {int(partseq) for partseq in wanted_parts}
        uploaded_parts: set[int] = set()
        partseq = 0
        part_size = 0
        total_read = 0
        part_path = part_dir / f"part-{partseq:06d}.bin"
        part_file = part_path.open("wb") if partseq in wanted_set else None

        def finish_part() -> None:
            nonlocal part_file
            if part_file is None:
                return
            part_file.close()
            part_file = None
            self.upload_part_with_progress(upload_base, target_path, uploadid, partseq, part_path, file_key, expected_size)
            uploaded_parts.add(partseq)
            safe_unlink(part_path)

        try:
            with self.seafile_client.open(download_url, timeout=1800) as response:
                while True:
                    self.check_cancelled()
                    chunk = response.read(min(1024 * 1024, BAIDU_CHUNK_SIZE - part_size))
                    if not chunk:
                        break
                    total_read += len(chunk)
                    if self.progress_meter.add_transfer("download", len(chunk)):
                        self.emit_progress()
                    if part_file is not None:
                        part_file.write(chunk)
                    part_size += len(chunk)
                    if part_size == BAIDU_CHUNK_SIZE:
                        finish_part()
                        partseq += 1
                        part_size = 0
                        part_path = part_dir / f"part-{partseq:06d}.bin"
                        part_file = part_path.open("wb") if partseq in wanted_set else None
        except urllib.error.HTTPError as exc:
            raise tsinghua_http_error("顺序读取完整文件", exc) from exc
        finally:
            if part_file is not None:
                part_file.close()
                part_file = None
        if part_size and partseq in wanted_set:
            self.upload_part_with_progress(upload_base, target_path, uploadid, partseq, part_path, file_key, expected_size)
            uploaded_parts.add(partseq)
            safe_unlink(part_path)
        if total_read != expected_size:
            raise IOError(f"sequential stream size mismatch: expected {expected_size}, got {total_read}")
        missing = wanted_set - uploaded_parts
        if missing:
            raise IOError(f"sequential stream missing uploaded parts: {sorted(missing)}")

    def upload_part_with_progress(
        self,
        upload_base: str,
        target_path: str,
        uploadid: str,
        partseq: int,
        part_path: Path,
        file_key: str,
        expected_size: int,
    ) -> None:
        part_size = part_path.stat().st_size
        started = time.monotonic()
        self.baidu_client.upload_part(upload_base, target_path, uploadid, partseq, part_path)
        elapsed = time.monotonic() - started
        should_emit = self.progress_meter.add_transfer("upload", part_size, elapsed=elapsed)
        progress = min((partseq + 1) * BAIDU_CHUNK_SIZE, expected_size or part_size)
        should_emit = self.progress_meter.set_active_progress(file_key, progress) or should_emit
        if should_emit:
            self.emit_progress()

    def ensure_dir(self, remote_dir: str) -> None:
        remote_dir = normalize_baidu_path(remote_dir)
        with self.dir_lock:
            if remote_dir in self.created_dirs:
                return
            parts = [part for part in PurePosixPath(remote_dir).parts if part != "/"]
            current = ""
            for part in parts:
                current = baidu_join(current or "/", part)
                if current == "/apps":
                    self.created_dirs.add(current)
                    continue
                if current not in self.created_dirs:
                    self.baidu_client.create_dir(current)
                    self.created_dirs.add(current)

    def target_base_path(self) -> str:
        components = ["apps", self.options.app_dir_name.strip().strip("/")]
        root = self.options.target_root.strip().strip("/")
        if root:
            components.append(root)
        return "/" + "/".join(components)

    def repo_target_path(self, repo: dict) -> str:
        return baidu_join(self.target_base_path(), category_for(repo), repo_component(repo))

    def cleanup_parts(self) -> None:
        if not self.parts_dir.exists():
            return
        for path in self.parts_dir.iterdir():
            if path.is_file():
                safe_unlink(path)

    def file_parts_dir(self, file_key: str) -> Path:
        digest = hashlib.sha1(file_key.encode("utf-8")).hexdigest()
        part_dir = self.parts_dir / digest
        part_dir.mkdir(parents=True, exist_ok=True)
        return part_dir

    def cleanup_file_parts(self, file_key: str) -> None:
        part_dir = self.file_parts_dir(file_key)
        shutil.rmtree(part_dir, ignore_errors=True)

    def write_file_row(
        self,
        writer: csv.DictWriter,
        handle,
        repo: dict,
        remote_path: str,
        target_path: str,
        size: int,
        mtime: int,
        status: str,
    ) -> None:
        with self.writer_lock:
            writer.writerow(
                {
                    "time": now_text(),
                    "repo_id": repo["id"],
                    "repo_name": repo["name"],
                    "remote_path": remote_path,
                    "baidu_path": target_path,
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


def repo_component(repo: dict) -> str:
    category = category_for(repo)
    owner = repo["owner_names"][0] if repo.get("owner_names") else ""
    group = repo["group_names"][0] if repo.get("group_names") else ""
    if category == "群组共享内容" and group:
        preferred = f"{group} - {repo['name']} [{repo['id'][:8]}]"
    elif category == "共享给我的":
        prefix = owner or (repo["share_from_names"][0] if repo.get("share_from_names") else "")
        preferred = f"{prefix} - {repo['name']} [{repo['id'][:8]}]" if prefix else f"{repo['name']} [{repo['id'][:8]}]"
    else:
        preferred = f"{repo['name']} [{repo['id'][:8]}]"
    return remote_component(preferred, fallback=repo["id"])


def remote_component(name: str | None, fallback: str = "unnamed") -> str:
    return shorten_component(safe_name(name, fallback=fallback))


def baidu_join(parent: str, *children: str) -> str:
    path = PurePosixPath(normalize_baidu_path(parent))
    for child in children:
        if not child:
            continue
        path = path / child.strip("/")
    return normalize_baidu_path(str(path))


def normalize_baidu_path(path: str) -> str:
    path = "/" + str(path or "").strip("/")
    return path.replace("//", "/")


def normalize_wanted_parts(raw_parts, part_count: int) -> list[int]:
    if not isinstance(raw_parts, list):
        return list(range(part_count))
    wanted: set[int] = set()
    for item in raw_parts:
        try:
            partseq = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= partseq < part_count:
            wanted.add(partseq)
    return sorted(wanted)


def safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def http_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def tsinghua_http_error(stage: str, exc: urllib.error.HTTPError, body: str | None = None) -> IOError:
    if body is None:
        body = http_error_body(exc)
    if len(body) > 800:
        body = body[:800] + "...[truncated]"
    detail = f" | body={body}" if body else ""
    return IOError(f"清华云盘下载失败（{stage}）：HTTP {exc.code} {exc.reason}{detail}")
