from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from pathlib import Path

from .core import ALL_CATEGORIES, BackupOptions, BackupRunner, SeafileClient, category_for, discover_repositories, human_size
from .credentials import DEFAULT_CREDENTIAL_TARGET, DEFAULT_CREDENTIAL_USER, CredentialError, delete_token, read_token, write_token


def load_token(env_name: str, credential_target: str) -> str:
    token = os.environ.get(env_name, "").strip()
    if token:
        return token
    stored = read_token(credential_target)
    if stored:
        return stored.strip()
    raise SystemExit(
        "No token found. Set the TSINGHUA_CLOUD_TOKEN environment variable "
        "or run `python -m tsinghua_cloud_backup.cli store-token` first."
    )


class ConsoleReporter:
    def __init__(self, quiet: bool = False, progress_interval: int = 60):
        self.quiet = quiet
        self.progress_interval = progress_interval
        self.last_progress = 0.0

    def __call__(self, event: dict) -> None:
        if self.quiet:
            return
        kind = event.get("kind")
        if kind in {"log", "status", "error"}:
            print(f"[{event.get('time')}] {event.get('message', '')}", flush=True)
        elif kind == "progress":
            now = time.time()
            if now - self.last_progress >= self.progress_interval:
                self.last_progress = now
                stats = event.get("stats") or {}
                print(
                    f"[{event.get('time')}] repos {stats.get('repositories_done', 0)}/"
                    f"{stats.get('repositories_total', 0)} | files {stats.get('files_seen', 0)} | "
                    f"downloaded {stats.get('downloaded', 0)} | skipped {stats.get('skipped', 0)} | "
                    f"would_download {stats.get('would_download', 0)} | failed {stats.get('failed', 0)} | "
                    f"bytes {stats.get('bytes_downloaded_text', '0 B')}",
                    flush=True,
                )
        elif kind == "done":
            stats = event.get("stats") or {}
            print(
                f"[{event.get('time')}] done | downloaded={stats.get('downloaded', 0)} | "
                f"skipped={stats.get('skipped', 0)} | would_download={stats.get('would_download', 0)} | "
                f"failed={stats.get('failed', 0)}",
                flush=True,
            )


def parse_categories(values: list[str] | None, all_categories: bool) -> set[str]:
    if all_categories or not values:
        return set(ALL_CATEGORIES)
    categories: set[str] = set()
    for value in values:
        for part in value.split(","):
            category = part.strip()
            if category:
                categories.add(category)
    unknown = categories.difference(ALL_CATEGORIES)
    if unknown:
        raise SystemExit(f"Unknown category: {', '.join(sorted(unknown))}")
    return categories


def cmd_sync(args: argparse.Namespace) -> int:
    token = load_token(args.token_env, args.credential_target)
    categories = parse_categories(args.category, args.all_categories)
    client = SeafileClient(token)
    options = BackupOptions(
        destination=Path(args.destination),
        categories=categories,
        workers=max(1, min(args.workers, 16)),
        overwrite_same_size=args.overwrite_same_size,
        dry_run=args.dry_run,
    )
    reporter = ConsoleReporter(quiet=args.quiet, progress_interval=args.progress_interval)
    runner = BackupRunner(client, options, event_callback=reporter)
    stats = runner.run()
    return 1 if stats.failed else 0


def cmd_check(args: argparse.Namespace) -> int:
    token = load_token(args.token_env, args.credential_target)
    client = SeafileClient(token)
    account = client.account_info()
    repos = discover_repositories(client)
    print(f"Account: {account.get('name') or account.get('email') or 'unknown'}")
    usage = int(account.get("usage") or 0)
    total = int(account.get("total") or 0)
    if total:
        print(f"Quota: {human_size(usage)} / {human_size(total)}")
    else:
        print(f"Usage: {human_size(usage)}")
    print(f"Repositories: {len(repos)}")
    for category in ALL_CATEGORIES:
        selected = [repo for repo in repos if category_for(repo) == category]
        size = sum(int(repo.get("size") or 0) for repo in selected)
        print(f"- {category}: {len(selected)} repos, {human_size(size)}")
    return 0


def cmd_store_token(args: argparse.Namespace) -> int:
    token = os.environ.get(args.token_env, "").strip()
    if not token and not sys.stdin.isatty():
        token = sys.stdin.read().strip()
    if not token:
        token = getpass.getpass("Tsinghua Cloud token: ").strip()
    write_token(token, args.credential_target, args.username)
    print(f"Stored token in Windows Credential Manager target: {args.credential_target}")
    return 0


def cmd_delete_token(args: argparse.Namespace) -> int:
    deleted = delete_token(args.credential_target)
    print("Deleted stored token." if deleted else "No stored token found.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="thu-cloud-keeper", description="THU Cloud Keeper command line tools.")
    parser.add_argument("--credential-target", default=DEFAULT_CREDENTIAL_TARGET)
    parser.add_argument("--token-env", default="TSINGHUA_CLOUD_TOKEN")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync = subparsers.add_parser("sync", help="Run an incremental backup/sync.")
    sync.add_argument("--destination", required=True)
    sync.add_argument("--workers", type=int, default=4)
    sync.add_argument("--category", action="append", help="Category to include. Can be repeated or comma-separated.")
    sync.add_argument("--all-categories", action="store_true", help="Include all categories.")
    sync.add_argument("--dry-run", action="store_true", help="Scan and compare without downloading files.")
    sync.add_argument("--overwrite-same-size", action="store_true", help="Download even when local file appears current.")
    sync.add_argument("--quiet", action="store_true")
    sync.add_argument("--progress-interval", type=int, default=60)
    sync.set_defaults(func=cmd_sync)

    check = subparsers.add_parser("check", help="Check token and print cloud library summary.")
    check.set_defaults(func=cmd_check)

    store = subparsers.add_parser("store-token", help="Store token in Windows Credential Manager.")
    store.add_argument("--username", default=DEFAULT_CREDENTIAL_USER)
    store.set_defaults(func=cmd_store_token)

    delete = subparsers.add_parser("delete-token", help="Delete token from Windows Credential Manager.")
    delete.set_defaults(func=cmd_delete_token)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CredentialError as exc:
        print(f"Credential error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
