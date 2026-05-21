"""
Core: constants, encoding, quadtree

QTR5/QTR6 format: 16bit nodes, DFS pre-order, subtree_size navigation
- Terrain leaf: [1b is_leaf=1][11b elevation][2b gradient][2b zone]
- Pop leaf:     [1b is_leaf=1][12b pop_density][3b urban_type]
- Branch:       [1b is_leaf=0][15b subtree_size]

QTR5 elevation is unsigned. QTR6 keeps the same 16-bit node layout but
uses a signed 11-bit elevation code so negative land elevations survive.
"""

import math
import struct
import os
from pathlib import Path

import numpy as np

# ── Constants ──────────────────────────────────────────────────────

TILE_DIR = os.environ.get("GEO_BAKER_TILE_DIR", "tiles")
LOG_FILE = os.environ.get("GEO_BAKER_LOG_FILE", "logs/bake.log")

ZONE_WATER, ZONE_NATURAL, ZONE_FOREST, ZONE_HARSH = 0, 1, 2, 3
ZONE_NAMES = {0: "Water", 1: "Natural", 2: "Forest", 3: "Harsh"}
ZONE_BUILD_COST = {0: 99.0, 1: 1.0, 2: 1.5, 3: 6.0}
ESA_TO_ZONE = {
    0: 0, 10: 2, 20: 1, 30: 1, 40: 1, 50: 1, 60: 1,
    70: 3, 80: 0, 90: 3, 95: 3, 100: 1,
}

GRADIENT_FLAT, GRADIENT_GENTLE, GRADIENT_STEEP, GRADIENT_CLIFF = 0, 1, 2, 3
GRADIENT_NAMES = {0: "Flat", 1: "Gentle", 2: "Steep", 3: "Cliff"}
GRADIENT_THRESHOLDS = [10, 50, 200]

URBAN_NONE, URBAN_RESIDENTIAL, URBAN_COMMERCIAL = 0, 1, 2
URBAN_INDUSTRIAL, URBAN_MIXED, URBAN_INSTITUTIONAL = 3, 4, 5
URBAN_NAMES = {
    0: "Uninhabited", 1: "Residential", 2: "Commercial",
    3: "Industrial", 4: "Mixed", 5: "Institutional", 6: "Reserved", 7: "Reserved",
}
URBAN_BUILD_COST = {0: 1.0, 1: 1.2, 2: 3.0, 3: 2.0, 4: 2.5, 5: 2.0, 6: 1.0, 7: 1.0}
ESA_URBAN_CLASS = 50
ESA_WATER_CLASS = 80

_POP_LOG_SCALE = 355.7
_POP_LOG_MAX = 4095
_POP_NOISE_FLOOR = 1.0

MAX_NODES = 30000
# Shallower forced subdivision → fewer nodes before adaptive rules kick in (was 3/4, hit MAX often).
FORCE_DEPTH_TERRAIN = 2
FORCE_DEPTH_POP = 3
TARGET_SIZE = 1024
# Elevation and zone split gates.  Variance catches broad relief; max error catches
# narrow ridges/cliffs that get averaged away; zone minority protects flat coastlines.
_TERRAIN_VAR = 1.0
_TERRAIN_MAX_ABS_ERROR = 4.0
_WATER_MIX_VAR = 5.0
_ZONE_MIX_MINORITY = 0.005
_COASTAL_POP_PX = 10.0
_POP_VAR = 16.0
_POP_HOTSPOT_DELTA = 25.0
_POP_HOTSPOT_RATIO = 3.0

STAC_PC = "https://planetarycomputer.microsoft.com/api/stac/v1"
STAC_E84 = "https://earth-search.aws.element84.com/v1"
OPEN_ELEVATION_URL = "https://api.open-elevation.com/api/v1/lookup"
WORLDPOP_ARCGIS_URL = (
    "https://worldpop.arcgis.com/arcgis/rest/services/"
    "WorldPop_Population_Density_1km/ImageServer/exportImage"
)
OVERPASS_ENDPOINTS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
]

