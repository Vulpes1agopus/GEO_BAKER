#!/usr/bin/env python3
"""
Clean up tiles that need re-baking:
  - Zone-mixed tiles (water+land) — need deeper quadtree splitting at boundaries
  - Tiles previously touched by fix operations — may have stale data

Backs up old tiles to data/tiles_old/, then deletes them.
After running this, use --global to re-bake the deleted tiles with the new code.
"""

import os
import sys
import shutil
import numpy as np
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from geo_baker_pkg.core import TILE_DIR, ZONE_WATER, navigate_qtr5, navigate_qtr5_pop

SAMPLE_GRID = 5
g = max(1, SAMPLE_GRID)
step = 1.0 / (g + 1)
sample_points = [((r + 1) * step, (c + 1) * step) for r in range(g) for c in range(g)]


def find_zone_mixed_tiles():
    """Find all tiles where sampling points show both water and land zones."""
    tiles = set()
    tile_dir = Path(TILE_DIR)
    if not tile_dir.exists():
        return tiles

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
            td = raw[16:] if raw[:4] == b'QTR5' else raw
            with open(pop_path, 'rb') as f:
                raw_pop = f.read()
            pd = raw_pop[16:] if raw_pop[:4] == b'QTR5' else raw_pop

            has_water = False
            has_land = False
            for fl, flo in sample_points:
                tn = navigate_qtr5(td, fl, flo)
                if not tn or not tn.get('is_leaf'):
                    continue
                if tn.get('zone') == ZONE_WATER:
                    has_water = True
                else:
                    has_land = True
                if has_water and has_land:
                    tiles.add((lat, lon))
                    break
        except Exception:
            continue

    return tiles


def find_fix_history_tiles():
    """Extract tile coordinates from bake log files."""
    import re
    tiles = set()
    pattern = re.compile(r'\[FIX[^\]]*\].*瓦片\s+(-?\d+)_(\-?\d+)')

    log_dir = Path(TILE_DIR).parent / "logs"
    if log_dir.exists():
        for lf in sorted(log_dir.glob("*.log")):
            try:
                with open(lf, encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        m = pattern.search(line)
                        if m:
                            lon, lat = int(m.group(1)), int(m.group(2))
                            tiles.add((lat, lon))
            except Exception:
                pass

    root_logs = ['bake_fix_coastal.log', 'bake_urban_fix.log', 'bake_urban_rebake.log',
                 'bake_rebake_6.log', 'fix_pop_zone_r2.log', 'fix_pop_zone_r3.log',
                 'fix_r4.log']
    for ln in root_logs:
        p = Path(ln)
        if p.exists():
            try:
                with open(p, encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        m = pattern.search(line)
                        if m:
                            lon, lat = int(m.group(1)), int(m.group(2))
                            tiles.add((lat, lon))
            except Exception:
                pass

    return tiles


def backup_and_delete(tiles):
    """Move old tiles to data/tiles_old/ then delete."""
    tile_dir = Path(TILE_DIR)
    old_dir = tile_dir.parent / "tiles_old"
    old_dir.mkdir(parents=True, exist_ok=True)

    moved = 0
    skipped = 0
    for lat, lon in sorted(tiles):
        for ext in ['.qtree', '.pop']:
            src = tile_dir / f"{lon}_{lat}{ext}"
            dst = old_dir / f"{lon}_{lat}{ext}"
            if src.exists():
                shutil.move(str(src), str(dst))
                moved += 1
            else:
                skipped += 1

    return moved, skipped


def main():
    print("=" * 60)
    print("GeoBaker Tile Cleanup & Re-bake Preparation")
    print("=" * 60)

    print("\n[1/3] Scanning zone-mixed tiles (water+land boundary)...")
    mixed = find_zone_mixed_tiles()
    print(f"      Found {len(mixed)} zone-mixed tiles")

    print("\n[2/3] Scanning fix-history tiles...")
    fix_hist = find_fix_history_tiles()
    print(f"      Found {len(fix_hist)} fix-history tiles")

    all_tiles = mixed | fix_hist
    print(f"\n      Total unique tiles: {len(all_tiles)}")

    if not all_tiles:
        print("\nNothing to clean up.")
        return

    print(f"\n[3/3] Backing up {len(all_tiles)} tiles to tiles_old/ ...")
    moved, skipped = backup_and_delete(all_tiles)
    print(f"      Moved {moved} files ({skipped} missing)")

    remaining_qtree = len(list(Path(TILE_DIR).glob("*.qtree")))
    remaining_pop = len(list(Path(TILE_DIR).glob("*.pop")))
    print(f"\n      Remaining tiles: {remaining_qtree} qtree, {remaining_pop} pop")

    print(f"\n{'=' * 60}")
    print(f"Ready! Run the following command:")
    print(f"  screen -dmS geo_global bash -c 'cd /home/fanziyu/geo_baker && \\")
    print(f"      nice -n 10 PYTHONUNBUFFERED=1 python3 -u -m geo_baker_pkg \\")
    print(f"      --global --workers 12 --conn 80 \\")
    print(f"      2>&1 | tee bake_global_r5.log; exec bash'")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
