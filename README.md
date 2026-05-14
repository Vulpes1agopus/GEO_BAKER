# GeoBaker

将 CopDEM 海拔、WorldPop 人口、ESA WorldCover 地表类型烘焙为紧凑的二进制四叉树瓦片（QTR5 格式），用于游戏、仿真和离线地理查询。

## 特性

- **多源数据融合**：DEM (30m) + 人口 (1km) + 地表类型 (100m)，默认使用公开数据源，无需项目私有凭据。
- **自适应四叉树 (QTR5)**：16bit 节点、非线性海拔编码、动态节点预算。
- **紧凑二进制格式**：支持 zstd 压缩的 GeoPack，360 x 180 网格索引，适合随机访问。
- **水陆一致性处理**：NO_DATA 瓦片可结合 ESA WorldCover 判断水陆，避免盲写水瓦片。
- **沿海城市修正**：用人口栅格辅助修正海岸线附近的水陆误判。
- **增量打包**：仅重新打包新增或变更瓦片。
- **诊断工具**：查询、验证、大小分析、城市抽样验证和可视化出图。

## 数据源

| 数据 | 来源 | 分辨率 | 认证 |
| --- | --- | --- | --- |
| CopDEM GLO-30 | Planetary Computer / Element84 STAC | 30m | 无需项目私有凭据 |
| Open-Elevation | REST API（降级备用） | ~100m | 无需项目私有凭据 |
| WorldPop | ArcGIS ImageServer | 1km | 无需项目私有凭据 |
| ESA WorldCover | Planetary Computer STAC | 100m | 无需项目私有凭据 |

## 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

也可以以可编辑模式安装：

```bash
pip install -e .
```

烘焙单个瓦片：

```bash
python -m geo_baker_pkg --tile 116,39
```

查询一个点：

```bash
python -m geo_baker_pkg --query 39.9 116.4
```

查看统计：

```bash
python -m geo_baker_pkg --stats
```

试运行（估算，不下载）：

```bash
python -m geo_baker_pkg --global --dry-run
```

## 用法

### 烘焙（数据处理）

| 命令 | 说明 |
| --- | --- |
| `--tile 116,39` | 烘焙单个 1 度瓦片 |
| `--bbox 70 20 140 55` | 烘焙区域 |
| `--global` | 烘焙全球 |
| `--global --split 2/1` | 分布式烘焙：第 1 份，共 2 份 |
| `--global --bake-ocean` | 不跳过海洋瓦片，下载 DEM 处理 |
| `--global --no-data-water` | NO_DATA 瓦片直接写水瓦片，跳过 zone 检查 |
| `--retry-errors` | 重试失败瓦片 |
| `--fix-coastal` | 检测并修复沿海问题瓦片 |
| `--fix-pop-zone` | 自动修复人口/城镇与 zone 冲突瓦片 |

### 打包（二进制导出）

| 命令 | 说明 |
| --- | --- |
| `--pack` | 打包地形为 `terrain.dat` |
| `--pack-pop` | 打包人口为 `population.dat` |
| `--incremental-pack` | 增量打包，仅处理新增或变更瓦片 |
| `--merge a.dat b.dat` | 合并两个 `.dat` 文件 |

### 查询

| 命令 | 说明 |
| --- | --- |
| `--query 39.9 116.4` | 从瓦片文件查询海拔 + 人口 |
| `--query-pack 39.9 116.4` | 从 `terrain.dat` 查询 |
| `--query-pop 39.9 116.4` | 仅查询人口 |
| `--stats` | 瓦片统计 |

### 工具

```bash
# 检查与验证
python tools/geo_inspect.py query 39.9 116.4
python tools/geo_inspect.py tile-info 116 39
python tools/geo_inspect.py stats
python tools/geo_inspect.py validate
python tools/geo_inspect.py validate --fix-ocean
python tools/geo_inspect.py size-report
python tools/verify_cities.py --cities data/global_cities.json

# 可视化
python tools/visualize.py elevation --bbox 70 20 140 55 -o china_elev.png
python tools/visualize.py population --bbox 70 20 140 55 -o china_pop.png
python tools/visualize.py zones --bbox 70 20 140 55 -o china_zones.png
python tools/visualize.py overview --pack terrain.dat -o global.png
python tools/visualize.py compare --lat 39.9 --lon 116.4 -o beijing.png

# 后台烘焙（低优先级）
bash tools/bake_background.sh
bash tools/bake_background.sh --retry
bash tools/bake_background.sh --region 70 20 140 55
```

