from __future__ import annotations

import queue
import re
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .baidu import BaiduAuthClient, BaiduNetdiskClient, baidu_auth_url
from .core import BackupOptions, BackupRunner, SeafileClient, category_for, discover_repositories, human_size
from .migration import MigrationOptions, MigrationRunner


CATEGORIES = ("我的资料库", "群组共享内容", "共享给我的")
TOKEN_PROFILE_URL = "https://cloud.tsinghua.edu.cn/profile/"
TOKEN_CANDIDATE_RE = re.compile(r"[A-Za-z0-9._-]{20,}")
APP_BG = "#f6f8fb"
PANEL_BG = "#ffffff"
TEXT_MUTED = "#667085"


def extract_token(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) <= 200 and not any(char.isspace() for char in text):
        return text
    candidates = TOKEN_CANDIDATE_RE.findall(text)
    return max(candidates, key=len) if candidates else ""


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


class BackupApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("清华云盘自助备份")
        self.geometry("1180x820")
        self.minsize(1040, 720)
        self.configure(background=APP_BG)
        self.configure_style()

        self.event_queue: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.repositories: list[dict] = []
        self.category_vars = {category: tk.BooleanVar(value=True) for category in CATEGORIES}
        self.baidu_device_data: dict | None = None

        self.token_var = tk.StringVar()
        self.baidu_app_key_var = tk.StringVar()
        self.baidu_app_secret_var = tk.StringVar()
        self.baidu_access_token_var = tk.StringVar()
        self.baidu_app_dir_var = tk.StringVar()
        self.baidu_root_var = tk.StringVar(value="清华云盘迁移")
        self.baidu_temp_dir_var = tk.StringVar(value=str(Path.home() / "Desktop" / "清华云盘迁移临时"))
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
        self.metric_vars = {
            "task": tk.StringVar(value="-"),
            "progress": tk.StringVar(value="-"),
            "download_rate": tk.StringVar(value="-"),
            "upload_rate": tk.StringVar(value="-"),
            "eta": tk.StringVar(value="-"),
            "transfer": tk.StringVar(value="-"),
        }

        self.create_widgets()
        self.after(100, self.process_events)

    def configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        base_font = ("Microsoft YaHei UI", 10)
        title_font = ("Microsoft YaHei UI", 10, "bold")
        metric_font = ("Microsoft YaHei UI", 11, "bold")
        style.configure(".", font=base_font)
        style.configure("App.TFrame", background=APP_BG)
        style.configure("Panel.TFrame", background=PANEL_BG)
        style.configure("TLabel", background=PANEL_BG, foreground="#111827")
        style.configure("TLabelFrame", background=PANEL_BG, bordercolor="#d0d5dd", relief=tk.SOLID)
        style.configure("TLabelFrame.Label", background=APP_BG, foreground="#344054", font=title_font)
        style.configure("TButton", padding=(10, 5))
        style.configure("Accent.TButton", background="#2563eb", foreground="#ffffff", padding=(12, 6))
        style.map("Accent.TButton", background=[("active", "#1d4ed8"), ("pressed", "#1e40af")])
        style.configure("Danger.TButton", background="#b42318", foreground="#ffffff", padding=(12, 6))
        style.map("Danger.TButton", background=[("active", "#912018"), ("pressed", "#7a271a")])
        style.configure("MetricTitle.TLabel", foreground=TEXT_MUTED, font=("Microsoft YaHei UI", 9), background=PANEL_BG)
        style.configure("MetricValue.TLabel", foreground="#111827", font=metric_font, background=PANEL_BG)
        style.configure("Treeview", rowheight=26, fieldbackground="#ffffff", background="#ffffff")
        style.configure("Treeview.Heading", font=title_font, background="#eef2f7", foreground="#344054")
        style.configure("Horizontal.TProgressbar", troughcolor="#e5e7eb", background="#2563eb", thickness=12)

    def create_widgets(self) -> None:
        root = ttk.Frame(self, padding=16, style="App.TFrame")
        root.pack(fill=tk.BOTH, expand=True)

        top = ttk.LabelFrame(root, text="连接", padding=10)
        top.pack(fill=tk.X)
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="个人 Token").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        token_entry = ttk.Entry(top, textvariable=self.token_var, show="*", width=80)
        token_entry.grid(row=0, column=1, sticky=tk.EW, pady=4)
        token_actions = ttk.Frame(top)
        token_actions.grid(row=0, column=2, sticky=tk.E, padx=(8, 0), pady=4)
        ttk.Button(token_actions, text="打开 Token 页面", command=self.open_token_page).pack(side=tk.LEFT)
        ttk.Button(token_actions, text="粘贴并连接", command=self.paste_and_connect, style="Accent.TButton").pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(token_actions, text="连接并读取资料库", command=self.load_repositories).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(top, text="下载目录").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        ttk.Entry(top, textvariable=self.destination_var).grid(row=1, column=1, sticky=tk.EW, pady=4)
        ttk.Button(top, text="选择...", command=self.choose_destination).grid(row=1, column=2, padx=(8, 0), pady=4)

        baidu = ttk.LabelFrame(root, text="百度网盘迁移目标", padding=10)
        baidu.pack(fill=tk.X, pady=(10, 0))
        baidu.columnconfigure(1, weight=1)
        baidu.columnconfigure(3, weight=1)

        ttk.Label(baidu, text="App Key").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        ttk.Entry(baidu, textvariable=self.baidu_app_key_var, width=34).grid(row=0, column=1, sticky=tk.EW, pady=4)
        ttk.Label(baidu, text="Secret Key").grid(row=0, column=2, sticky=tk.W, padx=(12, 8), pady=4)
        ttk.Entry(baidu, textvariable=self.baidu_app_secret_var, show="*", width=34).grid(row=0, column=3, sticky=tk.EW, pady=4)
        baidu_auth_actions = ttk.Frame(baidu)
        baidu_auth_actions.grid(row=0, column=4, sticky=tk.E, padx=(8, 0), pady=4)
        ttk.Button(baidu_auth_actions, text="获取百度授权", command=self.request_baidu_device_code).pack(side=tk.LEFT)
        ttk.Button(baidu_auth_actions, text="完成授权", command=self.finish_baidu_device_auth).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(baidu, text="Access Token").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        ttk.Entry(baidu, textvariable=self.baidu_access_token_var, show="*", width=80).grid(row=1, column=1, columnspan=3, sticky=tk.EW, pady=4)
        baidu_token_actions = ttk.Frame(baidu)
        baidu_token_actions.grid(row=1, column=4, sticky=tk.E, padx=(8, 0), pady=4)
        ttk.Button(baidu_token_actions, text="粘贴百度 Token", command=self.paste_baidu_access_token).pack(side=tk.LEFT)
        ttk.Button(baidu_token_actions, text="验证百度账号", command=self.validate_baidu_account).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(baidu, text="应用产品名称").grid(row=2, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        ttk.Entry(baidu, textvariable=self.baidu_app_dir_var, width=34).grid(row=2, column=1, sticky=tk.EW, pady=4)
        ttk.Label(baidu, text="迁移根目录").grid(row=2, column=2, sticky=tk.W, padx=(12, 8), pady=4)
        ttk.Entry(baidu, textvariable=self.baidu_root_var, width=34).grid(row=2, column=3, sticky=tk.EW, pady=4)
        baidu_temp_actions = ttk.Frame(baidu)
        baidu_temp_actions.grid(row=2, column=4, sticky=tk.E, padx=(8, 0), pady=4)
        ttk.Button(baidu_temp_actions, text="临时目录...", command=self.choose_baidu_temp_dir).pack(side=tk.LEFT)

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
        ttk.Button(options, text="开始下载", command=self.start_backup, style="Accent.TButton").grid(row=0, column=5, padx=(20, 6))
        ttk.Button(options, text="开始迁移到百度网盘", command=self.start_migration, style="Accent.TButton").grid(row=0, column=6, padx=(6, 6))
        ttk.Button(options, text="停止", command=self.stop_backup, style="Danger.TButton").grid(row=0, column=7)

        metrics = ttk.LabelFrame(root, text="运行状态", padding=10)
        metrics.pack(fill=tk.X, pady=(10, 0))
        for index in range(6):
            metrics.columnconfigure(index, weight=1)
        self.add_metric(metrics, 0, "当前资料库", self.metric_vars["task"])
        self.add_metric(metrics, 1, "完成进度", self.metric_vars["progress"])
        self.add_metric(metrics, 2, "下载速率", self.metric_vars["download_rate"])
        self.add_metric(metrics, 3, "上传速率", self.metric_vars["upload_rate"])
        self.add_metric(metrics, 4, "剩余时间", self.metric_vars["eta"])
        self.add_metric(metrics, 5, "本次传输", self.metric_vars["transfer"])

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
        self.log_text = tk.Text(
            log_frame,
            height=12,
            wrap=tk.WORD,
            state=tk.DISABLED,
            background="#0f172a",
            foreground="#e5e7eb",
            insertbackground="#e5e7eb",
            relief=tk.FLAT,
            padx=10,
            pady=8,
        )
        self.log_text.grid(row=0, column=0, sticky=tk.NSEW)
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky=tk.NS)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        main.add(log_frame, weight=2)

        bottom = ttk.Frame(root)
        bottom.pack(fill=tk.X, pady=(10, 0))
        ttk.Progressbar(bottom, variable=self.progress_var, maximum=100).pack(fill=tk.X)
        ttk.Label(bottom, textvariable=self.status_var, background=APP_BG, foreground="#344054").pack(anchor=tk.W, pady=(5, 0))

    def add_metric(self, parent, column: int, title: str, variable: tk.StringVar) -> None:
        frame = ttk.Frame(parent, style="Panel.TFrame")
        frame.grid(row=0, column=column, sticky=tk.EW, padx=(0 if column == 0 else 12, 0))
        ttk.Label(frame, text=title, style="MetricTitle.TLabel").pack(anchor=tk.W)
        ttk.Label(frame, textvariable=variable, style="MetricValue.TLabel").pack(anchor=tk.W, pady=(2, 0))

    def choose_destination(self) -> None:
        path = filedialog.askdirectory(title="选择下载目录")
        if path:
            self.destination_var.set(path)

    def choose_baidu_temp_dir(self) -> None:
        path = filedialog.askdirectory(title="选择百度迁移临时目录")
        if path:
            self.baidu_temp_dir_var.set(path)

    def selected_categories(self) -> set[str]:
        return {category for category, var in self.category_vars.items() if var.get()}

    def open_token_page(self) -> None:
        try:
            opened = webbrowser.open(TOKEN_PROFILE_URL)
        except Exception as exc:
            messagebox.showerror("无法打开浏览器", f"打开 Token 页面失败：{exc}")
            return
        if not opened:
            messagebox.showerror("无法打开浏览器", f"请手动打开：{TOKEN_PROFILE_URL}")
            return
        message = "已打开清华云盘个人设置页面。登录后复制个人 Token，再点击“粘贴并连接”。"
        self.status_var.set(message)
        self.log(message)

    def paste_and_connect(self) -> None:
        try:
            clipboard_text = self.clipboard_get()
        except tk.TclError:
            messagebox.showwarning("剪贴板为空", "请先从清华云盘个人设置页面复制个人 Token。")
            return
        token = extract_token(clipboard_text)
        if not token:
            messagebox.showwarning("未识别 Token", "剪贴板中没有识别到个人 Token，请重新复制后再试。")
            return
        self.token_var.set(token)
        self.run_background(self._validate_and_load_repositories_worker, token)

    def _validate_and_load_repositories_worker(self, token: str) -> None:
        try:
            self.event_queue.put({"kind": "status", "message": "正在验证 Token..."})
            client = SeafileClient(token)
            info = client.account_info()
            self.event_queue.put({"kind": "status", "message": "Token 验证通过，正在读取资料库..."})
            repos = discover_repositories(client)
            self.event_queue.put({"kind": "repos", "repos": repos, "account": info})
        except Exception as exc:
            self.event_queue.put({"kind": "error", "message": f"Token 验证或读取资料库失败：{exc}"})

    def request_baidu_device_code(self) -> None:
        app_key = self.baidu_app_key_var.get().strip()
        if not app_key:
            messagebox.showwarning("缺少 App Key", "请先填写百度网盘开放平台应用的 App Key。")
            return
        self.run_background(self._request_baidu_device_code_worker, app_key)

    def _request_baidu_device_code_worker(self, app_key: str) -> None:
        try:
            self.event_queue.put({"kind": "status", "message": "正在请求百度授权用户码..."})
            data = BaiduAuthClient().get_device_code(app_key)
            self.event_queue.put({"kind": "baidu_device_code", "data": data})
        except Exception as exc:
            self.event_queue.put({"kind": "error", "message": f"获取百度授权用户码失败：{exc}"})

    def finish_baidu_device_auth(self) -> None:
        if not self.baidu_device_data:
            messagebox.showwarning("尚未获取授权", "请先点击“获取百度授权”。")
            return
        app_key = self.baidu_app_key_var.get().strip()
        app_secret = self.baidu_app_secret_var.get().strip()
        if not app_key or not app_secret:
            messagebox.showwarning("缺少应用密钥", "请填写百度网盘开放平台应用的 App Key 和 Secret Key。")
            return
        self.run_background(self._finish_baidu_device_auth_worker, app_key, app_secret, dict(self.baidu_device_data))

    def _finish_baidu_device_auth_worker(self, app_key: str, app_secret: str, device_data: dict) -> None:
        try:
            self.event_queue.put({"kind": "status", "message": "等待百度授权完成并换取 Access Token..."})
            token_data = BaiduAuthClient().poll_device_token(
                app_key,
                app_secret,
                device_data["device_code"],
                interval=int(device_data.get("interval") or 5),
                expires_in=int(device_data.get("expires_in") or 300),
            )
            access_token = token_data["access_token"]
            info, quota = self._load_baidu_account(access_token)
            self.event_queue.put({"kind": "baidu_account", "access_token": access_token, "account": info, "quota": quota})
        except Exception as exc:
            self.event_queue.put({"kind": "error", "message": f"百度授权失败：{exc}"})

    def paste_baidu_access_token(self) -> None:
        try:
            clipboard_text = self.clipboard_get()
        except tk.TclError:
            messagebox.showwarning("剪贴板为空", "请先复制百度网盘 Access Token。")
            return
        token = extract_token(clipboard_text)
        if not token:
            messagebox.showwarning("未识别 Token", "剪贴板中没有识别到百度网盘 Access Token。")
            return
        self.baidu_access_token_var.set(token)
        self.validate_baidu_account()

    def validate_baidu_account(self) -> None:
        access_token = self.baidu_access_token_var.get().strip()
        if not access_token:
            messagebox.showwarning("缺少百度 Token", "请先完成百度授权或粘贴百度网盘 Access Token。")
            return
        self.run_background(self._validate_baidu_account_worker, access_token)

    def _validate_baidu_account_worker(self, access_token: str) -> None:
        try:
            self.event_queue.put({"kind": "status", "message": "正在验证百度网盘账号..."})
            info, quota = self._load_baidu_account(access_token)
            self.event_queue.put({"kind": "baidu_account", "access_token": access_token, "account": info, "quota": quota})
        except Exception as exc:
            self.event_queue.put({"kind": "error", "message": f"百度网盘账号验证失败：{exc}"})

    def _load_baidu_account(self, access_token: str) -> tuple[dict, dict]:
        client = BaiduNetdiskClient(access_token)
        info = client.user_info()
        quota = client.quota()
        return info, quota

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

    def reset_runtime_metrics(self) -> None:
        self.progress_var.set(0)
        for variable in self.metric_vars.values():
            variable.set("-")

    def update_runtime_metrics(self, stats: dict, mode: str | None = None) -> tuple[float, str]:
        mode = mode or "backup"
        total_bytes = int(stats.get("bytes_total") or 0)
        completed_bytes = int(stats.get("bytes_completed") or 0)
        repo_total = int(stats.get("repositories_total") or 0)
        repo_done = int(stats.get("repositories_done") or 0)
        if total_bytes:
            percent = min(completed_bytes / total_bytes * 100, 100.0)
            progress_text = f"{human_size(completed_bytes)} / {human_size(total_bytes)} ({percent:.1f}%)"
        elif repo_total:
            percent = min(repo_done / repo_total * 100, 100.0)
            progress_text = f"资料库 {repo_done}/{repo_total} ({percent:.1f}%)"
        else:
            percent = 0.0
            progress_text = "-"
        self.progress_var.set(percent)
        self.metric_vars["task"].set(stats.get("current_repo") or "-")
        self.metric_vars["progress"].set(progress_text)
        self.metric_vars["download_rate"].set(format_rate(stats.get("download_speed_bps")))
        self.metric_vars["upload_rate"].set(format_rate(stats.get("upload_speed_bps")) if mode == "migration" else "-")
        self.metric_vars["eta"].set(format_duration(stats.get("eta_seconds")))
        source_read = int(stats.get("bytes_source_read") or 0)
        uploaded = int(stats.get("bytes_uploaded") or 0)
        if mode == "migration":
            transfer_text = f"读 {human_size(source_read)} / 传 {human_size(uploaded)}"
        else:
            transfer_text = human_size(source_read)
        self.metric_vars["transfer"].set(transfer_text)
        return percent, progress_text

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
        self.reset_runtime_metrics()
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

    def start_migration(self) -> None:
        token = self.token_var.get().strip()
        if not token:
            messagebox.showwarning("缺少清华 Token", "请先连接清华云盘账号。")
            return
        access_token = self.baidu_access_token_var.get().strip()
        if not access_token:
            messagebox.showwarning("缺少百度 Token", "请先完成百度授权或粘贴百度网盘 Access Token。")
            return
        categories = self.selected_categories()
        if not categories:
            messagebox.showwarning("没有选择范围", "请至少勾选一个资料库分类。")
            return
        app_dir = self.baidu_app_dir_var.get().strip()
        if not app_dir:
            messagebox.showwarning("缺少应用产品名称", "请填写百度网盘开放平台中“申请接入的产品名称”，迁移路径会写入 /apps/<产品名称>/。")
            return
        temp_dir = Path(self.baidu_temp_dir_var.get().strip())
        if not temp_dir:
            messagebox.showwarning("缺少临时目录", "请选择迁移临时目录。")
            return
        workers = max(1, min(int(self.workers_var.get() or 1), 16))
        self.cancel_event.clear()
        self.clear_log()
        self.reset_runtime_metrics()
        self.run_background(
            self._migration_worker,
            token,
            access_token,
            categories,
            app_dir,
            self.baidu_root_var.get().strip(),
            temp_dir,
            workers,
        )

    def _migration_worker(
        self,
        tsinghua_token: str,
        baidu_access_token: str,
        categories: set[str],
        app_dir: str,
        target_root: str,
        temp_dir: Path,
        workers: int,
    ) -> None:
        try:
            seafile_client = SeafileClient(tsinghua_token)
            baidu_client = BaiduNetdiskClient(baidu_access_token)
            options = MigrationOptions(
                categories=categories,
                app_dir_name=app_dir,
                target_root=target_root,
                temp_dir=temp_dir,
                workers=workers,
            )
            runner = MigrationRunner(seafile_client, baidu_client, options, event_callback=self.event_queue.put, cancel_event=self.cancel_event)
            runner.run()
        except Exception as exc:
            if self.cancel_event.is_set():
                self.event_queue.put({"kind": "status", "message": "已停止。"})
            else:
                self.event_queue.put({"kind": "error", "message": f"迁移失败：{exc}"})

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
        elif kind == "baidu_device_code":
            data = event.get("data") or {}
            self.baidu_device_data = data
            user_code = data.get("user_code") or ""
            url = baidu_auth_url(user_code, data.get("verification_url") or "https://openapi.baidu.com/device")
            self.status_var.set(f"百度授权用户码：{user_code}。授权完成后点击“完成授权”。")
            self.log(f"百度授权用户码：{user_code}")
            try:
                webbrowser.open(url)
            except Exception as exc:
                self.log(f"打开百度授权页失败，请手动打开 {url} | {exc}")
        elif kind == "baidu_account":
            access_token = event.get("access_token")
            if access_token:
                self.baidu_access_token_var.set(access_token)
            account = event.get("account") or {}
            quota = event.get("quota") or {}
            name = account.get("netdisk_name") or account.get("baidu_name") or account.get("uk") or "未知账号"
            used = int(quota.get("used") or 0)
            total = int(quota.get("total") or 0)
            free = int(quota.get("free") or max(total - used, 0))
            quota_text = f"{human_size(used)} / {human_size(total)}，可用 {human_size(free)}" if total else f"可用 {human_size(free)}"
            message = f"百度网盘验证通过：{name}，{quota_text}"
            self.status_var.set(message)
            self.log(message)
        elif kind == "log":
            self.log(event.get("message", ""))
        elif kind == "progress":
            stats = event.get("stats") or {}
            total = stats.get("repositories_total") or 0
            done = stats.get("repositories_done") or 0
            mode = event.get("mode") or "backup"
            self.update_runtime_metrics(stats, mode)
            action = "迁移" if mode == "migration" else "下载"
            self.status_var.set(
                f"资料库 {done}/{total} | 文件 {stats.get('files_seen', 0)} | "
                f"{action} {stats.get('downloaded', 0)} | 跳过 {stats.get('skipped', 0)} | "
                f"失败 {stats.get('failed', 0)} | 下载 {format_rate(stats.get('download_speed_bps'))} | "
                f"上传 {format_rate(stats.get('upload_speed_bps')) if mode == 'migration' else '-'} | "
                f"剩余 {format_duration(stats.get('eta_seconds'))}"
            )
        elif kind == "done":
            stats = event.get("stats") or {}
            if stats:
                self.update_runtime_metrics(stats, event.get("mode") or "backup")
            self.progress_var.set(100)
            self.status_var.set("迁移完成。" if event.get("mode") == "migration" else "备份完成。")
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
