#!/usr/bin/env python3
"""
Generate simple population and elevation heatmaps from local baked tiles.

Example:
    d:/geo_data_pipeline/.venv/Scripts/python.exe visualize_heatmaps.py \
    --center-lat 39.9075 --center-lon 116.3972 --width-km 100 --height-km 100 \
        --rows 10 --cols 10 --label-cities
"""

from __future__ import annotations

import argparse
import json
import math
import struct
from functools import lru_cache
from pathlib import Path
from typing import List, Dict, Any, Tuple

import numpy as np
from matplotlib import colors, patheffects, ticker
import matplotlib.pyplot as plt
from PIL import Image

from geo_baker_pkg.io import query_elevation, query_population
from geo_baker_pkg.core import navigate_qtr5 as _navigate_qtr5, navigate_qtr5_pop as _navigate_qtr5_pop
from geo_baker_pkg.core import ZONE_NAMES, URBAN_NAMES, ZONE_WATER, URBAN_NONE


METERS_PER_DEG_LAT = 111320.0

TERRAIN_ZONE_COLORS = {
    0: "#3f88c5",
    1: "#d9c29c",
    2: "#3f9b62",
    3: "#8f8f8f",
}

TERRAIN_ZONE_LABELS = {
    0: "Water",
    1: "Natural",
    2: "Forest",
    3: "Harsh",
}

URBAN_ZONE_COLORS = {
    0: "#f0f0f0",
    1: "#f6bd60",
    2: "#e76f51",
    3: "#8d99ae",
    4: "#b565d9",
    5: "#43aa8b",
    6: "#bdbdbd",
    7: "#969696",
}

URBAN_ZONE_LABELS = {
    0: "None",
    1: "Residential",
    2: "Commercial",
    3: "Industrial",
    4: "Mixed",
    5: "Institutional",
    6: "Reserved-6",
    7: "Reserved-7",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render simple population/elevation heatmaps")
    parser.add_argument("--lat-min", type=float, default=39.75, help="Minimum latitude")
    parser.add_argument("--lat-max", type=float, default=40.05, help="Maximum latitude")
    parser.add_argument("--lon-min", type=float, default=116.2, help="Minimum longitude")
    parser.add_argument("--lon-max", type=float, default=116.6, help="Maximum longitude")
    parser.add_argument("--center-lat", type=float, default=None, help="Center latitude for km-based bounds")
    parser.add_argument("--center-lon", type=float, default=None, help="Center longitude for km-based bounds")
    parser.add_argument("--width-km", type=float, default=None, help="Map width in kilometers")
    parser.add_argument("--height-km", type=float, default=None, help="Map height in kilometers")
    parser.add_argument("--rows", type=int, default=10, help="Grid rows")
    parser.add_argument("--cols", type=int, default=10, help="Grid columns")
    parser.add_argument(
        "--preserve-min-block",
        action="store_true",
        help="Auto-set rows/cols to preserve minimum quadtree block scale",
    )
    parser.add_argument("--min-block-px", type=float, default=2.0, help="Minimum quadtree block in source pixels")
    parser.add_argument("--tile-resolution", type=int, default=1200, help="Source tile resolution per degree")
    parser.add_argument("--max-samples", type=int, default=500000, help="Upper bound for rows*cols in auto mode")
    parser.add_argument("--output", type=str, default="beijing_heatmaps.png", help="Output image path")
    parser.add_argument("--dpi", type=int, default=120, help="Output DPI")
    parser.add_argument("--figure-width", type=float, default=10.0, help="Figure width in inches")
    parser.add_argument("--figure-height", type=float, default=4.8, help="Figure height in inches")
    parser.add_argument("--scalebar-km", type=float, default=20.0, help="Scale bar target length in kilometers")
    parser.add_argument(
        "--pop-log",
        action="store_true",
        help="Use logarithmic color scale for population",
    )
    parser.add_argument(
        "--pop-vmax-percentile",
        type=float,
        default=99.0,
        help="Percentile used as max population color threshold in linear mode",
    )
    parser.add_argument(
        "--pop-gamma",
        type=float,
        default=0.65,
        help="Gamma for linear population color normalization (<1 enhances low-mid values)",
    )
    parser.add_argument(
        "--no-zero-water-values",
        action="store_false",
        dest="zero_water_values",
        help="Do not force population/elevation to 0 for water pixels in visualization",
    )
    parser.set_defaults(zero_water_values=True)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Only use local tiles; do not fall back to network queries for legacy tiles",
    )
    parser.add_argument(
        "--label-cities",
        action="store_true",
        help="Label nearby cities from global_cities.json",
    )
    parser.add_argument(
        "--render-zones",
        action="store_true",
        help="Also render terrain/urban zone maps from baked nodes",
    )
    parser.add_argument(
        "--zone-output",
        type=str,
        default=None,
        help="Output path for zone map image (default: <output>_zones.png)",
    )
    parser.add_argument("--cities-file", type=str, default="global_cities.json", help="City list JSON file")
    parser.add_argument("--max-city-labels", type=int, default=8, help="Max city labels")
    parser.add_argument("--min-city-pop", type=float, default=0.0, help="Minimum city population to annotate")
    return parser.parse_args()