## 项目结构

```text
GeoBaker/
├── geo_baker_pkg/               # 核心包
│   ├── core.py                  # 常量、编码、四叉树
│   ├── pipeline.py              # 数据下载、对齐、修正和烘焙编排
│   ├── io.py                    # QTR5/GeoPack 打包和查询
│   └── cli.py                   # 命令行入口
├── tools/                       # 查询、验证、可视化工具
│   ├── geo_inspect.py
│   ├── visualize.py
│   ├── verify_cities.py
│   └── bake_background.sh
├── scripts/visualization/       # 可选可视化辅助脚本
├── tests/                       # 单元测试
└── data/                        # 小型元数据，如城市验证列表
```

## 架构

### QTR5 格式（16bit 节点）

```text
地形叶节点: [1bit is_leaf=1][11bit 海拔(非线性)][2bit 坡度][2bit 区域]
人口叶节点: [1bit is_leaf=1][12bit 人口密度(对数)][3bit 城市类型]
分支节点:   [1bit is_leaf=0][15bit subtree_size]
```

- **非线性海拔**：0-511m @ 1m 精度，512-1535m @ 2m，1536-3583m @ 4m，3584-8190m @ 8m。
- **人口编码**：12bit 对数编码，覆盖低密度乡村到高密度城市。
- **DFS 前序遍历**：通过 `subtree_size` 跳过子树，点查询复杂度约为 `O(depth)`。

### 瓦片坐标

每个瓦片覆盖一个整数经纬度单元：

```text
tile lon = floor(longitude)
tile lat = floor(latitude)
terrain filename = tiles/{lon}_{lat}.qtree
population filename = tiles/{lon}_{lat}.pop
```

瓦片内部坐标归一化到 `[0, 1)`。四叉树象限顺序固定为：

```text
0 = NW
1 = NE
2 = SW
3 = SE
```

构建和读取必须保持相同象限顺序。

### 数据管线

```text
STAC API (CopDEM/ESA) ──┐
WorldPop ArcGIS ────────┤──→ 下载 ─→ 对齐 ─→ 修正 ─→ 四叉树 ─→ 打包 ─→ .dat
                        └── 降级备用数据源
```

### 降级链

- **DEM**：Planetary Computer → Element84 → Open-Elevation
- **地表类型**：ESA WorldCover

### NO_DATA 瓦片处理

当 DEM 下载失败或返回 NO_DATA 时，可以结合 ESA WorldCover 判断水陆：

1. 下载 ESA WorldCover zone 数据。
2. 若水体占比足够高，写 1 字节水瓦片。
3. 否则返回 no_data 状态，等待后续重试或人工检查。
4. `--no-data-water` 可跳过 zone 下载，直接写水瓦片，适合明确知道目标区域是海洋的批处理。

### 沿海城市修正

海岸线附近常见问题是 DEM 或地表分类把有人口的沿海城区标成水。GeoBaker 会用人口作为辅助证据：

1. `fix_water_consistency`：`zone=WATER` 且 `pop` 高于阈值的像素修正为陆地类。
2. `_enforce_water_value_consistency`：明显水体像素保留为水。
3. `_enforce_water_zone_consistency`：水体上的人口和 urban 数据清零。
4. 四叉树构建时继续保护有人口的小区域，避免粗化后又变回水。

## 输出格式

### `terrain.dat` / `population.dat` (GeoPack)

| 区域 | 大小 | 说明 |
| --- | --- | --- |
| Header | 32 bytes | Magic、网格维度、瓦片数、标志 |
| Index | 1,036,800 bytes | 360 x 180 网格的 `(offset, size)` 对 |
| Data | 可变 | zstd 压缩的瓦片数据块 |

### 瓦片二进制 (QTR5)

