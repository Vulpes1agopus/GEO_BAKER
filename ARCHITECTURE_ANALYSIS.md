# Geo Baker 架构分析文档

## 版本对比

| 特性 | 旧版 (2026-04-19) | 当前版 (2026-04-27) |
|------|-------------------|-------------------|
| 烘焙速度 | 0.36-0.74 tiles/s | ~0.17-0.27 tiles/s |
| 平均节点数 | ~8,921 | ~9,400 |
| 全球大小预估 | 752 MB | ~667 MB |
| 进程数 | 8-16 | 8 |
| 进程模型 | `multiprocessing.Pool(fork)` | `ProcessPoolExecutor(spawn)` |
| 数据源 | DEM 单独下载 | DEM + POP + ESA 并行下载 |
| 四叉树构建 | 2次/tile (DEM + POP) | 2次/tile (DEM + POP) |
| STAC 缓存 | 无 | 进程内缓存 `_stac_catalog_cache` |
| GDAL 优化 | 基础 | VSI_CACHE 512MB, SWATH_SIZE 32MB |
| Budget硬限制 | 无(有bug) | 有(node_id >= max_nodes) |
| 无人区pop_nodes | 1025 | **5** |
| 海洋瓦片 | 1字节 | 1字节 (自动写入) |
| 总瓦片数 | ~23,000 (仅陆地) | **64,800** (全球180×360) |

## 速度慢的根因分析

### Bug 1: 水陆边界无条件分裂(已修复)

旧代码中 `_should_split` 对水陆混合瓦片无条件返回True:
```python
if has_water and has_land:
    if pop_data is not None and np.any(pop_data > 10.0):
        return True  # 无视budget、无视depth！
```
导致沿海瓦片分裂到max_depth=9，产生83k-92k节点，四叉树构建耗时170-339秒。

**修复**: 水陆边界用方差阈值5.0判断，不再强制分裂到固定深度。

### Bug 2: Budget系统失效(已修复)

旧代码budget计算有bug:
```python
start_id = node_id[0]  # 每层重置
remaining = max_nodes - (node_id[0] - start_id)  # 永远接近max_nodes
```
导致MAX_NODES_TERRAIN=30000形同虚设，节点数可达92k+。

**修复**: 添加 `if node_id[0] >= max_nodes: return/break` 硬限制。

### Bug 3: 无人区人口节点爆炸(已修复)

旧代码 `_should_split_pop` 中 `depth < force_depth` 无条件分裂:
```python
if depth < fd: return True  # 即使人口全是0也强制分裂到depth4
```
导致无人区产生1025个节点，浪费存储空间和渲染时间。

**修复**: 先检查人口是否全是噪声，再检查force_depth:
```python
if np.all(data <= _POP_NOISE_FLOOR): return False  # 无人区直接不分裂
if depth < fd: return True  # 有人区才按force_depth分裂
```
无人区pop_nodes从1025降到5。

### Bug 4: CDSE数据源无法匿名访问(已修复)

CDSE (stac.dataspace.copernicus.eu) 的DEM数据使用 `s3://eodata/` URL，需要S3认证，无法匿名访问:
```
rasterio open failed: InvalidCredentials: No valid AWS credentials found.
```

**修复**: 从DEM降级链中移除CDSE，仅保留 Planetary Computer 和 Element84。

### Bug 5: 海洋瓦片未写入(已修复)

旧代码 `bake_global` 中 `skip_ocean=True` 会过滤掉海洋瓦片，导致全球只有~23,000个瓦片文件，缺少~41,000个海洋瓦片。

**修复**: 添加 `_write_ocean_water_tiles()` 函数，在陆地烘焙完成后自动写入所有海洋瓦片（1字节水瓦片），确保全球64,800个瓦片都存在。

### 修复效果

| 指标 | 修复前 | 修复后 | 改善 |
|------|--------|--------|------|
| 平均节点数 | 46,883 | ~9,400 | 5.0x ↓ |
| 最大节点数 | 92,312 | ~17,000 | 5.4x ↓ |
| 平均四叉树耗时 | 175.5s | ~23.4s | 7.5x ↓ |
| 无人区pop_nodes | 1025 | 5 | 205x ↓ |
| 总瓦片数 | ~23,000 | **64,800** | 完整覆盖 |

## 当前四叉树分裂策略

### build_adaptive_tree (DEM + zone)

