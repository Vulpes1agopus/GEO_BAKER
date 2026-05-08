"""
命令行入口 / Command-line Interface

用法 / Usage:
    python -m geo_baker_pkg --tile 116,39
    python -m geo_baker_pkg --bbox 70 20 140 55
    python -m geo_baker_pkg --global
    python -m geo_baker_pkg --pack
    python -m geo_baker_pkg --query 39.9 116.4
"""
import sys
import time
import logging
import argparse

from .core import TILE_DIR, LOG_FILE
from .pipeline import (
    bake_tile, bake_region, bake_global, retry_errors, _parse_split_arg,
    is_likely_ocean, configure_runtime_tuning, fix_coastal_batch,
    fix_population_zone_batch,
)
from .io import pack_tiles, pack_population, merge_gpk, incremental_pack
from .io import query_elevation, query_elevation_pack, query_population, query_population_pack


def setup_logging(log_file=None, console_level=logging.INFO, verbose=False):
    logger = logging.getLogger('geo_baker')
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        for h in logger.handlers:
            if isinstance(h, logging.StreamHandler) and getattr(h, 'stream', None) is sys.stdout:
                h.setLevel(console_level)
            elif isinstance(h, logging.FileHandler):
                h.setLevel(logging.DEBUG if verbose else logging.INFO)
        return
    if log_file is None:
        import datetime
        log_file = f"logs/bake_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    from pathlib import Path
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_file, encoding='utf-8')
    fh.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(console_level)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    logger.info(f"[LOG] Log file: {log_file}")


