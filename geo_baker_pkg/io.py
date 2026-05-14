"""
读写：打包 + 查询 / I/O: packing + querying

GeoPack格式: zstd压缩, 360×180网格索引, O(1)随机访问
瓦片查询: 从独立瓦片文件或.dat包查询
"""

import json
import os
import struct
import logging
from pathlib import Path

import numpy as np

from .core import (
    TILE_DIR, _GPK_MAGIC, _POP_MAGIC,
    _GPK_HEADER_SIZE, _GPK_GRID_W, _GPK_GRID_H, _GPK_INDEX_SIZE,
    MAX_PACK_SIZE_MB,
    ZONE_WATER, ZONE_NAMES, URBAN_NAMES, GRADIENT_NAMES,
    _POP_NOISE_FLOOR,
    decode_node_16, decode_pop_leaf_node,
    navigate_qtr5, navigate_qtr5_pop,
)

logger = logging.getLogger('geo_baker')

_SHARD_MAGIC = b"GSH1"
_SHARD_VERSION = 1
_SHARD_KIND_TERRAIN = 1
_SHARD_KIND_POP = 2
_SHARD_HEADER = struct.Struct("<4sHHhhHHIIIII")
_SHARD_INDEX_ENTRY_SIZE = 8


# ═══════════════════════════════════════════════════════════════════
# 打包 / Packing
# ═══════════════════════════════════════════════════════════════════

def _gpk_tile_index(lat, lon):
    return (lat + 90) * _GPK_GRID_W + (lon + 180)


def _pack_tiles_inner(output_path, magic, glob_pattern):
    tile_dir = Path(TILE_DIR)
    if not tile_dir.exists():
        logger.error(f"[PACK] 无瓦片目录")
        return
    tile_files = sorted(tile_dir.glob(glob_pattern))
    if not tile_files:
        logger.error(f"[PACK] 未找到 {glob_pattern} 文件")
        return
    zstd_cctx = None
    try:
        import zstandard as zstd
        zstd_cctx = zstd.ZstdCompressor(level=9, threads=-1)
    except ImportError:
        logger.warning("[PACK] zstandard 不可用，输出将不压缩（可安装 zstandard 后重打包）")
    index = bytearray(_GPK_INDEX_SIZE)
    data_buf = bytearray()
    data_count, raw_size = 0, 0
    for tf in tile_files:
        parts = tf.stem.split('_')
        if len(parts) != 2: continue
        try: lon, lat = int(parts[0]), int(parts[1])
        except ValueError: continue
        tile_data = tf.read_bytes()
        raw_size += len(tile_data)
        if zstd_cctx is not None:
            tile_data = zstd_cctx.compress(tile_data)
        idx = _gpk_tile_index(lat, lon)
        struct.pack_into("<QQ", index, idx * 16, len(data_buf), len(tile_data))
        data_buf.extend(tile_data)
        data_count += 1
    flags = 1 if zstd_cctx is not None else 0
    header = struct.pack("<4sHIIIIIIH", magic, 2, _GPK_GRID_W, _GPK_GRID_H,
                         data_count, len(data_buf), raw_size, flags, 0)
    packed_size = _GPK_HEADER_SIZE + _GPK_INDEX_SIZE + len(data_buf)
    if packed_size > MAX_PACK_SIZE_MB * 1024 * 1024:
        logger.warning(f"[PACK] 输出超出{MAX_PACK_SIZE_MB}MB限制: {packed_size / 1024 / 1024:.1f}MB")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'wb') as f:
        f.write(header)
        f.write(index)
        f.write(data_buf)
    label = "地形" if magic == _GPK_MAGIC else "人口"
    logger.info(f"[PACK] {label}: {data_count} 瓦片, {packed_size / 1024 / 1024:.2f} MB → {output_path}")


def pack_tiles(output_path="terrain.dat"):
    _pack_tiles_inner(output_path, _GPK_MAGIC, "*.qtree")


def pack_population(output_path="population.dat"):
    _pack_tiles_inner(output_path, _POP_MAGIC, "*.pop")


def _parse_tile_name(path):
    parts = path.stem.split('_')
    if len(parts) != 2:
        return None
    try:
        lon, lat = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (-180 <= lon < 180 and -90 <= lat < 90):
        return None
    return lat, lon