def _meters_per_degree_lon(lat: float) -> float:
    return max(1.0, METERS_PER_DEG_LAT * math.cos(math.radians(lat)))


def _bounds_from_center_km(center_lat: float, center_lon: float, width_km: float, height_km: float) -> Dict[str, float]:
    half_height_deg = (height_km * 1000.0 * 0.5) / METERS_PER_DEG_LAT
    half_width_deg = (width_km * 1000.0 * 0.5) / _meters_per_degree_lon(center_lat)
    return {
        "lat_min": center_lat - half_height_deg,
        "lat_max": center_lat + half_height_deg,
        "lon_min": center_lon - half_width_deg,
        "lon_max": center_lon + half_width_deg,
    }


def _auto_resolution_from_min_block(
    bounds: Dict[str, float],
    min_block_px: float,
    tile_resolution: int,
    max_samples: int,
) -> Tuple[int, int, float]:
    if min_block_px <= 0:
        raise ValueError("min-block-px must be positive")
    if tile_resolution <= 0:
        raise ValueError("tile-resolution must be positive")
    if max_samples < 4:
        raise ValueError("max-samples must be >= 4")

    min_block_deg = min_block_px / float(tile_resolution)
    lon_span = max(1e-9, bounds["lon_max"] - bounds["lon_min"])
    lat_span = max(1e-9, bounds["lat_max"] - bounds["lat_min"])

    cols = max(2, int(math.ceil(lon_span / min_block_deg)) + 1)
    rows = max(2, int(math.ceil(lat_span / min_block_deg)) + 1)

    total = rows * cols
    if total > max_samples:
        scale = math.sqrt(total / float(max_samples))
        rows = max(2, int(rows / scale))
        cols = max(2, int(cols / scale))

    return rows, cols, min_block_deg


class _NodesView:
    __slots__ = ("_data",)

    def __init__(self, data: bytes):
        self._data = data

    def __len__(self) -> int:
        return len(self._data) // 2

    def __getitem__(self, idx: int) -> bytes:
        i = idx * 2
        return self._data[i:i + 2]


@lru_cache(maxsize=4096)
def _load_terrain_tile_nodes(lon_int: int, lat_int: int) -> Dict[str, Any]:
    path = Path("tiles") / f"{lon_int}_{lat_int}.qtree"
    if not path.exists():
        return {"kind": "missing"}

    with path.open("rb") as f:
        first = f.read(1)
        if len(first) < 1:
            return {"kind": "missing"}
        if first == b"\xff":
            return {"kind": "water"}

        header_rest = f.read(15)
        header = first + header_rest
        if len(header) < 16:
            return {"kind": "missing"}

        magic = header[:4]
        if magic == b"QTR5":
            node_count = struct.unpack("<I", header[6:10])[0]
            data = f.read(node_count * 2)
            if len(data) < node_count * 2:
                return {"kind": "missing"}
            return {"kind": "qtr5", "nodes": _NodesView(data)}

    return {"kind": "legacy"}


@lru_cache(maxsize=4096)
def _load_population_tile_nodes(lon_int: int, lat_int: int) -> Dict[str, Any]:
    path = Path("tiles") / f"{lon_int}_{lat_int}.pop"
    if not path.exists():
        return {"kind": "missing"}

    with path.open("rb") as f:
        header = f.read(16)
        if len(header) < 16:
            return {"kind": "missing"}

        magic = header[:4]
        if magic == b"QTR5":
            node_count = struct.unpack("<I", header[6:10])[0]
            data = f.read(node_count * 2)
            if len(data) < node_count * 2:
                return {"kind": "missing"}
            return {"kind": "qtr5", "nodes": _NodesView(data)}

    return {"kind": "legacy"}