def main():
    if sys.platform == 'win32':
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    parser = argparse.ArgumentParser(description='Geo Baker - 全球地理数据管线')
    parser.add_argument('--tile', type=str, default=None, help='烘焙单瓦片: lon,lat')
    parser.add_argument(
        '--bbox', type=float, nargs=4, default=None,
        help='烘焙区域: lon_min lat_min lon_max lat_max (上界不含; 自动夹到 lon[-180,180),lat[-90,90))'
    )
    parser.add_argument('--global', action='store_true', dest='bake_global', help='烘焙全球')
    parser.add_argument('--bake-ocean', action='store_true', help='(已弃用，默认行为)全球烘焙时不跳过海洋瓦片')
    parser.add_argument('--no-data-water', action='store_true', help='NO_DATA瓦片直接写水瓦片(不下载zone判断)')
    parser.add_argument('--offline', action='store_true', help='离线模式(仅缓存)')
    parser.add_argument('--dry-run', action='store_true', dest='dry_run', help='仅计算索引')
    parser.add_argument('--verbose', action='store_true', help='DEBUG日志')
    parser.add_argument('--workers', type=int, default=16, help='并行进程数')
    parser.add_argument('--conn', '--max-conn', dest='conn', type=int, default=120,
                        help='HTTP并发连接数(兼容旧参数 --max-conn)')
    parser.add_argument('--tile-timeout', type=int, default=900,
                        help='批次空闲看门狗(秒): 若此时间内无任何瓦片完成则取消剩余任务并结束(0=关闭)')
    parser.add_argument('--no-skip-existing', action='store_true',
                        help='区域 --bbox 时强制重烘已存在的 qtree+pop 瓦片(默认跳过)')
    parser.add_argument('--query', type=float, nargs=2, default=None, help='查询点: lat lon')
    parser.add_argument('--query-pack', type=float, nargs=2, default=None, help='从包查询: lat lon')
    parser.add_argument('--query-pop', type=float, nargs=2, default=None, help='查询人口: lat lon')
    parser.add_argument('--query-pop-pack', type=float, nargs=2, default=None, help='从包查询人口: lat lon')
    parser.add_argument('--stats', action='store_true', help='统计瓦片')
    parser.add_argument('--pack', action='store_true', help='打包地形瓦片')
    parser.add_argument('--pack-output', type=str, default='terrain.dat', help='打包输出路径')
    parser.add_argument('--pack-pop', action='store_true', help='打包人口瓦片')
    parser.add_argument('--pack-pop-output', type=str, default='population.dat', help='人口打包输出')
    parser.add_argument('--incremental-pack', action='store_true', help='增量打包')
    parser.add_argument('--merge', type=str, nargs=2, default=None, metavar=('FILE1', 'FILE2'), help='合并.dat文件')
    parser.add_argument('--merge-output', type=str, default='merged.dat', help='合并输出')
    parser.add_argument('--split', type=str, default=None, help='分布式烘焙: N/M')
    parser.add_argument('--retry-errors', action='store_true', help='重试错误瓦片')
    parser.add_argument('--fix-coastal', action='store_true', help='检测并重新烘焙沿海问题瓦片')
    parser.add_argument('--fix-pop-zone', action='store_true', help='自动检测并重烘焙人口/城镇与zone冲突瓦片(含小城市乡镇)')
    parser.add_argument('--cities-json', type=str, default='data/global_cities.json', help='城市列表JSON路径')
    parser.add_argument('--pop-threshold', type=float, default=10.0, help='异常扫描人口阈值(/km²)')
    parser.add_argument('--fix-rounds', type=int, default=4, help='自动修复最大迭代轮数')
    parser.add_argument('--scan-grid', type=int, default=3, help='每瓦片采样网格边长(3表示9点)')
    parser.add_argument('--scan-min-hits', type=int, default=1, help='判定异常最小命中点数')
    parser.add_argument('--scan-max-tiles', type=int, default=2000, help='每轮最多重烘焙瓦片数')

    args = parser.parse_args()

    print()
    print("=" * 60)
    print("  Geo Baker - 全球地理数据管线")
    print("  CopDEM + WorldPop + ESA WorldCover")
    print("  16bit节点 | QTR5 | 11bit海拔 + 2bit坡度 + 2bit区域")
    print("=" * 60)
    print()

    setup_logging(console_level=logging.DEBUG if args.verbose else logging.INFO, verbose=args.verbose)

    if args.tile or args.bbox or args.bake_global or args.retry_errors:
        configure_runtime_tuning(max_conn=args.conn, workers=args.workers)

    start = time.time()

    if args.query:
        lat, lon = args.query
        result = query_elevation(lat, lon)
        pop_result = query_population(lat, lon)
        if result:
            print(f"  ({lat}N, {lon}E):")
            print(f"     海拔: {result['elevation']}m")
            print(f"     区域: {result.get('zone_name', result['zone'])}")
            print(f"     坡度: {result.get('gradient_name', '?')}")
            if pop_result:
                print(f"     人口密度: {pop_result['pop_density']} /km²")
                print(f"     城市类型: {pop_result['urban_name']}")
        else:
            print(f"  X 无数据 ({lat}N, {lon}E)")
        return

    if args.query_pack:
        lat, lon = args.query_pack
        result = query_elevation_pack(lat, lon, args.pack_output)
        pop_result = query_population_pack(lat, lon, args.pack_pop_output)
        if result:
            print(f"  ({lat}N, {lon}E) [from {args.pack_output}]:")
            print(f"     海拔: {result['elevation']}m")
            print(f"     区域: {result.get('zone_name', result['zone'])}")
            if pop_result:
                print(f"     人口密度: {pop_result['pop_density']} /km²")
        else:
            print(f"  X 无数据 ({lat}N, {lon}E)")
        return

    if args.query_pop:
        lat, lon = args.query_pop
        result = query_population(lat, lon)
        if result:
            print(f"  ({lat}N, {lon}E) 人口:")
            print(f"     密度: {result['pop_density']} /km²")
            print(f"     城市类型: {result['urban_name']}")
        else:
            print(f"  X 无人口数据 ({lat}N, {lon}E)")
        return

    if args.query_pop_pack:
        lat, lon = args.query_pop_pack
        result = query_population_pack(lat, lon, args.pack_pop_output)
        if result:
            print(f"  ({lat}N, {lon}E) [from {args.pack_pop_output}] 人口:")
            print(f"     密度: {result['pop_density']} /km²")
            print(f"     城市类型: {result['urban_name']}")
        else:
            print(f"  X 无人口数据 ({lat}N, {lon}E)")
        return

    if args.stats:
        _print_stats()
        return

    if args.pack:
        pack_tiles(args.pack_output)
        print(f"\n  耗时: {time.time() - start:.1f}s")
        return

    if args.pack_pop:
        pack_population(args.pack_pop_output)
        print(f"\n  耗时: {time.time() - start:.1f}s")
        return

    if args.incremental_pack:
        incremental_pack(args.pack_output)
        print(f"\n  耗时: {time.time() - start:.1f}s")
        return

    if args.merge:
        f1, f2 = args.merge
        from .core import _GPK_MAGIC, _POP_MAGIC
        with open(f1, 'rb') as f: magic = f.read(4)
        merge_gpk(f1, f2, args.merge_output, _GPK_MAGIC if magic == _GPK_MAGIC else _POP_MAGIC)
        print(f"\n  耗时: {time.time() - start:.1f}s")
        return

    if args.retry_errors:
        retry_errors(workers=args.workers, max_conn=args.conn, idle_timeout_s=args.tile_timeout)
        print(f"\n  耗时: {time.time() - start:.1f}s")
        return

    if args.fix_coastal:
        fix_coastal_batch(cities_json_path=args.cities_json, pop_threshold=args.pop_threshold,
                          workers=args.workers, max_conn=args.conn,
                          idle_timeout_s=args.tile_timeout)
        print(f"\n  耗时: {time.time() - start:.1f}s")
        return

    if args.fix_pop_zone:
        fix_population_zone_batch(
            pop_threshold=args.pop_threshold,
            workers=args.workers,
            max_conn=args.conn,
            max_rounds=args.fix_rounds,
            sample_grid=args.scan_grid,
            min_hits=args.scan_min_hits,
            max_tiles_per_round=args.scan_max_tiles,
            idle_timeout_s=args.tile_timeout,
        )
        print(f"\n  耗时: {time.time() - start:.1f}s")
        return

    split = None
    if args.split:
        try: split = _parse_split_arg(args.split)
        except ValueError as e: print(f"  X {e}"); return

    if args.tile:
        parts = args.tile.split(',')
        lon, lat = int(parts[0]), int(parts[1])
        if args.dry_run:
            print(f"  [DRY-RUN] 瓦片 {lon},{lat}: 预计下载约3-5MB")
            return
        bake_tile(lat, lon, offline=args.offline, max_conn=args.conn)
    elif args.bbox:
        bake_region(args.bbox[1], args.bbox[3], args.bbox[0], args.bbox[2],
                    offline=args.offline, workers=args.workers, max_conn=args.conn, split=split,
                    skip_existing=not args.no_skip_existing,
                    idle_timeout_s=args.tile_timeout)
    elif args.bake_global:
        bake_global(
            offline=args.offline,
            workers=args.workers,
            max_conn=args.conn,
            split=split,
            skip_ocean=False,
            no_data_water=args.no_data_water,
            idle_timeout_s=args.tile_timeout,
        )
    else:
        _print_usage()

    print(f"\n  耗时: {time.time() - start:.1f}s")
    print()


