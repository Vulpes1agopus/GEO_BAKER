#!/usr/bin/env python3
"""
验证全球城市数据的质量：检查沿海城市是否有正确的水陆分类。

用法:
    python tools/verify_cities.py                    # 验证全部城市
    python tools/verify_cities.py --limit 50        # 仅验证前50个
    python tools/verify_cities.py --problem-only    # 仅显示有问题的城市
    python tools/verify_cities.py --bbox 100 20 150 60  # 仅中国及周边
"""

import sys
import os
import json
import argparse
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geo_baker_pkg.core import TILE_DIR, ZONE_WATER, URBAN_NONE, URBAN_COMMERCIAL
from geo_baker_pkg.io import query_elevation, query_population


def load_cities(path):
    if not os.path.exists(path):
        print(f"X 城市文件不存在: {path}")
        sys.exit(1)
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _sample_offsets(sample_grid=1, radius_deg=0.02):
    g = max(1, int(sample_grid))
    if g == 1:
        return [(0.0, 0.0)]
    vals = np.linspace(-float(radius_deg), float(radius_deg), g)
    out = [(0.0, 0.0)]
    out.extend((float(dla), float(dlo)) for dla in vals for dlo in vals if dla or dlo)
    return out


def verify_city(city, sample_grid=1, sample_radius=0.02):
    lat = city.get('la', 0)
    lon = city.get('lo', 0)
    name = city.get('n', city.get('name', '?'))
    pop = city.get('p', 0)

    if lat == 0 and lon == 0:
        return None

    li, lo = int(np.floor(lat)), int(np.floor(lon))
    tile_q = os.path.join(TILE_DIR, f"{lo}_{li}.qtree")
    if not os.path.exists(tile_q):
        return {'name': name, 'lat': lat, 'lon': lon, 'pop': pop,
                'status': 'missing_tile', 'zone': -1, 'elev': -1,
                'pop_density': 0, 'issue': '无瓦片文件(需烘焙该格)'}

    samples = []
    for dla, dlo in _sample_offsets(sample_grid, sample_radius):
        sla, slo = lat + dla, lon + dlo
        elev_result = query_elevation(sla, slo)
        pop_result = query_population(sla, slo)
        samples.append((dla, dlo, elev_result, pop_result))

    center = samples[0]
    elev_result = center[2]
    pop_result = center[3]

    if elev_result is None:
        return {'name': name, 'lat': lat, 'lon': lon, 'pop': pop,
                'status': 'no_data', 'zone': -1, 'elev': -1,
                'pop_density': 0, 'issue': '无海拔数据(瓦片损坏/旧格式/查询失败)'}

    zone = elev_result.get('zone', -1)
    elev = elev_result.get('elevation', -1)
    pop_density = pop_result.get('pop_density', 0) if pop_result else 0

    is_water = zone == ZONE_WATER
    has_pop = pop_density > 50 or pop > 10000
    bad_samples = 0
    water_samples = 0
    valid_samples = 0
    max_pop_density = pop_density
    for idx, (_, _, er, pr) in enumerate(samples):
        if er is None:
            continue
        valid_samples += 1
        pd = pr.get('pop_density', 0) if pr else 0
        max_pop_density = max(max_pop_density, pd)
        if er.get('zone') == ZONE_WATER:
            water_samples += 1
            if pd > 50 or (idx == 0 and pop > 10000):
                bad_samples += 1

    if (is_water and has_pop) or bad_samples > 0:
        return {
            'name': name, 'lat': lat, 'lon': lon, 'pop': pop,
            'status': 'bad', 'zone': zone, 'elev': elev,
            'pop_density': max_pop_density,
            'issue': f'水域但人口密度{max_pop_density}/km² (water_samples={water_samples}/{valid_samples})'
        }
    elif is_water or water_samples > 0:
        return {
            'name': name, 'lat': lat, 'lon': lon, 'pop': pop,
            'status': 'warning', 'zone': zone, 'elev': elev,
            'pop_density': max_pop_density,
            'issue': f'采样含水域但人口较少{max_pop_density}/km² (water_samples={water_samples}/{valid_samples})'
        }
    else:
        return {
            'name': name, 'lat': lat, 'lon': lon, 'pop': pop,
            'status': 'ok', 'zone': zone, 'elev': elev,
            'pop_density': max_pop_density,
            'issue': None
        }


