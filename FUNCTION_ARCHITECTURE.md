# Geo Baker 函数架构文档

## 项目概述
Geo Baker 是一个全球地形数据烘焙工具，将 CopDEM（高程）、WorldPop（人口密度）、ESA WorldCover（土地覆盖）烘焙为 QTR5 四叉树二进制格式，用于游戏乘客流系统和铁路建设成本计算。

## 核心模块

### 1. `core.py` — 四叉树构建
核心函数：

| 函数 | 参数 | 返回 | 说明 |
|------|------|------|------|
| `build_adaptive_tree(dem, zone, pop=None)` | DEM 3600x3600, zone 3600x3600, pop | bytes (QTR5) | 构建 DEM 四叉树 |
| `build_adaptive_pop_tree(pop, urban)` | pop 3600x3600, urban 3600x3600 | bytes (QTR5) | 构建 POP 四叉树 |
| `_should_split(data, zone_data, pop_data, depth)` | 2D array | bool | 判断是否分裂 |
| `_emit_leaf(elevation, gradient, zone)` | int, int, int | bytes (2B) | 编码 DEM 节点 |
| `_emit_pop_leaf(density, urban)` | float, int | bytes (2B) | 编码 POP 节点 |
| `_compute_zone(lat, lon, x, y)` | float, float | int (0-9) | 计算 ESA zone |
| `navigate_qtr5(data, fx, fy)` | bytes, float, float | dict | 查询 DEM 节点 |
| `navigate_qtr5_pop(data, fx, fy)` | bytes, float, float | dict | 查询 POP 节点 |

#### 分裂逻辑 (`_should_split`)
```
depth < 5                    → 强制分裂
has_water + has_land:
  + pop > 10                 → 强制分裂（海岸城市）
  + depth < 7                → 强制分裂（海岸线高精度）
np.var(data) < 1.0           → 停止分裂（平坦区域）
return True                  → 继续分裂（复杂地形）
max_depth=9, max_nodes=32768
```

#### QTR5 节点格式
- 16-bit: 11-bit elevation (0-2047m) + 2-bit gradient (0-3) + 2-bit zone (0-3)
- Zone: 0=water, 1=natural, 2=urban, 3=cropland
- Gradient: 0=flat, 1=gentle, 2=steep, 3=cliff
- DFS pre-order traversal, root node omitted

#### POP 节点格式
- 12-bit density (0-1000) + 4-bit urban type (0-15)
- Urban types: 0=empty, 1=commercial, 2=industrial, 3=residential, 4=infrastructure

### 2. `pipeline.py` — 数据获取与烘焙流程

#### 关键函数

| 函数 | 说明 | 耗时 |
|------|------|------|
| `bake_global(workers, max_conn, skip_existing)` | 全局烘焙入口 | 16-40h |
| `_bake_tile_core(lat, lon, ...)` | 单瓦片烘焙 | ~19s |
| `_concurrent_download(lat, lon, max_conn)` | 并发下载 DEM+POP+ESA | 8s |
| `_download_dem(lat, lon, max_conn)` | 多源 DEM 下载（PC→E84→Open-Elevation） | 10-25s |
| `_download_pop(lat, lon)` | WorldPop 下载 | 4-10s |
| `_download_esa(lat, lon)` | ESA WorldCover 下载 | 7-21s |
| `_fetch_raster(url, bbox)` | rasterio 读取远程 COG | 5-20s |
| `_fetch_arcgis(bbox, layer)` | ArcGIS ImageServer 下载 | 3-8s |
| `_build_zone_grid(esa, pop, lat, lon)` | 从 ESA 构建 zone 网格 | 0.4s |
| `fix_coastal_cities(dem, pop, zone, urban)` | 修正海岸城市 zone | <0.1s |
| `_enforce_water_value_consistency(dem, pop, zone, urban)` | 低海拔标记为水体 | <0.1s |
| `_enforce_water_zone_consistency(dem, pop, zone, urban)` | 水体区域人口清零 | <0.1s |
| `align_tile_data(dem, pop, zone, urban, size)` | 对齐所有数组到 3600x3600 | 0.2s |

#### STAC 数据源

| 数据源 | URL | Collection | 速度 |
|--------|-----|------------|------|
| PC (Planetary Computer) | `https://planetarycomputer.microsoft.com/api/stac/v1` | cop-dem-glo-30 | 4.9 MB/s |
| E84 (Element84) | `https://earth-search.aws.element84.com/v1` | cop-dem-glo-30 | 11.4 MB/s |
| CDSE (Copernicus) | `https://stac.dataspace.copernicus.eu/v1` | cop-dem-glo-30-dged-cog | 未测试 |

#### 时间分解（单瓦片）