def _print_stats():
    import os
    from pathlib import Path
    if not os.path.exists(TILE_DIR):
        print("  尚未烘焙任何瓦片")
        return
    files = list(Path(TILE_DIR).glob("*.qtree"))
    total = len(files)
    water = sum(1 for f in files if os.path.getsize(f) <= 1)
    data = total - water
    total_size = sum(os.path.getsize(f) for f in files)
    pop_files = list(Path(TILE_DIR).glob("*.pop"))
    pop_size = sum(os.path.getsize(f) for f in pop_files) if pop_files else 0
    print(f"  瓦片统计:")
    print(f"     地形总数: {total:,}")
    print(f"     数据瓦片: {data:,}")
    print(f"     海域瓦片: {water:,}")
    print(f"     地形大小: {total_size / 1024 / 1024:.1f} MB")
    if data > 0:
        print(f"     平均: {total_size / total:.0f} bytes/瓦片")
    if pop_files:
        print(f"     人口瓦片: {len(pop_files):,}")
        print(f"     人口大小: {pop_size / 1024 / 1024:.1f} MB")


def _print_usage():
    print("  用法:")
    print("    python -m geo_baker_pkg --tile 116,39              # 烘焙北京")
    print("    python -m geo_baker_pkg --bbox 70 20 140 55        # 烘焙区域(上界不含)")
    print("    python -m geo_baker_pkg --global                   # 烘焙全球")
    print("    python -m geo_baker_pkg --global --split 2/1       # 分布式")
    print("    python -m geo_baker_pkg --global --tile-timeout 0  # 关闭空闲看门狗(默认900s)")
    print("    python -m geo_baker_pkg --global --max-conn 80     # --max-conn 为 --conn 兼容别名")
    print("    python -m geo_baker_pkg --pack                     # 打包地形")
    print("    python -m geo_baker_pkg --pack-pop                 # 打包人口")
    print("    python -m geo_baker_pkg --incremental-pack         # 增量打包")
    print("    python -m geo_baker_pkg --merge a.dat b.dat        # 合并")
    print("    python -m geo_baker_pkg --retry-errors             # 重试错误")
    print("    python -m geo_baker_pkg --fix-coastal             # 修复沿海问题瓦片")
    print("    python -m geo_baker_pkg --fix-pop-zone            # 修复有人口但zone异常的瓦片")
    print("    python -m geo_baker_pkg --query 39.9 116.4         # 查询")
    print("    python -m geo_baker_pkg --query-pack 39.9 116.4    # 从包查询")
    print("    python -m geo_baker_pkg --stats                    # 统计")
