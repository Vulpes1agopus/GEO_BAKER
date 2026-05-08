#!/usr/bin/env python3
"""
Fetch global major facilities / landmarks as point data.

Design goals:
- Keep the dataset focused on high-traffic or highly recognizable places.
- Prefer stable, compact storage for later lookup and visualization.
- Avoid pulling every low-value OSM feature on Earth.

Default categories:
- airports and heliports
- football / stadium venues
- major transport hubs (bus / ferry / public transport, no rail)
- visitor magnets and landmarks
- heavy-use civic facilities

Output:
- tile-binary package by default
- optional JSONL.GZ export for portability

This script uses OpenStreetMap Overpass as the primary source.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import struct
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import requests

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


OVERPASS_ENDPOINTS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]

SELECTED_TAG_KEYS = (
    "name",
    "official_name",
    "short_name",
    "alt_name",
    "operator",
    "brand",
    "iata",
    "icao",
    "wikidata",
    "wikipedia",
    "capacity",
    "website",
    "tourism",
    "historic",
    "amenity",
    "leisure",
    "sport",
    "aeroway",
    "country",
    "addr:country",
)


@dataclass(frozen=True)
class Tile:
    south: float
    west: float
    north: float
    east: float


@dataclass(frozen=True)
class FacilityRecord:
    osm_type: str
    osm_id: int
    category: str
    subcategory: str
    name: str
    lat: float
    lon: float
    importance: int
    tags_json: str


# Keep the global index layout aligned with geo_baker GeoPack files.
_GPK_HEADER_SIZE = 32
_GPK_GRID_W = 360
_GPK_GRID_H = 180
_GPK_INDEX_SIZE = _GPK_GRID_W * _GPK_GRID_H * 16

_FPK_MAGIC = b"GPK3"

_FTILE_MAGIC = b"FCT1"
_FTILE_HEADER_FMT = "<4sHHII"  # magic, version, count, name_blob_len, flags
_FTILE_HEADER_SIZE = struct.calcsize(_FTILE_HEADER_FMT)
_FTILE_RECORD_FMT = "<HHBBBBQII"  # lat_q, lon_q, cat_id, sub_id, imp, type_id, osm_id, name_off, name_len
_FTILE_RECORD_SIZE = struct.calcsize(_FTILE_RECORD_FMT)

_CATEGORY_TO_ID = {
    "airport": 1,
    "attraction": 2,
    "landmark": 3,
}
_ID_TO_CATEGORY = {v: k for k, v in _CATEGORY_TO_ID.items()}

_SUBCATEGORY_TO_ID = {
    "airport": 1,
    "attraction": 2,
    "museum": 3,
    "viewpoint": 4,
    "monument": 5,
    "memorial": 6,
    "archaeological_site": 7,
    "ruins": 8,
    "castle": 9,
    "fortress": 10,
}
_ID_TO_SUBCATEGORY = {v: k for k, v in _SUBCATEGORY_TO_ID.items()}

_OSM_TYPE_TO_ID = {"node": 1, "way": 2, "relation": 3}
_ID_TO_OSM_TYPE = {v: k for k, v in _OSM_TYPE_TO_ID.items()}

DEFAULT_CATEGORY_LIMITS = {
    "airport": 2,
    "attraction": 6,
    "landmark": 6,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch global major facilities from OpenStreetMap")
    parser.add_argument("--binary-output", type=str, default="facilities.dat", help="Tile-binary package output path")
    parser.add_argument("--jsonl-output", type=str, default=None, help="Optional JSONL.GZ output path")
    parser.add_argument("--bbox", nargs=4, type=float, metavar=("SOUTH", "WEST", "NORTH", "EAST"), default=None, help="Limit to a bbox")
    parser.add_argument("--tile-deg", type=float, default=15.0, help="Tile size in degrees for Overpass requests")
    parser.add_argument("--timeout", type=int, default=120, help="Overpass timeout per request")
    parser.add_argument("--delay", type=float, default=0.5, help="Delay between successful requests")
    parser.add_argument("--min-importance", type=int, default=1, help="Minimum importance score to keep")
    parser.add_argument("--max-total", type=int, default=30, help="Max records kept after filtering (0 = unlimited)")
    parser.add_argument("--limit-per-category", type=int, default=0, help="Optional uniform max records kept per category (0 = use category defaults)")
    parser.add_argument("--no-zstd", action="store_true", help="Disable zstd compression when packing binary")
    parser.add_argument("--query-binary", nargs=2, type=float, metavar=("LAT", "LON"), default=None, help="Query facilities near a point from tile-binary")
    parser.add_argument("--query-bbox", nargs=4, type=float, metavar=("SOUTH", "WEST", "NORTH", "EAST"), default=None, help="Query facilities in bbox from tile-binary")
    parser.add_argument("--query-limit", type=int, default=30, help="Max rows shown for query mode")
    parser.add_argument("--dry-run", action="store_true", help="Only print planned tiles and categories")
    parser.add_argument("--visualize", action="store_true", help="Overlay facilities on population heatmap after fetching")
    parser.add_argument("--vis-pop-pack", type=str, default="population.dat", help="Population dat path for heatmap")
    parser.add_argument("--vis-bbox", nargs=4, type=float, metavar=("SOUTH", "WEST", "NORTH", "EAST"), default=None, help="Heatmap bbox (required with --visualize)")
    parser.add_argument("--vis-rows", type=int, default=180, help="Heatmap sample rows")
    parser.add_argument("--vis-cols", type=int, default=180, help="Heatmap sample cols")
    parser.add_argument("--vis-output", type=str, default="images/facilities_heat_overlay.png", help="Visualization output path")
    parser.add_argument("--vis-dpi", type=int, default=140, help="Visualization DPI")
    parser.add_argument("--vis-label-top", type=int, default=20, help="Label top-N facilities by importance")
    return parser.parse_args()


def iter_tiles(bbox: Tuple[float, float, float, float], tile_deg: float) -> Iterator[Tile]:
    south, west, north, east = bbox
    lat = south
    while lat < north:
        next_lat = min(north, lat + tile_deg)
        lon = west
        while lon < east:
            next_lon = min(east, lon + tile_deg)
            yield Tile(lat, lon, next_lat, next_lon)
            lon = next_lon
        lat = next_lat


def build_overpass_query(tile: Tile, timeout: int) -> str:
    s = tile.south
    w = tile.west
    n = tile.north
    e = tile.east

    clauses = [
        f'node["aeroway"~"^(aerodrome|airport)$"]["iata"]["name"~"(International Airport|国际机场|机场|Airport)",i]({s},{w},{n},{e});',
        f'way["aeroway"~"^(aerodrome|airport)$"]["iata"]["name"~"(International Airport|国际机场|机场|Airport)",i]({s},{w},{n},{e});',
        f'relation["aeroway"~"^(aerodrome|airport)$"]["iata"]["name"~"(International Airport|国际机场|机场|Airport)",i]({s},{w},{n},{e});',
        f'node["aeroway"~"^(aerodrome|airport)$"]["icao"]["name"~"(International Airport|国际机场|机场|Airport)",i]({s},{w},{n},{e});',
        f'way["aeroway"~"^(aerodrome|airport)$"]["icao"]["name"~"(International Airport|国际机场|机场|Airport)",i]({s},{w},{n},{e});',
        f'relation["aeroway"~"^(aerodrome|airport)$"]["icao"]["name"~"(International Airport|国际机场|机场|Airport)",i]({s},{w},{n},{e});',
        f'node["tourism"~"^(attraction|museum|viewpoint)$"]({s},{w},{n},{e});',
        f'way["tourism"~"^(attraction|museum|viewpoint)$"]({s},{w},{n},{e});',
        f'relation["tourism"~"^(attraction|museum|viewpoint)$"]({s},{w},{n},{e});',
        f'node["historic"~"^(monument|memorial|archaeological_site|ruins|castle|fortress)$"]({s},{w},{n},{e});',
        f'way["historic"~"^(monument|memorial|archaeological_site|ruins|castle|fortress)$"]({s},{w},{n},{e});',
        f'relation["historic"~"^(monument|memorial|archaeological_site|ruins|castle|fortress)$"]({s},{w},{n},{e});',
    ]

    return f"""
