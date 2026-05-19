#!/usr/bin/env python3
"""
Geo-Data Visualizer - Generate map previews and heatmaps / 地理数据可视化 - 生成地图预览与热力图

Inspired by OpenFrontMapGenerator's preview capabilities.

Usage / 用法:
    python tools/visualize.py elevation --bbox 70 20 140 55 -o china_elev.png
    python tools/visualize.py population --bbox 70 20 140 55 -o china_pop.png
    python tools/visualize.py zones --bbox 70 20 140 55 -o china_zones.png
    python tools/visualize.py overview --pack terrain.dat -o global_overview.png
    python tools/visualize.py compare --lat 39.9 --lon 116.4 -o beijing_compare.png
"""
import os
import sys
import struct
import argparse
import json
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap, BoundaryNorm, LinearSegmentedColormap
from matplotlib.collections import LineCollection

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from geo_baker_pkg.core import (
    TILE_DIR, ZONE_WATER, ZONE_NATURAL, ZONE_FOREST, ZONE_HARSH,
    ZONE_NAMES, GRADIENT_NAMES, URBAN_NAMES,
    _GPK_MAGIC, _POP_MAGIC, _GPK_HEADER_SIZE, _GPK_GRID_W, _GPK_GRID_H, _GPK_INDEX_SIZE,
    decode_node_16, decode_pop_leaf_node, decode_elevation, decode_pop_density,
    navigate_qtr5, navigate_qtr5_pop,
)
from geo_baker_pkg.io import query_elevation, query_population
import geo_baker_pkg.core as gb_core
import geo_baker_pkg.io as gb_io

