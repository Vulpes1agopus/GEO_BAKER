# GeoBaker

将 CopDEM 海拔、WorldPop 人口、ESA WorldCover 地表类型烘焙为紧凑的二进制四叉树瓦片（QTR5 格式），支持 O(1) 随机访问查询。

**文档**：见 [docs/README.md](docs/README.md)（[总览](docs/overview.md) · [技术参考](docs/technical_reference.md)）。

## ✨ 特性

- **多源数据融合** — DEM (30m) + 人口 (1km) + 地表类型 (100m)，全部免费、无需 API Key
- **自适应四叉树 (QTR5)** — 16bit 节点，非线性海拔编码，动态节点分配
- **紧凑二进制格式** — zstd 压缩，360×180 网格索引，O(1) 点查询
- **智能水陆判断** — NO_DATA 瓦片下载 ESA zone 数据判断水陆，而非直接写水瓦片
- **沿海城市修正** — 以人口数据为真值，修正海岸线水陆误判（如雷克雅未克）
- **增量打包** — 仅重新打包新增/变更瓦片
- **可视化工具** — 海拔/人口热力图、区域图、高分辨率全球概览

## 📊 数据源（全部免费，无需 API Key）

| 数据 | 来源 | 分辨率 | 认证 |
|------|------|--------|------|
| CopDEM GLO-30 | Planetary Computer / Element84 STAC | 30m | 无需 |
| Open-Elevation | REST API（降级备用） | ~100m | 无需 |
| WorldPop | ArcGIS ImageServer | 1km | 无需 |
| ESA WorldCover | Planetary Computer STAC | 100m | 无需 |

## 🚀 快速开始

```bash
# 安装依赖
pip install numpy scipy requests aiohttp pystac-client planetary-computer \
            rasterio zstandard Pillow matplotlib tifffile shapely

# 烘焙单个瓦片（北京）
python -m geo_baker_pkg --tile 116,39

# 查询一个点
python -m geo_baker_pkg --query 39.9 116.4

# 查看统计
python -m geo_baker_pkg --stats

# 试运行（估算不下载）
python -m geo_baker_pkg --global --dry-run
```

## 📖 用法

### 烘焙（数据处理）

| 命令 | 说明 |
|------|------|
| `--tile 116,39` | 烘焙单个瓦片 |
| `--bbox 70 20 140 55` | 烘焙区域 |
| `--global` | 烘焙全球 |
| `--global --split 2/1` | 分布式：第1部分（共2部分） |
| `--global --bake-ocean` | 不跳过海洋瓦片（下载DEM处理） |
| `--global --no-data-water` | NO_DATA瓦片直接写水瓦片（不下载zone判断） |
| `--retry-errors` | 重试失败瓦片 |
| `--fix-coastal` | 检测并修复沿海问题瓦片 |
| `--fix-pop-zone` | 自动修复人口/城镇与zone冲突瓦片 |

### 打包（二进制导出）

| 命令 | 说明 |
|------|------|
| `--pack` | 打包地形 → `terrain.dat` |
| `--pack-pop` | 打包人口 → `population.dat` |
| `--incremental-pack` | 增量打包（仅新/变更瓦片） |
| `--merge a.dat b.dat` | 合并两个 .dat 文件 |

### 查询

| 命令 | 说明 |
|------|------|
| `--query 39.9 116.4` | 查询海拔+人口（瓦片文件） |
| `--query-pack 39.9 116.4` | 从 `terrain.dat` 查询 |
| `--query-pop 39.9 116.4` | 仅查询人口 |
| `--stats` | 瓦片统计 |

### 工具

```bash
# 检查与验证
python tools/geo_inspect.py query 39.9 116.4          # 查询点
python tools/geo_inspect.py tile-info 116 39           # 瓦片详情
python tools/geo_inspect.py stats                       # 全局统计
python tools/geo_inspect.py validate                    # 验证所有瓦片
python tools/geo_inspect.py validate --fix-ocean        # 修复海洋不匹配
python tools/geo_inspect.py size-report                 # 大小分布
python tools/verify_cities.py --cities data/global_cities.json  # 5000 城抽样验证

# 可视化
python tools/visualize.py elevation --bbox 70 20 140 55 -o china_elev.png
python tools/visualize.py population --bbox 70 20 140 55 -o china_pop.png
python tools/visualize.py zones --bbox 70 20 140 55 -o china_zones.png
python tools/visualize.py overview --pack terrain.dat -o global.png
python tools/visualize.py compare --lat 39.9 --lon 116.4 -o beijing.png

# 后台烘焙（低优先级）
bash tools/bake_background.sh                       # 烘焙剩余瓦片
bash tools/bake_background.sh --retry               # 重试错误
bash tools/bake_background.sh --region 70 20 140 55 # 指定区域
```