def normalize_bounds(args: argparse.Namespace) -> Dict[str, float]:
    km_params = (args.center_lat, args.center_lon, args.width_km, args.height_km)
    if any(v is not None for v in km_params):
        if any(v is None for v in km_params):
            raise ValueError("center-lat, center-lon, width-km, height-km must be provided together")
        if args.width_km <= 0 or args.height_km <= 0:
            raise ValueError("width-km and height-km must be positive")
        return _bounds_from_center_km(args.center_lat, args.center_lon, args.width_km, args.height_km)

    lat_min, lat_max = sorted((args.lat_min, args.lat_max))
    lon_min, lon_max = sorted((args.lon_min, args.lon_max))
    return {
        "lat_min": lat_min,
        "lat_max": lat_max,
        "lon_min": lon_min,
        "lon_max": lon_max,
    }


def sample_grids(bounds: Dict[str, float], rows: int, cols: int, zero_water_values: bool):
    lats = np.linspace(bounds["lat_max"], bounds["lat_min"], rows)
    lons = np.linspace(bounds["lon_min"], bounds["lon_max"], cols)

    pop = np.full((rows, cols), np.nan, dtype=np.float32)
    elev = np.full((rows, cols), np.nan, dtype=np.float32)
    terrain_zone = np.full((rows, cols), -1, dtype=np.int16)
    urban_zone = np.full((rows, cols), -1, dtype=np.int16)

    pop_missing = 0
    elev_missing = 0
    terrain_zone_missing = 0
    urban_zone_missing = 0

    lat_meta: List[Tuple[int, float, float]] = []
    for lat in lats:
        lat_clamped = float(np.clip(lat, -89.999, 89.999))
        lat_int = int(math.floor(lat_clamped))
        lat_meta.append((lat_int, lat_clamped - lat_int, lat_clamped))

    lon_meta: List[Tuple[int, float, float]] = []
    for lon in lons:
        lon_clamped = float(np.clip(lon, -179.999, 179.999))
        lon_int = int(math.floor(lon_clamped))
        lon_meta.append((lon_int, lon_clamped - lon_int, lon_clamped))

    for i, (lat_int, frac_lat, lat_val) in enumerate(lat_meta):
        for j, (lon_int, frac_lon, lon_val) in enumerate(lon_meta):
            terrain_tile = _load_terrain_tile_nodes(lon_int, lat_int)

            if terrain_tile["kind"] == "qtr5":
                result = _navigate_qtr5(terrain_tile["nodes"], frac_lat, frac_lon)
                if result and result.get("is_leaf"):
                    elev[i, j] = float(result.get("elevation", 0.0))
                    terrain_zone[i, j] = int(result.get("zone", ZONE_WATER))
                else:
                    elev_missing += 1
                    terrain_zone_missing += 1
            elif terrain_tile["kind"] == "water":
                elev[i, j] = 0.0
                terrain_zone[i, j] = ZONE_WATER
            elif terrain_tile["kind"] == "legacy":
                fallback = query_elevation(lat_val, lon_val)
                if fallback is None:
                    elev_missing += 1
                    terrain_zone_missing += 1
                else:
                    elev[i, j] = float(fallback.get("elevation", 0.0))
                    terrain_zone[i, j] = int(fallback.get("zone", ZONE_WATER))
            else:
                elev_missing += 1
                terrain_zone_missing += 1

            pop_tile = _load_population_tile_nodes(lon_int, lat_int)

            if pop_tile["kind"] == "qtr5":
                result = _navigate_qtr5_pop(pop_tile["nodes"], frac_lat, frac_lon)
                if result and result.get("is_leaf"):
                    pop[i, j] = float(result.get("pop_density", 0.0))
                    urban_zone[i, j] = int(result.get("urban_zone", URBAN_NONE))
                else:
                    pop_missing += 1
                    urban_zone_missing += 1
            elif pop_tile["kind"] == "legacy":
                fallback = query_population(lat_val, lon_val)
                if fallback is None:
                    pop_missing += 1
                    urban_zone_missing += 1
                else:
                    pop[i, j] = float(fallback.get("pop_density", 0.0))
                    urban_zone[i, j] = int(fallback.get("urban_zone", URBAN_NONE))
            else:
                pop_missing += 1
                urban_zone_missing += 1

            if zero_water_values and terrain_zone[i, j] == ZONE_WATER:
                elev[i, j] = 0.0
                pop[i, j] = 0.0
                urban_zone[i, j] = URBAN_NONE

    return (
        lats,
        lons,
        pop,
        elev,
        terrain_zone,
        urban_zone,
        pop_missing,
        elev_missing,
        terrain_zone_missing,
        urban_zone_missing,
    )


