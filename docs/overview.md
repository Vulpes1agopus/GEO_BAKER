# Geo Baker 总览

## 目标

将 CopDEM（30m 海拔）、WorldPop（1km 人口）、ESA WorldCover（100m 地表）烘焙为 **QTR5** 四叉树瓦片（每瓦片 1°×1°），供游戏运行时 O(depth) 导航查询。打包后为 **GeoPack**（`terrain.dat` / `population.dat`，zstd + 360×180 索引）。

## 模块结构

| 模块 | 职责 |
|------|------|
| `geo_baker_pkg/core.py` | 常量、海拔/人口编码、统一四叉树构建与导航、`verify_tile` |
| `geo_baker_pkg/pipeline.py` | STAC 下载、对齐、`fix_water_consistency`、全局/区域烘焙 |
| `geo_baker_pkg/io.py` | GeoPack 打包/解压查询 |
| `geo_baker_pkg/cli.py` | 命令行入口 |

## 数据管线（单瓦片）

1. `is_likely_ocean`：无 ESA 陆地索引则先拉 ESA，水体占比 >95% 写 1 字节水瓦片。
2. 并发下载 DEM（E84→PC→Open-Elevation）、WorldPop、ESA。
3. `align_tile_data`：统一到 `TARGET_SIZE`（默认 **1024**，2 的幂，四叉划分对齐）。
4. **`fix_water_consistency`**（三步，顺序固定）  
   - **海岸/河边/湖边**：`zone==WATER` 且 `pop > 10`（/km²）→ `NATURAL`（人口为真值，避免城区被标成水）。  
   - **海拔**：`elev > 0` 且 `zone==WATER` → `NATURAL`。  
   - **真水体**：剩余 `WATER` 上 `pop/urban` 清零。
5. `build_adaptive_tree` / `build_adaptive_pop_tree`：前序 BRANCH + 子树大小；烘焙后 **`verify_tile` 同步校验**。

## 水陆边界（海边 / 湖边 / 河边）

- **栅格级**：`fix_water_consistency` 用人口压低误标水域（阈值默认 **10** /km²，与叶节点 `_COASTAL_POP_PX` 一致）。
- **叶节点级**：若众数为 `WATER` 但格内任一点 `pop > 10`，叶节点改为 `NATURAL`，避免四叉树粗化后仍把城点读成水。
- **分裂级**：水陆混合区域 DEM 方差阈值 **`_WATER_MIX_VAR = 3.0`**（低于纯陆地的 1.0 不适用；混合区单独阈值），比旧版 5.0 更细，利于岸线、河湖过渡带。

详见 [technical_reference.md](technical_reference.md)。

## CLI 速查

```bash
python -m geo_baker_pkg --tile 116,39
python -m geo_baker_pkg --global --workers 8
python -m geo_baker_pkg --pack && python -m geo_baker_pkg --pack-pop
python tools/verify_cities.py --cities data/global_cities.json --min-pop 0
```

## 抽样验证与出图

- **5000 城**：`python tools/verify_cities.py --cities data/global_cities.json`  
  最近一次全量结果：**867 OK**、**0 警告/问题**、**4133 缺瓦片**（该格尚无 `.qtree`，需烘焙）；无「水域 + 高人口」严重冲突。
- **全球预览**（依赖已打包 `terrain.dat` / `population.dat` 与 `tiles/`；图面随数据版本变化）：

```bash
python tools/visualize.py overview --pack terrain.dat -o global_elevation.png
python tools/visualize.py quad-overview -o global_quad_overview.png
python tools/visualize.py population --bbox -90 -180 90 180 --resolution 120 -o global_population.png
python tools/visualize.py zones --bbox -90 -180 90 180 --resolution 120 -o global_zones.png
python tools/visualize.py elevation --bbox -90 -180 90 180 --resolution 200 -o global_elevation_detail.png
```

- **瓦片校验 / 修复**（不要整库乱删）：`python tools/geo_inspect.py validate`；人口与 zone 冲突：`python -m geo_baker_pkg --fix-pop-zone`；失败重试：`python -m geo_baker_pkg --retry-errors`。
- **误删已由 Git 跟踪的 `tiles/` 文件**：从带瓦片快照恢复，例如 `git checkout a423a2a6f -- tiles/`（会覆盖工作区里与索引同名的瓦片文件，执行前请确认无未提交的重要本地改动）。

## Agent 提示（精简）

- 象限顺序：NW=0, NE=1, SW=2, SE=3；构建与导航必须一致。
- `subtree_size` 必须等于该子树节点总数；旧瓦片若 root 不匹配则 `navigate_*` 返回 `None`，需重烘。
- 修改分裂或 `MAX_NODES` 时注意 15bit `subtree_size` 上限 32767。

## 历史长文

根目录 `ARCHITECTURE_ANALYSIS.md`、`GEO_BAKER_WORK_SUMMARY.md`、`FUNCTION_ARCHITECTURE.md`、`AGENT_PROMPT.md` 中的详细排障与版本对比已迁移思路至此；原文档保留为简短跳转，避免多处重复维护。
