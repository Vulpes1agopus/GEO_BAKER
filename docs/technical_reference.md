# Technical Reference

## Tile Coordinate System

Each tile covers one integer-degree cell:

```text
tile lon = floor(longitude)
tile lat = floor(latitude)
filename = tiles/{lon}_{lat}.qtree
population filename = tiles/{lon}_{lat}.pop
```

Within a tile, coordinates are normalized to `[0, 1)`.

## QTR5 Nodes

QTR5 stores a quadtree as 16-bit little-endian nodes in DFS pre-order.

### Terrain Leaf

```text
[1 bit is_leaf = 1][11 bit elevation][2 bit gradient][2 bit zone]
```

### Population Leaf

```text
[1 bit is_leaf = 1][12 bit population][3 bit urban zone]
```

### Branch

```text
[1 bit is_leaf = 0][15 bit subtree_size]
```

`subtree_size` is the total node count for the branch subtree, including the
branch node itself. It lets the reader skip non-selected quadrants without child
offset tables.

Quadrant order is:

```text
0 = NW
1 = NE
2 = SW
3 = SE
```

The builder and reader must keep this order identical.

## Special Tiles

A 1-byte tile represents all-water/no-data water:

```text
0xFF
```

Readers treat it as a single water leaf with zero population.

## GeoPack

GeoPack bundles many QTR5 tiles into one random-access file.

```text
Header: 32 bytes
Index:  360 * 180 * 16 bytes
Data:   zstd-compressed tile payloads
```

The fixed index maps longitude/latitude cells to `(offset, size)` pairs. Missing
entries indicate no tile in the pack.

## Precision Notes

Terrain and population are intentionally encoded differently:

- elevation uses nonlinear meter buckets for broad terrain precision
- population uses logarithmic density buckets to preserve dense urban contrast
- terrain zones are categorical and should be treated as a coarse semantic layer

For gameplay, prefer elevation and gradient for physical rules. Use terrain zone
as an additional hint rather than as a precise boundary source.
