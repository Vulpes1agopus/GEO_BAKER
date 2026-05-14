#!/usr/bin/env python3
"""
Unified visualization and verification toolkit for GeoBaker.

Subcommands:
  - verify:    tile/city consistency checks (from legacy scripts/verify.py)
  - quad-view: 4-panel tile-space renderer (from legacy scripts/quad_view.py)
  - geojson:   GeoJSON boundary preview renderer
  - heatmaps:  bbox heatmaps for elevation/population/zone/urban
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import colors
from matplotlib.colors import BoundaryNorm, ListedColormap
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from geo_baker_pkg.core import (
    TILE_DIR,
    URBAN_NAMES,
    ZONE_NAMES,
    ZONE_WATER,
    navigate_qtr5,
    navigate_qtr5_pop,
)
from geo_baker_pkg.io import query_elevation, query_population


# Shared colormaps
ZONE_CMAP = ListedColormap(["#1a5276", "#f9e79f", "#27ae60", "#7b241c"])
ZONE_NORM = BoundaryNorm([-0.5, 0.5, 1.5, 2.5, 3.5], ZONE_CMAP.N)

URBAN3_CMAP = ListedColormap(["#2c3e50", "#3498db", "#e74c3c"])
URBAN3_NORM = BoundaryNorm([-0.5, 0.5, 1.5, 2.5], URBAN3_CMAP.N)
URBAN3_NAMES = {0: "None", 1: "Residential", 2: "Commercial"}

URBAN8_CMAP = ListedColormap(["#111111", "#ffd60a", "#00bbf9", "#fb5607", "#8338ec", "#80ed99", "#ef476f", "#adb5bd"])
URBAN8_NORM = BoundaryNorm([-0.5, 0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5], URBAN8_CMAP.N)
URBAN8_LABELS = ["None", "Residential", "Commercial", "Industrial", "Mixed", "Institutional", "Reserved", "Reserved"]


def _read_node_payload(path: Path) -> Optional[bytes]:
    if not path.exists():
        return None
    raw = path.read_bytes()
    if not raw:
        return None
    if len(raw) <= 1:
        return raw
    return raw[16:] if raw[:4] == b"QTR5" else raw


def _make_sample_points(grid_size: int = 5) -> List[Tuple[float, float]]:
    g = max(1, int(grid_size))
    step = 1.0 / (g + 1)
    return [((r + 1) * step, (c + 1) * step) for r in range(g) for c in range(g)]


def cmd_verify(args: argparse.Namespace) -> None:
    tile_dir = Path(args.tile_dir or TILE_DIR)
    if not tile_dir.exists():
        print(f"No tiles directory: {tile_dir}")
        return

    sample_points = _make_sample_points(args.sample_grid)

    def check_urban_pop_mismatch(pop_threshold: float) -> List[Tuple[int, int, int, float]]:
        mismatches: List[Tuple[int, int, int, float]] = []
        for qf in sorted(tile_dir.glob("*.qtree")):
            parts = qf.stem.split("_")
            if len(parts) != 2:
                continue
            try:
                lon, lat = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            pop_path = tile_dir / f"{lon}_{lat}.pop"
            if not pop_path.exists() or qf.stat().st_size <= 1:
                continue
            terrain_data = _read_node_payload(qf)
            pop_data = _read_node_payload(pop_path)
            if terrain_data is None or pop_data is None or len(terrain_data) <= 1 or len(pop_data) <= 1:
                continue
            hits = 0
            max_pop = 0.0
            for frac_lat, frac_lon in sample_points:
                tn = navigate_qtr5(terrain_data, frac_lat, frac_lon)
                pn = navigate_qtr5_pop(pop_data, frac_lat, frac_lon)
                if not tn or not tn.get("is_leaf") or not pn or not pn.get("is_leaf"):
                    continue
                pd = float(pn.get("pop_density", 0))
                max_pop = max(max_pop, pd)
                if pd >= pop_threshold and int(pn.get("urban_zone", 0)) == 0:
                    hits += 1
            if hits > 0:
                mismatches.append((lat, lon, hits, max_pop))
        return mismatches

    def check_water_pop_mismatch(pop_threshold: float) -> List[Tuple[int, int, int, float]]:
        mismatches: List[Tuple[int, int, int, float]] = []
        for qf in sorted(tile_dir.glob("*.qtree")):
            parts = qf.stem.split("_")
            if len(parts) != 2:
                continue
            try:
                lon, lat = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            pop_path = tile_dir / f"{lon}_{lat}.pop"
            if not pop_path.exists() or qf.stat().st_size <= 1:
                continue
            terrain_data = _read_node_payload(qf)
            pop_data = _read_node_payload(pop_path)
            if terrain_data is None or pop_data is None or len(terrain_data) <= 1 or len(pop_data) <= 1:
                continue
            hits = 0
            max_pop = 0.0
            for frac_lat, frac_lon in sample_points:
                tn = navigate_qtr5(terrain_data, frac_lat, frac_lon)
                pn = navigate_qtr5_pop(pop_data, frac_lat, frac_lon)
                if not tn or not tn.get("is_leaf") or not pn or not pn.get("is_leaf"):
                    continue
                if int(tn.get("zone", -1)) != ZONE_WATER:
                    continue
                pd = float(pn.get("pop_density", 0))
                max_pop = max(max_pop, pd)
                if pd >= pop_threshold:
                    hits += 1
            if hits > 0:
                mismatches.append((lat, lon, hits, max_pop))
        return mismatches

    def check_cities(pop_threshold: float, cities_path: Path) -> List[Tuple[str, float, float, int, float, int, List[str]]]:
        if not cities_path.exists():
            return []
        cities = json.loads(cities_path.read_text(encoding="utf-8"))
        problems: List[Tuple[str, float, float, int, float, int, List[str]]] = []
        for city in cities:
            lat = float(city.get("la", 0))
            lon = float(city.get("lo", 0))
            if lat == 0 and lon == 0:
                continue
            lat_int, lon_int = int(np.floor(lat)), int(np.floor(lon))
            qf = tile_dir / f"{lon_int}_{lat_int}.qtree"
            pf = tile_dir / f"{lon_int}_{lat_int}.pop"
            if not qf.exists() or not pf.exists() or qf.stat().st_size <= 1:
                continue
            terrain_data = _read_node_payload(qf)
            pop_data = _read_node_payload(pf)
            if terrain_data is None or pop_data is None or len(terrain_data) <= 1 or len(pop_data) <= 1:
                continue
            frac_lat, frac_lon = lat - lat_int, lon - lon_int
            tn = navigate_qtr5(terrain_data, frac_lat, frac_lon)
            pn = navigate_qtr5_pop(pop_data, frac_lat, frac_lon)
            if not tn or not tn.get("is_leaf") or not pn or not pn.get("is_leaf"):
                continue
            zone = int(tn.get("zone", -1))
            pop = float(pn.get("pop_density", 0))
            urban = int(pn.get("urban_zone", 0))
            issues: List[str] = []
            if zone == ZONE_WATER and pop >= pop_threshold:
                issues.append(f"zone=water but pop={pop:.0f}")
            if pop >= pop_threshold and urban == 0:
                issues.append(f"pop={pop:.0f} but urban=0")
            if issues:
                name = str(city.get("n", city.get("name", "?")))
                problems.append((name, lat, lon, zone, pop, urban, issues))
        return problems

    print("=" * 60)
    print("GeoBaker Verify")
    print("=" * 60)
    print(f"tiles: {tile_dir}")
    print(f"pop threshold: {args.pop_threshold}")
    print(f"sample grid: {args.sample_grid}x{args.sample_grid}")

    if args.check in ("all", "urban") and not args.quick:
        u = check_urban_pop_mismatch(args.pop_threshold)
        print(f"\nUrban/pop mismatch tiles: {len(u)}")
        for lat, lon, hits, max_pop in sorted(u, key=lambda x: -x[3])[:20]:
            print(f"  {lon}_{lat}: hits={hits}, max_pop={max_pop:.0f}")

    if args.check in ("all", "water") and not args.quick:
        w = check_water_pop_mismatch(args.pop_threshold)
        print(f"\nWater/pop mismatch tiles: {len(w)}")
        for lat, lon, hits, max_pop in sorted(w, key=lambda x: -x[3])[:20]:
            print(f"  {lon}_{lat}: hits={hits}, max_pop={max_pop:.0f}")

    if args.check in ("all", "cities"):
        cpath = Path(args.cities_json) if args.cities_json else Path(__file__).resolve().parent.parent / "data" / "global_cities.json"
        c = check_cities(args.pop_threshold, cpath)
        print(f"\nCity mismatches: {len(c)}")
        for name, lat, lon, zone, pop, urban, issues in sorted(c, key=lambda x: -x[4])[:30]:
            zname = ZONE_NAMES.get(zone, str(zone))
            print(f"  {name} ({lat:.4f},{lon:.4f}) zone={zname}, pop={pop:.0f}, urban={urban} -> {'; '.join(issues)}")


def _query_tile(lat: float, lon: float, tile_dir: Path) -> Optional[Dict[str, float]]:
    lat_int, lon_int = int(np.floor(lat)), int(np.floor(lon))
    frac_lat, frac_lon = lat - lat_int, lon - lon_int
    qf = tile_dir / f"{lon_int}_{lat_int}.qtree"
    pf = tile_dir / f"{lon_int}_{lat_int}.pop"
    terrain_data = _read_node_payload(qf)
    pop_data = _read_node_payload(pf)
    if terrain_data is None or pop_data is None:
        return None
    if len(terrain_data) <= 1:
        return {"elevation": 0.0, "zone": float(ZONE_WATER), "pop": 0.0, "urban": 0.0}
    tn = navigate_qtr5(terrain_data, frac_lat, frac_lon)
    pn = navigate_qtr5_pop(pop_data, frac_lat, frac_lon) if len(pop_data) > 1 else None
    if not tn or not tn.get("is_leaf"):
        return None
    return {
        "elevation": float(tn.get("elevation", 0)),
        "zone": float(tn.get("zone", 0)),
        "pop": float(pn.get("pop_density", 0) if pn and pn.get("is_leaf") else 0),
        "urban": float(pn.get("urban_zone", 0) if pn and pn.get("is_leaf") else 0),
    }


def cmd_quad_view(args: argparse.Namespace) -> None:
    tile_dir = Path(args.tile_dir or TILE_DIR)
    if args.bbox:
        bbox = tuple(args.bbox)
    elif args.city:
        lat, lon = args.city
        half = args.span / 2.0
        bbox = (lat - half, lon - half, lat + half, lon + half)
    elif args.global_view:
        bbox = (-90.0, -180.0, 90.0, 180.0)
    else:
        raise ValueError("Need --bbox, --city, or --global-view")

    res = max(20, int(args.resolution))
    lats = np.linspace(bbox[0], bbox[2], res)
    lons = np.linspace(bbox[1], bbox[3], res)
    elev = np.full((res, res), np.nan, dtype=np.float32)
    zone = np.full((res, res), np.nan, dtype=np.float32)
    pop = np.full((res, res), np.nan, dtype=np.float32)
    urban = np.full((res, res), np.nan, dtype=np.float32)

    for i, lat in enumerate(lats):
        for j, lon in enumerate(lons):
            r = _query_tile(float(lat), float(lon), tile_dir)
            if not r:
                continue
            elev[i, j] = r["elevation"]
            zone[i, j] = r["zone"]
            pop[i, j] = r["pop"]
            urban[i, j] = r["urban"]

    fig, axes = plt.subplots(2, 2, figsize=(16, 12))
    extent = [bbox[1], bbox[3], bbox[0], bbox[2]]

    im0 = axes[0, 0].imshow(elev, origin="lower", extent=extent, cmap="terrain", interpolation="nearest", aspect="auto")
    axes[0, 0].set_title("Elevation")
    fig.colorbar(im0, ax=axes[0, 0], label="m", shrink=0.82)

    im1 = axes[0, 1].imshow(pop, origin="lower", extent=extent, cmap="magma", interpolation="nearest", aspect="auto")
    axes[0, 1].set_title("Population Density")
    fig.colorbar(im1, ax=axes[0, 1], label="/km^2", shrink=0.82)

    im2 = axes[1, 0].imshow(zone, origin="lower", extent=extent, cmap=ZONE_CMAP, norm=ZONE_NORM, interpolation="nearest", aspect="auto")
    axes[1, 0].set_title("Terrain Zone")
    cbar2 = fig.colorbar(im2, ax=axes[1, 0], ticks=[0, 1, 2, 3], shrink=0.82)
    cbar2.ax.set_yticklabels([ZONE_NAMES.get(i, f"zone-{i}") for i in range(4)])

    im3 = axes[1, 1].imshow(urban, origin="lower", extent=extent, cmap=URBAN3_CMAP, norm=URBAN3_NORM, interpolation="nearest", aspect="auto")
    axes[1, 1].set_title("Urban Zone")
    cbar3 = fig.colorbar(im3, ax=axes[1, 1], ticks=[0, 1, 2], shrink=0.82)
    cbar3.ax.set_yticklabels([URBAN3_NAMES.get(i, f"urban-{i}") for i in range(3)])

    for ax in axes.flat:
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")

    fig.suptitle(args.title or f"Quad View ({bbox[0]:.3f}..{bbox[2]:.3f}, {bbox[1]:.3f}..{bbox[3]:.3f})")
    fig.tight_layout()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=args.dpi)
    plt.close(fig)
    print(f"saved: {out}")


def _to_ring_array(coords: Iterable[Any]) -> Optional[np.ndarray]:
    pts: List[Tuple[float, float]] = []
    for c in coords:
        if not isinstance(c, (list, tuple)) or len(c) < 2:
            continue
        try:
            lon = float(c[0])
            lat = float(c[1])
        except Exception:
            continue
        pts.append((lon, lat))
    if len(pts) < 3:
        return None
    return np.asarray(pts, dtype=np.float64)


def _iter_exterior_rings(geometry: Dict[str, Any]) -> Iterable[np.ndarray]:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Polygon" and isinstance(coords, list) and coords:
        ring = _to_ring_array(coords[0])
        if ring is not None:
            yield ring
    elif gtype == "MultiPolygon" and isinstance(coords, list):
        for poly in coords:
            if not isinstance(poly, list) or not poly:
                continue
            ring = _to_ring_array(poly[0])
            if ring is not None:
                yield ring


def cmd_geojson(args: argparse.Namespace) -> None:
    ipath = Path(args.input)
    if not ipath.exists():
        raise FileNotFoundError(f"GeoJSON not found: {ipath}")
    data = json.loads(ipath.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict) and data.get("type") == "FeatureCollection":
        features = data.get("features", [])
    elif isinstance(data, dict) and data.get("type") == "Feature":
        features = [data]
    elif isinstance(data, list):
        features = data
    else:
        raise ValueError("Unsupported GeoJSON root type")

    fig, ax = plt.subplots(1, 1, figsize=(args.figure_width, args.figure_height), constrained_layout=True)
    lon_min, lon_max = float("inf"), float("-inf")
    lat_min, lat_max = float("inf"), float("-inf")
    rendered = 0

    for idx, feature in enumerate(features):
        if args.max_features > 0 and idx >= args.max_features:
            break
        if not isinstance(feature, dict):
            continue
        geom = feature.get("geometry")
        if not isinstance(geom, dict):
            continue
        for ring in _iter_exterior_rings(geom):
            xs = ring[:, 0]
            ys = ring[:, 1]
            if args.fill_alpha > 0:
                ax.fill(xs, ys, color=args.fill_color, alpha=max(0.0, min(1.0, args.fill_alpha)), linewidth=0)
            ax.plot(xs, ys, color=args.edge_color, linewidth=max(0.1, args.edge_width))
            rendered += 1
            lon_min, lon_max = min(lon_min, float(np.min(xs))), max(lon_max, float(np.max(xs)))
            lat_min, lat_max = min(lat_min, float(np.min(ys))), max(lat_max, float(np.max(ys)))

    if rendered == 0:
        raise ValueError("No Polygon/MultiPolygon rings found")

    if args.bbox:
        lon0, lat0, lon1, lat1 = args.bbox
        ax.set_xlim(min(lon0, lon1), max(lon0, lon1))
        ax.set_ylim(min(lat0, lat1), max(lat0, lat1))
    else:
        dx = max(1e-6, lon_max - lon_min)
        dy = max(1e-6, lat_max - lat_min)
        ax.set_xlim(lon_min - dx * 0.02, lon_max + dx * 0.02)
        ax.set_ylim(lat_min - dy * 0.02, lat_max + dy * 0.02)

    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.grid(True, color="#d0d0d0", linewidth=0.5, alpha=0.6)
    ax.set_title(args.title if args.title else Path(args.output).stem)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=args.dpi)
    plt.close(fig)
    print(f"saved: {out}")


def cmd_heatmaps(args: argparse.Namespace) -> None:
    lat_min, lat_max = sorted((args.lat_min, args.lat_max))
    lon_min, lon_max = sorted((args.lon_min, args.lon_max))
    rows = max(2, int(args.rows))
    cols = max(2, int(args.cols))

    lats = np.linspace(lat_max, lat_min, rows)
    lons = np.linspace(lon_min, lon_max, cols)
    elev = np.full((rows, cols), np.nan, dtype=np.float32)
    pop = np.full((rows, cols), np.nan, dtype=np.float32)
    zone = np.full((rows, cols), np.nan, dtype=np.float32)
    urban = np.full((rows, cols), np.nan, dtype=np.float32)

    for i, lat in enumerate(lats):
        for j, lon in enumerate(lons):
            e = query_elevation(float(lat), float(lon)) or {}
            p = query_population(float(lat), float(lon)) or {}
            elev[i, j] = float(e.get("elevation", np.nan))
            zone[i, j] = float(e.get("zone", np.nan))
            pop[i, j] = float(p.get("pop_density", np.nan))
            urban[i, j] = float(p.get("urban_zone", np.nan))

    extent = [lon_min, lon_max, lat_min, lat_max]
    fig, axes = plt.subplots(2, 2, figsize=(args.figure_width, args.figure_height), constrained_layout=True)

    im0 = axes[0, 0].imshow(np.flipud(elev), extent=extent, origin="lower", cmap="terrain", interpolation="nearest", aspect="auto")
    axes[0, 0].set_title("Elevation")
    fig.colorbar(im0, ax=axes[0, 0], label="m", fraction=0.046, pad=0.04)

    im1 = axes[0, 1].imshow(np.flipud(pop), extent=extent, origin="lower", cmap="magma", interpolation="nearest", aspect="auto")
    axes[0, 1].set_title("Population Density")
    fig.colorbar(im1, ax=axes[0, 1], label="/km^2", fraction=0.046, pad=0.04)

    im2 = axes[1, 0].imshow(np.flipud(zone), extent=extent, origin="lower", cmap=ZONE_CMAP, norm=ZONE_NORM, interpolation="nearest", aspect="auto")
    axes[1, 0].set_title("Terrain Zone")
    cbar2 = fig.colorbar(im2, ax=axes[1, 0], ticks=[0, 1, 2, 3], fraction=0.046, pad=0.04)
    cbar2.ax.set_yticklabels([ZONE_NAMES.get(i, f"zone-{i}") for i in range(4)])

    im3 = axes[1, 1].imshow(np.flipud(urban), extent=extent, origin="lower", cmap=URBAN8_CMAP, norm=URBAN8_NORM, interpolation="nearest", aspect="auto")
    axes[1, 1].set_title("Urban Zone")
    cbar3 = fig.colorbar(im3, ax=axes[1, 1], ticks=list(range(8)), fraction=0.046, pad=0.04)
    cbar3.ax.set_yticklabels(URBAN8_LABELS)

    for ax in axes.flat:
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")

    fig.suptitle(f"Heatmaps {rows}x{cols} lat[{lat_min:.3f},{lat_max:.3f}] lon[{lon_min:.3f},{lon_max:.3f}]")
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=args.dpi)
    plt.close(fig)
    print(f"saved: {out}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GeoBaker unified verification and visualization toolkit")
    sub = parser.add_subparsers(dest="command", required=True)

    p_verify = sub.add_parser("verify", help="Run tile/city consistency checks")
    p_verify.add_argument("--tile-dir", type=str, default=None, help="Tile directory (default from GEO_BAKER_TILE_DIR/TILE_DIR)")
    p_verify.add_argument("--pop-threshold", type=float, default=10.0)
    p_verify.add_argument("--sample-grid", type=int, default=5)
    p_verify.add_argument("--check", choices=["urban", "water", "cities", "all"], default="all")
    p_verify.add_argument("--cities-json", type=str, default=None)
    p_verify.add_argument("--quick", action="store_true", help="Skip tile-wide checks, run city check only")
    p_verify.set_defaults(func=cmd_verify)

    p_quad = sub.add_parser("quad-view", help="Render 4-panel local tile view (elevation/pop/zone/urban)")
    p_quad.add_argument("--bbox", nargs=4, type=float, metavar=("S", "W", "N", "E"))
    p_quad.add_argument("--city", nargs=2, type=float, metavar=("LAT", "LON"))
    p_quad.add_argument("--span", type=float, default=1.0)
    p_quad.add_argument("--global-view", action="store_true")
    p_quad.add_argument("--resolution", type=int, default=500)
    p_quad.add_argument("--tile-dir", type=str, default=None)
    p_quad.add_argument("--dpi", type=int, default=140)
    p_quad.add_argument("--title", type=str, default="")
    p_quad.add_argument("-o", "--output", type=str, required=True)
    p_quad.set_defaults(func=cmd_quad_view)

    p_geojson = sub.add_parser("geojson", help="Render GeoJSON boundary preview PNG")
    p_geojson.add_argument("--input", type=str, required=True)
    p_geojson.add_argument("--output", type=str, default="geojson_preview.png")
    p_geojson.add_argument("--dpi", type=int, default=140)
    p_geojson.add_argument("--figure-width", type=float, default=10.0)
    p_geojson.add_argument("--figure-height", type=float, default=6.0)
    p_geojson.add_argument("--edge-color", type=str, default="#224466")
    p_geojson.add_argument("--edge-width", type=float, default=0.6)
    p_geojson.add_argument("--fill-color", type=str, default="#9ecae1")
    p_geojson.add_argument("--fill-alpha", type=float, default=0.25)
    p_geojson.add_argument("--title", type=str, default=None)
    p_geojson.add_argument("--max-features", type=int, default=0)
    p_geojson.add_argument("--bbox", type=float, nargs=4, default=None, metavar=("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX"))
    p_geojson.set_defaults(func=cmd_geojson)

    p_heat = sub.add_parser("heatmaps", help="Render bbox heatmaps from local tiles")
    p_heat.add_argument("--lat-min", type=float, required=True)
    p_heat.add_argument("--lat-max", type=float, required=True)
    p_heat.add_argument("--lon-min", type=float, required=True)
    p_heat.add_argument("--lon-max", type=float, required=True)
    p_heat.add_argument("--rows", type=int, default=200)
    p_heat.add_argument("--cols", type=int, default=200)
    p_heat.add_argument("--dpi", type=int, default=140)
    p_heat.add_argument("--figure-width", type=float, default=16.0)
    p_heat.add_argument("--figure-height", type=float, default=12.0)
    p_heat.add_argument("-o", "--output", type=str, default="heatmaps.png")
    p_heat.set_defaults(func=cmd_heatmaps)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