def main():
    parser = argparse.ArgumentParser(description='验证全球城市数据质量')
    parser.add_argument('--cities', type=str, default='data/global_cities.json', help='城市JSON路径')
    parser.add_argument('--limit', type=int, default=0, help='限制验证城市数量(0=全部)')
    parser.add_argument('--problem-only', action='store_true', help='仅显示有问题的城市')
    parser.add_argument('--bbox', type=float, nargs=4, default=None,
                       metavar=('LON_MIN', 'LAT_MIN', 'LON_MAX', 'LAT_MAX'),
                       help='bbox过滤 (e.g., 100 20 150 60)')
    parser.add_argument('--min-pop', type=float, default=50000, help='最小人口阈值')
    parser.add_argument('--sample-grid', type=int, default=1,
                        help='城市周边采样网格边长(1=只查城市点; 3=中心+周边8点)')
    parser.add_argument('--sample-radius', type=float, default=0.02,
                        help='城市周边采样半径(经纬度)')
    args = parser.parse_args()

    cities = load_cities(args.cities)

    if args.bbox:
        lon_min, lat_min, lon_max, lat_max = args.bbox
        cities = [c for c in cities
                  if lon_min <= c.get('lo', 0) <= lon_max
                  and lat_min <= c.get('la', 0) <= lat_max]

    if args.limit > 0:
        cities = cities[:args.limit]

    cities = [c for c in cities if c.get('p', 0) >= args.min_pop]

    print(f"\n  验证 {len(cities)} 个城市 (人口≥{args.min_pop:,})")
    if args.bbox:
        print(f"  bbox: {args.bbox}")
    print(f"  {'='*70}")

    results = []
    for i, city in enumerate(cities):
        r = verify_city(city, sample_grid=args.sample_grid, sample_radius=args.sample_radius)
        if r is None:
            continue
        results.append(r)
        if not args.problem_only or r['status'] != 'ok':
            status_icon = {'ok': '✅', 'warning': '⚠️', 'bad': '❌', 'no_data': '❓',
                           'missing_tile': '📭'}.get(r['status'], '?')
            issue = r['issue'] or ''
            if args.problem_only and r['status'] == 'ok':
                continue
            print(f"{status_icon} {r['name']:30s} ({r['lat']:+.2f}N, {r['lon']:+.2f}E) "
                  f"elev={r['elev']:5d}m zone={r.get('zone', -1)} pop={r['pop_density']:>6}/km²  {issue}")

    ok_count = sum(1 for r in results if r['status'] == 'ok')
    warn_count = sum(1 for r in results if r['status'] == 'warning')
    bad_count = sum(1 for r in results if r['status'] == 'bad')
    no_data_count = sum(1 for r in results if r['status'] == 'no_data')
    miss_count = sum(1 for r in results if r['status'] == 'missing_tile')

    print(f"\n  {'='*70}")
    print(f"  统计: ✅ OK={ok_count}  ⚠️ 警告={warn_count}  ❌ 问题={bad_count}  "
          f"❓无数据={no_data_count}  📭缺瓦片={miss_count}")
    print(f"  总计: {len(results)} / {len(cities)} 个城市已验证")

    if bad_count > 0:
        print(f"\n  ❌ 有 {bad_count} 个城市存在严重问题（水域+高人口）")
        print(f"  建议运行: python -m geo_baker_pkg --fix-coastal")
    elif warn_count > 0:
        print(f"\n  ⚠️  有 {warn_count} 个城市存在警告（水域+低人口）")
    else:
        print(f"\n  ✅ 无严重水陆冲突（水域+高人口）")
    if miss_count > 0:
        print(f"  📭 有 {miss_count} 个城市所在格尚未烘焙 `.qtree`，可运行 `python -m geo_baker_pkg --global` 或按区域烘焙。")


if __name__ == '__main__':
    main()