## 🏗️ 项目结构

```
GeoBaker/
├── geo_baker_pkg/               # 核心包
│   ├── core.py                  # 常量、编码、四叉树（基础定义）
│   ├── pipeline.py              # 数据下载 + 烘焙编排（降级链、沿海城市修正、NO_DATA处理）
│   ├── io.py                    # 打包 + 查询（GeoPack .dat 格式）
│   └── cli.py                   # 命令行入口
├── tools/                       # 工具脚本
│   ├── geo_inspect.py           # 查询、验证、大小分析
│   ├── visualize.py             # 热力图、区域图、高分辨率全球概览
│   ├── verify_cities.py         # 城市数据验证
│   ├── bake_background.sh       # 低优先级后台烘焙
│   └── fix_coastal.sh           # 沿海修复脚本
├── scripts/                     # 数据准备脚本
│   ├── borders/                 # 国界提取
│   ├── facilities/              # 机场/设施数据获取
│   ├── interop_export/          # 国界矢量互操作导出
│   ├── visualization/           # GeoJSON/热力图渲染
│   ├── verify.py                # 瓦片验证
│   ├── quad_view.py             # 四视图渲染
│   └── cleanup_for_rebake.py    # 重烘焙清理
├── data/                        # 源数据（城市列表, 索引）
├── terrain.dat                  # 全球地形打包文件
└── population.dat               # 全球人口打包文件
```

## 🔧 架构

### QTR5 格式（16bit 节点）

```
地形叶节点: [1bit is_leaf=1][11bit 海拔(非线性)][2bit 坡度][2bit 区域]
人口叶节点: [1bit is_leaf=1][12bit 人口密度(对数)][3bit 城市类型]
分支节点:   [1bit is_leaf=0][15bit subtree_size]
```

- **非线性海拔**: 0-511m @ 1m 精度, 512-1535m @ 2m, 1536-3583m @ 4m, 3584-8190m @ 8m
- **人口编码**: 12bit 对数 (`log1p(density) × 355.7`)，覆盖 1-100,000/km²
- **DFS 前序遍历**: 通过 subtree_size 跳过子树导航，O(depth) 复杂度

### 数据管线

```
STAC API (CopDEM/ESA) ──┐
WorldPop ArcGIS ────────┤──→ 下载 ──→ 对齐 ──→ 修正 ──→ 四叉树 ──→ 打包 ──→ .dat
                        └  (无 OSM 降级；地表仅 ESA PC)
```

### 降级链

- **DEM**: Planetary Computer → Element84 → Open-Elevation
- **地表类型**: ESA WorldCover (Planetary Computer)

### NO_DATA 瓦片处理

当 DEM 下载失败（NO_DATA）时，不再直接写水瓦片，而是：
1. 下载 ESA WorldCover zone 数据判断水陆
2. 若水体占比 > 95%，写水瓦片
3. 否则返回 no_data 状态，等待后续处理
4. 可通过 `--no-data-water` 启动项跳过 zone 下载，直接写水瓦片

### 沿海城市修正

典型场景：雷克雅未克 (64.1°N, -21.9°W)
- DEM 显示海拔 0m 或负值（海岸线精度问题）
- ESA WorldCover 100m 分辨率可能将沿海城区标为水域
- WorldPop 显示该区域有显著人口（默认栅格修正阈值 **>10 人/km²**）
- **逻辑**：人口数据证明有人居住 → 该区域应为陆地，而非水域

修正流程（按执行顺序）：
1. `fix_water_consistency` — 人口为真值，zone=WATER 且 pop>10（/km²）的像素修正为 ZONE_NATURAL
2. `_enforce_water_value_consistency` — DEM≤0 且 pop≤1.0 的像素强制标记为 WATER
3. `_enforce_water_zone_consistency` — 所有 zone=WATER 的像素 pop/urban 清零
4. `build_adaptive_tree` — 构建四叉树时，若叶节点众数为 WATER 但区域内有 pop>0 像素，回退为非水 zone

## 📦 输出格式

### terrain.dat / population.dat (GeoPack)

