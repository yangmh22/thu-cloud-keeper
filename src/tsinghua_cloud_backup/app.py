from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .core import ALL_CATEGORIES, BackupOptions, BackupRunner, SeafileClient, category_for, discover_repositories, human_size


CATEGORIES = ALL_CATEGORIES


class BackupApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("清华云盘自助备份")
        self.geometry("1040x720")
        self.minsize(920, 620)

        self.event_queue: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.repositories: list[dict] = []
        self.category_vars = {category: tk.BooleanVar(value=True) for category in CATEGORIES}

        self.token_var = tk.StringVar()
        self.destination_var = tk.StringVar(value=str(Path.home() / "Desktop" / "清华云盘备份"))
        self.workers_var = tk.IntVar(value=4)
        self.status_var = tk.StringVar(value="请输入 token，然后点击“连接并读取资料库”。")
        self.progress_var = tk.DoubleVar(value=0)
        self.summary_vars = {
            "account": tk.StringVar(value="账号：未连接"),
            "all": tk.StringVar(value="全部：-"),
            "我的资料库": tk.StringVar(value="我的资料库：-"),
            "群组共享内容": tk.StringVar(value="群组共享内容：-"),
            "共享给我的": tk.StringVar(value="共享给我的：-"),
            "selected": tk.StringVar(value="当前选中：-"),
        }

        self.create_widgets()
        self.after(100, self.process_events)

    def create_widgets(self) -> None:
        root = ttk.Frame(self, padding=14)
        root.pack(fill=tk.BOTH, expand=True)

        top = ttk.LabelFrame(root, text="连接", padding=10)
        top.pack(fill=tk.X)
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="个人 Token").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        token_entry = ttk.Entry(top, textvariable=self.token_var, show="*", width=80)
        token_entry.grid(row=0, column=1, sticky=tk.EW, pady=4)
        ttk.Button(top, text="连接并读取资料库", command=self.load_repositories).grid(row=0, column=2, padx=(8, 0), pady=4)

        ttk.Label(top, text="下载目录").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        ttk.Entry(top, textvariable=self.destination_var).grid(row=1, column=1, sticky=tk.EW, pady=4)
        ttk.Button(top, text="选择...", command=self.choose_destination).grid(row=1, column=2, padx=(8, 0), pady=4)

        summary = ttk.LabelFrame(root, text="云盘概览", padding=10)
        summary.pack(fill=tk.X, pady=(10, 0))
        summary.columnconfigure(1, weight=1)
        ttk.Label(summary, textvariable=self.summary_vars["account"]).grid(row=0, column=0, sticky=tk.W, padx=(0, 18), pady=2)
        ttk.Label(summary, textvariable=self.summary_vars["all"]).grid(row=0, column=1, sticky=tk.W, pady=2)
        ttk.Label(summary, textvariable=self.summary_vars["我的资料库"]).grid(row=1, column=0, sticky=tk.W, padx=(0, 18), pady=2)
        ttk.Label(summary, textvariable=self.summary_vars["群组共享内容"]).grid(row=1, column=1, sticky=tk.W, pady=2)
        ttk.Label(summary, textvariable=self.summary_vars["共享给我的"]).grid(row=1, column=2, sticky=tk.W, pady=2)
        ttk.Label(summary, textvariable=self.summary_vars["selected"]).grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=(6, 0))

        options = ttk.LabelFrame(root, text="备份范围", padding=10)
        options.pack(fill=tk.X, pady=(10, 0))
        for index, category in enumerate(CATEGORIES):
            ttk.Checkbutton(options, text=category, variable=self.category_vars[category], command=self.refresh_repository_view).grid(row=0, column=index, sticky=tk.W, padx=(0, 24))
        ttk.Label(options, text="并发下载").grid(row=0, column=3, sticky=tk.E, padx=(20, 6))
        ttk.Spinbox(options, from_=1, to=16, textvariable=self.workers_var, width=6).grid(row=0, column=4, sticky=tk.W)
        ttk.Button(options, text="开始下载", command=self.start_backup).grid(row=0, column=5, padx=(20, 6))
        ttk.Button(options, text="停止", command=self.stop_backup).grid(row=0, column=6)

        main = ttk.PanedWindow(root, orient=tk.VERTICAL)
        main.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        repo_frame = ttk.LabelFrame(main, text="资料库", padding=8)
        repo_frame.columnconfigure(0, weight=1)
        repo_frame.rowconfigure(0, weight=1)
        columns = ("category", "name", "size", "owner", "permission")
        self.repo_tree = ttk.Treeview(repo_frame, columns=columns, show="headings", height=12)
        self.repo_tree.heading("category", text="分类")
        self.repo_tree.heading("name", text="名称")
        self.repo_tree.heading("size", text="大小")
        self.repo_tree.heading("owner", text="所有者/群组")
        self.repo_tree.heading("permission", text="权限")
        self.repo_tree.column("category", width=120, anchor=tk.W)
        self.repo_tree.column("name", width=360, anchor=tk.W)
        self.repo_tree.column("size", width=100, anchor=tk.E)
        self.repo_tree.column("owner", width=260, anchor=tk.W)
        self.repo_tree.column("permission", width=80, anchor=tk.CENTER)
        self.repo_tree.grid(row=0, column=0, sticky=tk.NSEW)
        repo_scroll = ttk.Scrollbar(repo_frame, orient=tk.VERTICAL, command=self.repo_tree.yview)
        repo_scroll.grid(row=0, column=1, sticky=tk.NS)
        self.repo_tree.configure(yscrollcommand=repo_scroll.set)
        main.add(repo_frame, weight=3)

        log_frame = ttk.LabelFrame(main, text="日志", padding=8)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=12, wrap=tk.WORD, state=tk.DISABLED)
        self.log_text.grid(row=0, column=0, sticky=tk.NSEW)
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky=tk.NS)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        main.add(log_frame, weight=2)

        bottom = ttk.Frame(root)
        bottom.pack(fill=tk.X, pady=(10, 0))
        ttk.Progressbar(bottom, variable=self.progress_var, maximum=100).pack(fill=tk.X)
        ttk.Label(bottom, textvariable=self.status_var).pack(anchor=tk.W, pady=(5, 0))

    def choose_destination(self) -> None:
        path = filedialog.askdirectory(title="选择下载目录")
        if path:
            self.destination_var.set(path)

    def selected_categories(self) -> set[str]:
        return {category for category, var in self.category_vars.items() if var.get()}

    def load_repositories(self) -> None:
        token = self.token_var.get().strip()
        if not token:
            messagebox.showwarning("缺少 Token", "请先输入清华云盘个人 Token。")
            return
        self.run_background(self._load_repositories_worker, token)

    def _load_repositories_worker(self, token: str) -> None:
        try:
            self.event_queue.put({"kind": "status", "message": "正在连接清华云盘..."})
            client = SeafileClient(token)
            info = client.account_info()
            repos = discover_repositories(client)
            self.event_queue.put({"kind": "repos", "repos": repos, "account": info})
        except Exception as exc:
            self.event_queue.put({"kind": "error", "message": f"读取资料库失败：{exc}"})

    def refresh_repository_view(self) -> None:
        for item in self.repo_tree.get_children():
            self.repo_tree.delete(item)
        categories = self.selected_categories()
        selected = [repo for repo in self.repositories if category_for(repo) in categories]
        for repo in selected:
            owner = ", ".join(repo.get("group_names") or repo.get("owner_names") or repo.get("share_from_names") or [])
            self.repo_tree.insert(
                "",
                tk.END,
                values=(
                    category_for(repo),
                    repo["name"],
                    human_size(repo.get("size")),
                    owner,
                    ",".join(repo.get("permissions") or []),
                ),
            )
        total_size = sum(int(repo.get("size") or 0) for repo in selected)
        self.update_summary()
        self.status_var.set(f"已选 {len(selected)} 个资料库，声明大小 {human_size(total_size)}。")

    def update_summary(self, account: dict | None = None) -> None:
        if account:
            total_quota = int(account.get("total") or 0)
            usage = int(account.get("usage") or 0)
            account_name = account.get("name") or account.get("email") or "未知账号"
            quota_text = f"{human_size(usage)} / {human_size(total_quota)}" if total_quota else human_size(usage)
            self.summary_vars["account"].set(f"账号：{account_name}，空间用量 {quota_text}")
        total_size = sum(int(repo.get("size") or 0) for repo in self.repositories)
        self.summary_vars["all"].set(f"全部资料库：{len(self.repositories)} 个，声明大小 {human_size(total_size)}")
        for category in CATEGORIES:
            repos = [repo for repo in self.repositories if category_for(repo) == category]
            size = sum(int(repo.get("size") or 0) for repo in repos)
            self.summary_vars[category].set(f"{category}：{len(repos)} 个，{human_size(size)}")
        selected = [repo for repo in self.repositories if category_for(repo) in self.selected_categories()]
        selected_size = sum(int(repo.get("size") or 0) for repo in selected)
        self.summary_vars["selected"].set(f"当前选中：{len(selected)} 个，{human_size(selected_size)}")

    def start_backup(self) -> None:
        token = self.token_var.get().strip()
        if not token:
            messagebox.showwarning("缺少 Token", "请先输入清华云盘个人 Token。")
            return
        categories = self.selected_categories()
        if not categories:
            messagebox.showwarning("没有选择范围", "请至少勾选一个资料库分类。")
            return
        destination = Path(self.destination_var.get().strip())
        if not destination:
            messagebox.showwarning("缺少下载目录", "请选择下载目录。")
            return
        workers = max(1, min(int(self.workers_var.get() or 1), 16))
        self.cancel_event.clear()
        self.clear_log()
        self.progress_var.set(0)
        self.run_background(self._backup_worker, token, destination, categories, workers)

    def _backup_worker(self, token: str, destination: Path, categories: set[str], workers: int) -> None:
        try:
            client = SeafileClient(token)
            options = BackupOptions(destination=destination, categories=categories, workers=workers)
            runner = BackupRunner(client, options, event_callback=self.event_queue.put, cancel_event=self.cancel_event)
            runner.run()
        except Exception as exc:
            if self.cancel_event.is_set():
                self.event_queue.put({"kind": "status", "message": "已停止。"})
            else:
                self.event_queue.put({"kind": "error", "message": f"备份失败：{exc}"})

    def stop_backup(self) -> None:
        self.cancel_event.set()
        self.status_var.set("正在停止，当前文件完成或中断后会退出...")

    def run_background(self, target, *args) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("任务运行中", "当前已有任务在运行，请稍后。")
            return
        self.worker_thread = threading.Thread(target=target, args=args, daemon=True)
        self.worker_thread.start()

    def process_events(self) -> None:
        try:
            while True:
                event = self.event_queue.get_nowait()
                self.handle_event(event)
        except queue.Empty:
            pass
        self.after(100, self.process_events)

    def handle_event(self, event: dict) -> None:
        kind = event.get("kind")
        if kind == "repos":
            self.repositories = event["repos"]
            account = event.get("account") or {}
            self.log(f"登录账号：{account.get('name') or account.get('email') or '未知'}")
            self.update_summary(account)
            self.refresh_repository_view()
        elif kind == "log":
            self.log(event.get("message", ""))
        elif kind == "progress":
            stats = event.get("stats") or {}
            total = stats.get("repositories_total") or 0
            done = stats.get("repositories_done") or 0
            if total:
                self.progress_var.set(done / total * 100)
            self.status_var.set(
                f"资料库 {done}/{total} | 文件 {stats.get('files_seen', 0)} | "
                f"下载 {stats.get('downloaded', 0)} | 跳过 {stats.get('skipped', 0)} | "
                f"失败 {stats.get('failed', 0)} | 新增 {stats.get('bytes_downloaded_text', '0 B')}"
            )
        elif kind == "done":
            self.progress_var.set(100)
            self.status_var.set("备份完成。")
        elif kind == "status":
            self.status_var.set(event.get("message", ""))
            self.log(event.get("message", ""))
        elif kind == "error":
            self.status_var.set(event.get("message", "发生错误。"))
            self.log(event.get("message", "发生错误。"))
            messagebox.showerror("错误", event.get("message", "发生错误。"))

    def log(self, message: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def clear_log(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)


def main() -> None:
    app = BackupApp()
    app.mainloop()


if __name__ == "__main__":
    main()