def _sample_grids_local_only(bounds: Dict[str, float], rows: int, cols: int, zero_water_values: bool):
    lats = np.linspace(bounds["lat_max"], bounds["lat_min"], rows)
    lons = np.linspace(bounds["lon_min"], bounds["lon_max"], cols)

    pop = np.full((rows, cols), np.nan, dtype=np.float32)
    elev = np.full((rows, cols), np.nan, dtype=np.float32)
    terrain_zone = np.full((rows, cols), -1, dtype=np.int16)
    urban_zone = np.full((rows, cols), -1, dtype=np.int16)

    pop_missing = 0
    elev_missing = 0
    terrain_zone_missing = 0
    urban_zone_missing = 0

    for i, lat in enumerate(lats):
        lat_clamped = float(np.clip(lat, -89.999, 89.999))
        lat_int = int(math.floor(lat_clamped))
        frac_lat = lat_clamped - lat_int

        for j, lon in enumerate(lons):
            lon_clamped = float(np.clip(lon, -179.999, 179.999))
            lon_int = int(math.floor(lon_clamped))
            frac_lon = lon_clamped - lon_int
            terrain_tile = _load_terrain_tile_nodes(lon_int, lat_int)

            if terrain_tile["kind"] == "qtr5":
                result = _navigate_qtr5(terrain_tile["nodes"], frac_lat, frac_lon)
                if result and result.get("is_leaf"):
                    elev[i, j] = float(result.get("elevation", 0.0))
                    terrain_zone[i, j] = int(result.get("zone", ZONE_WATER))
                else:
                    elev_missing += 1
                    terrain_zone_missing += 1
            elif terrain_tile["kind"] == "water":
                elev[i, j] = 0.0
                terrain_zone[i, j] = ZONE_WATER
            else:
                elev_missing += 1
                terrain_zone_missing += 1

            pop_tile = _load_population_tile_nodes(lon_int, lat_int)

            if pop_tile["kind"] == "qtr5":
                result = _navigate_qtr5_pop(pop_tile["nodes"], frac_lat, frac_lon)
                if result and result.get("is_leaf"):
                    pop[i, j] = float(result.get("pop_density", 0.0))
                    urban_zone[i, j] = int(result.get("urban_zone", URBAN_NONE))
                else:
                    pop_missing += 1
                    urban_zone_missing += 1
            else:
                pop_missing += 1
                urban_zone_missing += 1

            if zero_water_values and terrain_zone[i, j] == ZONE_WATER:
                elev[i, j] = 0.0
                pop[i, j] = 0.0
                urban_zone[i, j] = URBAN_NONE

    return (
        lats,
        lons,
        pop,
        elev,
        terrain_zone,
        urban_zone,
        pop_missing,
        elev_missing,
        terrain_zone_missing,
        urban_zone_missing,
    )


def load_cities(
    cities_path: Path,
    bounds: Dict[str, float],
    max_labels: int,
    min_population: float,
) -> List[Dict[str, Any]]:
    if not cities_path.exists():
        return []

    raw = json.loads(cities_path.read_text(encoding="utf-8"))
    selected: List[Dict[str, Any]] = []

    for item in raw:
        try:
            name = str(item.get("n", "")).strip()
            pop = float(item.get("p", 0.0))
            lat = float(item["la"])
            lon = float(item["lo"])
        except Exception:
            continue

        if not name:
            continue

        if pop < min_population:
            continue

        if (
            bounds["lat_min"] <= lat <= bounds["lat_max"]
            and bounds["lon_min"] <= lon <= bounds["lon_max"]
        ):
            selected.append({"name": name, "population": pop, "lat": lat, "lon": lon})

    selected.sort(key=lambda x: x["population"], reverse=True)
    return selected[:max(0, max_labels)]


