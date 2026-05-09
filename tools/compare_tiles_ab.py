#!/usr/bin/env python3
"""
A/B compare two tile directories by sampling points in each tile.

Example:
  python3 tools/compare_tiles_ab.py \
    --base-dir /path/to/base_tiles \
    --cand-dir /path/to/candidate_tiles \
    --tile-list logs/rebake_budget_20260509.list \
    --grid 8 --out reports/ab_report.csv
"""

import argparse
import csv
import math
from pathlib import Path

from geo_baker_pkg.core import (
    ZONE_WATER,
    decode_node_16,
    decode_pop_leaf_node,
    navigate_qtr5,
    navigate_qtr5_pop,
)


def _read_blob(tile_dir: Path, lon: int, lat: int, ext: str):
    p = tile_dir / f"{lon}_{lat}{ext}"
    if not p.exists():
        return None
    return p.read_bytes()


def _query_terrain(tile_dir: Path, lat: float, lon: float):
    lat_i = int(math.floor(lat))
    lon_i = int(math.floor(lon))
    blob = _read_blob(tile_dir, lon_i, lat_i, ".qtree")
    if blob is None:
        return None
    if len(blob) <= 1:
        return {"elevation": 0, "zone": ZONE_WATER}
    node = navigate_qtr5(blob, lat - lat_i, lon - lon_i)
    if not node:
        return None
    return {"elevation": float(node.get("elevation", 0)), "zone": int(node.get("zone", 0))}


def _query_population(tile_dir: Path, lat: float, lon: float):
    lat_i = int(math.floor(lat))
    lon_i = int(math.floor(lon))
    blob = _read_blob(tile_dir, lon_i, lat_i, ".pop")
    if blob is None:
        return None
    if len(blob) <= 1:
        return {"pop_density": 0.0}
    node = navigate_qtr5_pop(blob, lat - lat_i, lon - lon_i)
    if not node:
        return None
    return {"pop_density": float(node.get("pop_density", 0))}


def _parse_tile_list(path: Path):
    tiles = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split(",")
            if len(parts) != 2:
                continue
            try:
                lon = int(parts[0])
                lat = int(parts[1])
            except ValueError:
                continue
            tiles.append((lat, lon))
    return tiles


def _default_tiles(base_dir: Path, cand_dir: Path):
    base = {(int(p.stem.split("_")[1]), int(p.stem.split("_")[0])) for p in base_dir.glob("*.qtree") if "_" in p.stem}
    cand = {(int(p.stem.split("_")[1]), int(p.stem.split("_")[0])) for p in cand_dir.glob("*.qtree") if "_" in p.stem}
    return sorted(base & cand)


def _sample_points(lat: int, lon: int, grid: int):
    for yi in range(grid):
        for xi in range(grid):
            fy = (yi + 0.5) / grid
            fx = (xi + 0.5) / grid
            yield lat + fy, lon + fx


def main():
    ap = argparse.ArgumentParser(description="A/B compare two tile directories.")
    ap.add_argument("--base-dir", required=True, help="Baseline tiles directory")
    ap.add_argument("--cand-dir", required=True, help="Candidate tiles directory")
    ap.add_argument("--tile-list", default="", help="Optional lon,lat list file")
    ap.add_argument("--grid", type=int, default=8, help="Samples per tile side")
    ap.add_argument("--out", default="", help="Optional CSV output path")
    args = ap.parse_args()

    base_dir = Path(args.base_dir)
    cand_dir = Path(args.cand_dir)
    if not base_dir.exists() or not cand_dir.exists():
        raise SystemExit("base-dir/cand-dir not found")
    if args.grid < 1:
        raise SystemExit("grid must be >= 1")

    if args.tile_list:
        tiles = _parse_tile_list(Path(args.tile_list))
    else:
        tiles = _default_tiles(base_dir, cand_dir)
    if not tiles:
        raise SystemExit("no tiles to compare")

    rows = []
    total_samples = 0
    missing_pairs = 0
    zone_diff = 0
    water_land_flip = 0
    elev_abs_sum = 0.0
    elev_abs_max = 0.0
    pop_abs_sum = 0.0
    pop_abs_max = 0.0

    for lat, lon in tiles:
        tile_samples = 0
        tile_zone_diff = 0
        tile_wl_flip = 0
        tile_elev_sum = 0.0
        tile_elev_max = 0.0
        tile_pop_sum = 0.0
        tile_pop_max = 0.0
        tile_missing = 0

        for slat, slon in _sample_points(lat, lon, args.grid):
            bt = _query_terrain(base_dir, slat, slon)
            ct = _query_terrain(cand_dir, slat, slon)
            bp = _query_population(base_dir, slat, slon)
            cp = _query_population(cand_dir, slat, slon)

            if bt is None or ct is None or bp is None or cp is None:
                tile_missing += 1
                continue

            tile_samples += 1
            total_samples += 1

            if bt["zone"] != ct["zone"]:
                tile_zone_diff += 1
                zone_diff += 1
            b_water = bt["zone"] == ZONE_WATER
            c_water = ct["zone"] == ZONE_WATER
            if b_water != c_water:
                tile_wl_flip += 1
                water_land_flip += 1

            de = abs(bt["elevation"] - ct["elevation"])
            dp = abs(bp["pop_density"] - cp["pop_density"])
            tile_elev_sum += de
            tile_elev_max = max(tile_elev_max, de)
            tile_pop_sum += dp
            tile_pop_max = max(tile_pop_max, dp)
            elev_abs_sum += de
            elev_abs_max = max(elev_abs_max, de)
            pop_abs_sum += dp
            pop_abs_max = max(pop_abs_max, dp)

        missing_pairs += tile_missing
        denom = tile_samples if tile_samples else 1
        rows.append({
            "lon": lon,
            "lat": lat,
            "samples": tile_samples,
            "missing": tile_missing,
            "zone_diff_ratio": tile_zone_diff / denom,
            "water_land_flip_ratio": tile_wl_flip / denom,
            "elev_mae": tile_elev_sum / denom,
            "elev_max_abs": tile_elev_max,
            "pop_mae": tile_pop_sum / denom,
            "pop_max_abs": tile_pop_max,
        })

    denom = total_samples if total_samples else 1
    print(f"tiles={len(tiles)} samples={total_samples} missing={missing_pairs}")
    print(f"zone_diff_ratio={zone_diff / denom:.6f} water_land_flip_ratio={water_land_flip / denom:.6f}")
    print(f"elev_mae={elev_abs_sum / denom:.3f} elev_max_abs={elev_abs_max:.3f}")
    print(f"pop_mae={pop_abs_sum / denom:.3f} pop_max_abs={pop_abs_max:.3f}")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=[
                "lon", "lat", "samples", "missing",
                "zone_diff_ratio", "water_land_flip_ratio",
                "elev_mae", "elev_max_abs", "pop_mae", "pop_max_abs",
            ])
            w.writeheader()
            w.writerows(rows)
        print(f"csv={out_path}")


if __name__ == "__main__":
    main()
