#!/usr/bin/env python3
"""
Geo-Data Inspector - Query and validate baked tiles / 地理数据检查器 - 查询与验证烘焙瓦片

Usage / 用法:
    python tools/inspect.py --query 39.9 116.4              # Query a point / 查询点
    python tools/inspect.py --query-pack 39.9 116.4          # Query from .dat / 从包查询
    python tools/inspect.py --tile-info 116 39               # Tile details / 瓦片详情
    python tools/inspect.py --stats                           # Global stats / 全局统计
    python tools/inspect.py --validate                       # Validate all tiles / 验证所有瓦片
    python tools/inspect.py --validate --fix-ocean           # Fix ocean tiles / 修复海洋瓦片
    python tools/inspect.py --size-report                    # Size analysis / 大小分析
"""
import os
import sys
import struct
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from geo_baker_pkg.core import (
    TILE_DIR, _GPK_MAGIC, _POP_MAGIC,
    _GPK_HEADER_SIZE, _GPK_GRID_W, _GPK_GRID_H, _GPK_INDEX_SIZE,
    ZONE_NAMES, URBAN_NAMES, GRADIENT_NAMES,
    ZONE_WATER, MAX_PACK_SIZE_MB,
    decode_node_16, decode_pop_leaf_node, decode_elevation,
)
from geo_baker_pkg.io import query_elevation, query_elevation_pack, query_population, query_population_pack
from geo_baker_pkg.pipeline import is_likely_ocean


def cmd_query(args):
    """Query a geographic point / 查询地理点"""
    lat, lon = args.lat, args.lon
    if args.pack:
        result = query_elevation_pack(lat, lon, args.terrain_dat)
        pop_result = query_population_pack(lat, lon, args.population_dat)
    else:
        result = query_elevation(lat, lon)
        pop_result = query_population(lat, lon)

    print(f"\n  Query Result / 查询结果: ({lat}N, {lon}E)")
    print(f"  {'='*50}")
    if result:
        print(f"  Elevation / 海拔:       {result['elevation']} m")
        print(f"  Zone / 区域:            {result.get('zone_name', '?')} (code={result.get('zone', '?')})")
        grad = result.get('gradient_level', 0)
        print(f"  Gradient / 坡度:        {result.get('gradient_name', '?')} (level={grad})")
    else:
        print(f"  Elevation / 海拔:       NO DATA / 无数据")

    if pop_result:
        print(f"  Pop density / 人口密度: {pop_result['pop_density']} /km²")
        print(f"  Urban type / 城市类型:  {pop_result['urban_name']} (code={pop_result.get('urban_zone', '?')})")
    else:
        print(f"  Population / 人口:      NO DATA / 无数据")


def cmd_tile_info(args):
    """Show detailed tile info / 显示瓦片详细信息"""
    lon, lat = args.lon, args.lat
    tile_path = Path(TILE_DIR) / f"{lon}_{lat}.qtree"
    pop_path = Path(TILE_DIR) / f"{lon}_{lat}.pop"

    print(f"\n  Tile Info / 瓦片信息: ({lon}E, {lat}N)")
    print(f"  {'='*50}")

    if tile_path.exists():
        size = tile_path.stat().st_size
        print(f"  Terrain / 地形:  {size} bytes ({'water' if size <= 1 else 'data'})")
        if size > 1:
            with open(tile_path, 'rb') as f:
                data = f.read()
            node_count = len(data) // 2
            root = decode_node_16(data[:2])
            print(f"  Nodes / 节点数:  {node_count}")
            print(f"  Root / 根节点:   subtree_size={root.get('subtree_size', '?')}")
    else:
        print(f"  Terrain / 地形:  NOT FOUND / 未找到")

    if pop_path.exists():
        size = pop_path.stat().st_size
        print(f"  Population / 人口: {size} bytes ({'water' if size <= 1 else 'data'})")
    else:
        print(f"  Population / 人口: NOT FOUND / 未找到")

    ocean = is_likely_ocean(lat, lon)
    print(f"  Ocean check / 海洋检查: {'Yes' if ocean else 'No'}")


