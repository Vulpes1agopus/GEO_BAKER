#!/usr/bin/env python3
"""
Build country-level interop data from GeoJSON sources.

Subcommands:
  grid    – Rasterize admin polygons to a compact grid layer (npz + meta json)
  vector  – Export sovereign-level boundary vectors with SAR mechanism (JSON/HPP/Dart/SVG)

Usage:
  python build_country_interop.py grid --input-geojson data/cn_admin.geojson
  python build_country_interop.py vector --input-geojson data/world_with_cn_overlay.geojson
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import matplotlib.pyplot as plt
import numpy as np
from matplotlib import colors
from rasterio.features import rasterize
from rasterio.transform import from_origin
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.ops import unary_union


OFFICIAL_SOURCE_HINTS = [
    {"name": "Natural Resources Standard Map Service", "org": "Ministry of Natural Resources (PRC)", "url": "https://bzdt.ch.mnr.gov.cn/", "note": "Official standard maps and map review references."},
    {"name": "National Geomatics Center of China", "org": "NGCC / TianDiTu", "url": "https://www.tianditu.gov.cn/", "note": "National geographic public service platform and reference layers."},
    {"name": "Administrative Division Codes", "org": "National Bureau of Statistics (PRC)", "url": "https://www.stats.gov.cn/sj/tjbz/tjyqhdmhcxhfdm/", "note": "Authoritative statistical administrative division codes."},
    {"name": "Administrative Division Information", "org": "Ministry of Civil Affairs (PRC)", "url": "https://www.mca.gov.cn/", "note": "Official administrative division adjustments and notices."},
]

SAR_A3_CODES = {"HKG", "MAC", "TWN"}
SAR_A2_CODES = {"HK", "MO", "TW"}
SAR_NAMES_EN = ["Hong Kong", "Macao", "Taiwan"]

REFERENCE_WORLD_OVERVIEW_EN = {
    "countries_total": 197,
    "countries_breakdown": {
        "un_member_states": 193,
        "un_observer_states": {"count": 2, "items": ["Palestine", "Vatican City"]},
        "non_un_member_states": {"count": 2, "items": ["Niue", "Cook Islands"]},
    },
    "regions_total": 36,
    "regions_breakdown": {
        "french_territories": 10, "british_territories": 9, "us_territories": 5,
        "australian_territories": 3, "dutch_territories": 3, "danish_territories": 2,
        "new_zealand_territories": 1,
        "undetermined_control_territories": {"count": 2, "items": ["Western Sahara (de facto controlled by Morocco)", "Falkland Islands (Malvinas) (de facto administered by the United Kingdom)"]},
        "shared_territories": {"count": 1, "items": ["Antarctica"]},
    },
    "special_administrative_regions": {"count": 3, "items": SAR_NAMES_EN},
    "summary_en": (
        "The world has 197 countries and 36 regions. "
        "Countries include 193 UN member states, 2 UN observer states "
        "(Palestine and Vatican City), and 2 non-UN member states "
        "(Niue and the Cook Islands). "
        "Regions include 10 French territories, 9 British territories, "
        "5 U.S. territories, 3 Australian territories, 3 Dutch territories, "
        "2 Danish territories, 1 New Zealand territory, "
        "2 undetermined-control territories (Western Sahara and Falkland Islands/Malvinas), "
        "and 1 shared territory (Antarctica). "
        "Hong Kong, Macao, and Taiwan are listed as special administrative regions."
    ),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build country-level interop data from GeoJSON")
    sub = parser.add_subparsers(dest="command", required=True)

    grid_p = sub.add_parser("grid", help="Rasterize admin polygons to grid layer")
    grid_p.add_argument("--input-geojson", type=str, required=True, help="Input GeoJSON path")
    grid_p.add_argument("--code-field", type=str, default="adcode", help="Property name for admin code")
    grid_p.add_argument("--name-field", type=str, default="name", help="Property name for region name")
    grid_p.add_argument("--lon-min", type=float, default=73.0)
    grid_p.add_argument("--lon-max", type=float, default=136.0)
    grid_p.add_argument("--lat-min", type=float, default=3.0)
    grid_p.add_argument("--lat-max", type=float, default=54.0)
    grid_p.add_argument("--resolution-deg", type=float, default=0.05)
    grid_p.add_argument("--all-touched", action="store_true")
    grid_p.add_argument("--max-features", type=int, default=0)
    grid_p.add_argument("--output-grid", type=str, default="interop/china_admin_grid.npz")
    grid_p.add_argument("--output-meta", type=str, default="interop/china_admin_grid_meta.json")
    grid_p.add_argument("--output-preview", type=str, default="interop/china_admin_grid_preview.png")
    grid_p.add_argument("--no-preview", action="store_true")
    grid_p.add_argument("--print-sources", action="store_true")

    vec_p = sub.add_parser("vector", help="Export sovereign boundary vectors with SAR")
    vec_p.add_argument("--input-geojson", type=str, default="data/world_with_cn_overlay.geojson")
    vec_p.add_argument("--out-dir", type=str, default="interop")
    vec_p.add_argument("--base-name", type=str, default="world_country_vector_v2")
    vec_p.add_argument("--coord-scale", type=int, default=1_000_000)
    vec_p.add_argument("--simplify-deg", type=float, default=0.02)
    vec_p.add_argument("--grid-step-deg", type=float, default=30.0)

    return parser


def _normalize_code(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits if digits else text


def _load_geojson_features(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict) and data.get("type") == "FeatureCollection":
        features = data.get("features", [])
    elif isinstance(data, list):
        features = data
    else:
        raise ValueError("Input must be a FeatureCollection or a list of features")
    if not isinstance(features, list) or not features:
        raise ValueError("No features found in input GeoJSON")
    return features


def _iter_shapes(features: Iterable[Dict[str, Any]], code_field: str, name_field: str, max_features: int) -> Tuple[List[Tuple[Dict[str, Any], int]], List[Dict[str, Any]]]:
    shapes: List[Tuple[Dict[str, Any], int]] = []
    regions: List[Dict[str, Any]] = []
    idx = 0
    for feature in features:
        if max_features > 0 and idx >= max_features:
            break
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry")
        if not geometry:
            continue
        props = feature.get("properties") or {}
        code = _normalize_code(props.get(code_field))
        name = str(props.get(name_field, "")).strip()
        region_id = idx + 1
        if region_id > 65535:
            raise ValueError("Feature count exceeds uint16 capacity (65535)")
        shapes.append((geometry, region_id))
        regions.append({"region_id": region_id, "admin_code": code, "name": name})
        idx += 1
    if not shapes:
        raise ValueError("No valid polygon features to rasterize")
    return shapes, regions


def _build_grid(shapes, lon_min, lon_max, lat_min, lat_max, resolution_deg, all_touched) -> Tuple[np.ndarray, Dict[str, Any]]:
    if resolution_deg <= 0:
        raise ValueError("resolution-deg must be positive")
    if lon_max <= lon_min or lat_max <= lat_min:
        raise ValueError("Invalid bbox bounds")
    width = int(math.ceil((lon_max - lon_min) / resolution_deg))
    height = int(math.ceil((lat_max - lat_min) / resolution_deg))
    transform = from_origin(lon_min, lat_max, resolution_deg, resolution_deg)
    grid = rasterize(shapes=shapes, out_shape=(height, width), transform=transform, fill=0, all_touched=all_touched, dtype=np.uint16)
    info = {"width": width, "height": height, "lon_min": lon_min, "lon_max": lon_max, "lat_min": lat_min, "lat_max": lat_max, "resolution_deg": resolution_deg, "nodata": 0}
    return grid, info


def _save_grid_outputs(grid, info, regions, output_grid, output_meta) -> None:
    output_grid.parent.mkdir(parents=True, exist_ok=True)
    output_meta.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(output_grid, grid=grid, width=np.int32(info["width"]), height=np.int32(info["height"]),
                        lon_min=np.float64(info["lon_min"]), lon_max=np.float64(info["lon_max"]),
                        lat_min=np.float64(info["lat_min"]), lat_max=np.float64(info["lat_max"]),
                        resolution_deg=np.float64(info["resolution_deg"]), nodata=np.uint16(info["nodata"]))
    meta = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "grid": info, "regions": regions}
    output_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _save_grid_preview(grid, info, output_preview) -> None:
    output_preview.parent.mkdir(parents=True, exist_ok=True)
    max_id = int(np.max(grid))
    if max_id <= 0:
        cmap = colors.ListedColormap(["#efefef"])
        norm = colors.BoundaryNorm([-0.5, 0.5], cmap.N)
    else:
        base = plt.get_cmap("tab20")
        palette = ["#efefef"] + [base(i % base.N) for i in range(1, max_id + 1)]
        cmap = colors.ListedColormap(palette)
        norm = colors.BoundaryNorm(np.arange(-0.5, max_id + 1.5, 1.0), cmap.N)
    fig, ax = plt.subplots(figsize=(8.0, 6.0), constrained_layout=True)
    extent = [info["lon_min"], info["lon_max"], info["lat_min"], info["lat_max"]]
    view = np.flipud(grid.astype(np.float32))
    im = ax.imshow(view, extent=extent, origin="lower", cmap=cmap, norm=norm, interpolation="nearest")
    ax.set_title("Admin Grid Preview")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.grid(True, color="white", alpha=0.3, linewidth=0.5)
    cbar = fig.colorbar(im, ax=ax, fraction=0.045, pad=0.04)
    cbar.set_label("region_id")
    fig.savefig(output_preview, dpi=140)
    plt.close(fig)


def cmd_grid(args: argparse.Namespace) -> None:
    if args.print_sources:
        for item in OFFICIAL_SOURCE_HINTS:
            print(f"- {item['name']} ({item['org']}): {item['url']}")
        return
    input_geojson = Path(args.input_geojson)
    if not input_geojson.exists():
        raise FileNotFoundError(f"Input file not found: {input_geojson}")
    features = _load_geojson_features(input_geojson)
    shapes, regions = _iter_shapes(features, args.code_field, args.name_field, args.max_features)
    grid, info = _build_grid(shapes, args.lon_min, args.lon_max, args.lat_min, args.lat_max, args.resolution_deg, args.all_touched)
    output_grid = Path(args.output_grid)
    output_meta = Path(args.output_meta)
    _save_grid_outputs(grid, info, regions, output_grid, output_meta)
    if not args.no_preview:
        _save_grid_preview(grid, info, Path(args.output_preview))
    covered = int(np.count_nonzero(grid))
    total = int(grid.size)
    print(f"grid: {info['height']}x{info['width']} @ {info['resolution_deg']:.6f} deg, coverage: {covered}/{total} ({covered / total * 100:.2f}%)")
    print(f"saved: {output_grid} ({output_grid.stat().st_size / 1024 / 1024:.2f} MB)")


def _pick_name(props: Dict[str, Any]) -> str:
    for k in ("NAME_EN", "FORMAL_EN", "NAME_LONG", "NAME", "ADMIN", "SOVEREIGNT", "name", "NAME_ZH", "cn_name"):
        v = props.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return "UNKNOWN"


def _pick_iso(props: Dict[str, Any]) -> str:
    for k in ("ISO_A2", "iso_a2"):
        v = props.get(k)
        if v is not None and str(v).strip():
            return str(v).strip().upper()
    return ""


def _pick_adm0(props: Dict[str, Any]) -> str:
    for k in ("ADM0_A3", "adm0_a3"):
        v = props.get(k)
        if v is not None and str(v).strip():
            return str(v).strip().upper()
    return ""


def _pick_sov_a3(props: Dict[str, Any]) -> str:
    for k in ("SOV_A3", "sov_a3", "ADM0_A3", "adm0_a3"):
        v = props.get(k)
        if v is not None and str(v).strip() and str(v).strip() != "-99":
            return str(v).strip().upper()
    return ""


def _classify_region(props: Dict[str, Any]) -> Tuple[str, str]:
    iso_a2 = _pick_iso(props)
    adm0_a3 = _pick_adm0(props)
    sov_a3 = _pick_sov_a3(props)
    if iso_a2 in SAR_A2_CODES or adm0_a3 in SAR_A3_CODES or sov_a3 in SAR_A3_CODES:
        return "CHN", "SAR"
    if sov_a3:
        return sov_a3, "NORMAL"
    if adm0_a3:
        return adm0_a3, "NORMAL"
    if iso_a2:
        return iso_a2, "NORMAL"
    return (_pick_name(props).upper() or "UNKNOWN"), "NORMAL"


def _quantize_ring_xy(ring_xy: List[Tuple[float, float]], scale: int) -> List[int]:
    out: List[int] = []
    for x, y in ring_xy:
        out.append(int(round(x * scale)))
        out.append(int(round(y * scale)))
    return out


def _ring_to_xy_list(coords) -> List[Tuple[float, float]]:
    pts = [(float(x), float(y)) for x, y in coords]
    if pts and pts[0] != pts[-1]:
        pts.append(pts[0])
    return pts


def _geom_polygons(geom) -> List[Polygon]:
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    return []


def _build_vector_payload(fc: Dict[str, Any], coord_scale: int, simplify_deg: float) -> Tuple[Dict[str, Any], List[Tuple[str, Any]]]:
    features = fc.get("features", [])
    sovereign_regions: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for feat in features:
        if not isinstance(feat, dict):
            continue
        geom_json = feat.get("geometry")
        if not geom_json:
            continue
        try:
            geom = shape(geom_json)
        except Exception:
            continue
        if geom.is_empty:
            continue
        if simplify_deg > 0:
            geom = geom.simplify(simplify_deg, preserve_topology=True)
        if not geom.is_valid:
            geom = geom.buffer(0)
        if geom.is_empty:
            continue
        polygons = _geom_polygons(geom)
        if not polygons:
            continue
        props = feat.get("properties") or {}
        sovereign_key, region_class = _classify_region(props)
        sovereign_regions[sovereign_key].append({
            "region_name": _pick_name(props), "region_class": region_class,
            "iso_a2": _pick_iso(props), "adm0_a3": _pick_adm0(props), "sov_a3": _pick_sov_a3(props),
            "geometry": MultiPolygon(polygons) if len(polygons) > 1 else polygons[0],
        })

    sovereign_entities: List[Dict[str, Any]] = []
    admin_regions: List[Dict[str, Any]] = []
    rings: List[Dict[str, Any]] = []
    plot_geoms: List[Tuple[str, Any]] = []
    sovereign_id = 0
    region_id = 0

    for sovereign_key in sorted(sovereign_regions.keys()):
        entries = sovereign_regions[sovereign_key]
        if not entries:
            continue
        sovereign_name = None
        for e in entries:
            if e["region_class"] == "NORMAL":
                sovereign_name = e["region_name"]
                break
        if not sovereign_name:
            sovereign_name = entries[0]["region_name"]
        union_geom = unary_union([e["geometry"] for e in entries])
        if union_geom.is_empty:
            continue
        if not union_geom.is_valid:
            union_geom = union_geom.buffer(0)
        if union_geom.is_empty:
            continue
        minx, miny, maxx, maxy = union_geom.bounds
        entity_region_start = len(admin_regions)
        entity_ring_start = len(rings)

        for e in entries:
            region_geom = e["geometry"]
            if region_geom.is_empty:
                continue
            if not region_geom.is_valid:
                region_geom = region_geom.buffer(0)
            if region_geom.is_empty:
                continue
            region_ring_start = len(rings)
            polygons = _geom_polygons(region_geom)
            for poly in polygons:
                ext_xy = _ring_to_xy_list(poly.exterior.coords)
                if len(ext_xy) >= 4:
                    rings.append({"sovereign_id": sovereign_id, "region_id": region_id, "is_hole": 0, "points_q": _quantize_ring_xy(ext_xy, coord_scale)})
                for inner in poly.interiors:
                    inner_xy = _ring_to_xy_list(inner.coords)
                    if len(inner_xy) >= 4:
                        rings.append({"sovereign_id": sovereign_id, "region_id": region_id, "is_hole": 1, "points_q": _quantize_ring_xy(inner_xy, coord_scale)})
            region_ring_count = len(rings) - region_ring_start
            if region_ring_count <= 0:
                continue
            rminx, rminy, rmaxx, rmaxy = region_geom.bounds
            admin_regions.append({
                "region_id": region_id, "sovereign_id": sovereign_id, "region_name": e["region_name"],
                "region_class": e["region_class"], "iso_a2": e["iso_a2"], "adm0_a3": e["adm0_a3"], "sov_a3": e["sov_a3"],
                "bbox_q": [int(round(rminx * coord_scale)), int(round(rminy * coord_scale)), int(round(rmaxx * coord_scale)), int(round(rmaxy * coord_scale))],
                "ring_start": region_ring_start, "ring_count": region_ring_count,
            })
            region_id += 1

        entity_region_count = len(admin_regions) - entity_region_start
        entity_ring_count = len(rings) - entity_ring_start
        if entity_region_count <= 0 or entity_ring_count <= 0:
            continue
        sovereign_entities.append({
            "sovereign_id": sovereign_id, "sovereign_key": sovereign_key, "sovereign_name": sovereign_name,
            "bbox_q": [int(round(minx * coord_scale)), int(round(miny * coord_scale)), int(round(maxx * coord_scale)), int(round(maxy * coord_scale))],
            "region_start": entity_region_start, "region_count": entity_region_count,
            "ring_start": entity_ring_start, "ring_count": entity_ring_count,
        })
        plot_geoms.append((sovereign_name, union_geom))
        sovereign_id += 1

    payload = {
        "version": 2, "coord_scale": coord_scale, "simplify_deg": simplify_deg, "name_language": "en",
        "special_admin_region_codes_a3": sorted(SAR_A3_CODES), "special_admin_region_codes_a2": sorted(SAR_A2_CODES),
        "special_admin_region_names_en": SAR_NAMES_EN, "taiwan_policy": "MERGED_TO_CHINA_AS_SAR",
        "reference_world_overview_en": REFERENCE_WORLD_OVERVIEW_EN,
        "sovereign_entities": sovereign_entities, "admin_regions": admin_regions, "rings": rings,
    }
    return payload, plot_geoms


def _write_svg_preview(path: Path, plot_geoms: List[Tuple[str, Any]], grid_step_deg: float) -> None:
    fig, ax = plt.subplots(figsize=(18, 9), constrained_layout=True)
    lon_vals = np.arange(-180, 180 + 0.001, grid_step_deg)
    lat_vals = np.arange(-90, 90 + 0.001, grid_step_deg)
    for lon in lon_vals:
        ax.plot([lon, lon], [-90, 90], color="#cfd8dc", linewidth=0.4, zorder=0)
    for lat in lat_vals:
        ax.plot([-180, 180], [lat, lat], color="#cfd8dc", linewidth=0.4, zorder=0)
    for name, geom in plot_geoms:
        polys = _geom_polygons(geom)
        if not polys:
            continue
        for poly in polys:
            x, y = poly.exterior.xy
            ax.plot(x, y, color="#455a64", linewidth=0.4, zorder=2)
        rp = geom.representative_point()
        ax.text(rp.x, rp.y, name, fontsize=5, color="#263238", ha="center", va="center", zorder=3)
    ax.set_xlim(-180, 180)
    ax.set_ylim(-90, 90)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_title("Sovereign Vector Preview (with labels)")
    ax.set_aspect("equal", adjustable="box")
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, format="svg")
    plt.close(fig)


def _write_cpp_reader(path: Path) -> None:
    lines = [
        "#pragma once", "#include <cstdint>", "#include <string>", "#include <vector>", "",
        "namespace world_country_vector_v2 {",
        "struct Ring { int sovereign_id = -1; int region_id = -1; int is_hole = 0; std::vector<std::int32_t> points_q; };",
        "struct SovereignEntity { int sovereign_id = -1; std::string sovereign_key; std::string sovereign_name; std::int32_t bbox_q[4] = {0,0,0,0}; int region_start = 0; int region_count = 0; int ring_start = 0; int ring_count = 0; };",
        "struct AdminRegion { int region_id = -1; int sovereign_id = -1; std::string region_name; std::string region_class; std::string iso_a2; std::string adm0_a3; std::string sov_a3; std::int32_t bbox_q[4] = {0,0,0,0}; int ring_start = 0; int ring_count = 0; };",
        "struct Dataset { int version = 0; int coord_scale = 1000000; double simplify_deg = 0.0; std::vector<std::string> special_admin_region_codes_a3; std::vector<std::string> special_admin_region_codes_a2; std::string taiwan_policy; std::vector<SovereignEntity> sovereign_entities; std::vector<AdminRegion> admin_regions; std::vector<Ring> rings; };",
        "inline double q_to_double(std::int32_t v, int coord_scale) { return static_cast<double>(v) / static_cast<double>(coord_scale); }",
        "}",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_dart_reader(path: Path) -> None:
    lines = [
        "library world_country_vector_v2_reader;", "",
        "class Ring { final int sovereignId; final int regionId; final int isHole; final List<int> pointsQ; const Ring({required this.sovereignId, required this.regionId, required this.isHole, required this.pointsQ}); }", "",
        "class SovereignEntity { final int sovereignId; final String sovereignKey; final String sovereignName; final List<int> bboxQ; final int regionStart; final int regionCount; final int ringStart; final int ringCount; const SovereignEntity({required this.sovereignId, required this.sovereignKey, required this.sovereignName, required this.bboxQ, required this.regionStart, required this.regionCount, required this.ringStart, required this.ringCount}); }", "",
        "class AdminRegion { final int regionId; final int sovereignId; final String regionName; final String regionClass; final String isoA2; final String adm0A3; final String sovA3; final List<int> bboxQ; final int ringStart; final int ringCount; const AdminRegion({required this.regionId, required this.sovereignId, required this.regionName, required this.regionClass, required this.isoA2, required this.adm0A3, required this.sovA3, required this.bboxQ, required this.ringStart, required this.ringCount}); }", "",
        "class Dataset { final int version; final int coordScale; final double simplifyDeg; final List<String> specialAdminRegionCodesA3; final List<String> specialAdminRegionCodesA2; final String taiwanPolicy; final List<SovereignEntity> sovereignEntities; final List<AdminRegion> adminRegions; final List<Ring> rings; const Dataset({required this.version, required this.coordScale, required this.simplifyDeg, required this.specialAdminRegionCodesA3, required this.specialAdminRegionCodesA2, required this.taiwanPolicy, required this.sovereignEntities, required this.adminRegions, required this.rings}); }", "",
        "double qToDouble(int v, int coordScale) => v / coordScale;",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_vector(args: argparse.Namespace) -> None:
    in_path = Path(args.input_geojson)
    if not in_path.exists():
        raise FileNotFoundError(f"Input not found: {in_path}")
    fc = json.loads(in_path.read_text(encoding="utf-8-sig"))
    if not isinstance(fc, dict) or fc.get("type") != "FeatureCollection":
        raise ValueError("Input must be FeatureCollection")
    payload, plot_geoms = _build_vector_payload(fc, coord_scale=args.coord_scale, simplify_deg=args.simplify_deg)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{args.base_name}.json"
    hpp_path = out_dir / f"{args.base_name}_reader.hpp"
    dart_path = out_dir / f"{args.base_name}_reader.dart"
    svg_path = out_dir / f"{args.base_name}_map.svg"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    _write_cpp_reader(hpp_path)
    _write_dart_reader(dart_path)
    _write_svg_preview(svg_path, plot_geoms, grid_step_deg=args.grid_step_deg)
    sovereign_entities = payload["sovereign_entities"]
    admin_regions = payload["admin_regions"]
    rings = payload["rings"]
    points = sum(len(r["points_q"]) // 2 for r in rings)
    print(f"sovereign_entities: {len(sovereign_entities)}, admin_regions: {len(admin_regions)}, rings: {len(rings)}, points: {points}")
    print(f"saved: {json_path} ({json_path.stat().st_size / 1024 / 1024:.2f} MB), {hpp_path.name}, {dart_path.name}, {svg_path.name}")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "grid":
        cmd_grid(args)
    elif args.command == "vector":
        cmd_vector(args)


if __name__ == "__main__":
    main()