| 区域 | 大小 | 说明 |
|------|------|------|
| Header | 32 bytes | Magic、网格维度、瓦片数、标志 |
| Index | 1,036,800 bytes | 360×180 网格的 (offset, size) 对 |
| Data | 可变 | zstd 压缩的瓦片数据块 |

### 瓦片二进制 (QTR5)

- **水域瓦片**: 1 字节 (`0xFF`)
- **数据瓦片**: 2×N 字节（16bit 节点数组，DFS 前序）

## 📄 许可证

MIT License — 详见 [LICENSE](LICENSE)

---

# GeoBaker (English)

Bake CopDEM elevation, WorldPop population, and ESA WorldCover land cover into compact binary quadtree tiles (QTR5 format) with O(1) random-access queries.

## ✨ Features

- **Multi-source data fusion** — DEM (30m) + Population (1km) + Land cover (100m), all free, no API Key required
- **Adaptive quadtree (QTR5)** — 16-bit nodes, non-linear elevation encoding, dynamic node allocation
- **Compact binary format** — zstd compressed, 360×180 grid index, O(1) point queries
- **Smart water/land detection** — NO_DATA tiles download ESA zone data to determine water/land, instead of writing water tiles directly
- **Coastal city correction** — Uses population as ground truth to fix water/land misclassification (e.g. Reykjavik)
- **Incremental packing** — Only re-pack new/changed tiles
- **Visualization tools** — Elevation/population heatmaps, zone maps, high-resolution global overview

## 📊 Data Sources (All Free, No API Key)

| Data | Source | Resolution | Auth |
|------|--------|------------|------|
| CopDEM GLO-30 | Planetary Computer / Element84 STAC | 30m | None |
| Open-Elevation | REST API (fallback) | ~100m | None |
| WorldPop | ArcGIS ImageServer | 1km | None |
| ESA WorldCover | Planetary Computer STAC | 100m | None |

## 🚀 Quick Start

```bash
# Install dependencies
pip install numpy scipy requests aiohttp pystac-client planetary-computer \
            rasterio zstandard Pillow matplotlib tifffile shapely

# Bake a single tile (Beijing)
python -m geo_baker_pkg --tile 116,39

# Query a point
python -m geo_baker_pkg --query 39.9 116.4

# View stats
python -m geo_baker_pkg --stats

# Dry run (estimate without downloading)
python -m geo_baker_pkg --global --dry-run
```

## 📖 Usage

### Baking (Data Processing)

| Command | Description |
|---------|-------------|
| `--tile 116,39` | Bake a single tile |
| `--bbox 70 20 140 55` | Bake a region |
| `--global` | Bake globally |
| `--global --split 2/1` | Distributed: part 1 of 2 |
| `--global --bake-ocean` | Don't skip ocean tiles (download DEM) |
| `--global --no-data-water` | Write water tiles for NO_DATA directly (skip zone check) |
| `--retry-errors` | Retry failed tiles |
| `--fix-coastal` | Detect and fix coastal problem tiles |
| `--fix-pop-zone` | Auto-fix population/urban vs zone conflict tiles |

### Packing (Binary Export)

| Command | Description |
|---------|-------------|
| `--pack` | Pack terrain → `terrain.dat` |
| `--pack-pop` | Pack population → `population.dat` |
| `--incremental-pack` | Incremental pack (new/changed tiles only) |
| `--merge a.dat b.dat` | Merge two .dat files |

### Querying

| Command | Description |
|---------|-------------|
| `--query 39.9 116.4` | Query elevation+population (tile files) |
| `--query-pack 39.9 116.4` | Query from `terrain.dat` |
| `--query-pop 39.9 116.4` | Query population only |
| `--stats` | Tile statistics |

### Tools

```bash
# Inspection & Validation
python tools/geo_inspect.py query 39.9 116.4          # Query point
python tools/geo_inspect.py tile-info 116 39           # Tile details
python tools/geo_inspect.py stats                       # Global stats
python tools/geo_inspect.py validate                    # Validate all tiles
python tools/geo_inspect.py validate --fix-ocean        # Fix ocean mismatches
python tools/geo_inspect.py size-report                 # Size distribution
python tools/verify_cities.py --cities data/global_cities.json  # 5000-city sanity check
python tools/visualize.py elevation --bbox 70 20 140 55 -o china_elev.png
python tools/visualize.py population --bbox 70 20 140 55 -o china_pop.png
python tools/visualize.py zones --bbox 70 20 140 55 -o china_zones.png
python tools/visualize.py overview --pack terrain.dat -o global.png
python tools/visualize.py compare --lat 39.9 --lon 116.4 -o beijing.png

# Background baking (low priority)
bash tools/bake_background.sh                       # Bake remaining tiles
bash tools/bake_background.sh --retry               # Retry errors
bash tools/bake_background.sh --region 70 20 140 55 # Specify region
```

