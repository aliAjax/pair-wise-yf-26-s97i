# 数字档案长期保存服务

仅使用 Python 3.11+ 标准库实现的独立档案保存项目，支持清单校验、真实 SHA-256 内容校验、多个离线副本、损坏检测与自动修复、格式迁移、保留期限和访问控制。

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
- `GET /api/versions/{id}`：查看版本、文件清单和副本状态。
- `POST /api/versions/{id}/copies`：创建独立副本内容。
- `POST /api/copies/{id}/verify`：巡检副本；发现损坏时记录损坏清单（文件路径+发现时间），原副本进入待退役，**不会就地覆写原介质**。
- `POST /api/versions/{id}/replacements`：管理员为待退役副本登记新的存储位置，新副本从健康副本重建，状态为 `rebuilding`。
- `POST /api/copies/{id}/rebuild`：以健康副本重新同步重建中的新副本（可重复重试）。
- `POST /api/copies/{id}/complete-replacement`：对新副本做全量校验；全部文件与版本清单一致后才标记健康、旧副本退役、旧位置列入黑名单、闭环损坏清单。校验不过则保持 `rebuilding`，不能接替。
- `POST /api/copies/{id}/simulate-corruption`：演示/测试介质损坏，仅 owner 或 archivist 可用。
- `POST /api/versions/{id}/migrate`：生成格式迁移后的新版本并保留派生关系。
- `GET /api/archives/{id}/status`：保留期限、版本状态和审计记录。

档案路径拒绝绝对路径和 `..`；同一版本副本位置唯一；**旧位置退役后全局进入黑名单，任何版本都不能再在该位置登记副本**；**版本只有在至少 2 份健康副本（`healthy`）时才显示 `protected`，否则为 `at_risk` 并在 `protection.message`/`missing_safeguards` 中点明缺少的保障**（待退役、重建中、未闭环清单等）；所有变更写入审计日志。

## 副本替换流程

巡检不再"原地盖回"，介质是否可靠由管理员按流程决定：

1. **巡检发现**：`verify` 发现哈希/长度不符 → 写入 `damage_reports`（损坏文件清单 + `found_at`），副本状态 `pending_retirement`，原内容保持不动。
2. **登记新位置**：`replacements` 由 owner/archivist 登记新存储位置；必须存在另一份全量健康副本作为重建来源，新副本状态为 `rebuilding`，此时不接替。
3. **重建与校验**：新副本可反复 `rebuild`；`complete-replacement` 按版本清单逐文件比对（缺失/不一致/多出均算失败）。
4. **接替与退役**：仅当全部文件校验通过，新副本才变 `healthy`，旧副本变 `retired`，旧位置永久封禁，损坏清单标记 `resolved`。
5. **保护判定**：`healthy` 副本 ≥ 2 才 `protected`；0/1 份、有待退役或重建中副本、有未闭环清单时一律 `at_risk`。
