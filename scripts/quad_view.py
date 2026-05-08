#!/usr/bin/env python3
"""Generate 4-panel comparison images: elevation, population, zones, urban."""

import os
import sys
import argparse
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap, BoundaryNorm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from geo_baker_pkg.core import (
    TILE_DIR, ZONE_WATER, ZONE_NATURAL, ZONE_FOREST, ZONE_HARSH,
    navigate_qtr5, navigate_qtr5_pop,
)

plt.rcParams["font.sans-serif"] = ["DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

ZONE_COLORS = ["#1a5276", "#f9e79f", "#27ae60", "#7b241c"]
ZONE_CMAP = ListedColormap(ZONE_COLORS)
ZONE_NORM = BoundaryNorm([-0.5, 0.5, 1.5, 2.5, 3.5], ZONE_CMAP.N)
ZONE_NAMES = {0: "Water", 1: "Natural", 2: "Forest", 3: "Harsh"}

URBAN_COLORS = ["#2c3e50", "#3498db", "#e74c3c"]
URBAN_CMAP = ListedColormap(URBAN_COLORS)
URBAN_NORM = BoundaryNorm([-0.5, 0.5, 1.5, 2.5], URBAN_CMAP.N)
URBAN_NAMES = {0: "None", 1: "Residential", 2: "Commercial"}


def _query_tile(lat, lon):
    lat_int, lon_int = int(np.floor(lat)), int(np.floor(lon))
    frac_lat = lat - lat_int
    frac_lon = lon - lon_int
    tile_path = os.path.join(TILE_DIR, f"{lon_int}_{lat_int}.qtree")
    pop_path = os.path.join(TILE_DIR, f"{lon_int}_{lat_int}.pop")
    if not os.path.exists(tile_path) or not os.path.exists(pop_path):
        return None
    if os.path.getsize(tile_path) <= 1:
        return {'elevation': 0, 'zone': ZONE_WATER, 'pop': 0, 'urban': 0}
    try:
        with open(tile_path, 'rb') as f:
            raw = f.read()
        terrain_data = raw[16:] if raw[:4] == b'QTR5' else raw
        with open(pop_path, 'rb') as f:
            raw_pop = f.read()
        pop_data = raw_pop[16:] if raw_pop[:4] == b'QTR5' else raw_pop
        tn = navigate_qtr5(terrain_data, frac_lat, frac_lon)
        pn = navigate_qtr5_pop(pop_data, frac_lat, frac_lon)
        if not tn or not tn.get('is_leaf'):
            return None
        return {
            'elevation': tn.get('elevation', 0),
            'zone': tn.get('zone', 0),
            'pop': pn.get('pop_density', 0) if pn and pn.get('is_leaf') else 0,
            'urban': pn.get('urban_zone', 0) if pn and pn.get('is_leaf') else 0,
        }
    except Exception:
        return None


def sample_region(bbox, resolution):
    lat_min, lon_min, lat_max, lon_max = bbox
    lats = np.linspace(lat_min, lat_max, resolution)
    lons = np.linspace(lon_min, lon_max, resolution)
    elev = np.full((resolution, resolution), np.nan, dtype=np.float32)
    zone = np.full((resolution, resolution), np.nan, dtype=np.float32)
    pop = np.full((resolution, resolution), np.nan, dtype=np.float32)
    urban = np.full((resolution, resolution), np.nan, dtype=np.float32)

    for i, lat in enumerate(lats):
        for j, lon in enumerate(lons):
            r = _query_tile(lat, lon)
            if r:
                elev[i, j] = r['elevation']
                zone[i, j] = r['zone']
                pop[i, j] = r['pop']
                urban[i, j] = r['urban']

    return elev, zone, pop, urban


def render_quad(bbox, resolution, output, title="", dpi=140):
    print(f"Sampling {resolution}x{resolution} for bbox {bbox}...")
    elev, zone, pop, urban = sample_region(bbox, resolution)

    fig, axes = plt.subplots(2, 2, figsize=(16, 14))
    extent = [bbox[1], bbox[3], bbox[0], bbox[2]]

    im0 = axes[0, 0].imshow(elev, origin="lower", extent=extent,
                             cmap="terrain", interpolation="nearest", aspect="auto")
    axes[0, 0].set_title("Elevation")
    fig.colorbar(im0, ax=axes[0, 0], label="m", shrink=0.8)

    im1 = axes[0, 1].imshow(pop, origin="lower", extent=extent,
                             cmap="magma", interpolation="nearest", aspect="auto")
    axes[0, 1].set_title("Population Density")
    fig.colorbar(im1, ax=axes[0, 1], label="/km\u00b2", shrink=0.8)

    im2 = axes[1, 0].imshow(zone, origin="lower", extent=extent,
                             cmap=ZONE_CMAP, norm=ZONE_NORM, interpolation="nearest", aspect="auto")
    axes[1, 0].set_title("Terrain Zone")
    cbar2 = fig.colorbar(im2, ax=axes[1, 0], ticks=[0, 1, 2, 3], shrink=0.8)
    cbar2.ax.set_yticklabels([ZONE_NAMES.get(i, "?") for i in range(4)])

    im3 = axes[1, 1].imshow(urban, origin="lower", extent=extent,
                             cmap=URBAN_CMAP, norm=URBAN_NORM, interpolation="nearest", aspect="auto")
    axes[1, 1].set_title("Urban Zone")
    cbar3 = fig.colorbar(im3, ax=axes[1, 1], ticks=[0, 1, 2], shrink=0.8)
    cbar3.ax.set_yticklabels([URBAN_NAMES.get(i, "?") for i in range(3)])

    for ax in axes.flat:
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")

    fig.suptitle(title or f"Quad View ({bbox[0]}~{bbox[2]}N, {bbox[1]}~{bbox[3]}E)")
    fig.tight_layout()
    fig.savefig(output, dpi=dpi)
    plt.close(fig)
    print(f"Saved: {output}")


def main():
    parser = argparse.ArgumentParser(description="4-panel geo visualization")
    parser.add_argument("--bbox", nargs=4, type=float, metavar=("S", "W", "N", "E"))
    parser.add_argument("--city", nargs=2, type=float, metavar=("LAT", "LON"))
    parser.add_argument("--span", type=float, default=1.0, help="City view span in degrees")
    parser.add_argument("--global-view", action="store_true", help="Global view")
    parser.add_argument("--width", type=int, default=1000, help="Resolution width")
    parser.add_argument("--dpi", type=int, default=140)
    parser.add_argument("-o", "--output", type=str, required=True)
    parser.add_argument("--title", type=str, default="")
    args = parser.parse_args()

    if args.bbox:
        bbox = args.bbox
    elif args.city:
        lat, lon = args.city
        s = args.span / 2
        bbox = (lat - s, lon - s, lat + s, lon + s)
    elif args.global_view:
        bbox = (-90, -180, 90, 180)
    else:
        parser.error("Need --bbox, --city, or --global-view")

    res = args.width
    render_quad(bbox, res, args.output, title=args.title, dpi=args.dpi)


if __name__ == "__main__":
    main()
