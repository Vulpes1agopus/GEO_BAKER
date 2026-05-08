#!/usr/bin/env python3
"""
Build world border data from an EPS file and export to multiple formats.

Pipeline:
1. Parse EPS binary header → extract TIFF preview → detect border pixels
2. Quantize to coarse grid → emit Python module (world_border_hardcoded.py)
3. (Optional) Export to interop formats: JSON, C++ header, Dart module

Usage:
    python build_world_border.py --eps-path data/全球国界图.eps
    python build_world_border.py --eps-path data/全球国界图.eps --interop --out-dir interop
"""

from __future__ import annotations

import argparse
import io
import json
import struct
from pathlib import Path
from typing import Dict, List, Sequence, Set, Tuple

import numpy as np
import tifffile


EPS_MAGIC = 0xC6D3D0C5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build world border data from EPS")
    parser.add_argument("--eps-path", type=str, default="data/全球国界图.eps", help="Input EPS file path")
    parser.add_argument("--cell-deg", type=float, default=2.0, help="Grid size in degrees")
    parser.add_argument("--output-py", type=str, default="scripts/borders/world_border_hardcoded.py", help="Output Python module path")
    parser.add_argument("--interop", action="store_true", help="Also export interop formats (JSON/HPP/Dart)")
    parser.add_argument("--out-dir", type=str, default="interop", help="Interop output directory")
    parser.add_argument("--base-name", type=str, default="world_border_grid_v1", help="Interop base filename")
    return parser.parse_args()


def _read_eps_tiff_preview(eps_path: Path) -> np.ndarray:
    raw = eps_path.read_bytes()
    if len(raw) < 32:
        raise ValueError("EPS file is too small")
    magic, _ps_off, _ps_len, _wmf_off, _wmf_len, tiff_off, tiff_len, _chk = struct.unpack("<8I", raw[:32])
    if magic != EPS_MAGIC:
        raise ValueError("Unsupported EPS header; expected binary EPS with preview")
    if tiff_off <= 0 or tiff_len <= 0 or tiff_off + tiff_len > len(raw):
        raise ValueError("EPS does not contain a valid TIFF preview segment")
    preview = raw[tiff_off : tiff_off + tiff_len]
    with tifffile.TiffFile(io.BytesIO(preview)) as tf:
        page = tf.pages[0]
        data = page.asarray()
        if data.ndim != 3 or data.shape[2] < 2 or page.colormap is None:
            raise ValueError("Unexpected TIFF preview layout; expected indexed+alpha")
        idx = data[..., 0].astype(np.uint8)
        alpha = data[..., 1].astype(np.uint8)
        cmap = (page.colormap >> 8).astype(np.uint8)
        rgb = np.stack([cmap[0, idx], cmap[1, idx], cmap[2, idx]], axis=-1)
        rgba = np.dstack([rgb, alpha])
    return rgba


def _palette_stats(rgba: np.ndarray) -> Tuple[np.ndarray, Dict[int, int]]:
    rgb = rgba[..., :3]
    alpha = rgba[..., 3]
    flat = rgb[alpha > 0]
    if flat.size == 0:
        raise ValueError("Preview alpha mask is empty")
    colors, counts = np.unique(flat.reshape(-1, 3), axis=0, return_counts=True)
    count_map: Dict[int, int] = {}
    for i, c in enumerate(colors):
        key = (int(c[0]) << 16) | (int(c[1]) << 8) | int(c[2])
        count_map[key] = int(counts[i])
    return rgb, count_map


def _infer_map_bbox(rgb: np.ndarray, alpha: np.ndarray, color_count_map: Dict[int, int]) -> Tuple[int, int, int, int]:
    ocean_keys: List[int] = []
    for key, count in color_count_map.items():
        r = (key >> 16) & 0xFF
        g = (key >> 8) & 0xFF
        b = key & 0xFF
        if count > 1000 and (b - r) >= 40 and (g - r) >= 20:
            ocean_keys.append(key)
    if not ocean_keys:
        raise ValueError("Failed to infer ocean colors for bbox detection")
    key_img = (rgb[..., 0].astype(np.uint32) << 16) | (rgb[..., 1].astype(np.uint32) << 8) | rgb[..., 2].astype(np.uint32)
    ocean_mask = np.isin(key_img, np.asarray(ocean_keys, dtype=np.uint32)) & (alpha > 0)
    ys, xs = np.where(ocean_mask)
    if ys.size == 0:
        raise ValueError("Failed to detect map bbox from ocean mask")
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _infer_border_pixels(rgb: np.ndarray, alpha: np.ndarray, x0: int, y0: int, x1: int, y1: int, color_count_map: Dict[int, int]) -> np.ndarray:
    border_keys: List[int] = []
    for key, count in color_count_map.items():
        r = (key >> 16) & 0xFF
        g = (key >> 8) & 0xFF
        b = key & 0xFF
        is_dark = max(r, g, b) <= 120
        is_cyan_line = (b - r) >= 80 and r <= 120 and g >= 140
        if count >= 5 and count < 10000 and (is_dark or is_cyan_line):
            border_keys.append(key)
    if not border_keys:
        raise ValueError("Failed to infer border colors")
    x_lo, y_lo, x_hi, y_hi = x0 + 2, y0 + 2, x1 - 1, y1 - 1
    crop_rgb = rgb[y_lo:y_hi, x_lo:x_hi]
    crop_alpha = alpha[y_lo:y_hi, x_lo:x_hi]
    crop_key = (crop_rgb[..., 0].astype(np.uint32) << 16) | (crop_rgb[..., 1].astype(np.uint32) << 8) | crop_rgb[..., 2].astype(np.uint32)
    border_mask = np.isin(crop_key, np.asarray(border_keys, dtype=np.uint32)) & (crop_alpha > 0)
    ys, xs = np.where(border_mask)
    return np.column_stack((xs + x_lo, ys + y_lo))