def _make_cmap(name: str):
    cmap = plt.get_cmap(name).copy()
    cmap.set_bad(color="#d9d9d9")
    return cmap


def _make_categorical_cmap(max_zone: int, color_map: Dict[int, str]):
    palette = [color_map.get(i, "#bcbcbc") for i in range(max_zone + 1)]
    cmap = colors.ListedColormap(palette)
    cmap.set_bad(color="#d9d9d9")
    boundaries = np.arange(-0.5, max_zone + 1.5, 1.0)
    norm = colors.BoundaryNorm(boundaries, cmap.N)
    return cmap, norm


def _format_zone_counts(data: np.ndarray, names: Dict[int, str], max_zone: int) -> str:
    valid = data[data >= 0]
    if valid.size == 0:
        return "none"

    counts = np.bincount(valid.astype(np.int32), minlength=max_zone + 1)
    parts: List[str] = []
    for idx in range(max_zone + 1):
        count = int(counts[idx])
        if count <= 0:
            continue
        parts.append(f"{idx}:{names.get(idx, f'zone-{idx}')}={count}")
    return ", ".join(parts) if parts else "none"


def _default_zone_output_path(heatmap_output_path: Path) -> Path:
    return heatmap_output_path.with_name(f"{heatmap_output_path.stem}_zones{heatmap_output_path.suffix}")


def _annotate_cities(ax, cities: List[Dict[str, Any]], bounds: Dict[str, float]) -> None:
    if not cities:
        return

    dx = (bounds["lon_max"] - bounds["lon_min"]) * 0.01
    dy = (bounds["lat_max"] - bounds["lat_min"]) * 0.01

    for city in cities:
        ax.scatter(city["lon"], city["lat"], s=18, c="black", edgecolors="white", linewidths=0.5, zorder=5)
        label = ax.text(
            city["lon"] + dx,
            city["lat"] + dy,
            city["name"],
            fontsize=7,
            color="black",
            zorder=6,
        )
        label.set_path_effects([patheffects.withStroke(linewidth=2.5, foreground="white", alpha=0.9)])


def _setup_axis(ax, bounds: Dict[str, float]) -> None:
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_xlim(bounds["lon_min"], bounds["lon_max"])
    ax.set_ylim(bounds["lat_min"], bounds["lat_max"])
    ax.xaxis.set_major_locator(ticker.MaxNLocator(6))
    ax.yaxis.set_major_locator(ticker.MaxNLocator(6))
    ax.xaxis.set_major_formatter(ticker.FormatStrFormatter("%.3f"))
    ax.yaxis.set_major_formatter(ticker.FormatStrFormatter("%.3f"))
    ax.grid(True, color="white", alpha=0.35, linewidth=0.6)


def _add_scale_bar(ax, bounds: Dict[str, float], target_km: float) -> None:
    if target_km <= 0:
        return

    mid_lat = 0.5 * (bounds["lat_min"] + bounds["lat_max"])
    meters_per_deg_lon = _meters_per_degree_lon(mid_lat)
    range_lon = bounds["lon_max"] - bounds["lon_min"]
    range_lat = bounds["lat_max"] - bounds["lat_min"]
    if range_lon <= 0 or range_lat <= 0:
        return

    full_width_km = range_lon * meters_per_deg_lon / 1000.0
    bar_km = min(target_km, max(1.0, full_width_km * 0.35))
    bar_deg_lon = (bar_km * 1000.0) / meters_per_deg_lon

    x0 = bounds["lon_min"] + range_lon * 0.06
    y0 = bounds["lat_min"] + range_lat * 0.06
    x1 = x0 + bar_deg_lon
    tick_h = range_lat * 0.015

    ax.plot([x0, x1], [y0, y0], color="white", linewidth=4.5, zorder=8, solid_capstyle="butt")
    ax.plot([x0, x1], [y0, y0], color="black", linewidth=2.2, zorder=9, solid_capstyle="butt")
    ax.plot([x0, x0], [y0 - tick_h, y0 + tick_h], color="black", linewidth=1.8, zorder=9)
    ax.plot([x1, x1], [y0 - tick_h, y0 + tick_h], color="black", linewidth=1.8, zorder=9)

    text_label = f"{bar_km:.0f} km" if bar_km >= 10 else f"{bar_km:.1f} km"
    label = ax.text(
        0.5 * (x0 + x1),
        y0 + tick_h * 1.9,
        text_label,
        ha="center",
        va="bottom",
        fontsize=7,
        color="black",
        zorder=10,
    )
    label.set_path_effects([patheffects.withStroke(linewidth=2.2, foreground="white", alpha=0.95)])