def cmd_stats(args):
    """Show global statistics / 显示全局统计"""
    tile_dir = Path(TILE_DIR)
    if not tile_dir.exists():
        print("  No tiles directory / 无瓦片目录")
        return

    qtree_files = list(tile_dir.glob("*.qtree"))
    pop_files = list(tile_dir.glob("*.pop"))

    total = len(qtree_files)
    water = sum(1 for f in qtree_files if f.stat().st_size <= 1)
    data_tiles = total - water
    total_size = sum(f.stat().st_size for f in qtree_files)
    data_size = sum(f.stat().st_size for f in qtree_files if f.stat().st_size > 1)

    pop_total = len(pop_files)
    pop_size = sum(f.stat().st_size for f in pop_files) if pop_files else 0

    print(f"\n  Global Statistics / 全局统计")
    print(f"  {'='*50}")
    print(f"  Terrain tiles / 地形瓦片:  {total:,}")
    print(f"    Data tiles / 数据瓦片:   {data_tiles:,}")
    print(f"    Ocean tiles / 海域瓦片:  {water:,}")
    print(f"    Total size / 总大小:     {total_size / 1024 / 1024:.2f} MB")
    print(f"    Data size / 数据大小:    {data_size / 1024 / 1024:.2f} MB")
    if data_tiles > 0:
        print(f"    Avg data tile / 平均:    {data_size / data_tiles:.0f} bytes")
    print(f"  Population tiles / 人口瓦片: {pop_total:,}")
    print(f"    Total size / 总大小:     {pop_size / 1024 / 1024:.2f} MB")
    print(f"  Combined / 合计:           {(total_size + pop_size) / 1024 / 1024:.2f} MB")

    for dat_name in ["terrain.dat", "population.dat"]:
        dat_path = Path(dat_name)
        if dat_path.exists():
            print(f"  {dat_name}: {dat_path.stat().st_size / 1024 / 1024:.2f} MB")


def cmd_validate(args):
    """Validate tile integrity / 验证瓦片完整性"""
    tile_dir = Path(TILE_DIR)
    if not tile_dir.exists():
        print("  No tiles directory / 无瓦片目录")
        return

    qtree_files = sorted(tile_dir.glob("*.qtree"))
    errors = []
    ocean_mismatches = []
    empty_data = []

    for qf in qtree_files:
        parts = qf.stem.split('_')
        if len(parts) != 2:
            continue
        try:
            lon, lat = int(parts[0]), int(parts[1])
        except ValueError:
            continue

        size = qf.stat().st_size
        is_ocean = is_likely_ocean(lat, lon)

        if size <= 1 and not is_ocean:
            ocean_mismatches.append((lat, lon, "water tile on land / 陆地上的水域瓦片"))
        elif size > 1 and is_ocean:
            ocean_mismatches.append((lat, lon, "data tile in ocean / 海洋中的数据瓦片"))

        if size > 1:
            try:
                with open(qf, 'rb') as f:
                    data = f.read()
                if len(data) % 2 != 0:
                    errors.append((lat, lon, f"odd byte count / 奇数字节数: {len(data)}"))
                root = decode_node_16(data[:2])
                if root.get('is_leaf', True):
                    errors.append((lat, lon, "root is leaf / 根节点是叶节点"))
                else:
                    subtree_size = root.get('subtree_size', 0)
                    expected_nodes = len(data) // 2
                    if subtree_size > expected_nodes:
                        errors.append((lat, lon, f"subtree_size {subtree_size} > nodes {expected_nodes}"))
            except Exception as e:
                errors.append((lat, lon, f"decode error / 解码错误: {e}"))

    print(f"\n  Validation Report / 验证报告")
    print(f"  {'='*50}")
    print(f"  Total tiles / 总瓦片: {len(qtree_files)}")
    print(f"  Errors / 错误: {len(errors)}")
    print(f"  Ocean mismatches / 海洋不匹配: {len(ocean_mismatches)}")

    if errors:
        print(f"\n  Errors / 错误:")
        for lat, lon, msg in errors[:20]:
            print(f"    ({lat},{lon}): {msg}")

    if ocean_mismatches and args.fix_ocean:
        print(f"\n  Fixing ocean mismatches / 修复海洋不匹配...")
        from geo_baker_pkg.encoding import encode_water_tile
        for lat, lon, msg in ocean_mismatches:
            if "water tile on land" in msg:
                print(f"    Skipping ({lat},{lon}): {msg}")
            elif "data tile in ocean" in msg:
                tile_path = tile_dir / f"{lon}_{lat}.qtree"
                pop_path = tile_dir / f"{lon}_{lat}.pop"
                with open(tile_path, 'wb') as f:
                    f.write(encode_water_tile())
                if pop_path.exists():
                    with open(pop_path, 'wb') as f:
                        f.write(encode_water_tile())
                print(f"    Fixed ({lat},{lon}): replaced with water tile / 替换为水域瓦片")


