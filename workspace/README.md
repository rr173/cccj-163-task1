# 高精地图瓦片分区发布与车辆路线快照服务

面向自动驾驶车队的高精地图发布系统：地图编辑形成**候选版本**，系统验证
**相邻瓦片接缝**与**跨区路线**后按**区域代际**原子发布；车辆获取路线时
**固定所用瓦片集合**（路线快照），途中更新只能整体切换、绝不拼接新旧拓扑；
紧急封路走**高优先级补丁**通道，与基础版本兼容并支持车辆安全过渡；
发布失败、补丁撤销、车辆离线追赶时始终保持**单一可解释版本**。

## 架构

```
mapsvc/
  models.py       领域模型：瓦片(拓扑边界/车道规则)、候选版本、代际、补丁、快照
  store.py        SQLite 持久化；瓦片与代际记录不可变；发布在事务内完成
  routing.py      有效地图合成（基线代际+补丁）、边界端口配对、Dijkstra 路由
  validation.py   候选验证：接缝检查 + 跨区锚点可达性 + 版本线性检查
  services.py     发布引擎 / 补丁服务 / 快照服务（MapService 门面）
  gridmap.py      网格城市生成器（演示/测试/场景共用）
  api.py          FastAPI 发布与审查 API
web/review.html   地图审查页（瓦片画布 + 候选/补丁/快照面板）
tools/replay.py   路线复现工具（重放事件流，校验不变量）
scripts/demo.py   端到端演示        scripts/seed.py  向运行中服务灌演示数据
scenarios/        复现场景（JSONL）  tests/           31 个测试
```

## 核心概念

| 概念 | 说明 |
|---|---|
| 瓦片 TileData | 不可变地图分片：节点/有向车道边/边界端口（拓扑边界）/车道规则，`(tile_id, version)` 唯一 |
| 候选版本 Candidate | 一组瓦片编辑 + 生效区域；状态机 DRAFT→VALIDATED→PUBLISHED/FAILED |
| 区域代际 Generation | 每区域单调递增；代际记录 = 该区域完整瓦片版本集合，不可变且永久保留 |
| 补丁 Patch | 高优先级限制类操作（CLOSE_EDGE/CLOSE_LANE/SPEED_CAP），附着基线代际，可撤销 |
| 路线快照 Snapshot | 车辆路线 + 固定瓦片集合 + 基线代际 + 生效补丁；创建后不可变 |

## 关键不变量

1. **原子发布**：一次候选发布涉及的所有区域在同一事务内推进代际；
   任一失败整体回滚，线上代际不变（`test_publish_failure_rolls_back_all_regions`）。
2. **禁止拼接**：快照的 `base/tiles/patches` 创建后不可变；途中发布新版本
   不影响在途快照；更新只能**整体切换**（refresh/补丁过渡/追赶），
   每辆车同一时刻至多一个 ACTIVE 快照。
3. **补丁兼容**：仅允许限制类操作，引用的瓦片/边必须存在于声明的基线代际；
   补丁生效时自动为路线受影响的车辆生成同版本族的整体过渡快照；
   撤销时受影响快照整体重定基线。
4. **单一可解释版本**：任何时刻车辆有效地图 = 基线代际 + 生效补丁，
   `GET /api/snapshots/{id}` 返回完整解释（基线/瓦片/补丁/路线/来源链）；
   离线追赶直接给出单一目标版本，不追增量序列。

## 验证规则

- **接缝**：相邻瓦片（bbox 共边）共享边界上的端口按 2m 容差坐标配对；
  未配对 → `SEAM_DANGLING_PORT`；车道数/宽度不一致 → ERROR；
  限速突变 >30kph → WARNING。
- **跨区路线**：在候选合成图上检查相邻区域**锚点节点**（区域几何中心
  最近车道节点）双向可达，失败 → `CROSS_REGION_UNREACHABLE`；
  支持追加命名 OD 检查。
- **版本线性**：编辑版本号必须 = 当前发布版本 + 1，防止基于过期基线编辑。

## 快速开始

```bash
# 测试（31 个）
python3 -m pytest tests/ -q

# 端到端演示（内存库，打印完整故事线）
python3 scripts/demo.py

# 路线复现（重放场景并校验不变量）
python3 tools/replay.py scenarios/trip_demo.jsonl

# 启动服务 + 灌入演示数据 + 打开审查页
python3 -m uvicorn mapsvc.api:app --port 8000 &
python3 scripts/seed.py
# 浏览器打开 http://127.0.0.1:8000/review
```

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/regions` | 注册区域 |
| POST | `/api/candidates` | 创建候选版本（瓦片编辑 + 生效区域） |
| POST | `/api/candidates/{id}/validate` | 验证（接缝 + 跨区 + 版本线性） |
| POST | `/api/candidates/{id}/publish` | 按区域代际原子发布 |
| POST | `/api/patches` | 紧急补丁（兼容性校验 + 自动过渡受影响车辆） |
| POST | `/api/patches/{id}/revoke` | 撤销补丁（受影响快照整体重定基线） |
| POST | `/api/routes` | 车辆请求路线 → 固定快照 |
| POST | `/api/snapshots/{id}/refresh` | 过渡点整体切换到当前有效版本 |
| POST | `/api/vehicles/{vid}/catchup` | 离线追赶 → 单一目标版本 |
| GET | `/api/snapshots/{id}` | 快照完整解释（单一可解释版本） |
| GET | `/api/review/state` | 审查页聚合数据 |
| GET | `/review` | 地图审查页 |

## 路线复现工具

`tools/replay.py` 读取 JSONL 事件流（建图/路线/编辑/补丁/撤销/离线/重连/
切换/结束），逐步重放并校验不变量 I1–I4（无拼接、单 ACTIVE、路线连通、
追赶即当前）。退出码非 0 表示发现违例，可用于回归与事故复盘：

```bash
python3 tools/replay.py scenarios/trip_demo.jsonl          # 文本报告
python3 tools/replay.py scenarios/trip_demo.jsonl --json   # JSON 报告
```
