#!/usr/bin/env python3
"""
Download global country boundaries and overlay PRC-oriented China boundaries on top.

Outputs:
- raw global countries GeoJSON
- raw China admin GeoJSON
- merged world GeoJSON with China-related global features replaced by China overlay features
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests
import urllib3
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, mapping, shape
from shapely.ops import unary_union


DEFAULT_WORLD_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
    "master/geojson/ne_10m_admin_0_countries.geojson"
)
DEFAULT_CN_URL = "https://geo.datav.aliyun.com/areas_v3/bound/100000.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch world boundaries and overlay China boundaries"
    )
    parser.add_argument("--world-url", type=str, default=DEFAULT_WORLD_URL, help="Global countries GeoJSON URL")
    parser.add_argument("--china-url", type=str, default=DEFAULT_CN_URL, help="China admin GeoJSON URL")
    parser.add_argument("--timeout", type=int, default=120, help="HTTP timeout in seconds")
    parser.add_argument(
        "--allow-insecure-ssl",
        action="store_true",
        help="Allow verify=False fallback when SSL cert validation fails",
    )

    parser.add_argument(
        "--output-world",
        type=str,
        default="world_countries_ne.geojson",
        help="Path to save raw global countries GeoJSON",
    )
    parser.add_argument(
        "--output-china",
        type=str,
        default="china_admin_overlay.geojson",
        help="Path to save raw China admin GeoJSON",
    )
    parser.add_argument(
        "--output-merged",
        type=str,
        default="world_with_cn_overlay.geojson",
        help="Path to save merged world GeoJSON",
    )
    return parser.parse_args()


def _load_json_from_url(url: str, timeout: int, allow_insecure_ssl: bool) -> Dict[str, Any]:
    resp = None
    try:
        resp = requests.get(url, timeout=timeout, verify=True)
        resp.raise_for_status()
    except requests.exceptions.SSLError:
        if not allow_insecure_ssl:
            raise
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = requests.get(url, timeout=timeout, verify=False)
        resp.raise_for_status()

    if resp is None:
        raise RuntimeError(f"Failed to download JSON from {url}")

    try:
        return resp.json()
    except Exception:
        # Fallback for BOM or encoding quirks.
        text = resp.content.decode("utf-8-sig", errors="replace")
        return json.loads(text)


def _ensure_feature_collection(data: Dict[str, Any], name: str) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{name} is not a JSON object")
    if data.get("type") != "FeatureCollection":
        raise ValueError(f"{name} is not a FeatureCollection")
    features = data.get("features")
    if not isinstance(features, list):
        raise ValueError(f"{name} has invalid features array")
    return data


def _to_lower_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def _should_replace_global_feature(feature: Dict[str, Any]) -> bool:
    props = feature.get("properties") or {}

    iso_a2 = _to_lower_text(props.get("ISO_A2") or props.get("iso_a2"))
    iso_a3 = _to_lower_text(props.get("ADM0_A3") or props.get("adm0_a3"))

    if iso_a2 in {"cn", "tw", "hk", "mo"}:
        return True
    if iso_a3 in {"chn", "twn", "hkg", "mac"}:
        return True

    candidates = [
        props.get("ADMIN"),
        props.get("admin"),
        props.get("NAME"),
        props.get("name"),
        props.get("SOVEREIGNT"),
        props.get("sovereignt"),
        props.get("FORMAL_EN"),
        props.get("formal_en"),
    ]
    text = " | ".join(_to_lower_text(v) for v in candidates if v is not None)

    keywords = ["china", "taiwan", "hong kong", "macao", "macau"]
    return any(k in text for k in keywords)


def _convert_cn_feature_to_world_style(feature: Dict[str, Any], index: int) -> Dict[str, Any]:
    props = feature.get("properties") or {}
    geometry = feature.get("geometry")
    adcode = props.get("adcode")
    name = props.get("name")

    # Keep both original and harmonized fields for downstream compatibility.
    out_props = {
        "source": "china-overlay",
        "overlay_index": index,
        "cn_adcode": adcode,
        "cn_name": name,
        "name": name,
        "admin": "China",
        "sovereignt": "China",
        "iso_a2": "CN",
        "adm0_a3": "CHN",
    }

    return {
        "type": "Feature",
        "properties": out_props,
        "geometry": geometry,
    }


def _strip_interior_rings(geom):
    if geom.is_empty:
        return geom
    if isinstance(geom, Polygon):
        return Polygon(geom.exterior)
    if isinstance(geom, MultiPolygon):
        parts = [_strip_interior_rings(part) for part in geom.geoms]
        parts = [part for part in parts if not part.is_empty]
        if len(parts) == 1:
            return parts[0]
        return MultiPolygon(parts)
    if isinstance(geom, GeometryCollection):
        parts = [_strip_interior_rings(part) for part in geom.geoms]
        parts = [part for part in parts if not part.is_empty]
        if not parts:
            return geom
        if len(parts) == 1:
            return parts[0]
        return GeometryCollection(parts)
    return geom


def build_merged_feature_collection(
    world_fc: Dict[str, Any],
    cn_fc: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    world_features = world_fc.get("features", [])
    cn_features = cn_fc.get("features", [])

    kept_world: List[Dict[str, Any]] = []
    removed_count = 0

    for feature in world_features:
        if _should_replace_global_feature(feature):
            removed_count += 1
            continue
        kept_world.append(feature)

    cn_shapes = []
    for feature in cn_features:
        if isinstance(feature, dict) and feature.get("geometry"):
            try:
                geom = shape(feature["geometry"])
                if geom.is_valid:
                    cn_shapes.append(geom)
                else:
                    cn_shapes.append(geom.buffer(0))
            except Exception as e:
                print(f"Warning: Failed to parse a China geometry: {e}")

    if cn_shapes:
        print(f"Unioning {len(cn_shapes)} features into a single polygon...")
        united_cn = unary_union(cn_shapes)
        # buffer(0) helps resolve slight topological inaccuracies causing slivers
        united_cn = united_cn.buffer(0)
        united_cn = _strip_interior_rings(united_cn)
        united_cn = united_cn.buffer(0.02).buffer(-0.02)
        united_cn = united_cn.buffer(0)
        united_cn = _strip_interior_rings(united_cn)
        
        united_feature = {
            "type": "Feature",
            "properties": {
                "source": "china-overlay-united",
                "name": "China",
                "admin": "China",
                "sovereignt": "China",
                "iso_a2": "CN",
                "adm0_a3": "CHN",
            },
            "geometry": mapping(united_cn)
        }
        overlay_features = [united_feature]
    else:
        overlay_features = []

    merged = {
        "type": "FeatureCollection",
        "name": "world_with_cn_overlay",
        "meta": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "world_source": world_fc.get("name", "world-countries"),
            "cn_source": cn_fc.get("name", "china-overlay"),
            "note": "Global country features related to China/Taiwan/Hong Kong/Macao are replaced by China overlay features.",
        },
        "features": kept_world + overlay_features,
    }

    stats = {
        "world_original": len(world_features),
        "world_kept": len(kept_world),
        "world_removed": removed_count,
        "china_overlay": len(overlay_features),
        "merged_total": len(merged["features"]),
    }
    return merged, stats


def save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    args = parse_args()

    world_fc = _ensure_feature_collection(
        _load_json_from_url(args.world_url, args.timeout, args.allow_insecure_ssl),
        "world dataset",
    )
    cn_fc = _ensure_feature_collection(
        _load_json_from_url(args.china_url, args.timeout, args.allow_insecure_ssl),
        "china dataset",
    )

    merged_fc, stats = build_merged_feature_collection(world_fc, cn_fc)

    out_world = Path(args.output_world)
    out_china = Path(args.output_china)
    out_merged = Path(args.output_merged)

    save_json(out_world, world_fc)
    save_json(out_china, cn_fc)
    save_json(out_merged, merged_fc)

    print(f"saved world raw: {out_world} ({out_world.stat().st_size / 1024 / 1024:.2f} MB)")
    print(f"saved china raw: {out_china} ({out_china.stat().st_size / 1024 / 1024:.2f} MB)")
    print(f"saved merged: {out_merged} ({out_merged.stat().st_size / 1024 / 1024:.2f} MB)")
    print(
        "stats: "
        f"world_original={stats['world_original']}, "
        f"world_removed={stats['world_removed']}, "
        f"china_overlay={stats['china_overlay']}, "
        f"merged_total={stats['merged_total']}"
    )


if __name__ == "__main__":
    main()