## 🏗️ Project Structure

```
GeoBaker/
├── geo_baker_pkg/               # Core package
│   ├── core.py                  # Constants, encoding, quadtree (base definitions)
│   ├── pipeline.py              # Data download + bake orchestration (fallback chain, coastal fix, NO_DATA handling)
│   ├── io.py                    # Packing + querying (GeoPack .dat format)
│   └── cli.py                   # CLI entry point
├── tools/                       # Tool scripts
│   ├── geo_inspect.py           # Query, validate, size analysis
│   ├── visualize.py             # Heatmaps, zone maps, high-res global overview
│   ├── verify_cities.py         # City data verification
│   ├── bake_background.sh       # Low-priority background baking
│   └── fix_coastal.sh           # Coastal fix script
├── scripts/                     # Data preparation scripts
│   ├── borders/                 # Border extraction
│   ├── facilities/              # Airport/facility data fetching
│   ├── interop_export/          # Border vector interop export
│   ├── visualization/           # GeoJSON/heatmap rendering
│   ├── verify.py                # Tile verification
│   ├── quad_view.py             # Quad-view rendering
│   └── cleanup_for_rebake.py    # Re-bake cleanup
├── data/                        # Source data (city list, indices)
├── terrain.dat                  # Global terrain pack file
└── population.dat               # Global population pack file
```

## 🔧 Architecture

### QTR5 Format (16-bit Nodes)

```
Terrain leaf: [1bit is_leaf=1][11bit elevation(non-linear)][2bit gradient][2bit zone]
Population leaf: [1bit is_leaf=1][12bit pop density(log)][3bit urban type]
Branch node:  [1bit is_leaf=0][15bit subtree_size]
```

- **Non-linear elevation**: 0-511m @ 1m precision, 512-1535m @ 2m, 1536-3583m @ 4m, 3584-8190m @ 8m
- **Population encoding**: 12-bit log (`log1p(density) × 355.7`), covering 1-100,000/km²
- **DFS pre-order traversal**: Navigate by skipping subtrees via subtree_size, O(depth) complexity

### Data Pipeline

```
STAC API (CopDEM/ESA) ──┐
WorldPop ArcGIS ────────┤──→ Download ──→ Align ──→ Fix ──→ Quadtree ──→ Pack ──→ .dat
                        └  (no OSM fallback; land cover = ESA PC only)
```

### Fallback Chains

- **DEM**: Planetary Computer → Element84 → Open-Elevation
- **Land cover**: ESA WorldCover (Planetary Computer)

### NO_DATA Tile Handling

When DEM download fails (NO_DATA), instead of writing water tiles directly:
1. Download ESA WorldCover zone data to determine water/land
2. If water ratio > 95%, write water tile
3. Otherwise, return no_data status for later processing
4. Use `--no-data-water` flag to skip zone download and write water tiles directly

### Coastal City Correction

Typical case: Reykjavik (64.1°N, -21.9°W)
- DEM shows 0m or negative elevation (coastline precision issue)
- ESA WorldCover 100m resolution may label coastal urban area as water
- WorldPop shows significant population (default fix threshold **>10/km²**)
- **Logic**: Population proves human habitation → area should be land, not water

Correction pipeline (execution order):
1. `fix_water_consistency` — Population as ground truth, fix zone=WATER with pop>10 to ZONE_NATURAL
2. `_enforce_water_value_consistency` — Force DEM≤0 and pop≤1.0 pixels to WATER
3. `_enforce_water_zone_consistency` — Zero pop/urban for all zone=WATER pixels
4. `build_adaptive_tree` — During quadtree build, if leaf majority is WATER but has pop>0 pixels, revert to non-water zone

## 📦 Output Format

### terrain.dat / population.dat (GeoPack)

| Section | Size | Description |
|---------|------|-------------|
| Header | 32 bytes | Magic, grid dimensions, tile count, flags |
| Index | 1,036,800 bytes | 360×180 grid of (offset, size) pairs |
| Data | Variable | zstd-compressed tile data blocks |

### Tile Binary (QTR5)

- **Water tile**: 1 byte (`0xFF`)
- **Data tile**: 2×N bytes (16-bit node array, DFS pre-order)

## 📄 License

MIT License — See [LICENSE](LICENSE)
