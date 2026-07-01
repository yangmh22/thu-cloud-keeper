# THU Cloud Keeper

清华云盘自助备份工具，一个用于备份清华云盘（Seafile）资料库、群组共享内容和“共享给我的”内容的桌面 App。

## 项目缘起

作者在毕业离校整理资料时发现，清华云盘里分散着多年的课程资料、科研文档、群组共享文件和别人共享给自己的资料。网页端适合日常查看，但如果想在离校前完整备份所有可访问内容，就需要反复进入不同资料库手动下载，容易漏掉群组共享或“共享给我的”文件，也很难估算总数据量。

于是构建了这个小工具：输入清华云盘个人 Token，选择本地目录，勾选需要备份的范围，然后让程序自动枚举资料库、显示大小、并发下载、断点续跑。它的目标不是替代清华云盘，而是在重要时间点帮你稳稳地把自己的资料带走。

## 界面预览

![界面预览](docs/ui-preview.png)

## 功能

- 输入清华云盘个人 Token 连接账号。
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

打开清华云盘个人设置页面：

<https://cloud.tsinghua.edu.cn/profile/>

在页面中获取个人 Token，粘贴到 App 的“个人 Token”输入框，然后点击“连接并读取资料库”。

Token 只在运行时用于请求清华云盘 API。本工具不会主动保存 Token，也不会把 Token 写入日志或备份元数据。

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

```powershell
cd "D:\Project&Research\其他内容\260701清华云盘自助备份"
$env:PYTHONPATH = "$PWD\src"
python -m tsinghua_cloud_backup.app
```

或者安装为可编辑包：

```powershell
python -m pip install -e .
tsinghua-cloud-backup
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