def cmd_size_report(args):
    """Analyze tile size distribution / 分析瓦片大小分布"""
    tile_dir = Path(TILE_DIR)
    if not tile_dir.exists():
        print("  No tiles directory / 无瓦片目录")
        return

    qtree_files = sorted(tile_dir.glob("*.qtree"))
    sizes = [(f, f.stat().st_size) for f in qtree_files if f.stat().st_size > 1]

    if not sizes:
        print("  No data tiles / 无数据瓦片")
        return

    sizes.sort(key=lambda x: -x[1])

    print(f"\n  Size Report / 大小报告")
    print(f"  {'='*50}")
    print(f"  Data tiles / 数据瓦片: {len(sizes)}")
    print(f"  Total / 总计: {sum(s for _, s in sizes) / 1024 / 1024:.2f} MB")

    size_buckets = {"<1KB": 0, "1-10KB": 0, "10-50KB": 0, "50-100KB": 0, "100-500KB": 0, ">500KB": 0}
    for _, s in sizes:
        kb = s / 1024
        if kb < 1:
            size_buckets["<1KB"] += 1
        elif kb < 10:
            size_buckets["1-10KB"] += 1
        elif kb < 50:
            size_buckets["10-50KB"] += 1
        elif kb < 100:
            size_buckets["50-100KB"] += 1
        elif kb < 500:
            size_buckets["100-500KB"] += 1
        else:
            size_buckets[">500KB"] += 1

    print(f"\n  Distribution / 分布:")
    for bucket, count in size_buckets.items():
        bar = "█" * min(40, count)
        print(f"    {bucket:>10}: {count:5d} {bar}")

    print(f"\n  Top 10 largest / 最大的10个:")
    for f, s in sizes[:10]:
        print(f"    {f.name}: {s / 1024:.1f} KB")


def main():
    parser = argparse.ArgumentParser(description="Geo-Data Inspector / 地理数据检查器")
    sub = parser.add_subparsers(dest="command", required=True)

    q = sub.add_parser("query", help="Query a point / 查询点")
    q.add_argument("lat", type=float, help="Latitude / 纬度")
    q.add_argument("lon", type=float, help="Longitude / 经度")
    q.add_argument("--pack", action="store_true", help="Query from .dat / 从包查询")
    q.add_argument("--terrain-dat", type=str, default="terrain.dat")
    q.add_argument("--population-dat", type=str, default="population.dat")

    ti = sub.add_parser("tile-info", help="Tile details / 瓦片详情")
    ti.add_argument("lon", type=int, help="Longitude / 经度")
    ti.add_argument("lat", type=int, help="Latitude / 纬度")

    sub.add_parser("stats", help="Global statistics / 全局统计")

    v = sub.add_parser("validate", help="Validate tiles / 验证瓦片")
    v.add_argument("--fix-ocean", action="store_true", help="Fix ocean mismatches / 修复海洋不匹配")

    sub.add_parser("size-report", help="Size analysis / 大小分析")

    args = parser.parse_args()

    if args.command == "query":
        cmd_query(args)
    elif args.command == "tile-info":
        cmd_tile_info(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "validate":
        cmd_validate(args)
    elif args.command == "size-report":
        cmd_size_report(args)


if __name__ == "__main__":
    main()