def _pixel_to_lonlat(points_xy: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> Tuple[np.ndarray, np.ndarray]:
    px = points_xy[:, 0].astype(np.float64)
    py = points_xy[:, 1].astype(np.float64)
    lon = (px - x0) / max(1.0, (x1 - x0)) * 360.0 - 180.0
    lat = 90.0 - (py - y0) / max(1.0, (y1 - y0)) * 180.0
    return lon, lat


def _quantize_tiles(lon: np.ndarray, lat: np.ndarray, cell_deg: float) -> Set[Tuple[int, int]]:
    lon_cells = int(round(360.0 / cell_deg))
    lat_cells = int(round(180.0 / cell_deg))
    lon_idx = np.floor((lon + 180.0) / cell_deg).astype(np.int32)
    lat_idx = np.floor((lat + 90.0) / cell_deg).astype(np.int32)
    valid = (lon_idx >= 0) & (lon_idx < lon_cells) & (lat_idx >= 0) & (lat_idx < lat_cells)
    rows = np.where(valid)[0]
    return {(int(lat_idx[i]), int(lon_idx[i])) for i in rows}


def _build_row_masks(border_tiles: Set[Tuple[int, int]], lon_cells: int, lat_cells: int) -> List[int]:
    masks = [0] * lat_cells
    for lat_i, lon_i in border_tiles:
        masks[lat_i] |= 1 << lon_i
    return masks


def _write_python_module(output_path: Path, eps_path: Path, cell_deg: float, lon_cells: int, lat_cells: int, row_masks: Sequence[int], bbox_px: Tuple[int, int, int, int], sample_count: int) -> None:
    x0, y0, x1, y1 = bbox_px
    lines: List[str] = [
        "# Auto-generated by build_world_border.py",
        f"# Source EPS: {eps_path.name}",
        "# Projection assumption: equirectangular in detected map frame",
        f"# Detected map bbox px: x0={x0}, y0={y0}, x1={x1}, y1={y1}",
        f"# Border source pixel count: {sample_count}",
        "",
        f"BORDER_CELL_DEG = {cell_deg}",
        f"BORDER_LON_CELLS = {lon_cells}",
        f"BORDER_LAT_CELLS = {lat_cells}",
        "",
        "BORDER_ROW_MASKS = [",
    ]
    for m in row_masks:
        lines.append(f"    {m},")
    lines += [
        "]",
        "",
        "def has_border(lat: float, lon: float) -> bool:",
        "    if lon < -180.0 or lon >= 180.0 or lat < -90.0 or lat >= 90.0:",
        "        return False",
        "    lon_i = int((lon + 180.0) // BORDER_CELL_DEG)",
        "    lat_i = int((lat + 90.0) // BORDER_CELL_DEG)",
        "    if lon_i < 0 or lon_i >= BORDER_LON_CELLS:",
        "        return False",
        "    if lat_i < 0 or lat_i >= BORDER_LAT_CELLS:",
        "        return False",
        "    return ((BORDER_ROW_MASKS[lat_i] >> lon_i) & 1) == 1",
        "",
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")


def _split_mask_to_words(mask: int, lon_cells: int, word_bits: int = 64) -> List[int]:
    words = (lon_cells + word_bits - 1) // word_bits
    return [(mask >> (i * word_bits)) & ((1 << word_bits) - 1) for i in range(words)]


def _to_rows_words(row_masks: List[int], lon_cells: int) -> List[List[int]]:
    return [_split_mask_to_words(m, lon_cells) for m in row_masks]


def _write_interop(out_dir: Path, base_name: str, row_masks: List[int], cell_deg: float, lon_cells: int, lat_cells: int) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows_words = _to_rows_words(row_masks, lon_cells)
    words_per_row = len(rows_words[0]) if rows_words else 0

    json_path = out_dir / f"{base_name}.json"
    json_path.write_text(json.dumps({
        "version": 1, "cell_deg": cell_deg, "lon_cells": lon_cells, "lat_cells": lat_cells,
        "word_bits": 64, "words_per_row": words_per_row,
        "rows_hex_le": [[f"0x{w:016X}" for w in row] for row in rows_words],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    hpp_lines = [
        "#pragma once", "#include <cstdint>", "",
        "namespace world_border_v1 {",
        f"constexpr double kCellDeg = {float(cell_deg):.10g};",
        f"constexpr int kLonCells = {int(lon_cells)};",
        f"constexpr int kLatCells = {int(lat_cells)};",
        "constexpr int kWordBits = 64;",
        f"constexpr int kWordsPerRow = {int(words_per_row)};", "",
        "constexpr std::uint64_t kRows[kLatCells][kWordsPerRow] = {",
    ]
    for row in rows_words:
        hpp_lines.append("    {" + ", ".join(f"0x{w:016X}ULL" for w in row) + "},")
    hpp_lines += [
        "};", "",
        "inline bool has_border(double lat, double lon) {",
        "    if (lon < -180.0 || lon >= 180.0 || lat < -90.0 || lat >= 90.0) return false;",
        "    const int lon_i = static_cast<int>((lon + 180.0) / kCellDeg);",
        "    const int lat_i = static_cast<int>((lat + 90.0) / kCellDeg);",
        "    if (lon_i < 0 || lon_i >= kLonCells || lat_i < 0 || lat_i >= kLatCells) return false;",
        "    const int w = lon_i / kWordBits;",
        "    const int b = lon_i % kWordBits;",
        "    return ((kRows[lat_i][w] >> b) & 1ULL) != 0ULL;",
        "}", "}",
    ]
    (out_dir / f"{base_name}.hpp").write_text("\n".join(hpp_lines) + "\n", encoding="utf-8")

    dart_lines = [
        "// Auto-generated by build_world_border.py",
        "library world_border_v1;", "",
        f"const double kCellDeg = {float(cell_deg):.10g};",
        f"const int kLonCells = {int(lon_cells)};",
        f"const int kLatCells = {int(lat_cells)};",
        "const int kWordBits = 64;",
        f"const int kWordsPerRow = {int(words_per_row)};", "",
        "const List<List<int>> kRows = <List<int>>[",
    ]
    for row in rows_words:
        dart_lines.append("  <int>[" + ", ".join(f"0x{w:016X}" for w in row) + "],")
    dart_lines += [
        "];", "",
        "bool hasBorder(double lat, double lon) {",
        "  if (lon < -180.0 || lon >= 180.0 || lat < -90.0 || lat >= 90.0) return false;",
        "  final int lonI = ((lon + 180.0) / kCellDeg).floor();",
        "  final int latI = ((lat + 90.0) / kCellDeg).floor();",
        "  if (lonI < 0 || lonI >= kLonCells || latI < 0 || latI >= kLatCells) return false;",
        "  final int w = lonI ~/ kWordBits;",
        "  final int b = lonI % kWordBits;",
        "  return ((kRows[latI][w] >> b) & 1) == 1;",
        "}",
    ]
    (out_dir / f"{base_name}.dart").write_text("\n".join(dart_lines) + "\n", encoding="utf-8")

    print(f"interop: {json_path.name}, {base_name}.hpp, {base_name}.dart")


def main() -> None:
    args = parse_args()
    if args.cell_deg <= 0:
        raise ValueError("--cell-deg must be positive")
    inv_lon = 360.0 / args.cell_deg
    inv_lat = 180.0 / args.cell_deg
    if abs(inv_lon - round(inv_lon)) > 1e-9 or abs(inv_lat - round(inv_lat)) > 1e-9:
        raise ValueError("--cell-deg must divide both 360 and 180 exactly")

    eps_path = Path(args.eps_path)
    if not eps_path.exists():
        raise FileNotFoundError(f"EPS file not found: {eps_path}")

    rgba = _read_eps_tiff_preview(eps_path)
    rgb, color_count_map = _palette_stats(rgba)
    alpha = rgba[..., 3]
    x0, y0, x1, y1 = _infer_map_bbox(rgb, alpha, color_count_map)
    points_xy = _infer_border_pixels(rgb, alpha, x0, y0, x1, y1, color_count_map)
    if points_xy.size == 0:
        raise ValueError("No border pixels were detected")

    lon, lat = _pixel_to_lonlat(points_xy, x0, y0, x1, y1)
    border_tiles = _quantize_tiles(lon, lat, args.cell_deg)
    lon_cells = int(round(360.0 / args.cell_deg))
    lat_cells = int(round(180.0 / args.cell_deg))
    row_masks = _build_row_masks(border_tiles, lon_cells, lat_cells)

    output_path = Path(args.output_py)
    _write_python_module(output_path, eps_path, float(args.cell_deg), lon_cells, lat_cells, row_masks, (x0, y0, x1, y1), int(points_xy.shape[0]))

    coverage = len(border_tiles) / float(lon_cells * lat_cells) * 100.0
    print(f"grid: {lat_cells}x{lon_cells} @ {args.cell_deg} deg, border cells: {len(border_tiles)} ({coverage:.2f}%)")
    print(f"saved: {output_path} ({output_path.stat().st_size / 1024:.1f} KB)")

    if args.interop:
        _write_interop(Path(args.out_dir), args.base_name, row_masks, float(args.cell_deg), lon_cells, lat_cells)


if __name__ == "__main__":
    main()