[out:json][timeout:{timeout}];
(
  {chr(10).join(clauses)}
);
out tags center qt;
""".strip()


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:  # noqa: BLE001
        return default


def _selected_tags(tags: Dict[str, str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for key in SELECTED_TAG_KEYS:
        if key in tags and tags[key]:
            result[key] = str(tags[key])
    return result


def _pick_name(tags: Dict[str, str]) -> str:
    for key in ("name", "official_name", "short_name", "alt_name"):
        value = tags.get(key)
        if value:
            return str(value)
    return ""


def classify_feature(tags: Dict[str, str]) -> Optional[Tuple[str, str, int]]:
    aeroway = tags.get("aeroway", "").lower()
    tourism = tags.get("tourism", "").lower()
    historic = tags.get("historic", "").lower()

    name = _pick_name(tags)
    has_name = 1 if name else 0
    has_wiki = 2 if (tags.get("wikidata") or tags.get("wikipedia")) else 0
    has_aviation_id = 3 if (tags.get("iata") or tags.get("icao")) else 0
    airport_name = name.lower()

    if aeroway in {"airport", "aerodrome"} and has_aviation_id > 0:
        if not (
            "international airport" in airport_name
            or "国际机场" in name
            or airport_name.endswith(" airport")
            or airport_name.endswith("机场")
        ):
            return None
        score = 16 + has_name + has_wiki + has_aviation_id
        return "airport", "airport", score

    if tourism in {"attraction", "museum", "viewpoint"}:
        if has_wiki == 0:
            return None
        score = 14 + has_name + has_wiki
        return "attraction", tourism, score

    if historic in {"monument", "memorial", "archaeological_site", "ruins", "castle", "fortress"}:
        if has_wiki == 0:
            return None
        score = 13 + has_name + has_wiki
        return "landmark", historic, score

    return None


def extract_point(element: Dict[str, object]) -> Optional[Tuple[float, float]]:
    if "lat" in element and "lon" in element:
        return _safe_float(element["lat"]), _safe_float(element["lon"])
    center = element.get("center")
    if isinstance(center, dict) and "lat" in center and "lon" in center:
        return _safe_float(center["lat"]), _safe_float(center["lon"])
    return None


def iter_records(data: Dict[str, object], min_importance: int) -> Iterator[FacilityRecord]:
    elements = data.get("elements", [])
    if not isinstance(elements, list):
        return

    seen: set[Tuple[str, int]] = set()

    for element in elements:
        if not isinstance(element, dict):
            continue

        osm_type = str(element.get("type", ""))
        osm_id = element.get("id")
        if osm_type not in {"node", "way", "relation"} or not isinstance(osm_id, int):
            continue

        tags = element.get("tags", {})
        if not isinstance(tags, dict):
            continue

        classified = classify_feature(tags)
        if classified is None:
            continue

        category, subcategory, score = classified
        if score < min_importance:
            continue

        point = extract_point(element)
        if point is None:
            continue

        key = (osm_type, osm_id)
        if key in seen:
            continue
        seen.add(key)

        selected = _selected_tags({str(k): str(v) for k, v in tags.items()})
        name = _pick_name(tags)
        yield FacilityRecord(
            osm_type=osm_type,
            osm_id=osm_id,
            category=category,
            subcategory=subcategory,
            name=name,
            lat=point[0],
            lon=point[1],
            importance=score,
            tags_json=json.dumps(selected, ensure_ascii=False, separators=(",", ":")),
        )
def write_jsonl_line(handle, record: FacilityRecord) -> None:
    handle.write(
        json.dumps(
            {
                "osm_type": record.osm_type,
                "osm_id": record.osm_id,
                "category": record.category,
                "subcategory": record.subcategory,
                "name": record.name,
                "lat": record.lat,
                "lon": record.lon,
                "importance": record.importance,
                "tags": json.loads(record.tags_json),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )


def _tile_index(lat_int: int, lon_int: int) -> int:
    return (lat_int + 90) * _GPK_GRID_W + (lon_int + 180)


def _quantize_fraction(value: float) -> int:
    value = max(0.0, min(0.999999999, value))
    return int(round(value * 65535.0))


def _encode_tile_blob(lat_int: int, lon_int: int, rows: List[FacilityRecord]) -> bytes:
    records_buf = bytearray()
    names_buf = bytearray()

    for row in rows:
        lat = float(row.lat)
        lon = float(row.lon)
        frac_lat = lat - lat_int
        frac_lon = lon - lon_int

        lat_q = _quantize_fraction(frac_lat)
        lon_q = _quantize_fraction(frac_lon)

        cat_id = _CATEGORY_TO_ID.get(str(row.category), 0)
        sub_id = _SUBCATEGORY_TO_ID.get(str(row.subcategory), 0)
        imp = max(0, min(255, int(row.importance)))
        type_id = _OSM_TYPE_TO_ID.get(str(row.osm_type), 0)
        osm_id = int(row.osm_id) & 0xFFFFFFFFFFFFFFFF

        name = str(row.name or "")
        name_raw = name.encode("utf-8", errors="replace")
        name_off = len(names_buf)
        name_len = len(name_raw)
        names_buf.extend(name_raw)

        records_buf.extend(
            struct.pack(
                _FTILE_RECORD_FMT,
                lat_q,
                lon_q,
                cat_id,
                sub_id,
                imp,
                type_id,
                osm_id,
                name_off,
                name_len,
            )
        )

    header = struct.pack(_FTILE_HEADER_FMT, _FTILE_MAGIC, 1, len(rows), len(names_buf), 0)
    return header + records_buf + names_buf


def pack_facilities_binary(records: List[FacilityRecord], output_path: Path, use_zstd: bool = True) -> Dict[str, int]:
    if not records:
        raise ValueError("No facility records to pack")

    zstd_cctx = None
    if use_zstd:
        try:
            import zstandard as zstd

            zstd_cctx = zstd.ZstdCompressor(level=3, threads=-1)
        except Exception:
            zstd_cctx = None

    buckets: Dict[Tuple[int, int], List[FacilityRecord]] = defaultdict(list)
    total_records = 0
    for row in records:
        lat = float(row.lat)
        lon = float(row.lon)
        lat_int = int(math.floor(lat))
        lon_int = int(math.floor(lon))
        if lat_int < -90 or lat_int > 89 or lon_int < -180 or lon_int > 179:
            continue
        buckets[(lat_int, lon_int)].append(row)
        total_records += 1

    index = bytearray(_GPK_INDEX_SIZE)
    data_buf = bytearray()
    data_count = 0
    raw_size = 0

    for lat_int, lon_int in sorted(buckets.keys()):
        tile_blob = _encode_tile_blob(lat_int, lon_int, buckets[(lat_int, lon_int)])
        raw_size += len(tile_blob)
        if zstd_cctx is not None:
            tile_blob = zstd_cctx.compress(tile_blob)

        entry_i = _tile_index(lat_int, lon_int)
        entry_off = entry_i * 16
        rel_off = len(data_buf)
        struct.pack_into("<QQ", index, entry_off, rel_off, len(tile_blob))
        data_buf.extend(tile_blob)
        data_count += 1

    flags = 1 if zstd_cctx is not None else 0
    header = struct.pack(
        "<4sHIIIIIIH",
        _FPK_MAGIC,
        2,
        _GPK_GRID_W,
        _GPK_GRID_H,
        data_count,
        len(data_buf),
        raw_size,
        flags,
        0,
    )

    with output_path.open("wb") as f:
        f.write(header)
        f.write(index)
        f.write(data_buf)

    return {
        "tiles": data_count,
        "records": total_records,
        "raw_size": raw_size,
        "packed_size": len(data_buf),
        "file_size": _GPK_HEADER_SIZE + _GPK_INDEX_SIZE + len(data_buf),
        "zstd": 1 if zstd_cctx is not None else 0,
    }


class FacilityPackReader:
    def __init__(self, path: Path):
        self.path = path
        self.f = path.open("rb")
        header = self.f.read(_GPK_HEADER_SIZE)
        if len(header) < _GPK_HEADER_SIZE or header[:4] != _FPK_MAGIC:
            raise ValueError(f"Invalid facilities package: {path}")

        self.grid_w, self.grid_h = struct.unpack_from("<II", header, 6)
        self.data_count = struct.unpack_from("<I", header, 14)[0]
        self.flags = struct.unpack_from("<I", header, 26)[0]
        self.use_zstd = bool(self.flags & 1)
        self._index: Optional[bytes] = None
        self._tile_cache: Dict[Tuple[int, int], List[Dict[str, object]]] = {}

        self._zstd_dctx = None
        if self.use_zstd:
            try:
                import zstandard as zstd

                self._zstd_dctx = zstd.ZstdDecompressor()
            except Exception:
                self.use_zstd = False

    def _load_index(self) -> None:
        if self._index is not None:
            return
        self.f.seek(_GPK_HEADER_SIZE)
        self._index = self.f.read(_GPK_INDEX_SIZE)

    def _read_tile(self, lat_int: int, lon_int: int) -> List[Dict[str, object]]:
        key = (lat_int, lon_int)
        if key in self._tile_cache:
            return self._tile_cache[key]

        self._load_index()
        assert self._index is not None
        idx = _tile_index(lat_int, lon_int)
        eoff = idx * 16
        rel_off, size = struct.unpack_from("<QQ", self._index, eoff)
        if size == 0:
            self._tile_cache[key] = []
            return []

        abs_off = _GPK_HEADER_SIZE + _GPK_INDEX_SIZE + rel_off
        self.f.seek(abs_off)
        blob = self.f.read(size)
        if self.use_zstd and self._zstd_dctx is not None:
            blob = self._zstd_dctx.decompress(blob)

        if len(blob) < _FTILE_HEADER_SIZE:
            self._tile_cache[key] = []
            return []

        magic, version, count, name_blob_len, _flags = struct.unpack_from(_FTILE_HEADER_FMT, blob, 0)
        if magic != _FTILE_MAGIC or version != 1:
            self._tile_cache[key] = []
            return []

        records_start = _FTILE_HEADER_SIZE
        records_end = records_start + count * _FTILE_RECORD_SIZE
        names_start = records_end
        names_end = names_start + name_blob_len
        if len(blob) < names_end:
            self._tile_cache[key] = []
            return []

        names_blob = blob[names_start:names_end]
        parsed: List[Dict[str, object]] = []

        for i in range(count):
            off = records_start + i * _FTILE_RECORD_SIZE
            lat_q, lon_q, cat_id, sub_id, imp, type_id, osm_id, name_off, name_len = struct.unpack_from(
                _FTILE_RECORD_FMT, blob, off
            )
            if name_off + name_len > len(names_blob):
                name = ""
            else:
                name = names_blob[name_off:name_off + name_len].decode("utf-8", errors="replace")

            lat = lat_int + (lat_q / 65535.0)
            lon = lon_int + (lon_q / 65535.0)
            parsed.append(
                {
                    "lat": lat,
                    "lon": lon,
                    "category": _ID_TO_CATEGORY.get(cat_id, "unknown"),
                    "subcategory": _ID_TO_SUBCATEGORY.get(sub_id, "unknown"),
                    "importance": int(imp),
                    "osm_type": _ID_TO_OSM_TYPE.get(type_id, "unknown"),
                    "osm_id": int(osm_id),
                    "name": name,
                }
            )

        self._tile_cache[key] = parsed
        return parsed

    def query_bbox(self, south: float, west: float, north: float, east: float) -> List[Dict[str, object]]:
        south = max(-90.0, min(90.0, float(south)))
        north = max(-90.0, min(90.0, float(north)))
        west = max(-180.0, min(180.0, float(west)))
        east = max(-180.0, min(180.0, float(east)))
        if north < south:
            south, north = north, south
        if east < west:
            west, east = east, west

        lat0 = int(south // 1)
        lat1 = int((north - 1e-9) // 1)
        lon0 = int(west // 1)
        lon1 = int((east - 1e-9) // 1)

        results: List[Dict[str, object]] = []
        for lat_int in range(max(-90, lat0), min(89, lat1) + 1):
            for lon_int in range(max(-180, lon0), min(179, lon1) + 1):
                for rec in self._read_tile(lat_int, lon_int):
                    lat = float(rec["lat"])
                    lon = float(rec["lon"])
                    if south <= lat <= north and west <= lon <= east:
                        results.append(rec)
        return results

    def query_point(self, lat: float, lon: float, radius_deg: float = 0.05) -> List[Dict[str, object]]:
        return self.query_bbox(lat - radius_deg, lon - radius_deg, lat + radius_deg, lon + radius_deg)

    def close(self) -> None:
        if self.f:
            self.f.close()
            self.f = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def default_bbox() -> Tuple[float, float, float, float]:
    return (-90.0, -180.0, 90.0, 180.0)


_VIS_CATEGORY_STYLE = {
    "airport": ("#00b4d8", "*", 42),
    "stadium": ("#f72585", "o", 20),
    "football_pitch": ("#f15bb5", "o", 14),
    "transport_hub": ("#fee440", "^", 18),
    "attraction": ("#90be6d", "s", 16),
    "landmark": ("#43aa8b", "D", 16),
    "civic": ("#577590", "x", 16),
    "unknown": ("#ffffff", ".", 12),
}


def visualize_facilities_overlay(args: argparse.Namespace, pack_path: Path) -> None:
    if not args.vis_bbox:
        raise ValueError("--vis-bbox is required with --visualize")
    south, west, north, east = args.vis_bbox
    pop_pack = Path(args.vis_pop_pack)

    from geo_baker_pkg.io import query_population, query_population_pack

    lats = np.linspace(south, north, args.vis_rows)
    lons = np.linspace(west, east, args.vis_cols)
    grid = np.zeros((args.vis_rows, args.vis_cols), dtype=np.float32)
    for i, lat in enumerate(lats):
        for j, lon in enumerate(lons):
            result = query_population(float(lat), float(lon))
            if result is None and pop_pack.exists():
                result = query_population_pack(float(lat), float(lon), str(pop_pack))
            grid[i, j] = float(result.get("pop_density", 0.0)) if result else 0.0

    vmax = float(np.percentile(grid, 99)) if np.any(grid > 0) else 1.0
    if vmax <= 0:
        vmax = 1.0

    fig, ax = plt.subplots(figsize=(10, 8))
    im = ax.imshow(grid, origin="lower", extent=[west, east, south, north], cmap="magma", vmin=0, vmax=vmax, alpha=0.92, interpolation="nearest", aspect="auto")
    fig.colorbar(im, ax=ax, shrink=0.82).set_label("Population Density (relative)")

    with FacilityPackReader(pack_path) as reader:
        records = reader.query_bbox(south, west, north, east)

    grouped: Dict[str, List[dict]] = {}
    for rec in records:
        cat = str(rec.get("category", "unknown"))
        grouped.setdefault(cat, []).append(rec)

    for cat, rows in grouped.items():
        color, marker, size = _VIS_CATEGORY_STYLE.get(cat, _VIS_CATEGORY_STYLE["unknown"])
        xs = [float(r["lon"]) for r in rows]
        ys = [float(r["lat"]) for r in rows]
        scatter_kwargs = dict(s=size, c=color, marker=marker, linewidths=0.25, alpha=0.85, label=f"{cat} ({len(rows)})")
        if marker != "x":
            scatter_kwargs["edgecolors"] = "black"
        ax.scatter(xs, ys, **scatter_kwargs)

    top_rows = sorted(records, key=lambda r: int(r.get("importance", 0)), reverse=True)[:max(0, args.vis_label_top)]
    for rec in top_rows:
        name = str(rec.get("name", "")).strip()
        if not name:
            continue
        ax.text(float(rec["lon"]), float(rec["lat"]), name, fontsize=7, color="white", ha="left", va="bottom", alpha=0.9)

    ax.set_xlim(west, east)
    ax.set_ylim(south, north)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_title("Facilities Overlay on Population Heatmap")
    if records:
        ax.legend(loc="upper right", fontsize=8, framealpha=0.65)
    ax.grid(color="white", alpha=0.15, linewidth=0.5)

    out_path = Path(args.vis_output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=args.vis_dpi)
    plt.close(fig)
    print(f"Visualization saved: {out_path} ({len(records)} facilities in bbox)")


def main() -> int:
    args = parse_args()

    if args.query_binary is not None or args.query_bbox is not None:
        pack_path = Path(args.binary_output)
        if not pack_path.exists():
            raise FileNotFoundError(f"Binary package not found: {pack_path}")

        with FacilityPackReader(pack_path) as reader:
            if args.query_binary is not None:
                qlat, qlon = args.query_binary
                rows = reader.query_point(qlat, qlon)
            else:
                south, west, north, east = args.query_bbox
                rows = reader.query_bbox(south, west, north, east)

        rows = sorted(rows, key=lambda r: int(r.get("importance", 0)), reverse=True)
        for row in rows[: args.query_limit]:
            print(json.dumps(row, ensure_ascii=False))
        print(f"rows={len(rows)}")
        return 0

    bbox = tuple(args.bbox) if args.bbox is not None else default_bbox()
    tiles = list(iter_tiles(bbox, args.tile_deg))

    print(f"Plan: {len(tiles)} tiles, bbox={bbox}, tile={args.tile_deg}°")
    print("Categories: airports with IATA/ICAO, world-famous attractions and landmarks")

    if args.dry_run:
        return 0

    jsonl_handle = None
    jsonl_path = Path(args.jsonl_output) if args.jsonl_output else None
    if jsonl_path is not None:
        if jsonl_path.exists():
            jsonl_path.unlink()
        jsonl_handle = gzip.open(jsonl_path, "wt", encoding="utf-8")

    all_records: List[FacilityRecord] = []
    seen_global: set[Tuple[str, int]] = set()

    stats = {
        "tiles": 0,
        "requests": 0,
        "records": 0,
        "per_category": {},
        "per_subcategory": {},
        "failed_tiles": 0,
    }

    try:
        for index, tile in enumerate(tiles, start=1):
            query = build_overpass_query(tile, args.timeout)
            payload = None
            last_error: Optional[Exception] = None

            for endpoint in OVERPASS_ENDPOINTS:
                try:
                    response = requests.post(endpoint, data={"data": query}, timeout=args.timeout + 30)
                    stats["requests"] += 1
                    if response.status_code == 429:
                        time.sleep(5)
                        continue
                    response.raise_for_status()
                    payload = response.json()
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    continue

            if payload is None:
                stats["failed_tiles"] += 1
                print(f"[{index}/{len(tiles)}] tile {tile} failed: {last_error}")
                continue

            records = list(iter_records(payload, args.min_importance))
            deduped: List[FacilityRecord] = []
            for record in records:
                key = (record.osm_type, record.osm_id)
                if key in seen_global:
                    continue
                seen_global.add(key)
                deduped.append(record)
            records = deduped

            grouped: Dict[str, List[FacilityRecord]] = defaultdict(list)
            for record in records:
                grouped[record.category].append(record)
            capped: List[FacilityRecord] = []
            for category in sorted(grouped.keys()):
                if args.limit_per_category > 0:
                    category_limit = args.limit_per_category
                else:
                    category_limit = DEFAULT_CATEGORY_LIMITS.get(category, 6)
                capped.extend(sorted(grouped[category], key=lambda rec: rec.importance, reverse=True)[: category_limit])
            records = capped

            all_records.extend(records)

            for record in records:
                if jsonl_handle is not None:
                    write_jsonl_line(jsonl_handle, record)
                stats["records"] += 1
                stats["per_category"][record.category] = stats["per_category"].get(record.category, 0) + 1
                key = f"{record.category}:{record.subcategory}"
                stats["per_subcategory"][key] = stats["per_subcategory"].get(key, 0) + 1

            stats["tiles"] += 1
            print(f"[{index}/{len(tiles)}] tile {tile} -> {len(records)} records")
            time.sleep(args.delay)
    finally:
        if jsonl_handle is not None:
            jsonl_handle.close()

    all_records = sorted(all_records, key=lambda rec: rec.importance, reverse=True)
    if args.max_total > 0:
        all_records = all_records[: args.max_total]

    kept_category_counts: Dict[str, int] = {}
    kept_subcategory_counts: Dict[str, int] = {}
    for record in all_records:
        kept_category_counts[record.category] = kept_category_counts.get(record.category, 0) + 1
        key = f"{record.category}:{record.subcategory}"
        kept_subcategory_counts[key] = kept_subcategory_counts.get(key, 0) + 1

    out_path = Path(args.binary_output)
    if out_path.exists():
        out_path.unlink()

    stats_bin = pack_facilities_binary(all_records, out_path, use_zstd=not args.no_zstd)

    summary = {
        "bbox": list(bbox),
        "tile_deg": args.tile_deg,
        "tiles_total": len(tiles),
        "tiles_succeeded": stats["tiles"],
        "tiles_failed": stats["failed_tiles"],
        "requests": stats["requests"],
        "records": len(all_records),
        "per_category": kept_category_counts,
        "per_subcategory": kept_subcategory_counts,
        "jsonl_size_bytes": jsonl_path.stat().st_size if jsonl_path and jsonl_path.exists() else 0,
        "binary": {
            "path": str(out_path),
            "tiles": stats_bin["tiles"],
            "records": stats_bin["records"],
            "file_size_bytes": stats_bin["file_size"],
            "zstd": bool(stats_bin["zstd"]),
        },
    }

    summary_path = out_path.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Done: {summary['records']} records")
    print(f"Binary: {out_path} ({stats_bin['file_size'] / (1024 * 1024):.2f} MB)")
    if jsonl_path is not None:
        print(f"JSONL: {jsonl_path} ({summary['jsonl_size_bytes'] / (1024 * 1024):.2f} MB)")
    print(f"Summary: {summary_path}")

    if args.visualize:
        visualize_facilities_overlay(args, out_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())