def render_heatmaps(
    bounds: Dict[str, float],
    pop: np.ndarray,
    elev: np.ndarray,
    output_path: Path,
    dpi: int,
    fig_width: float,
    fig_height: float,
    pop_log: bool,
    pop_vmax_percentile: float,
    pop_gamma: float,
    scalebar_km: float,
    cities: List[Dict[str, Any]],
    pop_missing: int,
    elev_missing: int,
) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(fig_width, fig_height), constrained_layout=True)

    extent = [bounds["lon_min"], bounds["lon_max"], bounds["lat_min"], bounds["lat_max"]]

    pop_plot = np.flipud(pop)
    elev_plot = np.flipud(elev)

    pop_cmap = _make_cmap("YlOrRd")
    elev_cmap = _make_cmap("terrain")

    pop_norm = None
    pop_render = pop_plot.copy()
    pop_cbar_label = "people / km^2"
    if pop_log:
        pop_render[pop_render <= 0] = np.nan
        finite = pop_render[np.isfinite(pop_render)]
        if finite.size > 0:
            pop_norm = colors.LogNorm(vmin=max(1.0, float(np.nanmin(finite))), vmax=float(np.nanmax(finite)))
            pop_cbar_label = "people / km^2 (log)"
    else:
        finite = pop_render[np.isfinite(pop_render)]
        pos = finite[finite > 0]
        if pos.size > 0:
            pct = float(np.clip(pop_vmax_percentile, 1.0, 100.0))
            vmax = float(np.percentile(pos, pct))
            vmax = max(vmax, float(np.nanmax(pos)) * 0.05, 1.0)
            gamma = max(0.1, float(pop_gamma))
            pop_render = np.clip(pop_render, 0.0, vmax)
            pop_norm = colors.PowerNorm(gamma=gamma, vmin=0.0, vmax=vmax)
            if pct < 100.0:
                pop_cbar_label = f"people / km^2 (clip p{pct:g}, gamma={gamma:g})"
            else:
                pop_cbar_label = f"people / km^2 (gamma={gamma:g})"

    im_pop = axes[0].imshow(
        pop_render,
        extent=extent,
        origin="lower",
        cmap=pop_cmap,
        norm=pop_norm,
        interpolation="nearest",
        aspect="auto",
    )
    axes[0].set_title("Population Density")
    cbar_pop = fig.colorbar(im_pop, ax=axes[0], fraction=0.046, pad=0.04)
    cbar_pop.set_label(pop_cbar_label)

    im_elev = axes[1].imshow(
        elev_plot,
        extent=extent,
        origin="lower",
        cmap=elev_cmap,
        interpolation="nearest",
        aspect="auto",
    )
    axes[1].set_title("Elevation")
    cbar_elev = fig.colorbar(im_elev, ax=axes[1], fraction=0.046, pad=0.04)
    cbar_elev.set_label("meters")

    for ax in axes:
        _setup_axis(ax, bounds)
        _annotate_cities(ax, cities, bounds)
        _add_scale_bar(ax, bounds, scalebar_km)

    fig.suptitle(
        (
            f"Heatmaps {pop.shape[0]}x{pop.shape[1]} "
            f"lat[{bounds['lat_min']:.3f}, {bounds['lat_max']:.3f}] "
            f"lon[{bounds['lon_min']:.3f}, {bounds['lon_max']:.3f}]"
        ),
        fontsize=11,
    )

    fig.text(
        0.01,
        0.01,
        f"missing samples: pop={pop_missing}, elevation={elev_missing}",
        fontsize=8,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=dpi)
    plt.close(fig)


