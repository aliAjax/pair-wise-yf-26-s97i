# 数字档案长期保存服务

仅使用 Python 3.11+ 标准库实现的独立档案保存项目，支持清单校验、真实 SHA-256 内容校验、多个离线副本、损坏巡检与副本退役替换、格式迁移、保留期限和访问控制。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

服务地址为 <http://127.0.0.1:8102>，默认数据库 `preservation.db`。测试：

```bash
python3 -m unittest -v
```

演示用户：`owner`、`archivist`、`auditor`、`outsider`。API 使用 `X-User-Id`。文件通过 Base64 提交，单文件上限 10 MiB；这是为了保持示例自包含，生产部署应换成对象存储和流式上传。

## 主要接口

- `POST /api/archives`：创建受限档案。
- `POST /api/archives/{id}/members`：所有者授予 read/write 权限。
- `POST /api/archives/{id}/versions`：提交文件清单，服务端重新计算哈希和大小。
- `GET /api/versions/{id}`：查看版本、文件清单、副本状态、损坏记录和保护状况。
- `POST /api/versions/{id}/copies`：创建独立副本内容；已退役的存储位置会被拒绝（`location_retired`）。
- `POST /api/copies/{id}/verify`：巡检副本。发现损坏时记录损坏清单和发现时间，副本进入 `pending_retirement`（待退役），**不再原地盖回修复**。
- `POST /api/copies/{id}/replace`：为待退役副本登记新的存储位置。从版本主清单重建全部文件并逐一校验哈希，全部通过后新副本才转为 `healthy` 接替，旧副本转为 `retired`，旧位置进入退役黑名单，今后任何版本都不能再登记。
- `POST /api/copies/{id}/simulate-corruption`：演示/测试介质损坏，仅 owner 或 archivist 可用。
- `POST /api/versions/{id}/migrate`：生成格式迁移后的新版本并保留派生关系。
- `GET /api/archives/{id}/status`：保留期限、各版本保护状况和审计记录。

## 副本状态机

`healthy` →（巡检发现损坏）→ `pending_retirement` →（新位置重建并全量校验通过）→ `retired`。新副本在替换事务内先以 `pending_verification` 建立，校验全部通过才转 `healthy`；任一文件校验失败则整体回滚，旧副本保持待退役。`corrupt` 表示介质报告损坏、尚未巡检确认。

## 保护状况

版本至少有两份 `healthy` 副本才显示**受保护**（`protected: true`，版本状态 `verified`）；否则状态为 `degraded`，`missing_safeguards` 会点明缺少的保障，例如：

- `缺少健康副本`
- `健康副本不足两份，缺少冗余保障`
- `存在待退役副本，需登记新存储位置完成替换`
- `存在损坏待巡检确认的副本`
- `存在尚未校验的副本`

档案路径拒绝绝对路径和 `..`；同一版本副本位置唯一；所有变更写入审计日志。
