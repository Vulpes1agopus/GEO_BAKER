# GeoBaker

GeoBaker is a Python pipeline for baking global geospatial rasters into compact
quadtree tiles for games, simulations, and offline map tooling.

It combines CopDEM elevation, WorldPop population density, and ESA WorldCover
land-cover classes into two runtime-friendly products:

- QTR5 tile files under `tiles/`
- optional GeoPack bundles such as `terrain.dat` and `population.dat`

Generated tiles, packs, logs, previews, caches, and backups are intentionally
ignored by Git. Keep this repository source-only; publish data packs separately.

## Features

- Adaptive quadtree encoding for terrain and population.
- 16-bit node format with nonlinear elevation and logarithmic population
  encoding.
- Separate terrain and population trees, so games can stream only what they
  need.
- Budget-aware splitting to keep tile size predictable.
- Coastal consistency pass that uses population to avoid marking inhabited
  coastlines as water.
- GeoPack export with a fixed 360 x 180 tile index and zstd-compressed tile
  payloads.
- CLI tools for baking, querying, validation, tile inspection, and visualization.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Or install the package in editable mode:

```bash
pip install -e .
```

GeoBaker downloads public geospatial datasets from third-party services. The
default pipeline does not require project-specific credentials.

## Quick Start

Bake one 1-degree tile:

```bash
python -m geo_baker_pkg --tile 116,39
```

Query a point from local tile files:

```bash
python -m geo_baker_pkg --query 39.9 116.4
```

Bake a bounding box:

```bash
python -m geo_baker_pkg --bbox 70 20 140 55 --workers 8 --conn 64
```

Export packed runtime files:

```bash
python -m geo_baker_pkg --pack
python -m geo_baker_pkg --pack-pop
```

Visualize a point as a four-panel diagnostic image:

```bash
python tools/visualize.py compare --lat 31.2304 --lon 121.4737 -o shanghai.png
```

## CLI Overview

| Command | Purpose |
| --- | --- |
| `--tile LON,LAT` | Bake one tile. |
| `--bbox W S E N` | Bake a rectangular region. |
| `--global` | Bake all global land/ocean tiles. |
| `--retry-errors` | Retry tiles marked as failed. |
| `--fix-pop-zone` | Re-bake tiles where population and terrain zone disagree. |
| `--pack` | Build `terrain.dat`. |
| `--pack-pop` | Build `population.dat`. |
| `--query LAT LON` | Query local tile files. |
| `--query-pack LAT LON` | Query packed terrain data. |
| `--stats` | Print local tile statistics. |

## Repository Layout

```text
GeoBaker/
├── geo_baker_pkg/       # Core package: encoding, quadtree, baking, packing, CLI
├── tools/               # Inspection, verification, visualization helpers
├── scripts/             # Optional data-prep and utility scripts
├── tests/               # Unit tests for quadtree, IO, and pipeline controls
├── data/                # Small metadata files safe to commit
├── docs/                # Architecture and format notes
├── README.md
├── CHANGELOG.md
├── LICENSE
└── pyproject.toml
```

Ignored local/generated directories include `tiles/`, `logs/`, `cache/`,
`images/`, `backups/`, `.venv/`, and binary packs such as `*.dat`.

## Data Model

QTR5 stores nodes in DFS pre-order:

```text
terrain leaf: [1 bit leaf][11 bit elevation][2 bit gradient][2 bit zone]
pop leaf:     [1 bit leaf][12 bit population][3 bit urban type]
branch:       [1 bit branch][15 bit subtree size]
```

The runtime reader navigates by skipping subtrees with `subtree_size`, so a point
query is `O(depth)` with no child-offset table.

See [docs/overview.md](docs/overview.md) and
[docs/technical_reference.md](docs/technical_reference.md) for more detail.

## Public-Repo Hygiene

Before publishing:

```bash
git status --short
git ls-files | grep -E '(^tiles/|\.dat$|\.png$|\.log$|backups/|cache/)'
gitleaks detect --source .
```

The second command should print nothing for a source-only public repository.

## License

MIT License. See [LICENSE](LICENSE).
