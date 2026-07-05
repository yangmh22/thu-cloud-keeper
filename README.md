# THU Cloud Keeper

清华云盘自助备份工具，一个用于备份清华云盘（Seafile）资料库、群组共享内容和“共享给我的”内容的桌面 App，并可按需启动本地 Web 控制台。

## 项目缘起

作者在毕业离校整理资料时发现，清华云盘里分散着多年的课程资料、科研文档、群组共享文件和别人共享给自己的资料。网页端适合日常查看，但如果想在离校前完整备份所有可访问内容，就需要反复进入不同资料库手动下载，容易漏掉群组共享或“共享给我的”文件，也很难估算总数据量。

于是构建了这个小工具：输入清华云盘个人 Token，选择本地目录或百度网盘迁移目标，勾选需要备份的范围，然后让程序自动枚举资料库、显示大小、并发下载、断点续跑。它的目标不是替代清华云盘，而是在重要时间点帮你稳稳地把自己的资料带走。

## 界面

默认界面是 Tkinter 桌面窗口：

```bash
tsinghua-cloud-backup
```

如果需要本地 Web 控制台，可以显式选择 WebUI：

```bash
tsinghua-cloud-backup --frontend webui
```

WebUI 启动后会自动打开浏览器，地址形如 `http://127.0.0.1:8765/`。也可以继续使用专用入口 `tsinghua-cloud-backup-web` 或 Tkinter 专用入口 `tsinghua-cloud-backup-tk`。

## 功能

- 在图形界面中输入清华云盘个人 Token 连接账号。
- 选择本地备份目录。
- 勾选备份范围：
  - 我的资料库
  - 群组共享内容
  - 共享给我的
- 连接后显示云盘概览：
  - 账号空间用量
  - 全部资料库数量与声明大小
  - 各分类资料库数量与大小
  - 当前勾选范围的总大小
- 多线程下载，支持设置并发数。
- 已存在且大小一致的文件自动跳过。
- 使用 `.part` 临时文件，支持中断后续跑。
- 自动生成备份元数据、仓库清单、文件清单和失败日志。
- 可将选中的清华云盘资料库迁移到百度网盘应用目录。
- 运行中显示总体进度、实时下载/上传速率、本次传输量和预计剩余时间。

## 下载与使用

### Windows

从 GitHub Actions 或 Release 下载 Windows 构建产物：

```text
清华云盘自助备份-windows.zip
```

解压后双击：

```text
清华云盘自助备份\清华云盘自助备份.exe
```

Windows 版采用 PyInstaller 文件夹模式，比单文件自解压模式启动更稳定。

### macOS

macOS 版会由 GitHub Actions 在 macOS runner 上构建：

```text
清华云盘自助备份-macos.zip
```

下载后解压，打开 `清华云盘自助备份.app`。如果 macOS 阻止打开未签名 App，可以在“系统设置 -> 隐私与安全性”中允许打开，或在终端中移除隔离属性：

```bash
xattr -dr com.apple.quarantine "清华云盘自助备份.app"
```

## 获取清华云盘 Token

在图形界面中点击“打开 Token 页面”，会打开清华云盘个人设置页面：

<https://cloud.tsinghua.edu.cn/profile/>

登录后在页面中复制个人 Token，回到工具中点击“粘贴”或直接填入 Token，再点击“连接并读取资料库”。工具会先验证 Token，验证通过后自动读取资料库。

Token 只在运行时用于请求清华云盘 API。本工具不会主动保存 Token，也不会把 Token 写入日志或备份元数据。

## 迁移到百度网盘

百度网盘开放平台的公开上传接口没有提供“从另一个云盘 URL 直接转存到百度网盘”的云端对云端迁移能力。当前实现采用本机中转：程序从清华云盘读取文件内容，按百度网盘要求计算分片 MD5，然后上传到百度网盘。正常情况下只会在临时目录保存正在上传的 4 MiB 分片；如果清华云盘下载地址不支持 `Range` 分片读取，会顺序读取完整文件并逐片上传，仍只保留当前分片临时文件。

