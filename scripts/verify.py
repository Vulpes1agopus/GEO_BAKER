#!/usr/bin/env python3
"""
GeoBaker verification toolkit / GeoBaker 验证工具集

Usage / 用法:
    python scripts/verify.py                    # Full verification (all checks)
    python scripts/verify.py --quick            # Quick check (cities only)
    python scripts/verify.py --pop-threshold 20 # Custom threshold
    python scripts/verify.py --check urban      # Only urban/pop check
    python scripts/verify.py --check water      # Only water/pop check
    python scripts/verify.py --check cities     # Only city check
"""

import os
import sys
import json
import argparse
import numpy as np
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from geo_baker_pkg.core import (TILE_DIR, ZONE_WATER, ZONE_NATURAL, ZONE_FOREST, ZONE_HARSH,
                                 navigate_qtr5, navigate_qtr5_pop)

ZONE_NAMES = {0: "Water", 1: "Natural", 2: "Forest", 3: "Harsh"}
URBAN_NAMES = {0: "None", 1: "Residential", 2: "Commercial"}


def _make_sample_points(grid_size=5):
    g = max(1, grid_size)
    step = 1.0 / (g + 1)
    return [((r + 1) * step, (c + 1) * step) for r in range(g) for c in range(g)]


def check_urban_pop_mismatch(pop_threshold, sample_points):
    """Check 1: urban=0 but pop>=threshold."""
    print("=" * 60)
    print(f"CHECK: urban=0 (uninhabited) but pop >= {pop_threshold:.0f}")
    print("=" * 60)

    tile_dir = Path(TILE_DIR)
    if not tile_dir.exists():
        print("No tiles directory")
        return []

    mismatches = []
    total = 0

    for qf in sorted(tile_dir.glob("*.qtree")):
        parts = qf.stem.split('_')
        if len(parts) != 2:
            continue
        try:
            lon, lat = int(parts[0]), int(parts[1])
        except ValueError:
            continue

        pop_path = tile_dir / f"{lon}_{lat}.pop"
        if not pop_path.exists() or qf.stat().st_size <= 1:
            continue

        total += 1
        try:
            with open(qf, 'rb') as f:
                raw = f.read()
            terrain_data = raw[16:] if raw[:4] == b'QTR5' else raw
            with open(pop_path, 'rb') as f:
                raw_pop = f.read()
            pop_data = raw_pop[16:] if raw_pop[:4] == b'QTR5' else raw_pop

            hits = 0
            max_pop = 0
            for frac_lat, frac_lon in sample_points:
                tn = navigate_qtr5(terrain_data, frac_lat, frac_lon)
                pn = navigate_qtr5_pop(pop_data, frac_lat, frac_lon)
                if not tn or not tn.get('is_leaf') or not pn or not pn.get('is_leaf'):
                    continue
                pd = pn.get('pop_density', 0)
                if pd > max_pop:
                    max_pop = pd
                if pd >= pop_threshold and pn.get('urban_zone', 0) == 0:
                    hits += 1

            if hits > 0:
                mismatches.append((lat, lon, hits, max_pop))
        except Exception:
            continue

    print(f"Scanned {total} tiles, found {len(mismatches)} with urban/pop mismatch")
    if mismatches:
        for lat, lon, hits, max_pop in sorted(mismatches, key=lambda x: -x[3])[:20]:
            print(f"  ({lon}_{lat}): {hits} pts, max_pop={max_pop:.0f}")
        if len(mismatches) > 20:
            print(f"  ... and {len(mismatches) - 20} more")
    return mismatches


def check_water_pop_mismatch(pop_threshold, sample_points):
    """Check 2: zone=water but pop>=threshold."""
    print()
    print("=" * 60)
    print(f"CHECK: zone=water but pop >= {pop_threshold:.0f}")
    print("=" * 60)

    tile_dir = Path(TILE_DIR)
    if not tile_dir.exists():
        print("No tiles directory")
        return []

    mismatches = []
    for qf in sorted(tile_dir.glob("*.qtree")):
        parts = qf.stem.split('_')
        if len(parts) != 2:
            continue
        try:
            lon, lat = int(parts[0]), int(parts[1])
        except ValueError:
            continue

        pop_path = tile_dir / f"{lon}_{lat}.pop"
        if not pop_path.exists() or qf.stat().st_size <= 1:
            continue

        try:
            with open(qf, 'rb') as f:
                raw = f.read()
            terrain_data = raw[16:] if raw[:4] == b'QTR5' else raw
            with open(pop_path, 'rb') as f:
                raw_pop = f.read()
            pop_data = raw_pop[16:] if raw_pop[:4] == b'QTR5' else raw_pop

            hits = 0
            max_pop = 0
            for frac_lat, frac_lon in sample_points:
                tn = navigate_qtr5(terrain_data, frac_lat, frac_lon)
                pn = navigate_qtr5_pop(pop_data, frac_lat, frac_lon)
                if not tn or not tn.get('is_leaf') or not pn or not pn.get('is_leaf'):
                    continue
                if tn.get('zone') == ZONE_WATER:
                    pd = pn.get('pop_density', 0)
                    if pd > max_pop:
                        max_pop = pd
                    if pd >= pop_threshold:
                        hits += 1

            if hits > 0:
                mismatches.append((lat, lon, hits, max_pop))
        except Exception:
            continue

    print(f"Found {len(mismatches)} tiles with water/pop mismatch")
    if mismatches:
        for lat, lon, hits, max_pop in sorted(mismatches, key=lambda x: -x[3])[:20]:
            print(f"  ({lon}_{lat}): {hits} pts, max_pop={max_pop:.0f}")
    return mismatches