def render_zone_maps(
    bounds: Dict[str, float],
    terrain_zone: np.ndarray,
    urban_zone: np.ndarray,
    output_path: Path,
    dpi: int,
    fig_width: float,
    fig_height: float,
    scalebar_km: float,
    cities: List[Dict[str, Any]],
    terrain_zone_missing: int,
    urban_zone_missing: int,
) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(fig_width, fig_height), constrained_layout=True)

    extent = [bounds["lon_min"], bounds["lon_max"], bounds["lat_min"], bounds["lat_max"]]

    terrain_plot = np.flipud(terrain_zone.astype(np.float32))
    terrain_plot[terrain_plot < 0] = np.nan
    urban_plot = np.flipud(urban_zone.astype(np.float32))
    urban_plot[urban_plot < 0] = np.nan

    terrain_cmap, terrain_norm = _make_categorical_cmap(3, TERRAIN_ZONE_COLORS)
    urban_cmap, urban_norm = _make_categorical_cmap(7, URBAN_ZONE_COLORS)

    im_terrain = axes[0].imshow(
        terrain_plot,
        extent=extent,
        origin="lower",
        cmap=terrain_cmap,
        norm=terrain_norm,
        interpolation="nearest",
        aspect="auto",
    )
    axes[0].set_title("Terrain Zone")
    cbar_terrain = fig.colorbar(im_terrain, ax=axes[0], fraction=0.046, pad=0.04, ticks=np.arange(0, 4))
    cbar_terrain.ax.set_yticklabels([TERRAIN_ZONE_LABELS.get(i, f"zone-{i}") for i in range(4)])

    im_urban = axes[1].imshow(
        urban_plot,
        extent=extent,
        origin="lower",
        cmap=urban_cmap,
        norm=urban_norm,
        interpolation="nearest",
        aspect="auto",
    )
    axes[1].set_title("Urban Zone")
    cbar_urban = fig.colorbar(im_urban, ax=axes[1], fraction=0.046, pad=0.04, ticks=np.arange(0, 8))
    cbar_urban.ax.set_yticklabels([URBAN_ZONE_LABELS.get(i, f"urban-{i}") for i in range(8)])

    for ax in axes:
        _setup_axis(ax, bounds)
        _annotate_cities(ax, cities, bounds)
        _add_scale_bar(ax, bounds, scalebar_km)

    fig.suptitle(
        (
            f"Zone Maps {terrain_zone.shape[0]}x{terrain_zone.shape[1]} "
            f"lat[{bounds['lat_min']:.3f}, {bounds['lat_max']:.3f}] "
            f"lon[{bounds['lon_min']:.3f}, {bounds['lon_max']:.3f}]"
        ),
        fontsize=11,
    )

    fig.text(
        0.01,
        0.01,
        f"missing samples: terrain_zone={terrain_zone_missing}, urban_zone={urban_zone_missing}",
        fontsize=8,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=dpi)
    plt.close(fig)


