# 社区公共设施报修统筹服务

网格中心周末收到路灯、健身器材、楼道扶手等设施报修。本服务统一受理报修、
按风险与区域匹配维修班组、生成可追踪处理链，解决多班组各自记录导致的
**重复派工**与**紧急故障无优先级依据**问题。

仅依赖 Python 3.11 标准库，持久化使用 SQLite。

## 设计要点

- **事件溯源（Event Sourcing）**：所有状态变更都是不可变事件，带工单内单调
  递增的 `seq`。当前状态由事件重放得到，处理链全程可查，进程重启后派工状态
  与完整历史自动恢复（不存易失的"当前状态快照"）。
- **幂等与防过期**：班组移动端每次上报带 `(client_id, client_seq)`，离线重发、
  网络重试同序号一律幂等返回原事件；上报可携带所基于的 `base_seq`，落后于
  服务端版本的过期更新返回 `409 stale_update`；事件写入还有 `(ticket_id, seq)`
  乐观锁兜底并发冲突。
- **重复报修治理**：同位置（全角/空格归一化）+ 同类别 + 未结案的报修自动并入
  在办件，作为一条"原始来源"挂到主工单上，不再重复派工；也支持人工
  `merge`（双工单各落事件、单事务原子完成）。合并后每个原始来源的报修人、
  联系方式、照片摘要永久保留，可按来源号回溯到处理链。
- **风险/区域派工**：风险等级（低/中/高/紧急）× 设施类别设定 SLA；同区域且
  具备该类别资质的在岗班组中，选当前**加权负荷最低**者（紧急件权重最高），
  区域无匹配时回退跨区同专长班组。
- **严格处理链**：
  `待派工 → 已派工 → 维修中 → 待验收 → 已验收`；
  任意在办环节可暂停（**必须填原因**，暂停时长不计 SLA），恢复后回到暂停前
  阶段；**验收不通过只能回到整改阶段**，不能借进度上报或转交跳过整改。
- **联系方式按岗位隐藏**：
  - 调度员/网格员：完整号码；
  - 管理员、承担班组：脱敏（`138****5678`、`li******@example.com`、`张*姨`）；
  - 其他班组、匿名：不可见，且无权查看该工单。

## 目录结构

```
src/
  models.py     风险/类别/状态枚举、SLA 表、风险权重
  events.py     14 种领域事件及序列化
  storage.py    SQLite 事件存储（幂等键唯一索引、seq 乐观锁、班组注册表）
  aggregate.py  事件重放 -> 工单聚合状态机
  dispatch.py   区域/资质/负荷匹配
  security.py   岗位身份与联系方式脱敏
  service.py    FacilityService 领域服务（命令与查询）
  dto.py        只读投影（脱敏、SLA/超时计算）
  http_app.py   零依赖 HTTP 接口
  __main__.py   启动入口
  seed.py       演示种子数据
tests/          30 个 unittest 用例（含 HTTP 端到端、重启恢复）
```

## 启动

```bash
python -m src.seed --db data/facility.db     # 可选：写入演示班组与三笔周末报修
python -m src --host 0.0.0.0 --port 8080 --db data/facility.db
python -m unittest discover -s tests         # 运行测试
```

## 身份与请求约定

请求头：`X-User-Id`、`X-User-Role`（`dispatcher` | `crew` | `admin`）、
`X-Crew-Id`（班组角色必填）、`X-User-Name`。
移动端幂等：请求体 `client_id` + `client_seq`（或同名 `X-Client-*` 头），
可选 `base_seq` 表示所基于的事件序号。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/crews` | 注册/更新班组（区域、可修类别） |
| GET  | `/api/crews` | 班组列表 |
| POST | `/api/reports` | 登记报修（自动派工 + 自动去重并源） |
| GET  | `/api/tickets` | 工单列表（按岗位过滤/脱敏） |
| GET  | `/api/tickets/{id}` | 工单详情（含 SLA、实时超时、验收记录） |
| GET  | `/api/tickets/{id}/history` | 完整事件链 |
| POST | `/api/tickets/{id}/supplement` | 补充照片/联系方式/风险 |
| POST | `/api/tickets/{id}/dispatch` | 人工派工 |
| POST | `/api/tickets/{id}/transfer` | 转交班组（校验资质） |
| POST | `/api/tickets/{id}/pause` `/resume` | 暂停（必填原因）/恢复 |
| POST | `/api/tickets/{id}/progress` | 班组进度上报（幂等、防过期） |
| POST | `/api/tickets/{id}/submit` | 提交验收 |
| POST | `/api/tickets/{id}/review` | 验收 `{passed, opinion}`；不通过只回整改 |
| POST | `/api/merge` | 人工合并两工单，来源可回溯 |
| POST | `/api/tickets/{id}/close-duplicate` | 关闭重复件 |
| GET  | `/api/sources/{source_id}` | 按原始来源号回溯处理链 |
| POST | `/api/sweep-overdue` | SLA 超时巡检（落 `OverdueMarked` 事件，原因留痕） |
| GET  | `/api/dashboard` | 管理端：班组负荷、超时原因、**每项**验收意见 |

## 主要错误码

| HTTP | code | 场景 |
|---|---|---|
| 409 | `stale_update` | 过期更新：`base_seq`/事件版本落后 |
| 422 | `invalid_transition` | 非法状态流转（暂停工单上报、跳过整改等） |
| 422 | `merge_error` | 非法合并（已验收件、终态主件等） |
| 403 | `permission_denied` | 岗位/班组越权 |
| 404 | `not_found` | 工单或来源不存在 |

## 示例

```bash
# 紧急路灯报修（自动派给东区路灯班）
curl -s -XPOST localhost:8080/api/reports \
  -H 'X-User-Role: dispatcher' -H 'Content-Type: application/json' \
  -d '{"location":"东区滨河路12号路灯杆","category":"路灯","risk":"紧急",
       "photo_summary":"灯头熄灭","reporter_name":"张阿姨",
       "reporter_contact":"13812345678","source_id":"S-20260921-001"}'

# 班组离线进度（重复发同一 client_seq 幂等）
curl -XPOST localhost:8080/api/tickets/Txxxx/progress \
  -H 'X-User-Role: crew' -H 'X-Crew-Id: crew-east-light' -H 'Content-Type: application/json' \
  -d '{"status_text":"更换镇流器","phase":"in_progress",
       "client_id":"phone-7","client_seq":3,"base_seq":2}'
```