def check_cities(pop_threshold, cities_json_path=None):
    """Check 3: City coordinate verification."""
    print()
    print("=" * 60)
    print("CHECK: City coordinate verification (zone + pop + urban)")
    print("=" * 60)

    if not cities_json_path or not os.path.exists(cities_json_path):
        default_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'global_cities.json')
        if os.path.exists(default_path):
            cities_json_path = default_path
        else:
            print(f"Cities JSON not found")
            return []

    with open(cities_json_path, encoding='utf-8') as f:
        cities = json.load(f)

    tile_dir = Path(TILE_DIR)
    problems = []
    checked = 0

    for city in cities:
        lat, lon = city.get('la', 0), city.get('lo', 0)
        if lat == 0 and lon == 0:
            continue

        lat_int, lon_int = int(np.floor(lat)), int(np.floor(lon))
        frac_lat, frac_lon = lat - lat_int, lon - lon_int

        tile_path = tile_dir / f"{lon_int}_{lat_int}.qtree"
        pop_path = tile_dir / f"{lon_int}_{lat_int}.pop"

        if not tile_path.exists() or not pop_path.exists() or tile_path.stat().st_size <= 1:
            continue

        try:
            with open(tile_path, 'rb') as f:
                raw = f.read()
            terrain_data = raw[16:] if raw[:4] == b'QTR5' else raw
            with open(pop_path, 'rb') as f:
                raw_pop = f.read()
            pop_data = raw_pop[16:] if raw_pop[:4] == b'QTR5' else raw_pop

            tn = navigate_qtr5(terrain_data, frac_lat, frac_lon)
            pn = navigate_qtr5_pop(pop_data, frac_lat, frac_lon)

            if not tn or not tn.get('is_leaf') or not pn or not pn.get('is_leaf'):
                continue

            checked += 1
            zone = tn.get('zone', -1)
            pop = pn.get('pop_density', 0)
            urban = pn.get('urban_zone', 0)

            issues = []
            if zone == ZONE_WATER and pop >= pop_threshold:
                issues.append(f"zone=water but pop={pop:.0f}")
            if pop >= pop_threshold and urban == 0:
                issues.append(f"pop={pop:.0f} but urban=0")

            if issues:
                name = city.get('n', city.get('name', '?'))
                problems.append((name, lat, lon, zone, pop, urban, issues))
        except Exception:
            continue

    print(f"Checked {checked} cities, found {len(problems)} with problems")
    if problems:
        for name, lat, lon, zone, pop, urban, issues in sorted(problems, key=lambda x: -x[4])[:30]:
            zn = ZONE_NAMES.get(zone, f"?({zone})")
            print(f"  {name} ({lat},{lon}): zone={zn}, pop={pop:.0f}, urban={urban} -> {'; '.join(issues)}")
        if len(problems) > 30:
            print(f"  ... and {len(problems) - 30} more")
    return problems


def main():
    parser = argparse.ArgumentParser(description="GeoBaker Verification Toolkit")
    parser.add_argument("--pop-threshold", type=float, default=10.0)
    parser.add_argument("--sample-grid", type=int, default=5)
    parser.add_argument("--check", choices=["urban", "water", "cities", "all"], default="all")
    parser.add_argument("--cities-json", type=str, default=None)
    parser.add_argument("--quick", action="store_true", help="Cities only, skip tile scan")
    args = parser.parse_args()

    sample_points = _make_sample_points(args.sample_grid)
    results = {}

    if args.check in ("all", "urban") and not args.quick:
        results["urban"] = check_urban_pop_mismatch(args.pop_threshold, sample_points)

    if args.check in ("all", "water") and not args.quick:
        results["water"] = check_water_pop_mismatch(args.pop_threshold, sample_points)

    if args.check in ("all", "cities"):
        results["cities"] = check_cities(args.pop_threshold, args.cities_json)

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for k, v in results.items():
        print(f"  {k}: {len(v)} problems")

    return 0 if all(len(v) == 0 for v in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
