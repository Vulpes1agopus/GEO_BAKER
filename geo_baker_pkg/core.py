"""
核心定义：常量、编码、四叉树 / Core: constants, encoding, quadtree

本模块包含整个管线的基础定义，无外部项目依赖。
This module contains foundational definitions for the entire pipeline, no external project deps.

QTR5 格式: 16bit节点, DFS前序遍历, subtree_size导航
- 地形叶节点: [1bit is_leaf=1][11bit 海拔(非线性)][2bit 坡度][2bit 区域]
- 人口叶节点: [1bit is_leaf=1][12bit 人口密度(对数)][3bit 城市类型]
- 分支节点:   [1bit is_leaf=0][15bit subtree_size]
"""

import math
import struct
import os

import numpy as np


# ═══════════════════════════════════════════════════════════════════
# 常量与配置 / Constants & Configuration
# ═══════════════════════════════════════════════════════════════════

TILE_DIR = os.environ.get("GEO_BAKER_TILE_DIR", "tiles")
LOG_FILE = os.environ.get("GEO_BAKER_LOG_FILE", "logs/bake.log")
GLOBAL_GPK = "data/global.gpk"

# ── 地形区域 / Terrain Zone ──────────────────────────────────────
ZONE_WATER = 0
ZONE_NATURAL = 1
ZONE_FOREST = 2
ZONE_HARSH = 3

ZONE_NAMES = {
    0: "水体 / Water",
    1: "自然裸地 / Natural bare",
    2: "森林 / Forest",
    3: "严酷地形 / Harsh terrain",
}

ZONE_BUILD_COST = {0: 99.0, 1: 1.0, 2: 1.5, 3: 6.0}

ESA_TO_ZONE = {
    # 0 is ESA nodata/unknown; do not force it to water.
    0: ZONE_NATURAL, 10: ZONE_FOREST, 20: ZONE_NATURAL, 30: ZONE_NATURAL,
    40: ZONE_NATURAL, 50: ZONE_NATURAL, 60: ZONE_NATURAL, 70: ZONE_HARSH,
    80: ZONE_WATER, 90: ZONE_HARSH, 95: ZONE_HARSH, 100: ZONE_NATURAL,
}

# ── 坡度等级 / Gradient ──────────────────────────────────────────
GRADIENT_FLAT = 0
GRADIENT_GENTLE = 1
GRADIENT_STEEP = 2
GRADIENT_CLIFF = 3

GRADIENT_NAMES = {0: "平坦 / Flat", 1: "缓坡 / Gentle", 2: "陡坡 / Steep", 3: "悬崖 / Cliff"}
GRADIENT_THRESHOLDS = [10, 50, 200]

# ── 城市区域 / Urban Zone ────────────────────────────────────────
URBAN_NONE = 0
URBAN_RESIDENTIAL = 1
URBAN_COMMERCIAL = 2
URBAN_INDUSTRIAL = 3
URBAN_MIXED = 4
URBAN_INSTITUTIONAL = 5

URBAN_NAMES = {
    0: "无人区 / Uninhabited", 1: "住宅区 / Residential", 2: "商业区 / Commercial",
    3: "工业区 / Industrial", 4: "混合区 / Mixed", 5: "机构区 / Institutional",
    6: "保留 / Reserved", 7: "保留 / Reserved",
}

URBAN_BUILD_COST = {0: 1.0, 1: 1.2, 2: 3.0, 3: 2.0, 4: 2.5, 5: 2.0, 6: 1.0, 7: 1.0}
ESA_URBAN_CLASS = 50

# ── 人口编码参数 / Population Encoding Params ────────────────────
_POP_LOG_SCALE = 355.7
_POP_LOG_MAX = 4095
_POP_NOISE_FLOOR = 1.0

# ── 四叉树限制 / Quadtree Limits ────────────────────────────────
MAX_NODES_TERRAIN = 30000
MAX_NODES_POP = 30000
FORCE_DEPTH_TERRAIN = 3
FORCE_DEPTH_POP = 4
TARGET_SIZE = 1200

# ── 数据源URL / Data Source URLs ────────────────────────────────
STAC_PC = "https://planetarycomputer.microsoft.com/api/stac/v1"
STAC_E84 = "https://earth-search.aws.element84.com/v1"
STAC_CDSE = "https://stac.dataspace.copernicus.eu/v1"
OPEN_ELEVATION_URL = "https://api.open-elevation.com/api/v1/lookup"
WORLDPOP_ARCGIS_URL = (
    "https://worldpop.arcgis.com/arcgis/rest/services/"
    "WorldPop_Population_Density_1km/ImageServer/exportImage"
)
OVERPASS_ENDPOINTS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]

