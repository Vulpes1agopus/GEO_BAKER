#!/usr/bin/env python3
"""
Render a GeoJSON file to a PNG preview.

Supports FeatureCollection / Feature with Polygon and MultiPolygon geometry.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render GeoJSON boundaries to an image")
    parser.add_argument("--input", type=str, required=True, help="Input GeoJSON path")
    parser.add_argument("--output", type=str, default="geojson_preview.png", help="Output PNG path")
    parser.add_argument("--dpi", type=int, default=140, help="Output DPI")
    parser.add_argument("--figure-width", type=float, default=10.0, help="Figure width in inches")
    parser.add_argument("--figure-height", type=float, default=6.0, help="Figure height in inches")
    parser.add_argument("--edge-color", type=str, default="#224466", help="Boundary line color")
    parser.add_argument("--edge-width", type=float, default=0.6, help="Boundary line width")
    parser.add_argument("--fill-color", type=str, default="#9ecae1", help="Fill color")
    parser.add_argument("--fill-alpha", type=float, default=0.25, help="Fill alpha [0,1]")
    parser.add_argument("--title", type=str, default=None, help="Figure title")
    parser.add_argument("--max-features", type=int, default=0, help="Optional cap on rendered features")
    parser.add_argument(
        "--bbox",
        type=float,
        nargs=4,
        default=None,
        metavar=("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX"),
        help="Optional output bbox to crop view",
    )
    return parser.parse_args()


def load_geojson(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict) and data.get("type") == "FeatureCollection":
        features = data.get("features", [])
    elif isinstance(data, dict) and data.get("type") == "Feature":
        features = [data]
    elif isinstance(data, list):
        features = data
    else:
        raise ValueError("Unsupported GeoJSON root type")

    if not isinstance(features, list):
        raise ValueError("GeoJSON features is not a list")
    return features


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


def iter_exterior_rings(geometry: Dict[str, Any]) -> Iterable[np.ndarray]:
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


def render_geojson(
    features: List[Dict[str, Any]],
    output: Path,
    dpi: int,
    fig_w: float,
    fig_h: float,
    edge_color: str,
    edge_width: float,
    fill_color: str,
    fill_alpha: float,
    title: Optional[str],
    max_features: int,
    bbox: Optional[Tuple[float, float, float, float]],
) -> Dict[str, Any]:
    fig, ax = plt.subplots(1, 1, figsize=(fig_w, fig_h), constrained_layout=True)

    rings_drawn = 0
    features_used = 0
    lon_min = float("inf")
    lon_max = float("-inf")
    lat_min = float("inf")
    lat_max = float("-inf")

    for idx, feature in enumerate(features):
        if max_features > 0 and idx >= max_features:
            break
        if not isinstance(feature, dict):
            continue

        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            continue

        has_ring = False
        for ring in iter_exterior_rings(geometry):
            xs = ring[:, 0]
            ys = ring[:, 1]
            if fill_alpha > 0:
                ax.fill(xs, ys, color=fill_color, alpha=max(0.0, min(1.0, fill_alpha)), linewidth=0)
            ax.plot(xs, ys, color=edge_color, linewidth=max(0.1, edge_width))
            rings_drawn += 1
            has_ring = True

            lon_min = min(lon_min, float(np.min(xs)))
            lon_max = max(lon_max, float(np.max(xs)))
            lat_min = min(lat_min, float(np.min(ys)))
            lat_max = max(lat_max, float(np.max(ys)))

        if has_ring:
            features_used += 1

    if rings_drawn == 0:
        plt.close(fig)
        raise ValueError("No Polygon/MultiPolygon rings found to render")

    if bbox is not None:
        ax.set_xlim(bbox[0], bbox[2])
        ax.set_ylim(bbox[1], bbox[3])
    else:
        dx = max(1e-6, lon_max - lon_min)
        dy = max(1e-6, lat_max - lat_min)
        ax.set_xlim(lon_min - dx * 0.02, lon_max + dx * 0.02)
        ax.set_ylim(lat_min - dy * 0.02, lat_max + dy * 0.02)

    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.grid(True, color="#d0d0d0", linewidth=0.5, alpha=0.6)
    ax.set_title(title if title else output.stem)

    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=dpi)
    plt.close(fig)

    return {
        "features_used": features_used,
        "rings_drawn": rings_drawn,
        "bounds": [lon_min, lat_min, lon_max, lat_max],
    }


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"GeoJSON not found: {input_path}")

    features = load_geojson(input_path)
    output_path = Path(args.output)

    bbox = None
    if args.bbox is not None:
        lon0, lat0, lon1, lat1 = map(float, args.bbox)
        bbox = (min(lon0, lon1), min(lat0, lat1), max(lon0, lon1), max(lat0, lat1))

    stats = render_geojson(
        features=features,
        output=output_path,
        dpi=args.dpi,
        fig_w=args.figure_width,
        fig_h=args.figure_height,
        edge_color=args.edge_color,
        edge_width=args.edge_width,
        fill_color=args.fill_color,
        fill_alpha=args.fill_alpha,
        title=args.title,
        max_features=args.max_features,
        bbox=bbox,
    )

    print(f"saved: {output_path}")
    print(f"source features: {len(features)}")
    print(f"rendered features: {stats['features_used']}")
    print(f"rings drawn: {stats['rings_drawn']}")
    b = stats["bounds"]
    print(f"data bounds: lon[{b[0]:.6f}, {b[2]:.6f}] lat[{b[1]:.6f}, {b[3]:.6f}]")
    print(f"file size: {output_path.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
