# GeoBaker Overview

GeoBaker turns public raster datasets into runtime-friendly quadtree data.

## Pipeline

1. Select a 1-degree tile by integer longitude and latitude.
2. Download or load aligned source rasters:
   - CopDEM for elevation
   - WorldPop for population density
   - ESA WorldCover for terrain zone classification
3. Resample sources to a common power-of-two grid.
4. Fix obvious water/land conflicts:
   - populated coastal pixels are treated as land
   - true water pixels clear population and urban metadata
5. Build adaptive quadtree tiles:
   - terrain: elevation, gradient, natural zone
   - population: density, urban zone
6. Optionally pack tiles into GeoPack `.dat` files for distribution.

## Modules

| Module | Responsibility |
| --- | --- |
| `geo_baker_pkg/core.py` | binary encoding, quadtree construction, navigation, validation |
| `geo_baker_pkg/pipeline.py` | downloads, alignment, consistency fixes, tile/global bake orchestration |
| `geo_baker_pkg/io.py` | QTR5 tile IO, GeoPack export, point queries |
| `geo_baker_pkg/cli.py` | command-line interface |
| `tools/geo_inspect.py` | tile inspection, validation, and size reports |
| `tools/verify_cities.py` | city-sample validation |
| `tools/visualize.py` | elevation/population/zone preview images |

## Runtime Strategy

For games, keep terrain and population separate. Terrain tends to be needed for
movement, visibility, and map rendering; population can often be streamed or
queried at lower priority. This split keeps IO and memory pressure lower than a
single monolithic tile type.

Recommended distribution pattern:

- ship or download regional GeoPack shards
- keep `tiles/` for local bake/debug workflows
- build `terrain.dat` and `population.dat` only when producing runtime artifacts

## Generated Files

These are local build outputs and should not be committed:

- `tiles/*.qtree`
- `tiles/*.pop`
- `terrain.dat`
- `population.dat`
- `logs/`
- `cache/`
- preview images
- backup archives

## Validation

```bash
python -m unittest
python tools/geo_inspect.py validate
python tools/verify_cities.py --cities data/global_cities.json
```

For visual debugging:

```bash
python tools/visualize.py compare --lat 31.2304 --lon 121.4737 -o shanghai.png
```