# ── GeoPack 二进制格式 / Binary Pack Format ─────────────────────
_GPK_MAGIC = b"GPK3"
_POP_MAGIC = b"GPOP"
_GPK_HEADER_SIZE = 32
_GPK_GRID_W = 360
_GPK_GRID_H = 180
_GPK_INDEX_SIZE = _GPK_GRID_W * _GPK_GRID_H * 16
MAX_PACK_SIZE_MB = 1024


# ═══════════════════════════════════════════════════════════════════
# 编码 / Encoding
# ═══════════════════════════════════════════════════════════════════

# 海拔: 11bit分段线性编码 / Elevation: 11-bit piecewise-linear
# 0-511m @ 1m精度, 512-1535m @ 2m, 1536-3583m @ 4m, 3584-8190m @ 8m

def encode_elevation(meters: float) -> int:
    meters = max(0, min(8190, int(meters)))
    if meters <= 511:
        return meters
    elif meters <= 1535:
        return 512 + (meters - 512) // 2
    elif meters <= 3583:
        return 1024 + (meters - 1536) // 4
    else:
        return 1536 + min(511, (meters - 3584) // 8)


def decode_elevation(stored: int) -> int:
    stored = max(0, min(2047, int(stored)))
    if stored <= 511:
        return stored
    elif stored <= 1023:
        return 512 + (stored - 512) * 2
    elif stored <= 1535:
        return 1536 + (stored - 1024) * 4
    else:
        return 3584 + (stored - 1536) * 8


# 人口: 12bit对数编码 / Population: 12-bit logarithmic

def encode_pop_density(density: float) -> int:
    if density <= 0:
        return 0
    encoded = int(math.log1p(density) * _POP_LOG_SCALE)
    return max(1, min(_POP_LOG_MAX, encoded))


def decode_pop_density(encoded: int) -> int:
    if encoded <= 0:
        return 0
    return max(1, round(math.expm1(encoded / _POP_LOG_SCALE)))


# 坡度计算 / Gradient

def compute_max_gradient(data) -> float:
    if data.size < 4:
        return 0.0
    arr = np.asarray(data, dtype=np.float32)
    gy, gx = np.gradient(arr)
    return float(np.max(np.sqrt(gx ** 2 + gy ** 2)))


def compute_gradient_level(data) -> int:
    if data.size < 4:
        return GRADIENT_FLAT
    max_grad = compute_max_gradient(data)
    for i, threshold in enumerate(GRADIENT_THRESHOLDS):
        if max_grad < threshold:
            return i
    return GRADIENT_CLIFF


# ── 16bit 节点编解码 / 16-bit Node Codec ────────────────────────

def encode_leaf_node_16(elevation_meters, zone_type, gradient_level=0):
    is_leaf = 1
    elev = max(0, min(2047, encode_elevation(elevation_meters)))
    zone = max(0, min(3, int(zone_type)))
    grad = max(0, min(3, int(gradient_level)))
    value = (is_leaf << 15) | (elev << 4) | (grad << 2) | zone
    return struct.pack('<H', value)


def encode_branch_node_16(subtree_size):
    is_leaf = 0
    size = max(0, min(0x7FFF, int(subtree_size)))
    value = (is_leaf << 15) | size
    return struct.pack('<H', value)


def decode_node_16(raw_bytes):
    value = struct.unpack('<H', raw_bytes)[0]
    is_leaf = (value >> 15) & 1
    if is_leaf:
        elev_stored = (value >> 4) & 0x7FF
        return {
            'is_leaf': True,
            'elevation': decode_elevation(elev_stored),
            'elevation_stored': elev_stored,
            'gradient_level': (value >> 2) & 0x3,
            'zone': value & 0x3,
        }
    return {'is_leaf': False, 'subtree_size': value & 0x7FFF}


def encode_pop_leaf_node(density, urban_zone):
    is_leaf = 1
    pop = max(0, min(0xFFF, encode_pop_density(density)))
    zone = max(0, min(7, int(urban_zone)))
    value = (is_leaf << 15) | (pop << 3) | zone
    return struct.pack('<H', value)


def decode_pop_leaf_node(raw_bytes):
    value = struct.unpack('<H', raw_bytes)[0]
    is_leaf = (value >> 15) & 1
    if is_leaf:
        pop_stored = (value >> 3) & 0xFFF
        return {
            'is_leaf': True,
            'pop_density': decode_pop_density(pop_stored),
            'pop_stored': pop_stored,
            'urban_zone': value & 0x7,
        }
    return {'is_leaf': False, 'subtree_size': value & 0x7FFF}


def encode_water_tile():
    return struct.pack('B', 0xFF)


# ── 旧版32bit节点（兼容） / Legacy 32-bit Node ──────────────────

def encode_leaf_node(elevation_meters, pop_weight, zone_type):
    is_leaf = 1
    elev = max(0, min(4095, encode_elevation(elevation_meters)))
    pop = max(0, min(255, int(pop_weight)))
    zone = max(0, min(15, int(zone_type)))
    value = (is_leaf << 31) | (elev << 19) | (pop << 11) | (zone << 7)
    return struct.pack('<I', value)


def encode_branch_node(child_offset):
    is_leaf = 0
    offset = max(0, min(0x7FFFFFFF, int(child_offset)))
    value = (is_leaf << 31) | offset
    return struct.pack('<I', value)


def decode_node(raw_bytes):
    value = struct.unpack('<I', raw_bytes)[0]
    is_leaf = (value >> 31) & 1
    if is_leaf:
        elev_stored = (value >> 19) & 0xFFF
        return {
            'is_leaf': True,
            'elevation': decode_elevation(elev_stored),
            'elevation_stored': elev_stored,
            'pop_weight': (value >> 11) & 0xFF,
            'zone': (value >> 7) & 0xF,
            'reserved': value & 0x7F,
        }
    return {'is_leaf': False, 'child_offset': value & 0x7FFFFFFF}


# ═══════════════════════════════════════════════════════════════════
# 四叉树 / Quadtree
# ═══════════════════════════════════════════════════════════════════

def build_adaptive_tree(dem, zone, pop=None, max_depth=9, max_nodes=MAX_NODES_TERRAIN,
                        force_depth=FORCE_DEPTH_TERRAIN):
    """从DEM+zone构建地形四叉树 / Build adaptive terrain quadtree"""
    node_id = [0]
    nodes = []

    def _split(data, depth, budget):
        if node_id[0] >= max_nodes:
            return
        h, w = data.shape
        if h <= 2 or w <= 2:
            return
        mid_y, mid_x = h // 2, w // 2
        quadrants = [data[:mid_y, :mid_x], data[:mid_y, mid_x:],
                     data[mid_y:, :mid_x], data[mid_y:, mid_x:]]
        zone_quads = [zone[:mid_y, :mid_x], zone[:mid_y, mid_x:],
                      zone[mid_y:, :mid_x], zone[mid_y:, mid_x:]]
        pop_quads = ([pop[:mid_y, :mid_x], pop[:mid_y, mid_x:],
                      pop[mid_y:, :mid_x], pop[mid_y:, mid_x:]]
                     if pop is not None else [None] * 4)
        for ci in range(4):
            if node_id[0] >= max_nodes:
                break
            remaining = max_nodes - node_id[0]
            alloc = max(1, min(budget // 4, remaining))
            q_data, q_zone, q_pop = quadrants[ci], zone_quads[ci], pop_quads[ci]
            if _should_split(q_data, q_zone, q_pop, depth, alloc, force_depth):
                node_id[0] += 1
                _split(q_data, depth + 1, alloc)
            else:
                node_id[0] += 1
                _emit_leaf(q_data, q_zone, q_pop)

    _COASTAL_POP_THRESHOLD = 10.0

    def _should_split(data, zone_data, pop_data, depth, budget, fd):
        if budget <= 1: return False
        if depth < fd: return True
        if depth >= max_depth: return False
        if node_id[0] >= max_nodes: return False

        water_count = int(np.count_nonzero(zone_data == ZONE_WATER))
        total = zone_data.size
        has_water = water_count > 0
        has_land = water_count < total

        if has_water and not has_land:
            if pop_data is not None and np.any(pop_data > _COASTAL_POP_THRESHOLD):
                return np.var(data) >= 1.0
            return False

        if has_water and has_land:
            return np.var(data) >= 5.0

        if np.var(data) < 1.0: return False
        return True

    def _emit_leaf(data, zone_data, pop_data):
        mean_elev = float(np.mean(data))
        zone_val = int(np.argmax(np.bincount(zone_data.ravel().astype(int))))
        if mean_elev > 0 and zone_val == ZONE_WATER:
            zone_val = ZONE_NATURAL
        grad = compute_gradient_level(data)
        nodes.append(encode_leaf_node_16(mean_elev, zone_val, grad))

    _split(dem, 0, max_nodes)
    return encode_branch_node_16(node_id[0]) + b''.join(nodes)


def build_adaptive_pop_tree(pop, urban, max_depth=9, max_nodes=MAX_NODES_POP,
                            force_depth=FORCE_DEPTH_POP):
    """构建人口自适应四叉树 / Build adaptive population quadtree"""
    node_id = [0]
    nodes = []

    def _split(data, urban_data, depth, budget):
        if node_id[0] >= max_nodes:
            return
        h, w = data.shape
        if h <= 2 or w <= 2:
            return
        mid_y, mid_x = h // 2, w // 2
        quadrants = [data[:mid_y, :mid_x], data[:mid_y, mid_x:],
                     data[mid_y:, :mid_x], data[mid_y:, mid_x:]]
        urban_quads = [
            urban_data[:mid_y, :mid_x] if urban_data is not None else None,
            urban_data[:mid_y, mid_x:] if urban_data is not None else None,
            urban_data[mid_y:, :mid_x] if urban_data is not None else None,
            urban_data[mid_y:, mid_x:] if urban_data is not None else None,
        ]
        for ci in range(4):
            if node_id[0] >= max_nodes:
                break
            remaining = max_nodes - node_id[0]
            alloc = max(1, min(budget // 4, remaining))
            q_data, q_urban = quadrants[ci], urban_quads[ci]
            if _should_split_pop(q_data, q_urban, depth, alloc, force_depth):
                node_id[0] += 1
                _split(q_data, q_urban, depth + 1, alloc)
            else:
                node_id[0] += 1
                _emit_pop_leaf(q_data, q_urban)

    def _should_split_pop(data, urban_data, depth, budget, fd):
        if budget <= 1: return False
        if depth >= max_depth: return False
        if np.all(data <= _POP_NOISE_FLOOR): return False
        if depth < fd: return True
        if urban_data is not None and np.unique(urban_data).size > 2:
            unique_nonzero = np.unique(urban_data[urban_data > 0])
            if len(unique_nonzero) > 1: return True
        if np.var(data) < 10.0: return False
        return True

    def _emit_pop_leaf(data, urban_data):
        mean_pop = float(np.mean(data))
        if urban_data is not None:
            urban_val = int(np.argmax(np.bincount(urban_data.ravel().astype(int))))
            if urban_val == URBAN_NONE:
                if mean_pop > 5000: urban_val = URBAN_COMMERCIAL
                elif mean_pop > 10: urban_val = URBAN_RESIDENTIAL
        else:
            if mean_pop > 5000: urban_val = URBAN_COMMERCIAL
            elif mean_pop > 10: urban_val = URBAN_RESIDENTIAL
            else: urban_val = URBAN_NONE
        nodes.append(encode_pop_leaf_node(mean_pop, urban_val))

    _split(pop, urban, 0, max_nodes)
    return encode_branch_node_16(node_id[0]) + b''.join(nodes)


def write_tile_binary(nodes, output_path):
    from pathlib import Path
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'wb') as f:
        f.write(nodes)


def write_water_tile(output_path):
    from pathlib import Path
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'wb') as f:
        f.write(encode_water_tile())


def write_pop_tile_binary(nodes, output_path):
    from pathlib import Path
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'wb') as f:
        f.write(nodes)


# ── 导航 / Navigation ───────────────────────────────────────────

def navigate_qtr5(nodes_raw, frac_lat, frac_lon):
    """导航地形四叉树到指定点 / Navigate terrain quadtree to a point"""
    pos = 0
    node_count = len(nodes_raw) // 2
    for _ in range(20):
        if pos >= node_count:
            return None
        node = decode_node_16(nodes_raw[pos * 2 : pos * 2 + 2])
        if node['is_leaf']:
            return node
        quadrant = 0
        if frac_lon >= 0.5: quadrant += 1
        if frac_lat < 0.5: quadrant += 2
        child_pos = pos + 1
        for i in range(quadrant):
            if child_pos >= node_count:
                return None
            child = decode_node_16(nodes_raw[child_pos * 2 : child_pos * 2 + 2])
            if child['is_leaf']:
                child_pos += 1
            else:
                child_pos += child['subtree_size']
        pos = child_pos
        frac_lat = (frac_lat % 0.5) * 2
        frac_lon = (frac_lon % 0.5) * 2
    return None


def navigate_qtr5_pop(nodes_raw, frac_lat, frac_lon):
    """导航人口四叉树到指定点 / Navigate population quadtree to a point"""
    pos = 0
    node_count = len(nodes_raw) // 2
    for _ in range(20):
        if pos >= node_count:
            return None
        node = decode_pop_leaf_node(nodes_raw[pos * 2 : pos * 2 + 2])
        if node['is_leaf']:
            return node
        quadrant = 0
        if frac_lon >= 0.5: quadrant += 1
        if frac_lat < 0.5: quadrant += 2
        child_pos = pos + 1
        for i in range(quadrant):
            if child_pos >= node_count:
                return None
            child = decode_pop_leaf_node(nodes_raw[child_pos * 2 : child_pos * 2 + 2])
            if child['is_leaf']:
                child_pos += 1
            else:
                child_pos += child['subtree_size']
        pos = child_pos
        frac_lat = (frac_lat % 0.5) * 2
        frac_lon = (frac_lon % 0.5) * 2
    return None