def main() -> None:
    args = parse_args()
    bounds = normalize_bounds(args)

    if args.rows <= 0 or args.cols <= 0:
        raise ValueError("rows and cols must be positive")

    rows = args.rows
    cols = args.cols
    min_block_deg = None
    if args.preserve_min_block:
        rows, cols, min_block_deg = _auto_resolution_from_min_block(
            bounds=bounds,
            min_block_px=args.min_block_px,
            tile_resolution=args.tile_resolution,
            max_samples=args.max_samples,
        )

    (
        lats,
        lons,
        pop,
        elev,
        terrain_zone,
        urban_zone,
        pop_missing,
        elev_missing,
        terrain_zone_missing,
        urban_zone_missing,
    ) = (
        _sample_grids_local_only(bounds, rows, cols, args.zero_water_values)
        if args.offline
        else sample_grids(bounds, rows, cols, args.zero_water_values)
    )

    cities = []
    if args.label_cities:
        cities = load_cities(
            Path(args.cities_file),
            bounds,
            max_labels=args.max_city_labels,
            min_population=args.min_city_pop,
        )

    output_path = Path(args.output)
    render_heatmaps(
        bounds=bounds,
        pop=pop,
        elev=elev,
        output_path=output_path,
        dpi=args.dpi,
        fig_width=args.figure_width,
        fig_height=args.figure_height,
        pop_log=args.pop_log,
        pop_vmax_percentile=args.pop_vmax_percentile,
        pop_gamma=args.pop_gamma,
        scalebar_km=args.scalebar_km,
        cities=cities,
        pop_missing=pop_missing,
        elev_missing=elev_missing,
    )

    zone_output_path = None
    if args.render_zones:
        zone_output_path = Path(args.zone_output) if args.zone_output else _default_zone_output_path(output_path)
        render_zone_maps(
            bounds=bounds,
            terrain_zone=terrain_zone,
            urban_zone=urban_zone,
            output_path=zone_output_path,
            dpi=args.dpi,
            fig_width=args.figure_width,
            fig_height=args.figure_height,
            scalebar_km=args.scalebar_km,
            cities=cities,
            terrain_zone_missing=terrain_zone_missing,
            urban_zone_missing=urban_zone_missing,
        )

    with Image.open(output_path) as img:
        width, height = img.size

    pop_valid = pop[np.isfinite(pop)]
    elev_valid = elev[np.isfinite(elev)]
    center_lat = 0.5 * (bounds["lat_min"] + bounds["lat_max"])
    width_km = (bounds["lon_max"] - bounds["lon_min"]) * _meters_per_degree_lon(center_lat) / 1000.0
    height_km = (bounds["lat_max"] - bounds["lat_min"]) * METERS_PER_DEG_LAT / 1000.0
    step_x_km = width_km / max(1, cols - 1)
    step_y_km = height_km / max(1, rows - 1)

    print(f"saved: {output_path}")
    print(
        "bounds: "
        f"lat[{bounds['lat_min']:.6f}, {bounds['lat_max']:.6f}] "
        f"lon[{bounds['lon_min']:.6f}, {bounds['lon_max']:.6f}]"
    )
    print(f"approx map size: {width_km:.2f} km x {height_km:.2f} km")
    print(f"grid: {rows} x {cols} (step ~ {step_x_km:.3f} km x {step_y_km:.3f} km)")
    if min_block_deg is not None:
        min_block_km_x = min_block_deg * _meters_per_degree_lon(center_lat) / 1000.0
        min_block_km_y = min_block_deg * METERS_PER_DEG_LAT / 1000.0
        print(
            "min block target: "
            f"{args.min_block_px:.2f}px @ {args.tile_resolution}/deg "
            f"(~{min_block_km_x:.3f} km x {min_block_km_y:.3f} km)"
        )
    print(f"image size: {width}x{height} px")
    print(f"file size: {output_path.stat().st_size / 1024:.1f} KB")
    print(
        "population stats: "
        f"min={float(np.nanmin(pop_valid)) if pop_valid.size else float('nan'):.2f}, "
        f"max={float(np.nanmax(pop_valid)) if pop_valid.size else float('nan'):.2f}, "
        f"mean={float(np.nanmean(pop_valid)) if pop_valid.size else float('nan'):.2f}"
    )
    print(
        "elevation stats: "
        f"min={float(np.nanmin(elev_valid)) if elev_valid.size else float('nan'):.2f}, "
        f"max={float(np.nanmax(elev_valid)) if elev_valid.size else float('nan'):.2f}, "
        f"mean={float(np.nanmean(elev_valid)) if elev_valid.size else float('nan'):.2f}"
    )
    print(f"missing samples: pop={pop_missing}, elevation={elev_missing}")
    if args.pop_log:
        print("population color mode: log")
    else:
        print(
            "population color mode: linear "
            f"(clip p{float(np.clip(args.pop_vmax_percentile, 1.0, 100.0)):g}, "
            f"gamma={max(0.1, float(args.pop_gamma)):g})"
        )
    print(f"water zero values: {'on' if args.zero_water_values else 'off'}")
    print(
        "terrain zone stats: "
        f"{_format_zone_counts(terrain_zone, ZONE_NAMES, 3)}"
    )
    print(
        "urban zone stats: "
        f"{_format_zone_counts(urban_zone, URBAN_NAMES, 7)}"
    )
    print(f"missing samples: terrain_zone={terrain_zone_missing}, urban_zone={urban_zone_missing}")
    if zone_output_path is not None:
        with Image.open(zone_output_path) as zone_img:
            zone_width, zone_height = zone_img.size
        print(f"zone map saved: {zone_output_path}")
        print(f"zone image size: {zone_width}x{zone_height} px")
        print(f"zone file size: {zone_output_path.stat().st_size / 1024:.1f} KB")
    if args.label_cities:
        print(f"city labels: {len(cities)}")


if __name__ == "__main__":
    main()
