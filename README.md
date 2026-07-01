# 清华云盘自助备份

一个用于备份清华云盘（Seafile）的本地 GUI 小工具。

## 功能

- 输入清华云盘个人 Token 连接账号
- 选择备份目录
- 勾选备份范围：
  - 我的资料库
  - 群组共享内容
  - 共享给我的
- 多线程下载
- 已存在且大小一致的文件自动跳过，支持断点续跑
- 生成备份元数据、仓库清单、文件清单和失败日志

## 运行

```powershell
cd "D:\Project&Research\其他内容\260701清华云盘自助备份"
python -m tsinghua_cloud_backup.app
```

如果没有安装为可编辑包，请先设置：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m tsinghua_cloud_backup.app
```

## 打包

Windows：

```powershell
cd "D:\Project&Research\其他内容\260701清华云盘自助备份"
.\scripts\package_windows.ps1
```

生成结果：

```text
dist\清华云盘自助备份\
dist\清华云盘自助备份-windows.zip
```

Windows 版采用 PyInstaller 的文件夹模式，启动更稳定。解压 zip 后双击 `清华云盘自助备份.exe` 即可启动。

macOS 需要在 macOS 系统上构建：

```bash
cd /path/to/tsinghua-cloud-backup
chmod +x scripts/package_macos.sh
./scripts/package_macos.sh
```

生成结果：

```text
dist/清华云盘自助备份.app
```

仓库中也包含 GitHub Actions 工作流。推送 tag 或手动触发 workflow 后，会分别在 Windows 和 macOS runner 上生成构建产物。

## 界面预览

![界面预览](docs/ui-preview.png)

## Token

在清华云盘个人设置页面获取 Token：

<https://cloud.tsinghua.edu.cn/profile/>

Token 只在运行时使用，不会主动写入磁盘。备份日志不会记录 Token。

## 输出结构

备份目录下会生成：

```text
我的资料库/
群组共享内容/
共享给我的/
_backup_metadata/
```

`_backup_metadata` 中包括：

- `manifest.json`
- `repositories.csv`
- `files.csv`
- `backup.log`
- `failures.jsonl`

## 说明

本工具通过清华云盘 Seafile API 下载你账号有权限访问的资料库内容。重复运行同一备份目录会自动跳过已完整下载的文件，适合中断后续传。