_GPK_MAGIC = b"GPK3"
_POP_MAGIC = b"GPOP"
QTR5_TILE_MAGIC = b"QTR5"
QTR6_TILE_MAGIC = b"QTR6"
TILE_HEADER_SIZE = 16
DEFAULT_TERRAIN_CODEC = os.environ.get("GEO_BAKER_TERRAIN_CODEC", "qtr6").lower()
_GPK_HEADER_SIZE = 32
_GPK_GRID_W, _GPK_GRID_H = 360, 180
_GPK_INDEX_SIZE = _GPK_GRID_W * _GPK_GRID_H * 16
MAX_PACK_SIZE_MB = 1024


# ── Encoding ───────────────────────────────────────────────────────
# QTR5 elevation: unsigned 11-bit piecewise-linear
# 0-511m@1m, 512-1535m@2m, 1536-3583m@4m, 3584-8190m@8m

def encode_elevation_qtr5(meters):
    m = max(0, min(8190, int(round(meters))))
    if m <= 511: return m
    if m <= 1535: return 512 + (m - 512) // 2
    if m <= 3583: return 1024 + (m - 1536) // 4
    return 1536 + min(511, (m - 3584) // 8)


def decode_elevation_qtr5(s):
    s = max(0, min(2047, int(s)))
    if s <= 511: return s
    if s <= 1023: return 512 + (s - 512) * 2
    if s <= 1535: return 1536 + (s - 1024) * 4
    return 3584 + (s - 1536) * 8


# QTR6 elevation: signed 11-bit piecewise-linear.  The 16-bit terrain
# node layout is unchanged; only the elevation codebook changes.
def encode_elevation_qtr6(meters):
    m = max(-512, min(8176, int(round(meters))))
    if m < 0:
        return max(0, min(127, int(round((m + 512) / 4.0))))
    if m <= 511:
        return 128 + m
    if m <= 1535:
        return 640 + (m - 512) // 2
    if m <= 3583:
        return 1152 + (m - 1536) // 4
    return 1664 + min(383, (m - 3584) // 12)


def decode_elevation_qtr6(s):
    s = max(0, min(2047, int(s)))
    if s <= 127:
        return -512 + s * 4
    if s <= 639:
        return s - 128
    if s <= 1151:
        return 512 + (s - 640) * 2
    if s <= 1663:
        return 1536 + (s - 1152) * 4
    return 3584 + (s - 1664) * 12


# Backward-compatible names keep QTR5 semantics for old callers/tests.
encode_elevation = encode_elevation_qtr5
decode_elevation = decode_elevation_qtr5


def encode_pop_density(d):
    return 0 if d <= 0 else max(1, min(_POP_LOG_MAX, int(math.log1p(d) * _POP_LOG_SCALE)))


def decode_pop_density(e):
    return 0 if e <= 0 else max(1, round(math.expm1(e / _POP_LOG_SCALE)))


def compute_gradient_level(data):
    if data.size < 4:
        return GRADIENT_FLAT
    a = np.asarray(data, dtype=np.float32)
    g = float(max(np.max(np.abs(a[1:] - a[:-1])), np.max(np.abs(a[:, 1:] - a[:, :-1]))))
    for i, t in enumerate(GRADIENT_THRESHOLDS):
        if g < t: return i
    return GRADIENT_CLIFF


def _precompute_grad_img(dem):
    """Max per-pixel gradient image (one-time per tile, ~5ms at 1024x1024).

    Each pixel stores the maximum of its horizontal and vertical differences
    with neighbours.  Per-leaf gradient lookup becomes float(subview.max()),
    replacing per-leaf compute_gradient_level which allocates 4 tmp arrays.
    """
    a = dem.astype(np.float32)
    gy = np.abs(a[1:, :] - a[:-1, :])
    gx = np.abs(a[:, 1:] - a[:, :-1])
    g = np.zeros_like(a)
    g[:-1, :] = np.maximum(g[:-1, :], gy)
    g[1:,  :] = np.maximum(g[1:,  :], gy)
    g[:,  :-1] = np.maximum(g[:,  :-1], gx)
    g[:,  1:] = np.maximum(g[:,  1:],  gx)
    return g


def _grad_level_from_img(grad_sub):
    g = float(grad_sub.max()) if grad_sub.size > 0 else 0.0
    for i, t in enumerate(GRADIENT_THRESHOLDS):
        if g < t:
            return i
    return GRADIENT_CLIFF


# ── 16-bit Node Codec ─────────────────────────────────────────────

def _pack_terrain_leaf(elev_code, zone, gradient=0):
    return struct.pack('<H', (1 << 15) | ((max(0, min(2047, int(elev_code)))) << 4)
                       | (max(0, min(3, int(gradient))) << 2)
                       | max(0, min(3, int(zone))))


def encode_leaf_node_16(elev_m, zone, gradient=0):
    return _pack_terrain_leaf(encode_elevation_qtr5(elev_m), zone, gradient)


def encode_leaf_node_qtr6(elev_m, zone, gradient=0):
    return _pack_terrain_leaf(encode_elevation_qtr6(elev_m), zone, gradient)


def encode_branch_node_16(subtree_size):
    return struct.pack('<H', max(0, min(0x7FFF, int(subtree_size))))


def _decode_terrain_node(raw, decode_elev_fn, codec):
    v = struct.unpack('<H', raw)[0]
    if v >> 15:
        es = (v >> 4) & 0x7FF
        return {'is_leaf': True, 'elevation': decode_elev_fn(es),
                'elevation_stored': es, 'elevation_codec': codec,
                'gradient_level': (v >> 2) & 3, 'zone': v & 3}
    return {'is_leaf': False, 'subtree_size': v & 0x7FFF}


def decode_node_16(raw):
    return _decode_terrain_node(raw, decode_elevation_qtr5, 'qtr5')


def decode_node_qtr6(raw):
    return _decode_terrain_node(raw, decode_elevation_qtr6, 'qtr6')


def encode_pop_leaf_node(density, urban):
    p = max(0, min(0xFFF, encode_pop_density(density)))
    return struct.pack('<H', (1 << 15) | (p << 3) | max(0, min(7, int(urban))))


def decode_pop_leaf_node(raw):
    v = struct.unpack('<H', raw)[0]
    if v >> 15:
        ps = (v >> 3) & 0xFFF
        return {'is_leaf': True, 'pop_density': decode_pop_density(ps),
                'pop_stored': ps, 'urban_zone': v & 7}
    return {'is_leaf': False, 'subtree_size': v & 0x7FFF}


def encode_water_tile():
    return b'\xff'


WATER_BYTE = encode_water_tile()


def terrain_tile_header(codec=DEFAULT_TERRAIN_CODEC):
    magic = QTR6_TILE_MAGIC if codec == 'qtr6' else QTR5_TILE_MAGIC
    return magic + struct.pack('<HHII', 1, 0, 0, 0)


def wrap_terrain_tile(raw, codec=DEFAULT_TERRAIN_CODEC):
    if codec == 'raw' or raw == WATER_BYTE:
        return raw
    if len(raw) >= TILE_HEADER_SIZE and raw[:4] in (QTR5_TILE_MAGIC, QTR6_TILE_MAGIC):
        return raw
    return terrain_tile_header(codec) + raw


def unwrap_terrain_tile(raw):
    if len(raw) >= TILE_HEADER_SIZE and raw[:4] in (QTR5_TILE_MAGIC, QTR6_TILE_MAGIC):
        codec = 'qtr6' if raw[:4] == QTR6_TILE_MAGIC else 'qtr5'
        return raw[TILE_HEADER_SIZE:], codec
    return raw, 'qtr5'


# ── Quadtree Builder (unified) ─────────────────────────────────────

def _quad_split(arrays):
    quads = [[], [], [], []]
    for arr in arrays:
        if arr is None:
            for q in quads:
                q.append(None)
        else:
            my, mx = arr.shape[0] // 2, arr.shape[1] // 2
            quads[0].append(arr[:my, :mx])
            quads[1].append(arr[:my, mx:])
            quads[2].append(arr[my:, :mx])
            quads[3].append(arr[my:, mx:])
    return quads


def _build_quadtree(arrays, should_split_fn, emit_leaf_fn,
                    max_depth=9, max_nodes=MAX_NODES, force_depth=3):
    """Build adaptive quadtree. Always produces valid structure (no holes)."""
    nodes = []

    def _rec(arrs, depth, budget):
        if arrs[0].shape[0] <= 2 or arrs[0].shape[1] <= 2 or budget < 5:
            emit_leaf_fn(arrs, nodes)
            return 1
        bp = len(nodes)
        nodes.append(b'\x00\x00')
        total = 1
        quads = _quad_split(arrs)
        decisions = [
            should_split_fn(q, depth, budget, depth < force_depth)
            for q in quads
        ]
        remaining_budget = max(0, budget - 1)
        for i, q in enumerate(quads):
            reserve_for_later = len(quads) - i - 1
            if remaining_budget <= reserve_for_later:
                break

            if decisions[i]:
                remaining_split = max(1, sum(1 for d in decisions[i:] if d))
                extra = max(0, remaining_budget - (len(quads) - i))
                alloc = 1 + extra // remaining_split
                alloc = max(1, min(alloc, remaining_budget - reserve_for_later))
            else:
                alloc = 1

            if decisions[i] and alloc >= 5:
                used = _rec(q, depth + 1, alloc)
            else:
                emit_leaf_fn(q, nodes)
                used = 1
            total += used
            remaining_budget -= used
        nodes[bp] = encode_branch_node_16(total)
        return total

    _rec(arrays, 0, min(max_nodes, 0x7FFF))
    return b''.join(nodes)


def _elevation_needs_split(data, var_threshold=_TERRAIN_VAR):
    mean = float(data.mean())
    max_abs_error = max(abs(float(data.min()) - mean), abs(float(data.max()) - mean))
    return float(data.var()) >= var_threshold or max_abs_error >= _TERRAIN_MAX_ABS_ERROR


def _zone_needs_split(zone_data):
    counts = np.bincount(zone_data.ravel().astype(int), minlength=4)
    total = int(zone_data.size)
    if total <= 0 or np.count_nonzero(counts) <= 1:
        return False
    minority = total - int(counts.max())
    return (minority / float(total)) >= _ZONE_MIX_MINORITY


def build_adaptive_tree(dem, zone, pop=None, max_depth=9,
                        max_nodes=MAX_NODES, force_depth=FORCE_DEPTH_TERRAIN,
                        codec=DEFAULT_TERRAIN_CODEC):
    # Precompute gradient image once — avoids 4 tmp array allocs per leaf
    grad_img = _precompute_grad_img(dem)
    pop_arr = pop  # may be None

    # arrays layout: [dem, zone, pop_or_None, grad_img]
    # grad_img is always arrs[3]; pop is arrs[2] (may be None via _quad_split)
    def _split(arrs, depth, budget, forced):
        if budget <= 1: return False
        if forced: return True
        if depth >= max_depth: return False
        d, z = arrs[0], arrs[1]
        wc = int(np.count_nonzero(z == ZONE_WATER))
        if wc == z.size:
            p = arrs[2]
            return _elevation_needs_split(d) if (p is not None and np.any(p > _COASTAL_POP_PX)) else False
        if wc > 0:
            return _zone_needs_split(z) or _elevation_needs_split(d, _WATER_MIX_VAR)
        if _zone_needs_split(z):
            return True
        return _elevation_needs_split(d)

    def _leaf(arrs, nodes):
        d, z, p, gi = arrs[0], arrs[1], arrs[2], arrs[3]
        zv = int(np.argmax(np.bincount(z.ravel().astype(int))))
        # Zone cleanup has already happened before tree building.  Do not infer
        # land from positive elevation here: high-altitude lakes and rivers are
        # still water, and DEM can be positive over inland water surfaces.
        me = 0.0 if zv == ZONE_WATER else float(d.mean())
        encoder = encode_leaf_node_qtr6 if codec == 'qtr6' else encode_leaf_node_16
        nodes.append(encoder(me, zv, _grad_level_from_img(gi)))

    arrays = [dem, zone, pop_arr, grad_img]
    return _build_quadtree(arrays, _split, _leaf, max_depth, max_nodes, force_depth)


def build_adaptive_pop_tree(pop, urban, max_depth=9,
                            max_nodes=MAX_NODES, force_depth=FORCE_DEPTH_POP):
    def _split(arrs, depth, budget, forced):
        if budget <= 1 or depth >= max_depth: return False
        if not np.any(arrs[0] > _POP_NOISE_FLOOR): return False
        if forced: return True
        u = arrs[1]
        if u is not None and len(np.unique(u[u > 0])) > 1: return True
        p = arrs[0]
        mean_pop = float(p.mean())
        max_pop = float(p.max())
        if max_pop >= max(mean_pop * _POP_HOTSPOT_RATIO, mean_pop + _POP_HOTSPOT_DELTA):
            return True
        return float(p.var()) >= _POP_VAR

    def _leaf(arrs, nodes):
        mp = float(arrs[0].mean())
        u = arrs[1]
        if u is not None:
            uv = int(np.argmax(np.bincount(u.ravel().astype(int))))
            if uv == 0:
                uv = URBAN_COMMERCIAL if mp > 5000 else (URBAN_RESIDENTIAL if mp > 10 else 0)
        else:
            uv = URBAN_COMMERCIAL if mp > 5000 else (URBAN_RESIDENTIAL if mp > 10 else 0)
        nodes.append(encode_pop_leaf_node(mp, uv))

    return _build_quadtree([pop, urban], _split, _leaf, max_depth, max_nodes, force_depth)


# ── Navigation (unified) ──────────────────────────────────────────

def _navigate(nodes_raw, frac_lat, frac_lon, decode_fn):
    nc = len(nodes_raw) // 2
    if nc < 1:
        return None
    pos = 0
    for _ in range(20):
        if pos >= nc:
            return None
        node = decode_fn(nodes_raw[pos * 2:pos * 2 + 2])
        if node['is_leaf']:
            return node
        q = (1 if frac_lon >= 0.5 else 0) + (2 if frac_lat < 0.5 else 0)
        cp = pos + 1
        for _ in range(q):
            if cp >= nc:
                return None
            c = decode_fn(nodes_raw[cp * 2:cp * 2 + 2])
            cp += 1 if c['is_leaf'] else c['subtree_size']
        pos = cp
        frac_lat = (frac_lat % 0.5) * 2
        frac_lon = (frac_lon % 0.5) * 2
    return None


def _navigate_terrain(nodes_raw, frac_lat, frac_lon, decode_fn):
    nc = len(nodes_raw) // 2
    if nc > 1:
        root = decode_fn(nodes_raw[0:2])
        if not root['is_leaf'] and root.get('subtree_size', 0) != nc:
            return None
    return _navigate(nodes_raw, frac_lat, frac_lon, decode_fn)


def navigate_qtr5(nodes_raw, frac_lat, frac_lon):
    return _navigate_terrain(nodes_raw, frac_lat, frac_lon, decode_node_16)


def navigate_qtr6(nodes_raw, frac_lat, frac_lon):
    return _navigate_terrain(nodes_raw, frac_lat, frac_lon, decode_node_qtr6)


def navigate_qtr5_pop(nodes_raw, frac_lat, frac_lon):
    nc = len(nodes_raw) // 2
    if nc > 1:
        root = decode_pop_leaf_node(nodes_raw[0:2])
        if not root['is_leaf'] and root.get('subtree_size', 0) != nc:
            return None
    return _navigate(nodes_raw, frac_lat, frac_lon, decode_pop_leaf_node)


# ── Tile I/O & Verification ───────────────────────────────────────

def write_tile_binary(data, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)


write_pop_tile_binary = write_tile_binary


def write_water_tile(path):
    write_tile_binary(WATER_BYTE, path)


def verify_tile(tile_data, decode_fn):
    """Verify quadtree: root subtree_size matches, sample navigations hit leaves."""
    nc = len(tile_data) // 2
    if nc == 0: return False
    if nc == 1: return decode_fn(tile_data[0:2])['is_leaf']
    root = decode_fn(tile_data[0:2])
    if root['is_leaf']: return True
    if root.get('subtree_size', 0) != nc: return False
    for fl, flo in [(0.25, 0.25), (0.75, 0.75), (0.5, 0.5), (0.1, 0.9)]:
        r = _navigate(tile_data, fl, flo, decode_fn)
        if r is None or not r.get('is_leaf'): return False
    return True


# ── Backward Compatibility ────────────────────────────────────────

GLOBAL_GPK = "data/global.gpk"
MAX_NODES_TERRAIN = MAX_NODES
MAX_NODES_POP = MAX_NODES
STAC_CDSE = "https://stac.dataspace.copernicus.eu/v1"