- **水域瓦片**：1 字节 (`0xFF`)
- **数据瓦片**：2 x N 字节，16bit 节点数组，DFS 前序

### 精度说明

- 海拔采用非线性米级桶，优先保留低海拔和常见地形精度。
- 人口采用对数密度编码，避免城市高密度区域被压扁。
- terrain zone 是分类语义层，不应当当作精确边界；游戏逻辑里更建议以海拔、坡度和人口为主判据，zone 作为辅助提示。

## 许可证

MIT License，详见 [LICENSE](LICENSE)。

---

# GeoBaker (English)

GeoBaker bakes CopDEM elevation, WorldPop population, and ESA WorldCover land cover into compact binary quadtree tiles (QTR5 format) for games, simulations, and offline geospatial queries.

## Features

- **Multi-source data fusion**: DEM (30m), population (1km), and land cover (100m), using public data sources by default.
- **Adaptive quadtree (QTR5)**: 16-bit nodes, nonlinear elevation encoding, and budget-aware splitting.
- **Compact binary format**: zstd-compressed GeoPack files with a 360 x 180 tile index.
- **Water/land consistency**: optional ESA-based handling for NO_DATA tiles instead of blindly writing water.
- **Coastal city correction**: uses population as supporting evidence for inhabited coastlines.
- **Incremental packing**: re-pack only new or changed tiles.
- **Diagnostic tools**: query, validation, size reports, city sampling, and visualization.

## Data Sources

| Data | Source | Resolution | Auth |
| --- | --- | --- | --- |
| CopDEM GLO-30 | Planetary Computer / Element84 STAC | 30m | No project-specific credential |
| Open-Elevation | REST API fallback | ~100m | No project-specific credential |
| WorldPop | ArcGIS ImageServer | 1km | No project-specific credential |
| ESA WorldCover | Planetary Computer STAC | 100m | No project-specific credential |

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Editable install:

```bash
pip install -e .
```

Bake one tile:

```bash
python -m geo_baker_pkg --tile 116,39
```

Query a point:

```bash
python -m geo_baker_pkg --query 39.9 116.4
```

Show stats:

```bash
python -m geo_baker_pkg --stats
```

Dry run:

```bash
python -m geo_baker_pkg --global --dry-run
```

## Usage

### Baking

| Command | Description |
| --- | --- |
| `--tile 116,39` | Bake one 1-degree tile |
| `--bbox 70 20 140 55` | Bake a region |
| `--global` | Bake globally |
| `--global --split 2/1` | Distributed bake, part 1 of 2 |
| `--global --bake-ocean` | Do not skip ocean tiles |
| `--global --no-data-water` | Write NO_DATA tiles as water directly |
| `--retry-errors` | Retry failed tiles |
| `--fix-coastal` | Detect and fix coastal problem tiles |
| `--fix-pop-zone` | Auto-fix population/urban vs terrain-zone conflicts |

### Packing

| Command | Description |
| --- | --- |
| `--pack` | Pack terrain into `terrain.dat` |
| `--pack-pop` | Pack population into `population.dat` |
| `--incremental-pack` | Pack only new or changed tiles |
| `--merge a.dat b.dat` | Merge two `.dat` files |

### Querying

| Command | Description |
| --- | --- |
| `--query 39.9 116.4` | Query elevation and population from tile files |
| `--query-pack 39.9 116.4` | Query from `terrain.dat` |
| `--query-pop 39.9 116.4` | Query population only |
| `--stats` | Tile statistics |

### Tools

```bash
# Inspection and validation
python tools/geo_inspect.py query 39.9 116.4
python tools/geo_inspect.py tile-info 116 39
python tools/geo_inspect.py stats
python tools/geo_inspect.py validate
python tools/geo_inspect.py validate --fix-ocean
python tools/geo_inspect.py size-report
python tools/verify_cities.py --cities data/global_cities.json

# Visualization
python tools/visualize.py elevation --bbox 70 20 140 55 -o china_elev.png
python tools/visualize.py population --bbox 70 20 140 55 -o china_pop.png
python tools/visualize.py zones --bbox 70 20 140 55 -o china_zones.png
python tools/visualize.py overview --pack terrain.dat -o global.png
python tools/visualize.py compare --lat 39.9 --lon 116.4 -o beijing.png

# Background baking
bash tools/bake_background.sh
bash tools/bake_background.sh --retry
bash tools/bake_background.sh --region 70 20 140 55
```

