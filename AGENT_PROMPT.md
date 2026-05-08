# Geo-Data Pipeline Agent Prompt / 地理数据管线 Agent 提示词

## Project Overview / 项目概述

This is a global geospatial data pipeline that bakes CopDEM elevation, WorldPop population, and ESA WorldCover land cover into compact binary quadtree format (QTR5) for efficient real-time queries.
/ 这是一个全球地理数据管线，将 CopDEM 海拔、WorldPop 人口、ESA WorldCover 地表类型烘焙为紧凑的二进制四叉树格式(QTR5)，支持高效实时查询。

Core package: `geo_baker_pkg/` with modular structure / 核心包：`geo_baker_pkg/` 模块化结构

---

## Key Architecture / 关键架构

### 1. 16-bit Compact Node Format / 16bit紧凑节点格式

Each node is 2 bytes / 每个节点仅2字节:

```
Leaf node / 叶节点:   [1bit is_leaf=1][12bit payload][3bit zone]
Branch node / 分支节点: [1bit is_leaf=0][15bit subtree_size]
```

- **12-bit payload**: Terrain uses nonlinear elevation encoding; Population uses logarithmic density encoding
  / 地形用非线性海拔编码，人口用对数密度编码
- **3-bit zone**: Terrain uses natural zones (water/bare/forest/harsh); Population uses urban zones
  / 地形用自然区域(水体/裸地/森林/严酷)，人口用城市区域
- **15-bit subtree_size**: DFS navigation skip, max 32767 / DFS导航跳过，最大32767

### 2. DFS Pre-order + subtree_size Navigation / DFS前序遍历+subtree_size导航

Nodes written in depth-first order, no child_offset needed. Navigate by skipping subtrees via subtree_size, O(depth) complexity.
/ 节点按深度优先顺序写入，无需child_offset。通过subtree_size跳过子树导航，O(depth)复杂度。

**Quadrant order / 象限顺序**: 0=NW, 1=NE, 2=SW, 3=SE

### 3. Dynamic Node Allocation / 动态节点分配

```python
for ci, sub_data in enumerate(four_quadrants):
    remaining = max_nodes - (node_id[0] - start_id)
    siblings_left = 4 - ci - 1
    if siblings_left > 0:
        alloc = max(child_max, remaining // (siblings_left + 1))
    else:
        alloc = remaining
    alloc = max(1, min(alloc, 32000))
```

---

## Module Structure / 模块结构

```
geo_baker_pkg/
├── constants.py   - Zone/gradient/urban enums, data source URLs, path defaults
├── encoding.py    - Nonlinear elevation + population encoding, 16-bit node codec
├── qtree.py       - Adaptive quadtree build/navigate/serialize
├── download.py    - DEM/POP/ESA data fetching with fallback chains, water edge refinement
├── pack.py        - Tile packing into .dat (GeoPack format), incremental pack, merge
├── query.py       - Point query from tiles or .dat packs
├── bake.py        - Core bake orchestration (tile/region/global)
└── cli.py         - Command-line interface
```

---

## Data Sources / 数据源

| Data / 数据 | Source / 来源 | Resolution / 分辨率 | Auth / 认证 |
|-------------|--------------|---------------------|-------------|
| CopDEM GLO-30 | Element84 / CDSE STAC | 30m | None / 无需 |
| Open-Elevation | API fallback | ~100m | None / 无需 |
| WorldPop | ArcGIS ImageServer | 1km | None / 无需 |
| ESA WorldCover | Planetary Computer / Element84 STAC | 100m | None / 无需 |
| OSM Overpass | Multiple endpoints | Variable / 可变 | None / 无需 |

---

## Modification Checklist / 修改检查清单

1. **Population encoding params**: Verify `log1p(100000) × scale ≤ 4095` / 验证对数编码参数
2. **MAX_NODES**: Ensure root subtree_size < 32767 (MAX_NODES ≤ 30000 safe) / 确保根节点subtree_size不溢出
3. **FORCE_DEPTH**: Each +1 level ≈ ×4 nodes / 每增加1层强制分裂约×4节点
4. **Split threshold**: Lower threshold → more nodes → possible subtree_size overflow / 降低阈值→更多节点→可能溢出
5. **New split conditions**: Must check `max_nodes > 1` / 必须检查预算耗尽
6. **Quadrant order**: Navigation must match build / 导航代码象限必须与构建一致