plt.rcParams["font.sans-serif"] = ["Noto Sans CJK SC", "Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


# ── Color Palettes / 调色板 ────────────────────────────────────────

WATER_COLOR = "#274b9f"
ZERO_LAND_COLOR = "#b0005a"
ELEVATION_LAND_CMAP = LinearSegmentedColormap.from_list(
    "geobaker_land_elevation",
    [
        "#d7c58a",
        "#8abf62",
        "#d6cf72",
        "#a67854",
        "#f2f0e8",
    ],
    N=256,
)
POPULATION_CMAP = "magma"
ZONE_COLORS = ["#1a5276", "#f9e79f", "#27ae60", "#7b241c"]
ZONE_CMAP = ListedColormap(ZONE_COLORS)
ZONE_NORM = BoundaryNorm([-0.5, 0.5, 1.5, 2.5, 3.5], ZONE_CMAP.N)

GRADIENT_COLORS = ["#2ecc71", "#f1c40f", "#e67e22", "#e74c3c"]
GRADIENT_CMAP = ListedColormap(GRADIENT_COLORS)
GRADIENT_NORM = BoundaryNorm([-0.5, 0.5, 1.5, 2.5, 3.5], GRADIENT_CMAP.N)

URBAN_COLORS = ["#111111", "#ffd60a", "#00bbf9", "#fb5607", "#8338ec", "#80ed99", "#ef476f", "#adb5bd"]
URBAN_CMAP = ListedColormap(URBAN_COLORS)
URBAN_CMAP.set_bad("#ff00ff")
URBAN_NORM = BoundaryNorm([-0.5, 0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5], URBAN_CMAP.N)
URBAN_LABELS = ["None", "Residential", "Commercial", "Industrial", "Mixed", "Institutional", "Reserved", "Reserved"]


def _draw_elevation(ax, elev_grid, extent, zone_grid=None, interpolation="nearest", title=None):
    """Draw elevation with explicit water and suspect zero-land colors."""
    elev = np.asarray(elev_grid, dtype=np.float32)
    water = np.isfinite(elev) & (elev <= 0)
    if zone_grid is not None:
        water = water | (np.isfinite(zone_grid) & (np.asarray(zone_grid) == ZONE_WATER))
    suspect_zero_land = np.zeros_like(water, dtype=bool)

    ax.set_facecolor(WATER_COLOR)
    if np.any(water):
        water_layer = np.ma.masked_where(~water, np.ones_like(elev, dtype=np.float32))
        ax.imshow(
            water_layer, origin="lower", extent=extent,
            cmap=ListedColormap([WATER_COLOR]), interpolation="nearest", aspect="auto"
        )

    land = np.ma.masked_where(water | suspect_zero_land | ~np.isfinite(elev), elev)
    finite_land = np.asarray(land.compressed(), dtype=np.float32)
    vmax = float(np.nanpercentile(finite_land, 99.5)) if finite_land.size else 1.0
    im = ax.imshow(
        land, origin="lower", extent=extent,
        cmap=ELEVATION_LAND_CMAP, vmin=0, vmax=max(vmax, 1.0),
        interpolation=interpolation, aspect="auto"
    )

    if np.any(suspect_zero_land):
        bad_layer = np.ma.masked_where(~suspect_zero_land, np.ones_like(elev, dtype=np.float32))
        ax.imshow(
            bad_layer, origin="lower", extent=extent,
            cmap=ListedColormap([ZERO_LAND_COLOR]), interpolation="nearest", aspect="auto"
        )
    if title:
        ax.set_title(title)
    return im


def _sample_grid(bbox, resolution, query_fn):
    """Sample a grid of values from query function / 从查询函数采样网格"""
    lat_min, lon_min, lat_max, lon_max = bbox
    lats = np.linspace(lat_min, lat_max, resolution)
    lons = np.linspace(lon_min, lon_max, resolution)
    grid = np.full((resolution, resolution), np.nan, dtype=np.float32)

    for i, lat in enumerate(lats):
        for j, lon in enumerate(lons):
            result = query_fn(lat, lon)
            if result is not None:
                grid[i, j] = result

    return lats, lons, grid


def _sample_elevation_grid(bbox, resolution):
    """Sample elevation grid / 采样海拔网格"""
    return _sample_grid(bbox, resolution, lambda lat, lon: (query_elevation(lat, lon) or {}).get('elevation', np.nan))


def _sample_elevation_zone_grids(bbox, resolution):
    lat_min, lon_min, lat_max, lon_max = bbox
    lats = np.linspace(lat_min, lat_max, resolution)
    lons = np.linspace(lon_min, lon_max, resolution)
    elev = np.full((resolution, resolution), np.nan, dtype=np.float32)
    zone = np.full((resolution, resolution), np.nan, dtype=np.float32)
    for i, lat in enumerate(lats):
        for j, lon in enumerate(lons):
            result = query_elevation(lat, lon)
            if result is not None:
                elev[i, j] = result.get('elevation', np.nan)
                zone[i, j] = result.get('zone', np.nan)
    return lats, lons, elev, zone


def _sample_population_grid(bbox, resolution):
    """Sample population grid / 采样人口网格"""
    return _sample_grid(bbox, resolution, lambda lat, lon: (query_population(lat, lon) or {}).get('pop_density', np.nan))


def _sample_asset_grids(bbox, width, height):
    """Sample all public export layers in one pass / 一次采样所有公开导出图层"""
    lat_min, lon_min, lat_max, lon_max = bbox
    lats = np.linspace(lat_min, lat_max, height)
    lons = np.linspace(lon_min, lon_max, width)
    elev = np.full((height, width), np.nan, dtype=np.float32)
    zone = np.full((height, width), ZONE_WATER, dtype=np.uint8)
    water = np.zeros((height, width), dtype=np.uint8)
    pop = np.zeros((height, width), dtype=np.float32)
    urban = np.zeros((height, width), dtype=np.uint8)

    total = width * height
    done = 0
    report_step = max(1, total // 50)
    for i, lat in enumerate(lats):
        for j, lon in enumerate(lons):
            terrain = query_elevation(lat, lon) or {}
            population = query_population(lat, lon) or {}
            e = terrain.get("elevation", np.nan)
            z = int(terrain.get("zone", ZONE_WATER))
            elev[i, j] = e
            zone[i, j] = np.uint8(np.clip(z, 0, 3))
            water[i, j] = 1 if z == ZONE_WATER or (np.isfinite(e) and e <= 0) else 0
            pop[i, j] = float(population.get("pop_density", 0.0) or 0.0)
            urban[i, j] = np.uint8(np.clip(int(population.get("urban_zone", 0) or 0), 0, 7))
            done += 1
            if done % report_step == 0 or done == total:
                pct = done / total * 100.0
                print(f"  Sampling assets: {pct:5.1f}% ({done}/{total})", end="\r")
    print()
    return lats, lons, elev, zone, water, pop, urban


def _encode_gray(arr, bit_depth):
    arr = np.clip(np.nan_to_num(arr, nan=0.0), 0.0, 1.0)
    if bit_depth == 8:
        return (arr * 255.0 + 0.5).astype(np.uint8), "L"
    if bit_depth == 16:
        return (arr * 65535.0 + 0.5).astype(np.uint16), "I;16"
    raise ValueError("bit_depth must be 8 or 16")


def _save_gray_png(path, normalized, bit_depth):
    from PIL import Image

    encoded, mode = _encode_gray(normalized, bit_depth)
    Image.fromarray(encoded, mode=mode).save(path)


def _save_index_png(path, arr):
    from PIL import Image

    Image.fromarray(np.asarray(arr, dtype=np.uint8), mode="L").save(path)


def _format_bytes(num):
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(num)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}"
        value /= 1024.0


def cmd_export_map_assets(args):
    """Export game-friendly raster layers / 导出游戏友好的栅格图层"""
    bbox = tuple(args.bbox)
    resolution = max(16, int(args.resolution))
    width = max(16, int(args.width or resolution))
    height = max(16, int(args.height or resolution))
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = args.prefix

    _, _, elev, zone, water, pop, urban = _sample_asset_grids(bbox, width, height)
    finite_elev = elev[np.isfinite(elev)]
    land_elev = elev[(water == 0) & np.isfinite(elev)]
    if args.min_elevation is not None:
        elev_min = float(args.min_elevation)
    elif land_elev.size:
        elev_min = float(np.nanmin(land_elev))
    elif finite_elev.size:
        elev_min = float(np.nanmin(finite_elev))
    else:
        elev_min = 0.0

    if args.max_elevation is not None:
        elev_max = float(args.max_elevation)
    elif land_elev.size:
        elev_max = float(np.nanpercentile(land_elev, float(args.percentile_max)))
    elif finite_elev.size:
        elev_max = float(np.nanpercentile(finite_elev, float(args.percentile_max)))
    else:
        elev_max = 1.0
    if elev_max <= elev_min:
        elev_max = elev_min + 1.0

    height_source = elev.copy()
    if not args.no_water_zero:
        height_source[water == 1] = elev_min
    height_norm = (height_source - elev_min) / (elev_max - elev_min)

    pop_nonzero = pop[np.isfinite(pop) & (pop > 0)]
    pop_max = float(np.nanpercentile(pop_nonzero, float(args.population_percentile))) if pop_nonzero.size else 1.0
    pop_scale_max = max(np.log1p(pop_max), 1.0)
    pop_norm = np.log1p(np.clip(pop, 0.0, None)) / pop_scale_max

    height_path = out_dir / f"{prefix}_heightmap_u{args.bit_depth}.png"
    pop_path = out_dir / f"{prefix}_population_log_u{args.bit_depth}.png"
    water_path = out_dir / f"{prefix}_water_mask.png"
    zone_path = out_dir / f"{prefix}_zone_index.png"
    urban_path = out_dir / f"{prefix}_urban_index.png"
    metadata_path = out_dir / f"{prefix}_metadata.json"

    _save_gray_png(height_path, height_norm, args.bit_depth)
    _save_gray_png(pop_path, pop_norm, args.bit_depth)
    _save_index_png(water_path, water * 255)
    _save_index_png(zone_path, zone)
    _save_index_png(urban_path, urban)

    metadata = {
        "bbox": {"south": bbox[0], "west": bbox[1], "north": bbox[2], "east": bbox[3]},
        "resolution": {"width": width, "height": height},
        "heightmap": {
            "file": height_path.name,
            "bit_depth": args.bit_depth,
            "encoding": "linear normalized elevation",
            "min_elevation_m": elev_min,
            "max_elevation_m": elev_max,
            "water_encoded_as_min": not args.no_water_zero,
        },
        "population": {
            "file": pop_path.name,
            "bit_depth": args.bit_depth,
            "encoding": "log1p(pop_density) normalized",
            "percentile_max": float(args.population_percentile),
            "pop_density_max_for_scale": pop_max,
        },
        "water_mask": {"file": water_path.name, "values": {"0": "land", "255": "water"}},
        "zone_index": {"file": zone_path.name, "values": {str(i): name for i, name in enumerate(ZONE_NAMES)}},
        "urban_index": {"file": urban_path.name, "values": {str(i): name for i, name in enumerate(URBAN_NAMES)}},
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    raw_bytes = width * height * ((2 if args.bit_depth == 16 else 1) * 2 + 3)
    print(f"Exported / 已导出: {out_dir}")
    for path in (height_path, pop_path, water_path, zone_path, urban_path, metadata_path):
        print(f"  {path.name}: {_format_bytes(path.stat().st_size)}")
    print(f"Approx raw layer memory / 近似未压缩图层内存: {_format_bytes(raw_bytes)}")


def _sample_zone_grid(bbox, resolution):
    """Sample zone grid / 采样区域网格"""
    return _sample_grid(bbox, resolution, lambda lat, lon: (query_elevation(lat, lon) or {}).get('zone', np.nan))


def _sample_gradient_grid(bbox, resolution):
    """Sample gradient grid / 采样坡度网格"""
    return _sample_grid(bbox, resolution, lambda lat, lon: (query_elevation(lat, lon) or {}).get('gradient_level', np.nan))


def _load_tile_blob(tile_dir, lon_int, lat_int, kind):
    ext = ".qtree" if kind == "terrain" else ".pop"
    path = Path(tile_dir) / f"{lon_int}_{lat_int}{ext}"
    if not path.exists():
        return None
    return path.read_bytes()


def _iter_leaf_boxes(nodes_raw, decode_fn, lat_min, lat_max, lon_min, lon_max):
    node_count = len(nodes_raw) // 2
    if node_count == 0:
        return

    def rec(pos, la0, la1, lo0, lo1):
        if pos >= node_count:
            return pos
        node = decode_fn(nodes_raw[pos * 2: pos * 2 + 2])
        if node.get("is_leaf", False):
            yield (la0, la1, lo0, lo1)
            return pos + 1

        mid_la = (la0 + la1) * 0.5
        mid_lo = (lo0 + lo1) * 0.5
        nxt = pos + 1
        # child order: NW, NE, SW, SE
        for child_bounds in (
            (mid_la, la1, lo0, mid_lo),
            (mid_la, la1, mid_lo, lo1),
            (la0, mid_la, lo0, mid_lo),
            (la0, mid_la, mid_lo, lo1),
        ):
            sub = rec(nxt, *child_bounds)
            nxt = yield from sub
        return nxt

    yield from rec(0, lat_min, lat_max, lon_min, lon_max)


def _collect_boundary_segments(bbox, tile_dir, kind="pop", max_leaf_lines=25000):
    lat0, lon0, lat1, lon1 = bbox
    segs = []
    lat_start, lat_end = int(np.floor(lat0)), int(np.floor(lat1))
    lon_start, lon_end = int(np.floor(lon0)), int(np.floor(lon1))
    decode_fn = decode_node_16 if kind == "terrain" else decode_pop_leaf_node

    for lat_i in range(lat_start, lat_end + 1):
        for lon_i in range(lon_start, lon_end + 1):
            blob = _load_tile_blob(tile_dir, lon_i, lat_i, kind)
            if not blob or len(blob) <= 1:
                continue
            for la0, la1, lo0, lo1 in _iter_leaf_boxes(blob, decode_fn, lat_i, lat_i + 1, lon_i, lon_i + 1):
                if la1 < lat0 or la0 > lat1 or lo1 < lon0 or lo0 > lon1:
                    continue
                segs.append(((lo0, la0), (lo1, la0)))
                segs.append(((lo1, la0), (lo1, la1)))
                segs.append(((lo1, la1), (lo0, la1)))
                segs.append(((lo0, la1), (lo0, la0)))
                if len(segs) >= max_leaf_lines:
                    return segs
    return segs


def cmd_elevation(args):
    """Render elevation heatmap / 渲染海拔热力图"""
    bbox = (args.south, args.west, args.north, args.east)
    lats, lons, grid, zone_grid = _sample_elevation_zone_grids(bbox, args.resolution)

    fig, ax = plt.subplots(figsize=(12, 8))
    im = _draw_elevation(
        ax, grid, [args.west, args.east, args.south, args.north], zone_grid,
        interpolation="nearest",
        title=f"Elevation / 海拔 ({args.west}E-{args.east}E, {args.south}N-{args.north}N)",
    )
    ax.set_xlabel("Longitude / 经度")
    ax.set_ylabel("Latitude / 纬度")
    fig.colorbar(im, ax=ax, label="Land elevation (m) / 陆地海拔(米)", shrink=0.8)
    fig.tight_layout()
    fig.savefig(args.output, dpi=args.dpi)
    plt.close(fig)
    print(f"Saved / 已保存: {args.output}")


def cmd_population(args):
    """Render population density heatmap / 渲染人口密度热力图"""
    bbox = (args.south, args.west, args.north, args.east)
    lats, lons, grid = _sample_population_grid(bbox, args.resolution)

    fig, ax = plt.subplots(figsize=(12, 8))
    vmax = float(np.nanpercentile(grid, 99)) if np.any(~np.isnan(grid)) else 1.0
    im = ax.imshow(grid, origin="lower", extent=[args.west, args.east, args.south, args.north],
                   cmap=POPULATION_CMAP, vmin=0, vmax=max(vmax, 1), interpolation="nearest", aspect="auto")
    ax.set_xlabel("Longitude / 经度")
    ax.set_ylabel("Latitude / 纬度")
    ax.set_title(f"Population Density / 人口密度 ({args.west}E-{args.east}E, {args.south}N-{args.north}N)")
    fig.colorbar(im, ax=ax, label="Pop density (/km²) / 人口密度", shrink=0.8)
    fig.tight_layout()
    fig.savefig(args.output, dpi=args.dpi)
    plt.close(fig)
    print(f"Saved / 已保存: {args.output}")


def cmd_zones(args):
    """Render terrain zone map / 渲染地形区域图"""
    bbox = (args.south, args.west, args.north, args.east)
    lats, lons, grid = _sample_zone_grid(bbox, args.resolution)

    fig, ax = plt.subplots(figsize=(12, 8))
    im = ax.imshow(grid, origin="lower", extent=[args.west, args.east, args.south, args.north],
                   cmap=ZONE_CMAP, norm=ZONE_NORM, interpolation="nearest", aspect="auto")
    ax.set_xlabel("Longitude / 经度")
    ax.set_ylabel("Latitude / 纬度")
    ax.set_title(f"Terrain Zones / 地形区域 ({args.west}E-{args.east}E, {args.south}N-{args.north}N)")
    cbar = fig.colorbar(im, ax=ax, shrink=0.8, ticks=[0, 1, 2, 3])
    cbar.ax.set_yticklabels(["Water / 水体", "Natural / 自然", "Forest / 森林", "Harsh / 严酷"])
    fig.tight_layout()
    fig.savefig(args.output, dpi=args.dpi)
    plt.close(fig)
    print(f"Saved / 已保存: {args.output}")


def cmd_overview(args):
    """Render global overview from .dat pack / 从.dat包渲染全球概览"""
    pack_path = Path(args.pack)
    if not pack_path.exists():
        print(f"  Pack file not found / 包文件未找到: {pack_path}")
        return

    samples_per_tile = getattr(args, 'samples_per_tile', 4)
    h = 180 * samples_per_tile
    w = 360 * samples_per_tile
    step = 1.0 / samples_per_tile

    from geo_baker_pkg.io import GeoPackReader
    with GeoPackReader(pack_path) as reader:
        grid = np.full((h, w), np.nan, dtype=np.float32)
        for lat in range(-90, 90):
            for lon in range(-180, 180):
                for si in range(samples_per_tile):
                    for sj in range(samples_per_tile):
                        q_lat = lat + (si + 0.5) * step
                        q_lon = lon + (sj + 0.5) * step
                        node = reader.query_terrain(q_lat, q_lon)
                        if node and node.get('is_leaf'):
                            row = (lat + 90) * samples_per_tile + si
                            col = (lon + 180) * samples_per_tile + sj
                            grid[row, col] = node.get('elevation', 0)

    fig, ax = plt.subplots(figsize=(36, 18))
    im = _draw_elevation(
        ax, grid, [-180, 180, -90, 90], None,
        interpolation="nearest", title="Global Elevation Overview"
    )
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    fig.colorbar(im, ax=ax, label="Land elevation (m)", shrink=0.6)
    fig.tight_layout()
    fig.savefig(args.output, dpi=args.dpi)
    plt.close(fig)
    print(f"Saved / 已保存: {args.output} ({w}x{h})")


def cmd_quad_overview(args):
    terrain_path = Path(args.terrain_pack)
    pop_path = Path(args.pop_pack)
    if not terrain_path.exists():
        print(f"  Terrain pack not found: {terrain_path}")
        return
    if not pop_path.exists():
        print(f"  Population pack not found: {pop_path}")
        return

    spt = max(2, int(args.samples_per_tile))
    h = 180 * spt
    w = 360 * spt
    step = 1.0 / spt
    total = 180 * 360
    done = 0

    elev_grid = np.full((h, w), np.nan, dtype=np.float32)
    zone_grid = np.full((h, w), np.nan, dtype=np.float32)
    urban_grid = np.full((h, w), np.nan, dtype=np.float32)
    pop_grid = np.full((h, w), np.nan, dtype=np.float32)

    from geo_baker_pkg.io import GeoPackReader
    with GeoPackReader(terrain_path) as t_reader, GeoPackReader(pop_path) as p_reader:
        for lat in range(-90, 90):
            for lon in range(-180, 180):
                tile_blob = t_reader._read_tile(lat, lon)
                is_water_tile = tile_blob is not None and len(tile_blob) <= 1
                pop_blob = p_reader._read_tile(lat, lon)
                is_water_pop = pop_blob is not None and len(pop_blob) <= 1

                for si in range(spt):
                    for sj in range(spt):
                        row = (lat + 90) * spt + si
                        col = (lon + 180) * spt + sj
                        if is_water_tile:
                            elev_grid[row, col] = 0
                            zone_grid[row, col] = ZONE_WATER
                        else:
                            q_lat = lat + (si + 0.5) * step
                            q_lon = lon + (sj + 0.5) * step
                            tn = t_reader.query_terrain(q_lat, q_lon)
                            if tn and tn.get('is_leaf'):
                                elev_grid[row, col] = tn.get('elevation', 0)
                                zone_grid[row, col] = tn.get('zone', 0)
                        if is_water_pop:
                            pop_grid[row, col] = 0
                            urban_grid[row, col] = 0
                        else:
                            q_lat = lat + (si + 0.5) * step
                            q_lon = lon + (sj + 0.5) * step
                            pn = p_reader.query_population(q_lat, q_lon)
                            if pn and pn.get('is_leaf'):
                                pop_grid[row, col] = pn.get('pop_density', 0)
                                urban_grid[row, col] = pn.get('urban_zone', 0)
                done += 1
                if done % 60 == 0 or done == total:
                    pct = done / total * 100
                    print(f"  Sampling: {pct:.1f}% ({done}/{total})", end='\r')

    print()

    fig, axes = plt.subplots(2, 2, figsize=(56, 28))
    extent = [-180, 180, -90, 90]

    im0 = _draw_elevation(
        axes[0, 0], elev_grid, extent, zone_grid,
        interpolation="nearest", title="Global Elevation"
    )
    axes[0, 0].title.set_fontsize(18)
    fig.colorbar(im0, ax=axes[0, 0], label="Land elevation (m)", shrink=0.6)

    valid_pop = pop_grid[np.isfinite(pop_grid)]
    vmax = float(np.nanpercentile(valid_pop, 99)) if len(valid_pop) > 0 else 1.0
    im1 = axes[0, 1].imshow(pop_grid, origin="lower", extent=extent,
                             cmap=POPULATION_CMAP, vmin=0, vmax=max(vmax, 1),
                             interpolation="bilinear", aspect="auto")
    axes[0, 1].set_title("Global Population Density", fontsize=18)
    fig.colorbar(im1, ax=axes[0, 1], label="Pop density (/km²)", shrink=0.6)

    im2 = axes[1, 0].imshow(zone_grid, origin="lower", extent=extent,
                             cmap=ZONE_CMAP, norm=ZONE_NORM, interpolation="nearest", aspect="auto")
    axes[1, 0].set_title("Global Terrain Zones", fontsize=18)
    cbar2 = fig.colorbar(im2, ax=axes[1, 0], ticks=[0, 1, 2, 3], shrink=0.6)
    cbar2.ax.set_yticklabels(["Water", "Natural", "Forest", "Harsh"])

    im3 = axes[1, 1].imshow(urban_grid, origin="lower", extent=extent,
                             cmap=URBAN_CMAP, norm=URBAN_NORM, interpolation="nearest", aspect="auto")
    axes[1, 1].set_title("Global Urban Zone", fontsize=18)
    cbar3 = fig.colorbar(im3, ax=axes[1, 1], ticks=list(range(8)), shrink=0.6)
    cbar3.ax.set_yticklabels(URBAN_LABELS)

    for ax in axes.flat:
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")

    fig.suptitle("GeoBaker Global Overview", fontsize=22, y=0.98)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(args.output, dpi=args.dpi)
    plt.close(fig)
    print(f"Saved: {args.output} ({w}x{h})")

def cmd_compare(args):
    """Compare urban zone + terrain zone + elevation + population at a point / 对比城镇区+地形区+海拔+人口"""
    lat, lon = args.lat, args.lon
    span = max(0.05, float(args.span))
    bbox = (lat - span, lon - span, lat + span, lon + span)
    resolution = max(80, int(args.resolution))

    tile_dir = str(Path(args.tile_dir).resolve())
    if args.tile_dir:
        os.environ["GEO_BAKER_TILE_DIR"] = tile_dir
        gb_core.TILE_DIR = tile_dir
        gb_io.TILE_DIR = tile_dir

    _, _, urban_grid = _sample_grid(
        bbox,
        resolution,
        lambda qlat, qlon: (query_population(qlat, qlon) or {}).get('urban_zone', np.nan),
    )
    _, _, zone_grid = _sample_zone_grid(bbox, resolution)
    _, _, elev_grid = _sample_elevation_grid(bbox, resolution)
    _, _, pop_grid = _sample_population_grid(bbox, resolution)

    min_w = max(1920, int(args.min_width))
    min_h = max(1080, int(args.min_height))
    fig_w = max(min_w / float(args.dpi), 16.0)
    fig_h = max(min_h / float(args.dpi), 10.0)
    fig, axes = plt.subplots(2, 2, figsize=(fig_w, fig_h))
    extent = [bbox[1], bbox[3], bbox[0], bbox[2]]

    im0 = axes[0, 0].imshow(
        urban_grid, origin="lower", extent=extent,
        cmap=URBAN_CMAP, norm=URBAN_NORM, interpolation="nearest", aspect="auto"
    )
    # Add class-boundary contour to avoid "blank-looking" large-city panels.
    if np.any(np.isfinite(urban_grid)):
        u = np.nan_to_num(urban_grid, nan=-1)
        axes[0, 0].contour(
            u,
            levels=[0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5],
            colors="white",
            linewidths=0.25,
            origin="lower",
            extent=extent,
        )
    axes[0, 0].set_title("Urban Zone / 城镇区")
    cbar0 = fig.colorbar(im0, ax=axes[0, 0], ticks=list(range(8)), shrink=0.82)
    cbar0.ax.set_yticklabels(["No Urban", "Residential", "Commercial", "Industrial", "Mixed", "Institutional", "Reserved", "Reserved"])

    im1 = axes[0, 1].imshow(
        zone_grid, origin="lower", extent=extent,
        cmap=ZONE_CMAP, norm=ZONE_NORM, interpolation="nearest", aspect="auto"
    )
    axes[0, 1].set_title("Terrain Zone / 自然区")
    cbar1 = fig.colorbar(im1, ax=axes[0, 1], ticks=[0, 1, 2, 3], shrink=0.82)
    cbar1.ax.set_yticklabels(["Water", "Natural", "Forest", "Harsh"])

    im2 = _draw_elevation(
        axes[1, 0], elev_grid, extent, zone_grid,
        interpolation="nearest", title="Elevation / 海拔"
    )
    fig.colorbar(im2, ax=axes[1, 0], label="m", shrink=0.82)

    vmax = float(np.nanpercentile(pop_grid, 99)) if np.any(~np.isnan(pop_grid)) else 1.0
    im3 = axes[1, 1].imshow(
        pop_grid, origin="lower", extent=extent,
        cmap=POPULATION_CMAP, vmin=0, vmax=max(vmax, 1), interpolation="nearest", aspect="auto"
    )
    axes[1, 1].set_title("Population Density / 人口密度")
    fig.colorbar(im3, ax=axes[1, 1], label="/km²", shrink=0.82)

    if args.show_leaf_boundary != "none":
        segs_pop = []
        segs_terrain = []
        if args.show_leaf_boundary in ("pop", "both"):
            segs_pop = _collect_boundary_segments(bbox, tile_dir, kind="pop", max_leaf_lines=args.max_leaf_lines)
        if args.show_leaf_boundary in ("terrain", "both"):
            segs_terrain = _collect_boundary_segments(bbox, tile_dir, kind="terrain", max_leaf_lines=args.max_leaf_lines)

        if segs_pop:
            lc = LineCollection(segs_pop, colors="white", linewidths=0.12, alpha=0.55)
            axes[1, 1].add_collection(lc)
            if args.show_leaf_boundary == "both":
                axes[0, 0].add_collection(LineCollection(segs_pop, colors="white", linewidths=0.10, alpha=0.45))
        if segs_terrain:
            axes[0, 1].add_collection(LineCollection(segs_terrain, colors="white", linewidths=0.10, alpha=0.50))
            axes[1, 0].add_collection(LineCollection(segs_terrain, colors="white", linewidths=0.10, alpha=0.45))

    for ax in axes.flat:
        ax.set_xlabel("Longitude / 经度")
        ax.set_ylabel("Latitude / 纬度")
        ax.plot(lon, lat, "r*", markersize=12)

    fig.suptitle(f"Point 4-Panel Comparison / 四联图: ({lat}N, {lon}E), span=±{span}°")
    fig.tight_layout()
    fig.savefig(args.output, dpi=args.dpi)
    plt.close(fig)
    print(f"Saved / 已保存: {args.output}")


def main():
    parser = argparse.ArgumentParser(description="Geo-Data Visualizer / 地理数据可视化")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_bbox_args(p):
        p.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("SOUTH", "WEST", "NORTH", "EAST"))
        p.add_argument("--resolution", type=int, default=200, help="Grid resolution / 网格分辨率")
        p.add_argument("-o", "--output", type=str, default="output.png", help="Output path / 输出路径")
        p.add_argument("--dpi", type=int, default=140, help="Output DPI / 输出DPI")

    e = sub.add_parser("elevation", help="Elevation heatmap / 海拔热力图")
    add_bbox_args(e)

    p = sub.add_parser("population", help="Population heatmap / 人口热力图")
    add_bbox_args(p)

    z = sub.add_parser("zones", help="Terrain zone map / 地形区域图")
    add_bbox_args(z)

    o = sub.add_parser("overview", help="Global overview from .dat / 全球概览")
    o.add_argument("--pack", type=str, default="terrain.dat", help=".dat pack path / 包路径")
    o.add_argument("-o", "--output", type=str, default="global_overview.png", help="Output path / 输出路径")
    o.add_argument("--dpi", type=int, default=140, help="Output DPI / 输出DPI")

    q = sub.add_parser("quad-overview", help="Global 4-panel overview / 全球四合一概览")
    q.add_argument("--terrain-pack", type=str, default="terrain.dat", help="Terrain .dat path")
    q.add_argument("--pop-pack", type=str, default="population.dat", help="Population .dat path")
    q.add_argument("--samples-per-tile", type=int, default=8, help="Samples per 1° tile edge / 每度瓦片每边采样点数")
    q.add_argument("-o", "--output", type=str, default="global_quad_overview.png", help="Output path")
    q.add_argument("--dpi", type=int, default=140, help="Output DPI")

    c = sub.add_parser("compare", help="Compare 4 panels at point / 四联图对比")
    c.add_argument("--lat", type=float, required=True, help="Latitude / 纬度")
    c.add_argument("--lon", type=float, required=True, help="Longitude / 经度")
    c.add_argument("--resolution", type=int, default=720, help="Grid resolution / 网格分辨率")
    c.add_argument("--span", type=float, default=0.25, help="Half-span in degrees around point / 采样半径(度)")
    c.add_argument(
        "--tile-dir",
        type=str,
        default=str(Path(__file__).resolve().parent.parent / "tiles"),
        help="Tile directory path / 瓦片目录路径",
    )
    c.add_argument("-o", "--output", type=str, default="compare.png", help="Output path / 输出路径")
    c.add_argument("--dpi", type=int, default=180, help="Output DPI / 输出DPI")
    c.add_argument("--min-width", type=int, default=1920, help="Minimum output width in pixels / 最小输出宽度像素")
    c.add_argument("--min-height", type=int, default=1080, help="Minimum output height in pixels / 最小输出高度像素")
    c.add_argument(
        "--show-leaf-boundary",
        type=str,
        default="none",
        choices=("none", "pop", "terrain", "both"),
        help="Overlay quadtree leaf boundaries / 叠加四叉树叶节点边界",
    )
    c.add_argument("--max-leaf-lines", type=int, default=25000, help="Boundary segment cap / 边界线段上限")

    a = sub.add_parser("export-map-assets", help="Export raster map assets / 导出栅格地图资产")
    a.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("SOUTH", "WEST", "NORTH", "EAST"))
    a.add_argument("--resolution", type=int, default=1024, help="Fallback square resolution / 默认方形输出分辨率")
    a.add_argument("--width", type=int, default=None, help="Output width in pixels / 输出宽度像素")
    a.add_argument("--height", type=int, default=None, help="Output height in pixels / 输出高度像素")
    a.add_argument("-o", "--output-dir", type=str, required=True, help="Output directory / 输出目录")
    a.add_argument("--prefix", type=str, default="geobaker", help="Output filename prefix / 输出文件名前缀")
    a.add_argument("--bit-depth", type=int, default=16, choices=(8, 16), help="Gray image bit depth / 灰度图位深")
    a.add_argument("--min-elevation", type=float, default=None, help="Heightmap minimum elevation / 高度图最小海拔")
    a.add_argument("--max-elevation", type=float, default=None, help="Heightmap maximum elevation / 高度图最大海拔")
    a.add_argument("--percentile-max", type=float, default=99.7, help="Auto height max percentile / 自动高度最大百分位")
    a.add_argument("--population-percentile", type=float, default=99.5, help="Population log scale percentile / 人口对数缩放百分位")
    a.add_argument("--no-water-zero", action="store_true", help="Keep raw water elevation instead of clamping to minimum / 水体不压到最低值")

    args = parser.parse_args()

    if args.command == "elevation":
        args.south, args.west, args.north, args.east = args.bbox
        cmd_elevation(args)
    elif args.command == "population":
        args.south, args.west, args.north, args.east = args.bbox
        cmd_population(args)
    elif args.command == "zones":
        args.south, args.west, args.north, args.east = args.bbox
        cmd_zones(args)
    elif args.command == "overview":
        cmd_overview(args)
    elif args.command == "quad-overview":
        cmd_quad_overview(args)
    elif args.command == "compare":
        cmd_compare(args)
    elif args.command == "export-map-assets":
        cmd_export_map_assets(args)


if __name__ == "__main__":
    main()