## Project Structure

```text
GeoBaker/
├── geo_baker_pkg/               # Core package
│   ├── core.py                  # Constants, encoding, quadtree
│   ├── pipeline.py              # Download, align, fix, and bake orchestration
│   ├── io.py                    # QTR5/GeoPack packing and querying
│   └── cli.py                   # CLI entry point
├── tools/                       # Query, validation, and visualization tools
│   ├── geo_inspect.py
│   ├── visualize.py
│   ├── verify_cities.py
│   └── bake_background.sh
├── scripts/visualization/       # Optional visualization helper scripts
├── tests/                       # Unit tests
└── data/                        # Small metadata files, such as city validation lists
```

## Architecture

### QTR5 Format (16-bit Nodes)

```text
Terrain leaf:    [1bit is_leaf=1][11bit elevation(non-linear)][2bit gradient][2bit zone]
Population leaf: [1bit is_leaf=1][12bit pop density(log)][3bit urban type]
Branch node:     [1bit is_leaf=0][15bit subtree_size]
```

- **Nonlinear elevation**: compact meter buckets for low and high terrain.
- **Population encoding**: logarithmic 12-bit density encoding.
- **DFS pre-order traversal**: navigate by skipping subtrees with `subtree_size`.

### Tile Coordinates

Each tile covers one integer-degree cell:

```text
tile lon = floor(longitude)
tile lat = floor(latitude)
terrain filename = tiles/{lon}_{lat}.qtree
population filename = tiles/{lon}_{lat}.pop
```

Coordinates inside a tile are normalized to `[0, 1)`. Quadrant order is fixed:

```text
0 = NW
1 = NE
2 = SW
3 = SE
```

The builder and reader must use the same quadrant order.

### Data Pipeline

```text
STAC API (CopDEM/ESA) ──┐
WorldPop ArcGIS ────────┤──→ Download ─→ Align ─→ Fix ─→ Quadtree ─→ Pack ─→ .dat
                        └── Fallback sources
```

### Fallback Chains

- **DEM**: Planetary Computer → Element84 → Open-Elevation
- **Land cover**: ESA WorldCover

### NO_DATA Tile Handling

When DEM download fails or returns NO_DATA, GeoBaker can use ESA WorldCover to
decide whether a tile is likely water:

1. Download ESA WorldCover zone data.
2. If water ratio is high enough, write a 1-byte water tile.
3. Otherwise return no_data for later retry or inspection.
4. `--no-data-water` skips the zone check and writes water directly, which is
   useful only when the target region is known to be ocean.

### Coastal City Correction

Coastline-adjacent data can classify inhabited land as water. GeoBaker uses
population as supporting evidence:

1. `fix_water_consistency` fixes `zone=WATER` pixels with meaningful population.
2. `_enforce_water_value_consistency` keeps obvious water as water.
3. `_enforce_water_zone_consistency` clears population and urban metadata on water.
4. Quadtree building keeps small populated areas from being averaged back into water.

## Output Format

### `terrain.dat` / `population.dat` (GeoPack)

| Section | Size | Description |
| --- | --- | --- |
| Header | 32 bytes | Magic, grid dimensions, tile count, flags |
| Index | 1,036,800 bytes | 360 x 180 grid of `(offset, size)` pairs |
| Data | Variable | zstd-compressed tile payloads |

### Tile Binary (QTR5)

- **Water tile**: 1 byte (`0xFF`)
- **Data tile**: 2 x N bytes, 16-bit node array in DFS pre-order

### Precision Notes

- Elevation uses nonlinear meter buckets to preserve useful terrain precision.
- Population uses logarithmic density buckets to preserve dense urban contrast.
- Terrain zone is a coarse categorical layer. For gameplay, prefer elevation,
  gradient, and population as primary signals, and use zone as an auxiliary hint.

## License

MIT License. See [LICENSE](LICENSE).