使用前需要在[百度网盘开放平台控制台](https://pan.baidu.com/union/console/applist?from=person)创建或选择一个应用，并确认：

- 应用拥有网盘能力，授权范围包含 `basic,netdisk`。
- 记录应用的 `App Key` 和 `Secret Key`。
- 控制台中填写的“应用产品名称”必须和开放平台创建应用时的“申请接入的产品名称”一致。百度网盘开放平台要求应用文件写入 `/apps/<产品名称>/...`。

Web 控制台中的迁移流程：

1. 先完成清华云盘 Token 连接，确认资料库列表已读取。
2. 在“百度网盘授权”里填写 `App Key` 和 `Secret Key`。
3. 点击“获取授权码”，在浏览器中按用户码完成授权。
4. 回到控制台点击“完成授权”，或手动粘贴 `Access Token` 后点击“验证百度账号”。
5. 设置“迁移根目录”和“临时目录”。
6. 勾选需要迁移的资料库范围，点击“开始迁移到百度网盘”。

目标路径为：

```text
/apps/<申请接入的产品名称>/清华云盘迁移/
├── 我的资料库/
├── 群组共享内容/
└── 共享给我的/
```

迁移临时目录会保存过程元数据：

```text
_migration_metadata/
├── manifest.json
├── repositories.csv
├── files.csv
├── migration.log
└── failures.jsonl
```

迁移支持续跑：

- 每次启动会读取 `files.csv` 中已有的成功记录。源资料库、源路径、目标路径、大小和修改时间都一致时，会作为续跑候选。
- 默认使用“快速续跑”：本地成功清单中已完成的文件会直接跳过，不再逐个查询百度目标目录。这能显著减少断电后续跑时的校验时间。
- 如果怀疑百度网盘目标目录被手动改动，可以在控制台勾选“严格远端校验”。该模式会查询百度目标父目录；如果目标文件已经存在且大小一致，会跳过并写入 `skipped_remote` 或 `skipped_manifest`。
- 严格远端校验下，如果本地清单显示已迁移，但百度目标文件不存在或大小不一致，会重新迁移该文件；如果百度目录查询失败，但本地清单显示该文件已成功迁移，会写入 `skipped_manifest_unverified` 并跳过。
- 单个文件上传过程中中断时，下次会重新计算分片 MD5 并调用百度预上传接口；如果百度端仍保留已上传分片，程序只补传缺失分片。如果百度端未保留分片状态，则会重新上传该文件；已经成功创建到百度网盘的文件会在下次被跳过。
- 迁移支持文件级并发。Web 控制台中的“迁移并发”决定同时处理几个文件；建议从 `4` 开始，根据清华云盘和百度网盘限速情况逐步调到 `6` 或 `8`。过高并发可能触发接口限流或导致单文件速度波动。

运行状态面板会显示：

- 完成进度：按当前选中资料库声明大小估算，跳过、完成和失败的文件都会推进总体进度。
- 下载速率：本机从清华云盘读取数据的滚动速率。迁移时计算分片 MD5 和读取上传分片都会计入。
- 上传速率：迁移到百度网盘时，按分片上传完成情况估算滚动速率。
- 剩余时间：按最近一段时间的完成进度估算；刚启动、仅在枚举目录或等待接口响应时可能显示 `-`。

注意：

- 百度开放平台上传路径需要位于 `/apps/<申请接入的产品名称>/` 下。这个名称不是 App Key，也不是 Secret Key。
- 空文件当前会被跳过，因为分片上传链路没有可提交的文件内容。
- 普通百度网盘用户按官方文档采用 4 MiB 分片；迁移很大的单文件时，请确认百度账号权限和容量足够。
- 临时目录需要容纳当前 4 MiB 上传分片和迁移元数据。

## 输出结构

备份目录下会生成：

```text
我的资料库/
群组共享内容/
共享给我的/
_backup_metadata/
```

`_backup_metadata` 中包括：

```text
manifest.json
repositories.csv
files.csv
backup.log
failures.jsonl
```

## 从源码运行

需要 Python 3.10 或更高版本。

```bash
cd /path/to/thu-cloud-keeper
PYTHONPATH=src python -m tsinghua_cloud_backup.cli
```

或者安装为可编辑包：

```bash
python -m pip install -e .
tsinghua-cloud-backup
```

默认启动 Tkinter 桌面界面。启动 WebUI 时可以指定端口；默认端口是 `8765`，如果端口被占用，程序会自动尝试后续端口。

```bash
tsinghua-cloud-backup --frontend webui --port 8877
```

## 本地打包

### Windows

```powershell
cd "D:\Project&Research\其他内容\260701清华云盘自助备份"
.\scripts\package_windows.ps1
```

生成结果：

```text
dist\清华云盘自助备份\
dist\清华云盘自助备份-windows.zip
```

### macOS

macOS 需要在 macOS 系统上构建：

```bash
cd /path/to/thu-cloud-keeper
chmod +x scripts/package_macos.sh
./scripts/package_macos.sh
```

生成结果：

```text
dist/清华云盘自助备份.app
dist/清华云盘自助备份-macos.zip
```

## GitHub Actions 构建

仓库包含 `.github/workflows/build.yml`。可以在 GitHub 页面中手动触发 `Build desktop apps` workflow，也可以推送 `v*` tag 自动触发。

构建完成后会生成两个 artifact：

```text
tsinghua-cloud-backup-windows
tsinghua-cloud-backup-macos
```

## 注意事项

- 本项目是个人自助备份工具，不是清华大学或清华云盘官方项目。
- 下载范围取决于你的清华云盘账号权限；账号无权访问的内容无法备份。
- 页面显示的大小来自清华云盘/Seafile API 的声明大小，实际落盘大小可能因重复文件、临时文件或文件系统差异略有变化。
- 第一次完整备份可能很慢，建议连接稳定网络并预留足够磁盘空间。
- 重复运行同一备份目录会跳过已完整下载的文件，适合中断后继续。

## 许可证

暂未指定许可证。公开分发前建议补充合适的开源许可证。