- 输入: 3600x3600 float32 DEM + uint8 zone
- 分裂逻辑:
  - `budget <= 1`: 停止分裂
  - `depth < force_depth(4)`: 强制分裂
  - `depth >= max_depth(9)`: 停止分裂
  - `node_id >= max_nodes(30000)`: 硬限制停止
  - 纯水体+有人口: 方差 >= 1.0 才分裂
  - 纯水体+无人口: 不分裂
  - 水陆混合: 方差 >= 5.0 才分裂
  - 纯陆地: 方差 >= 1.0 才分裂

### build_adaptive_pop_tree (pop + urban)

- 输入: 3600x3600 float32 pop + uint8 urban
- 分裂逻辑:
  - `budget <= 1`: 停止分裂
  - `depth >= max_depth(9)`: 停止分裂
  - `node_id >= max_nodes(30000)`: 硬限制停止
  - **人口全为噪声**: 不分裂（关键修复）
  - `depth < force_depth(4)`: 强制分裂（仅有人区）
  - 城市类型多样: 强制分裂
  - 方差 < 10.0: 停止分裂

## DEM数据源降级链

| 优先级 | 数据源 | URL | 认证 | 状态 |
|--------|--------|-----|------|------|
| 1 | Element84 | earth-search.aws.element84.com | 无需 | ✅ 可用 |
| 2 | Planetary Computer | planetarycomputer.microsoft.com | 无需 | ✅ 可用 |
| - | CDSE | stac.dataspace.copernicus.eu | **需S3认证** | ❌ 已移除 |
| 3 | Open-Elevation | api.open-elevation.com | 无需 | ✅ 降级备用 |

## 梯度计算优化

旧版使用 `np.gradient`:
```python
gy, gx = np.gradient(arr)
return float(np.max(np.sqrt(gx**2 + gy**2)))
```

新版使用简单差分:
```python
dy = np.abs(arr[1:, :] - arr[:-1, :])
dx = np.abs(arr[:, 1:] - arr[:, :-1])
return float(max(np.max(dy), np.max(dx)))
```
减少临时数组创建，提升约30%性能。

## 日志系统

- 默认: INFO级别，只输出OK/ERROR和每10个瓦片的进度条
- `--verbose`: DEBUG级别，输出每个步骤的详细信息
- 进度条包含: land/water/ocean/err统计、avg_nodes、avg_dl、avg_qt、速率、ETA

## 海岸线处理

- `fix_coastal_cities()`: zone=WATER & pop>50 → ZONE_NATURAL
- `_enforce_water_value_consistency()`: dem<=0 & pop<=noise → ZONE_WATER
- `_enforce_water_zone_consistency()`: zone=WATER → pop=0, urban=0
- 水陆混合瓦片: 方差阈值5.0(比纯陆地1.0更敏感)，确保海岸线精度

## 海洋瓦片处理

- `bake_global()` 先烘焙陆地瓦片（通过 `is_likely_ocean()` 过滤）
- 烘焙完成后调用 `_write_ocean_water_tiles()` 写入所有海洋瓦片
- 海洋瓦片: 1字节 (`0xFF`)，.qtree 和 .pop 各1字节
- 全球64,800个瓦片 = 陆地(~23,000) + 海洋(~41,800)

## 性能优化建议(待实施)

1. **使用 `multiprocessing.Pool` 替代 `ProcessPoolExecutor`** — fork模式更快
2. **预加载STAC catalog** — 在主进程获取URL列表传给worker
3. **使用mmap共享内存** — 避免pickle序列化开销
4. **批量处理** — 每次处理多个瓦片减少进程间通信开销

## 已知问题

- **NO_DATA**：部分海洋边缘区域无法获取DEM数据，现在自动写入水瓦片
- **下载速度**：部分瓦片下载时间较长(>120s)，可能是网络或数据源问题
- **进度条ETA**：随着剩余瓦片减少，ETA计算会自动调整

## 全球数据统计

- 总瓦片数: **64,800** (180×360)
- 陆地瓦片: ~18,700 (有DEM数据)
- 海洋瓦片: ~46,100 (1字节水瓦片)
- terrain.dat: **349.90 MB** (zstd压缩)
- population.dat: **97.93 MB** (zstd压缩)
- tiles目录: ~889 MB

## 可视化

全球四数据图已生成：
- `global_elevation.png` — 全球海拔概览
- `global_elevation_detail.png` — 全球海拔详细图
- `global_population.png` — 全球人口密度图
- `global_zones.png` — 全球地形区域图