| 阶段 | 时间 | 占比 | 优化空间 |
|------|------|------|---------|
| 四叉树构建 (DEM + POP) | **10.2s** | **54%** | 高 — 使用 Cython/numba |
| 下载 (DEM/POP/ESA) | 8.2s | 43% | 中 — 多源并行 |
| zone_grid | 0.4s | 2% | 低 |
| align | 0.2s | 1% | 低 |
| fixes | <0.1s | 0% | 低 |

#### 缓存机制
- `_stac_catalog_cache` — 进程内 STAC catalog 缓存
- `CACHE_DIR` — DEM/POP/ESA `.npy` 本地缓存（已禁用，保护 SSD）
- `skip_existing` — 跳过已烘焙的 .qtree 文件

### 3. `cli.py` — 命令行入口

```
--global                    全局烘焙
--city FILE                 城市烘焙
--workers N                 进程数 (默认 14)
--conn N                    并发连接数 (默认 80)
--scan-grid N               扫描网格大小 (默认 12)
--fix-pop-zone              修复人口/zone 冲突
--fix-rounds N              修复轮数 (默认 3)
```

## 数据流

```
bake_global()
  ├── build_global_index()          # 构建全球陆地索引
  ├── _rank_dem_sources_main()      # 测速排序数据源
  └── _run_tile_batch(tile_list)
       └── _bake_tile_worker(lat, lon)
            └── _bake_tile_core(lat, lon)
                 ├── _concurrent_download()    # DEM + POP + ESA (8s)
                 ├── _build_zone_grid()         # zone 网格 (0.4s)
                 ├── align_tile_data()          # 对齐到 3600x3600 (0.2s)
                 ├── fix_coastal_cities()       # 海岸修正 (<0.1s)
                 ├── _enforce_water_*()         # 水体一致性 (<0.1s)
                 └── _compute_tile_from_data()
                      ├── build_adaptive_tree()  # DEM 四叉树 (5s)
                      └── build_adaptive_pop_tree() # POP 四叉树 (5s)
```

## 性能分析

### 全局烘焙速度
- 旧版：0.36-0.74 tiles/s (8 workers, 16.5h 完成 23,123 tiles)
- 新版：0.02-0.03 tiles/s (14 workers, ~30h 预计)

### 大小统计
| 版本 | 瓦片数 | 平均节点 | 平均大小 | 全球推算 |
|------|--------|---------|---------|---------|
| 旧版 | 9,708 | 17,035 | 34,090 B | 752 MB |
| 新版 | 22,229 | **17,319** | **34,652 B** | **764 MB (<1GB ✅)** |

### 为什么新版慢 25-35 倍？

1. **ProcessPoolExecutor vs multiprocessing.Pool**
   - 旧版用 `multiprocessing.Pool` (fork 模式) — 子进程共享父进程内存
   - 新版用 `ProcessPoolExecutor` (spawn 模式) — 每个 worker 独立初始化

2. **Python GIL 竞争**
   - `_concurrent_download` 使用 ThreadPoolExecutor 并发下载
   - 但 Python GIL 会让 I/O 线程阻塞，无法真正并行

3. **STAC catalog 获取**
   - 每个 worker 第一次获取 catalog 需要 2-5s
   - 14 workers × 5s = 70s 额外开销（虽然有缓存，但每个 worker 仍需第一次获取）

4. **GDAL/rasterio 初始化**
   - 每个 worker 需要单独初始化 GDAL 环境
   - 包括 HTTP 连接池、COG 元数据解析等

## 优化方向

1. **改用 `multiprocessing.Pool`** — fork 模式可节省 30-50% 启动开销
2. **使用 Cython/numba 加速四叉树构建** — 54% 时间在 quadtree build
3. **预加载 STAC catalog** — 在主进程获取，worker 复用
4. **减少下载数据源** — 如果不需要 POP/ESA，只下载 DEM 可节省 40% 时间
5. **使用 mmap 共享内存** — 避免 pickle 序列化开销

## 常量定义

| 常量 | 值 | 说明 |
|------|-----|------|
| `TARGET_SIZE` | 3600 | 瓦片目标分辨率 (3600x3600) |
| `MAX_DEPTH` | 9 | 四叉树最大深度 |
| `MAX_NODES` | 32768 | 四叉树最大节点数 |
| `_POP_NOISE_FLOOR` | 1.0 | 人口噪声阈值 |
| `_COASTAL_POP_THRESHOLD` | 10.0 | 海岸人口阈值 |
| `STAC_PC` | planetarycomputer... | Planetary Computer STAC |
| `STAC_E84` | earth-search.aws... | Element84 STAC |
| `STAC_CDSE` | stac.dataspace... | Copernicus STAC |
