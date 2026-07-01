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