def _shard_origin(lat, lon, shard_degrees):
    size = max(1, int(shard_degrees))
    lon_min = -180 + ((lon + 180) // size) * size
    lat_min = -90 + ((lat + 90) // size) * size
    return int(lat_min), int(lon_min)


def _shard_local_index(lat, lon, lat_min, lon_min, tile_w):
    return (lat - lat_min) * tile_w + (lon - lon_min)


def _pack_shards_inner(output_dir, glob_pattern, prefix, kind, shard_degrees=10):
    tile_dir = Path(TILE_DIR)
    if not tile_dir.exists():
        logger.error("[SHARD-PACK] 无瓦片目录")
        return []
    size = max(1, int(shard_degrees))
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tile_files = []
    for tf in sorted(tile_dir.glob(glob_pattern)):
        key = _parse_tile_name(tf)
        if key is None:
            continue
        tile_files.append((key[0], key[1], tf))
    if not tile_files:
        logger.warning("[SHARD-PACK] 未找到 %s 文件", glob_pattern)
        return []

    zstd_cctx = None
    try:
        import zstandard as zstd
        zstd_cctx = zstd.ZstdCompressor(level=9, threads=-1)
    except ImportError:
        logger.warning("[SHARD-PACK] zstandard 不可用，shard 将不压缩")

    groups = {}
    for lat, lon, tf in tile_files:
        groups.setdefault(_shard_origin(lat, lon, size), []).append((lat, lon, tf))

    entries = []
    for (lat_min, lon_min), files in sorted(groups.items()):
        tile_w = min(size, 180 - lon_min)
        tile_h = min(size, 90 - lat_min)
        index = bytearray(tile_w * tile_h * _SHARD_INDEX_ENTRY_SIZE)
        data = bytearray()
        raw_size = 0
        count = 0
        for lat, lon, tf in files:
            blob = tf.read_bytes()
            raw_size += len(blob)
            if zstd_cctx is not None:
                blob = zstd_cctx.compress(blob)
            local_idx = _shard_local_index(lat, lon, lat_min, lon_min, tile_w)
            if local_idx < 0 or local_idx >= tile_w * tile_h:
                continue
            if len(data) > 0xFFFFFFFF or len(blob) > 0xFFFFFFFF:
                raise ValueError("Shard offsets exceed 32-bit range; reduce shard size")
            struct.pack_into("<II", index, local_idx * _SHARD_INDEX_ENTRY_SIZE, len(data), len(blob))
            data.extend(blob)
            count += 1
        flags = 1 if zstd_cctx is not None else 0
        name = f"{prefix}_{lon_min:+04d}_{lat_min:+03d}.gsh"
        path = out_dir / name
        header = _SHARD_HEADER.pack(
            _SHARD_MAGIC, _SHARD_VERSION, kind, lon_min, lat_min, tile_w, tile_h,
            count, len(index), len(data), raw_size, flags,
        )
        with path.open("wb") as f:
            f.write(header)
            f.write(index)
            f.write(data)
        entries.append({
            "path": name,
            "kind": "terrain" if kind == _SHARD_KIND_TERRAIN else "population",
            "lon_min": lon_min,
            "lat_min": lat_min,
            "tile_w": tile_w,
            "tile_h": tile_h,
            "tile_count": count,
            "index_bytes": len(index),
            "data_bytes": len(data),
            "raw_bytes": raw_size,
            "file_bytes": path.stat().st_size,
        })
    return entries


def pack_shards(output_dir="shards", shard_degrees=10, include_population=True):
    """Pack tiles into small geographic shards for lazy game-side loading."""
    entries = []
    entries.extend(_pack_shards_inner(output_dir, "*.qtree", "terrain", _SHARD_KIND_TERRAIN, shard_degrees))
    if include_population:
        entries.extend(_pack_shards_inner(output_dir, "*.pop", "population", _SHARD_KIND_POP, shard_degrees))
    manifest = {
        "format": "GeoShard",
        "version": _SHARD_VERSION,
        "shard_degrees": max(1, int(shard_degrees)),
        "header_bytes": _SHARD_HEADER.size,
        "index_entry_bytes": _SHARD_INDEX_ENTRY_SIZE,
        "compression": "zstd-per-tile" if entries and any(e["data_bytes"] < e["raw_bytes"] for e in entries) else "none",
        "global_grid": {"lon_min": -180, "lat_min": -90, "width": 360, "height": 180},
        "shards": entries,
    }
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
    total_bytes = sum(e["file_bytes"] for e in entries)
    logger.info(
        "[SHARD-PACK] %d shards, %.2f MB total → %s",
        len(entries),
        total_bytes / 1024 / 1024,
        manifest_path,
    )
    return manifest


def incremental_pack(output_path="terrain.dat", max_size_mb=MAX_PACK_SIZE_MB):
    """增量打包: 仅打包新增/变更瓦片 / Incremental pack: only new/changed tiles"""
    tile_dir = Path(TILE_DIR)
    if not tile_dir.exists():
        logger.error("[PACK] 无瓦片目录")
        return
    existing_tiles = set()
    if os.path.exists(output_path):
        try:
            with open(output_path, 'rb') as f:
                header = f.read(_GPK_HEADER_SIZE)
                if header[:4] in (_GPK_MAGIC, _POP_MAGIC):
                    f.seek(_GPK_HEADER_SIZE)
                    index_data = f.read(_GPK_INDEX_SIZE)
                    for i in range(_GPK_GRID_W * _GPK_GRID_H):
                        rel_off, size = struct.unpack_from("<QQ", index_data, i * 16)
                        if size > 0:
                            existing_tiles.add(((i // _GPK_GRID_W) - 90, (i % _GPK_GRID_W) - 180))
        except Exception:
            existing_tiles = set()
    current_tiles = set()
    for qf in tile_dir.glob("*.qtree"):
        parts = qf.stem.split('_')
        if len(parts) == 2:
            try: current_tiles.add((int(parts[1]), int(parts[0])))
            except ValueError: pass
    new_tiles = current_tiles - existing_tiles
    logger.info(f"[PACK-INC] 新增 {len(new_tiles)} 瓦片待打包" if new_tiles else "[PACK-INC] 无新增瓦片")
    pack_tiles(output_path)
    if os.path.exists(output_path):
        size_mb = os.path.getsize(output_path) / 1024 / 1024
        if size_mb > max_size_mb:
            logger.warning(f"[PACK] 输出 {output_path} 超出{max_size_mb}MB限制: {size_mb:.1f}MB")
        else:
            logger.info(f"[PACK] 输出 {output_path}: {size_mb:.1f}MB (在{max_size_mb}MB限制内)")


def merge_gpk(file1, file2, output_path, expected_magic=_GPK_MAGIC):
    """合并两个.dat文件 / Merge two .dat files"""
    with open(file1, 'rb') as f: magic1 = f.read(4)
    with open(file2, 'rb') as f: magic2 = f.read(4)
    if magic1 != expected_magic or magic2 != expected_magic:
        raise ValueError(f"Magic不匹配: {magic1} vs {magic2} (期望 {expected_magic})")
    with open(file1, 'rb') as f:
        header1, index1, data1 = f.read(_GPK_HEADER_SIZE), f.read(_GPK_INDEX_SIZE), f.read()
    with open(file2, 'rb') as f:
        header2, index2, data2 = f.read(_GPK_HEADER_SIZE), f.read(_GPK_INDEX_SIZE), f.read()
    merged_index, merged_data, data_count = bytearray(index1), bytearray(data1), 0
    for i in range(_GPK_GRID_W * _GPK_GRID_H):
        off = i * 16
        rel_off2, size2 = struct.unpack_from("<QQ", index2, off)
        if size2 == 0: continue
        rel_off1, size1 = struct.unpack_from("<QQ", index1, off)
        if size1 > 0: continue
        tile_data = data2[rel_off2:rel_off2 + size2]
        struct.pack_into("<QQ", merged_index, off, len(merged_data), size2)
        merged_data.extend(tile_data)
        data_count += 1
    flags = struct.unpack_from("<I", header1, 26)[0]
    new_header = struct.pack("<4sHIIIIIIH", expected_magic, 2, _GPK_GRID_W, _GPK_GRID_H,
                             data_count, len(merged_data), 0, flags, 0)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'wb') as f:
        f.write(new_header)
        f.write(merged_index)
        f.write(merged_data)
    logger.info(f"[MERGE] 从 {file2} 合并 {data_count} 瓦片到 {output_path}")


# ═══════════════════════════════════════════════════════════════════
# GeoPack 读取器 / GeoPack Reader
# ═══════════════════════════════════════════════════════════════════

class GeoPackReader:
    """.dat包随机访问读取器 / Random-access reader for .dat packs"""

    def __init__(self, path):
        self.path = Path(path)
        self.f = self.path.open('rb')
        header = self.f.read(_GPK_HEADER_SIZE)
        if len(header) < _GPK_HEADER_SIZE or header[:4] not in (_GPK_MAGIC, _POP_MAGIC):
            raise ValueError(f"无效的包文件: {self.path}")
        self.magic = header[:4]
        self.grid_w, self.grid_h = struct.unpack_from("<II", header, 6)
        self.data_count = struct.unpack_from("<I", header, 14)[0]
        self.flags = struct.unpack_from("<I", header, 26)[0]
        self.use_zstd = bool(self.flags & 1)
        self._index = None
        self._tile_cache = {}
        self._zstd_dctx = None
        if self.use_zstd:
            try:
                import zstandard as zstd
                self._zstd_dctx = zstd.ZstdDecompressor()
            except ImportError:
                self.use_zstd = False

    def _load_index(self):
        if self._index is not None: return
        self.f.seek(_GPK_HEADER_SIZE)
        self._index = self.f.read(_GPK_INDEX_SIZE)

    def _read_tile(self, lat, lon):
        key = (lat, lon)
        if key in self._tile_cache: return self._tile_cache[key]
        self._load_index()
        idx = _gpk_tile_index(lat, lon)
        rel_off, size = struct.unpack_from("<QQ", self._index, idx * 16)
        if size == 0:
            self._tile_cache[key] = None
            return None
        self.f.seek(_GPK_HEADER_SIZE + _GPK_INDEX_SIZE + rel_off)
        blob = self.f.read(size)
        if self.use_zstd and self._zstd_dctx:
            blob = self._zstd_dctx.decompress(blob)
        self._tile_cache[key] = blob
        return blob

    def query_terrain(self, lat, lon):
        lat_int, lon_int = int(np.floor(lat)), int(np.floor(lon))
        blob = self._read_tile(lat_int, lon_int)
        if blob is None:
            return None
        if len(blob) <= 1:
            return {'is_leaf': True, 'elevation': 0, 'gradient_level': 0, 'zone': ZONE_WATER}
        if len(blob) >= 16 and blob[:4] == b'QTR5':
            blob = blob[16:]
        return navigate_qtr5(blob, lat - lat_int, lon - lon_int)

    def query_population(self, lat, lon):
        lat_int, lon_int = int(np.floor(lat)), int(np.floor(lon))
        blob = self._read_tile(lat_int, lon_int)
        if blob is None:
            return None
        if len(blob) <= 1:
            return {'is_leaf': True, 'pop_density': 0, 'urban_zone': 0}
        if len(blob) >= 16 and blob[:4] == b'QTR5':
            blob = blob[16:]
        return navigate_qtr5_pop(blob, lat - lat_int, lon - lon_int)

    def close(self):
        if self.f: self.f.close(); self.f = None

    def __enter__(self): return self
    def __exit__(self, *_): self.close()


# ═══════════════════════════════════════════════════════════════════
# 查询 / Query
# ═══════════════════════════════════════════════════════════════════

def _normalize_elevation_result(result):
    if result is None: return None
    result['zone_name'] = ZONE_NAMES.get(result.get('zone', 0), 'Unknown')
    result['gradient_name'] = GRADIENT_NAMES.get(result.get('gradient_level', 0), 'Unknown')
    return result


def query_elevation(lat, lon):
    """从瓦片文件查询海拔 / Query elevation from tile files"""
    lat_int, lon_int = int(np.floor(lat)), int(np.floor(lon))
    tile_path = os.path.join(TILE_DIR, f"{lon_int}_{lat_int}.qtree")
    if not os.path.exists(tile_path): return None
    if os.path.getsize(tile_path) <= 1:
        return _normalize_elevation_result({'is_leaf': True, 'elevation': 0, 'gradient_level': 0, 'zone': ZONE_WATER})
    with open(tile_path, 'rb') as f:
        raw = f.read()
    data = raw[16:] if raw[:4] == b'QTR5' else raw
    node = navigate_qtr5(data, lat - lat_int, lon - lon_int)
    return _normalize_elevation_result(node)


def query_elevation_pack(lat, lon, pack_path="terrain.dat"):
    """从.dat包查询海拔 / Query elevation from .dat pack"""
    with GeoPackReader(pack_path) as reader:
        node = reader.query_terrain(lat, lon)
        return _normalize_elevation_result(node)


def query_population(lat, lon):
    """从瓦片文件查询人口 / Query population from tile files"""
    lat_int, lon_int = int(np.floor(lat)), int(np.floor(lon))
    tile_path = os.path.join(TILE_DIR, f"{lon_int}_{lat_int}.pop")
    if not os.path.exists(tile_path): return None
    if os.path.getsize(tile_path) <= 1:
        return {'pop_density': 0, 'urban_zone': 0, 'urban_name': URBAN_NAMES.get(0, 'Unknown')}
    with open(tile_path, 'rb') as f:
        raw = f.read()
    data = raw[16:] if raw[:4] == b'QTR5' else raw
    node = navigate_qtr5_pop(data, lat - lat_int, lon - lon_int)
    if node is None: return None
    node['urban_name'] = URBAN_NAMES.get(node.get('urban_zone', 0), 'Unknown')
    return node


def query_population_pack(lat, lon, pack_path="population.dat"):
    """从.dat包查询人口 / Query population from .dat pack"""
    with GeoPackReader(pack_path) as reader:
        node = reader.query_population(lat, lon)
        if node is None: return None
        node['urban_name'] = URBAN_NAMES.get(node.get('urban_zone', 0), 'Unknown')
        return node
