"""
Data Pipeline: Download + Bake

DEM: Element84 → Planetary Computer → Open-Elevation
Population: WorldPop ArcGIS ImageServer
Land cover: ESA WorldCover (Planetary Computer STAC)
Coastal fix: population as ground truth for water/land correction.
"""

import os
import time
import logging
import numpy as np
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed, wait, FIRST_COMPLETED, CancelledError

from .core import (
    TILE_DIR, STAC_PC, STAC_E84,
    OPEN_ELEVATION_URL, WORLDPOP_ARCGIS_URL,
    ZONE_WATER, ZONE_NATURAL, ESA_TO_ZONE, ESA_URBAN_CLASS,
    _POP_NOISE_FLOOR, TARGET_SIZE, MAX_NODES, WATER_BYTE,
    build_adaptive_tree, build_adaptive_pop_tree,
    navigate_qtr5, navigate_qtr5_pop,
    decode_node_16, decode_pop_leaf_node,
    write_tile_binary, write_water_tile,
    verify_tile,
)

logger = logging.getLogger('geo_baker')


# ── Land Tile Index ────────────────────────────────────────────────

_land_tile_cache = None


def _build_land_tile_set():
    global _land_tile_cache
    if _land_tile_cache is not None:
        return _land_tile_cache
    land = set()
    try:
        import pystac_client
        try:
            import planetary_computer
            cat = pystac_client.Client.open(STAC_PC, modifier=planetary_computer.sign_inplace)
        except Exception:
            cat = pystac_client.Client.open(STAC_PC)
        for item in cat.search(collections=["esa-worldcover"],
                               bbox=[-180, -90, 180, 90], max_items=None).items():
            b = item.bbox
            if not b or len(b) < 4: continue
            for la in range(int(np.floor(b[1])), int(np.ceil(b[3])) + 1):
                for lo in range(int(np.floor(b[0])), int(np.ceil(b[2])) + 1):
                    if -90 <= la < 90 and -180 <= lo < 180:
                        land.add((la, lo))
    except Exception:
        pass
    _land_tile_cache = land
    return land


def is_likely_ocean(lat, lon):
    land = _build_land_tile_set()
    return bool(land) and (int(np.floor(lat)), int(np.floor(lon))) not in land


# ── STAC Utilities ─────────────────────────────────────────────────

_stac_catalog_cache = {}


def _open_stac(url):
    if url in _stac_catalog_cache:
        return _stac_catalog_cache[url]
    import pystac_client
    if url == STAC_PC:
        try:
            import planetary_computer
            cat = pystac_client.Client.open(url, modifier=planetary_computer.sign_inplace)
            _stac_catalog_cache[url] = cat
            return cat
        except Exception:
            pass
    cat = pystac_client.Client.open(url)
    _stac_catalog_cache[url] = cat
    return cat


def _fetch_raster(url, bbox, band=1):
    import rasterio
    for attempt in range(2):
        try:
            ds = rasterio.open(url)
            try:
                window = rasterio.windows.from_bounds(*bbox, ds.transform)
                data = ds.read(band, window=window, fill_value=0, boundless=True)
            finally:
                ds.close()
            if data is not None:
                return data
        except Exception:
            if attempt == 1: return None
            time.sleep(1)
    return None


def _fetch_stac_raster(stac_url, collection, bbox, asset_keys=("data", "DEM")):
    """Unified STAC raster fetcher for DEM and ESA."""
    try:
        cat = _open_stac(stac_url)
        items = list(cat.search(collections=[collection], bbox=bbox, max_items=10).items())
        if not items: return None
        try:
            import shapely.geometry as sg
            target = sg.box(*bbox)
            best = max(items, key=lambda it: target.intersection(
                sg.box(*it.bbox)).area if it.bbox and len(it.bbox) >= 4 else 0)
        except ImportError:
            best = items[0]
        for key in asset_keys:
            asset = best.assets.get(key)
            if asset and asset.href:
                data = _fetch_raster(asset.href, bbox)
                if data is not None:
                    return data
    except Exception:
        pass
    return None


# ── DEM Source Ranking ─────────────────────────────────────────────

_DEM_SPEED_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "dem_speed.json")
_DEM_SOURCES = [(STAC_E84, "cop-dem-glo-30"), (STAC_PC, "cop-dem-glo-30")]


def _rank_dem_sources_main():
    results = []
    bbox = [10, 48, 11, 49]
    for url, col in _DEM_SOURCES:
        t0 = time.time()
        try:
            data = _fetch_stac_raster(url, col, bbox)
            if data is not None:
                mbps = data.nbytes / (1024 * 1024) / max(time.time() - t0, 0.01)
                results.append({"url": url, "collection": col, "mbps": mbps})
                logger.info(f"[SPEED] {url.split('//')[1].split('/')[0]}: {mbps:.1f} MB/s")
                continue
        except Exception:
            pass
        results.append({"url": url, "collection": col, "mbps": 0.0})
    results.sort(key=lambda x: x["mbps"], reverse=True)
    try:
        import json
        Path(_DEM_SPEED_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(_DEM_SPEED_FILE, 'w') as f:
            json.dump(results, f)
    except Exception:
        pass


def _ranked_dem_sources():
    try:
        import json
        with open(_DEM_SPEED_FILE) as f:
            return [(r["url"], r["collection"]) for r in json.load(f) if r["mbps"] > 0]
    except Exception:
        return _DEM_SOURCES


# ── Downloads ──────────────────────────────────────────────────────

def _is_empty(data):
    if data is None: return True
    a = np.asarray(data)
    return a.size == 0 or (float(np.nanmax(a)) == 0.0 and float(np.nanmin(a)) == 0.0)


def _download_dem(lat, lon, max_conn=200):
    bbox = [lon, lat, lon + 1, lat + 1]
    for url, col in _ranked_dem_sources():
        try:
            dem = _fetch_stac_raster(url, col, bbox)
            if not _is_empty(dem): return dem
        except Exception:
            continue
    try:
        return _download_open_elevation(lat, lon, max_conn)
    except Exception:
        return None


_pop_session = None


def _get_pop_session():
    global _pop_session
    if _pop_session is None:
        import requests
        _pop_session = requests.Session()
        _pop_session.headers.update({'Connection': 'keep-alive'})
    return _pop_session


def _download_pop(lat, lon):
    params = {
        "bbox": f"{lon},{lat},{lon+1},{lat+1}",
        "bboxSR": "4326", "imageSR": "4326",
        "size": f"{TARGET_SIZE},{TARGET_SIZE}",
        "format": "tiff", "pixelType": "F32", "noData": "-9999", "f": "image",
    }
    try:
        resp = _get_pop_session().get(WORLDPOP_ARCGIS_URL, params=params, timeout=60)
        if resp.status_code == 200 and len(resp.content) > 1000:
            import rasterio
            from io import BytesIO
            with rasterio.open(BytesIO(resp.content)) as ds:
                data = ds.read(1)
                return np.clip(np.where(data == -9999, 0, data), 0, None)
    except Exception:
        pass
    return None


def _download_esa(lat, lon):
    bbox = [lon, lat, lon + 1, lat + 1]
    try:
        data = _fetch_stac_raster(STAC_PC, "esa-worldcover", bbox, asset_keys=("data", "map"))
        if not _is_empty(data): return data
    except Exception:
        pass
    return None


def _concurrent_download(lat, lon, max_conn=200):
    import threading
    results = {}

    def _dl(key, fn, *a):
        try: results[key] = fn(*a)
        except Exception: pass

    threads = [
        threading.Thread(target=_dl, args=('dem', _download_dem, lat, lon, max_conn)),
        threading.Thread(target=_dl, args=('pop', _download_pop, lat, lon)),
        threading.Thread(target=_dl, args=('esa', _download_esa, lat, lon)),
    ]
    for t in threads: t.start()
    for t in threads: t.join(timeout=120)
    return results.get('dem'), results.get('pop'), results.get('esa')


# ── Data Processing ────────────────────────────────────────────────

def _build_zone_grid(esa_data):
    if esa_data is not None:
        zone = np.full_like(esa_data, ZONE_NATURAL, dtype=np.uint8)
        for cls, zv in ESA_TO_ZONE.items():
            zone[esa_data == cls] = zv
        urban = (esa_data == ESA_URBAN_CLASS).astype(np.uint8)
        return zone, urban
    return (np.full((TARGET_SIZE, TARGET_SIZE), ZONE_NATURAL, dtype=np.uint8),
            np.zeros((TARGET_SIZE, TARGET_SIZE), dtype=np.uint8))


def align_tile_data(dem, pop, zone, urban=None, target_size=TARGET_SIZE):
    import scipy.ndimage

    def _resize(arr, sz):
        if arr is None: return np.zeros((sz, sz), dtype=np.float32)
        if arr.shape == (sz, sz): return arr
        zy, zx = sz / arr.shape[0], sz / arr.shape[1]
        order = 0 if arr.dtype in (np.uint8, np.int16, np.int32) else 1
        return scipy.ndimage.zoom(arr, (zy, zx), order=order).astype(arr.dtype)

    return (_resize(dem, target_size), _resize(pop, target_size),
            _resize(zone, target_size),
            _resize(urban, target_size) if urban is not None else None)


def fix_water_consistency(dem, pop, zone, urban, coastal_threshold=10.0):
    """Three-pass water/land consistency fix using population as ground truth."""
    if zone is None or pop is None: return
    # Pass 1: coastal cities — pop proves habitation, override water zone
    coast = (zone == ZONE_WATER) & (pop > coastal_threshold)
    if np.any(coast):
        zone[coast] = ZONE_NATURAL
        if urban is not None: urban[coast] = 0
        logger.debug(f"[FIX] coastal: {int(np.sum(coast))} px")
    # Pass 2: elevation > 0 can't be water
    if dem is not None:
        land = (dem > 0) & (zone == ZONE_WATER)
        if np.any(land):
            zone[land] = ZONE_NATURAL
            logger.debug(f"[FIX] elev>0: {int(np.sum(land))} px")
    # Pass 3: water zones must have zero pop/urban
    water = zone == ZONE_WATER
    pop[water] = 0
    if urban is not None: urban[water] = 0


# ── Runtime Tuning ─────────────────────────────────────────────────

def configure_runtime_tuning(max_conn=120, workers=16, **_):
    for k, v in {
        'GDAL_HTTP_TIMEOUT': '120', 'GDAL_HTTP_CONNECTTIMEOUT': '30',
        'GDAL_HTTP_MAX_RETRY': '5', 'GDAL_HTTP_RETRY_DELAY': '2',
        'GDAL_HTTP_KEEPALIVE': 'YES', 'GDAL_HTTP_MULTIPLEX': 'YES',
        'GDAL_HTTP_MERGE_CONSECUTIVE_RANGES': 'YES',
        'AWS_NO_SIGN_REQUEST': 'YES', 'AWS_EC2_METADATA_DISABLED': 'TRUE',
        'GDAL_DISABLE_READDIR_ON_OPEN': 'EMPTY_DIR',
    }.items():
        os.environ.setdefault(k, v)
    os.environ['AWS_NO_SIGN_REQUEST'] = 'YES'


# ── Bake Core ──────────────────────────────────────────────────────

def _tile_paths(lat, lon):
    return (os.path.join(TILE_DIR, f"{lon}_{lat}.qtree"),
            os.path.join(TILE_DIR, f"{lon}_{lat}.pop"))


def _write_water(lat, lon):
    Path(TILE_DIR).mkdir(parents=True, exist_ok=True)
    for p in _tile_paths(lat, lon):
        write_water_tile(p)


def _compute_tile(lat, lon, dem, pop, zone, urban):
    Path(TILE_DIR).mkdir(parents=True, exist_ok=True)
    has_pop = pop is not None and np.any(pop > 10.0)
    if np.all(zone == ZONE_WATER) and not has_pop:
        _write_water(lat, lon)
        return {'status': 'water', 'nodes': 0}

    qtree_path, pop_path = _tile_paths(lat, lon)

    terrain = build_adaptive_tree(dem, zone, pop)
    if not verify_tile(terrain, decode_node_16):
        logger.error(f"[VERIFY] {lon}_{lat}: terrain tree FAILED verification")
    write_tile_binary(terrain, qtree_path)
    nc = len(terrain) // 2
    if nc >= MAX_NODES - 64:
        logger.warning(f"[BUDGET] {lon}_{lat}: terrain nodes near MAX_NODES ({nc}/{MAX_NODES})")

    pnc = 0
    if pop is not None:
        pop_tree = build_adaptive_pop_tree(pop, urban)
        if not verify_tile(pop_tree, decode_pop_leaf_node):
            logger.error(f"[VERIFY] {lon}_{lat}: pop tree FAILED verification")
        write_tile_binary(pop_tree, pop_path)
        pnc = len(pop_tree) // 2
        if pnc >= MAX_NODES - 64:
            logger.warning(f"[BUDGET] {lon}_{lat}: pop nodes near MAX_NODES ({pnc}/{MAX_NODES})")

    return {'status': 'ok', 'nodes': nc, 'detail': f"nodes={nc}, pop_nodes={pnc}"}


def _bake_tile_core(lat, lon, offline=False, max_conn=200,
                    skip_ocean=True, no_data_water=False):
    t0 = time.time()

    if is_likely_ocean(lat, lon):
        esa = _download_esa(lat, lon)
        if esa is not None:
            zg = np.full_like(esa, ZONE_NATURAL, dtype=np.uint8)
            for c, z in ESA_TO_ZONE.items(): zg[esa == c] = z
            if np.count_nonzero(zg == ZONE_WATER) / max(zg.size, 1) > 0.95:
                _write_water(lat, lon)
                return {'status': 'ocean', 'nodes': 0}
            if abs(lat) >= 80:
                return {'status': 'no_data', 'nodes': 0}
        else:
            if no_data_water:
                _write_water(lat, lon)
                return {'status': 'ocean', 'nodes': 0}
            return {'status': 'no_data', 'nodes': 0}

    dem, pop, esa = _concurrent_download(lat, lon, max_conn)
    dl_t = time.time() - t0

    if dem is None:
        if no_data_water:
            _write_water(lat, lon)
            return {'status': 'ocean', 'nodes': 0}
        if esa is None: esa = _download_esa(lat, lon)
        if esa is not None:
            zg = np.full_like(esa, ZONE_NATURAL, dtype=np.uint8)
            for c, z in ESA_TO_ZONE.items(): zg[esa == c] = z
            if np.count_nonzero(zg == ZONE_WATER) / max(zg.size, 1) > 0.95:
                _write_water(lat, lon)
                return {'status': 'ocean', 'nodes': 0}
        return {'status': 'no_data', 'nodes': 0}

    zone, urban = _build_zone_grid(esa)
    dem, pop, zone, urban = align_tile_data(dem, pop, zone, urban)
    fix_water_consistency(dem, pop, zone, urban)

    qt0 = time.time()
    result = _compute_tile(lat, lon, dem, pop, zone, urban)
    result['timings'] = {'download': dl_t, 'quadtree': time.time() - qt0,
                         'total': time.time() - t0}
    return result


def _bake_tile_worker(lat, lon, offline=False, max_conn=200,
                      skip_ocean=True, no_data_water=False):
    try:
        result = _bake_tile_core(lat, lon, offline=offline, max_conn=max_conn,
                                 skip_ocean=skip_ocean, no_data_water=no_data_water)
        s = result['status'].upper()
        t = result.get('timings', {})
        if s == 'OK':
            logger.info(f"[OK] {lon}_{lat}: {result.get('detail','')} | "
                        f"total={t.get('total',0):.1f}s dl={t.get('download',0):.1f}s")
        elif s not in ('OCEAN', 'WATER'):
            logger.warning(f"[{s}] {lon}_{lat}")
        return result
    except Exception as e:
        logger.error(f"[ERROR] {lon}_{lat}: {e}")
        return {'status': 'error', 'nodes': 0, 'detail': str(e)}


def bake_tile(lat, lon, offline=False, max_conn=200):
    return _bake_tile_core(lat, lon, offline=offline, max_conn=max_conn)


# ── Batch Processing ──────────────────────────────────────────────────

# If no tile completes within this many seconds, assume workers are hung (network / IO) and stop the batch.
DEFAULT_BATCH_IDLE_TIMEOUT_S = 900


def _run_tile_batch(tile_list, workers, max_conn, start_time,
                    phase_label, no_data_water=False, idle_timeout_s=None):
    import sys as _sys
    if idle_timeout_s is None:
        idle_timeout_s = DEFAULT_BATCH_IDLE_TIMEOUT_S
    idle_timeout_s = int(idle_timeout_s)

    total = len(tile_list)
    done = 0
    stats = {'ok': 0, 'water': 0, 'ocean': 0, 'no_data': 0, 'no_land': 0, 'error': 0}
    node_sum = dl_sum = qt_sum = 0.0

    def _progress_line(force_log=False):
        elapsed = time.time() - start_time
        rate = done / max(elapsed, 0.1)
        eta_sec = (total - done) / max(rate, 0.01)
        pct = done / max(total, 1) * 100

        bar_len = 30
        filled = int(bar_len * done / max(total, 1))
        bar = '#' * filled + '-' * (bar_len - filled)

        avg_nodes = node_sum / max(stats['ok'], 1)
        avg_dl = dl_sum / max(done, 1)
        avg_qt = qt_sum / max(stats['ok'], 1)
        eta_min = int(eta_sec // 60)
        eta_rem = int(eta_sec % 60)

        msg = (
            f"[{phase_label}] [{bar}] {pct:.1f}% ({done}/{total}) "
            f"land={stats['ok']} water={stats['water']} ocean={stats['ocean']} err={stats['error']} "
            f"avg_nodes={avg_nodes:.0f} avg_dl={avg_dl:.1f}s avg_qt={avg_qt:.1f}s "
            f"{rate:.2f}T/s ETA:{eta_min}m{eta_rem:02d}s"
        )
        _sys.stdout.write(f"\r  {msg}   ")
        _sys.stdout.flush()
        if force_log or done % 10 == 0 or done == total:
            logger.info(msg)

    def _consume_future(fut, la, lo):
        nonlocal done, node_sum, dl_sum, qt_sum
        try:
            if fut.cancelled():
                stats['error'] = stats.get('error', 0) + 1
                logger.warning(f"[{phase_label}] cancelled {lo}_{la} (idle watchdog)")
                return
            r = fut.result()
        except CancelledError:
            stats['error'] = stats.get('error', 0) + 1
            logger.warning(f"[{phase_label}] cancelled {lo}_{la} (idle watchdog)")
            return
        except Exception as e:
            stats['error'] = stats.get('error', 0) + 1
            logger.error(f"[{phase_label}] future error {lo}_{la}: {e}")
            return
        s = r.get('status', 'error')
        stats[s] = stats.get(s, 0) + 1
        t = r.get('timings', {})
        dl_sum += t.get('download', 0)
        qt_sum += t.get('quadtree', 0)
        if s == 'ok':
            node_sum += r.get('nodes', 0)
        done += 1
        if done % 5 == 0 or done == total:
            _progress_line()

    pool = ProcessPoolExecutor(max_workers=workers)
    try:
        future_to_tile = {
            pool.submit(_bake_tile_worker, la, lo, False, max_conn, True, no_data_water): (la, lo)
            for la, lo in tile_list
        }
        if idle_timeout_s <= 0:
            for fut in as_completed(future_to_tile):
                la, lo = future_to_tile[fut]
                _consume_future(fut, la, lo)
        else:
            pending = set(future_to_tile.keys())
            while pending:
                done_set, pending = wait(
                    pending, timeout=idle_timeout_s, return_when=FIRST_COMPLETED
                )
                if not done_set:
                    n_left = len(pending)
                    logger.error(
                        f"[{phase_label}] No tile finished in {idle_timeout_s}s "
                        f"(possible hung worker / network). Cancelling {n_left} pending futures "
                        f"(running workers may still spin until pool shutdown). "
                        f"Re-run with --retry-errors, smaller --bbox, or --tile-timeout 0 to disable watchdog."
                    )
                    for f in list(pending):
                        f.cancel()
                    stats['error'] = stats.get('error', 0) + n_left
                    break
                for fut in done_set:
                    la, lo = future_to_tile[fut]
                    _consume_future(fut, la, lo)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    _progress_line(force_log=True)
    _sys.stdout.write('\n')
    elapsed = time.time() - start_time
    ok = stats['ok']
    logger.info(
        f"[{phase_label}] DONE {done} tiles. "
        f"land={ok} water={stats['water']} ocean={stats['ocean']} err={stats['error']} "
        f"nodata={stats['no_data']} noland={stats['no_land']} "
        f"avg_nodes={node_sum/max(ok,1):.0f} rate={done/max(elapsed,0.1):.2f}T/s elapsed={elapsed:.0f}s"
    )
    return stats


# ── Public API ─────────────────────────────────────────────────────

def bake_region(lat_min, lat_max, lon_min, lon_max, offline=False,
                workers=16, max_conn=120, split=None, skip_existing=True,
                idle_timeout_s=None):
    lat_lo, lat_hi = sorted((float(lat_min), float(lat_max)))
    lon_lo, lon_hi = sorted((float(lon_min), float(lon_max)))
    # Convert bbox to half-open integer tile ranges and clamp to global grid:
    # lat in [-90, 90), lon in [-180, 180)
    lat_start = max(-90, int(np.floor(lat_lo)))
    lat_end = min(90, int(np.ceil(lat_hi)))
    lon_start = max(-180, int(np.floor(lon_lo)))
    lon_end = min(180, int(np.ceil(lon_hi)))
    if lat_start >= lat_end or lon_start >= lon_end:
        logger.warning(
            f"[REGION] Empty bbox after clamp: "
            f"input=({lon_min},{lat_min},{lon_max},{lat_max}) "
            f"-> lon[{lon_start},{lon_end}) lat[{lat_start},{lat_end})"
        )
        return
    tiles = [(la, lo) for la in range(lat_start, lat_end)
             for lo in range(lon_start, lon_end)]
    if split:
        n, m = split
        tiles = tiles[m - 1::n]
    logger.info(
        f"[REGION] Total: {len(tiles)} "
        f"(lon[{lon_start},{lon_end}) lat[{lat_start},{lat_end}))"
    )
    if skip_existing:
        before = len(tiles)
        existing = set()
        td = Path(TILE_DIR)
        if td.exists():
            for qf in td.glob("*.qtree"):
                parts = qf.stem.split('_')
                if len(parts) != 2:
                    continue
                try:
                    lon_i, lat_i = int(parts[0]), int(parts[1])
                    key = (lat_i, lon_i)
                except ValueError:
                    continue
                pp = td / f"{parts[0]}_{parts[1]}.pop"
                if not pp.exists():
                    continue
                # Do not treat suspicious land-water placeholders as "done":
                # if qtree is 1-byte water but tile is likely land, keep it for rebake.
                if qf.stat().st_size <= 1 and not is_likely_ocean(lat_i, lon_i):
                    continue
                existing.add(key)
        tiles = [t for t in tiles if t not in existing]
        logger.info(f"[REGION] Skip existing: {before} -> {len(tiles)} ({before - len(tiles)} done)")
    _run_tile_batch(tiles, workers, max_conn, time.time(), "REGION", idle_timeout_s=idle_timeout_s)


def bake_global(offline=False, workers=16, max_conn=120, split=None,
                skip_ocean=True, skip_existing=True, no_data_water=False, idle_timeout_s=None):
    _rank_dem_sources_main()
    tiles = [(la, lo) for la in range(-90, 90) for lo in range(-180, 180)]
    if split:
        n, m = split
        tiles = tiles[m - 1::n]
    logger.info(f"[GLOBAL] Total: {len(tiles)}")
    if skip_existing:
        before = len(tiles)
        existing = set()
        td = Path(TILE_DIR)
        for qf in td.glob("*.qtree"):
            parts = qf.stem.split('_')
            if len(parts) != 2: continue
            try:
                lon_i, lat_i = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            key = (lat_i, lon_i)
            pp = td / f"{parts[0]}_{parts[1]}.pop"
            if not pp.exists():
                continue
            # Do not skip suspicious placeholders (water byte on likely land).
            if qf.stat().st_size <= 1 and not is_likely_ocean(lat_i, lon_i):
                continue
            existing.add(key)
        tiles = [t for t in tiles if t not in existing]
        logger.info(f"[GLOBAL] Skip existing: {before} -> {len(tiles)} ({before - len(tiles)} done)")
    _run_tile_batch(tiles, workers, max_conn, time.time(), "GLOBAL", no_data_water,
                    idle_timeout_s=idle_timeout_s)


def retry_errors(workers=16, max_conn=120, idle_timeout_s=None):
    td = Path(TILE_DIR)
    if not td.exists(): return
    errors = []
    for qf in td.glob("*.qtree"):
        if qf.stat().st_size <= 1:
            parts = qf.stem.split('_')
            if len(parts) == 2:
                try:
                    lon, lat = int(parts[0]), int(parts[1])
                    if not is_likely_ocean(lat, lon):
                        errors.append((lat, lon))
                except ValueError:
                    pass
    if not errors:
        logger.info("[RETRY] No error tiles")
        return
    logger.info(f"[RETRY] Retrying {len(errors)} tiles")
    _run_tile_batch(errors, workers, max_conn, time.time(), "RETRY", idle_timeout_s=idle_timeout_s)


# ── Anomaly Detection & Fix ───────────────────────────────────────

def _scan_problem_tiles(pop_threshold=10.0, grid_size=3, min_hits=2):
    td = Path(TILE_DIR)
    if not td.exists(): return []
    g = max(1, int(grid_size))
    step = 1.0 / (g + 1)
    pts = [((r + 1) * step, (c + 1) * step) for r in range(g) for c in range(g)]
    problems = []
    for qf in sorted(td.glob("*.qtree")):
        parts = qf.stem.split('_')
        if len(parts) != 2: continue
        try: lon, lat = int(parts[0]), int(parts[1])
        except ValueError: continue
        pp = td / f"{lon}_{lat}.pop"
        if not pp.exists(): continue
        try:
            with open(qf, 'rb') as f: traw = f.read()
            with open(pp, 'rb') as f: praw = f.read()
            tdata = traw[16:] if traw[:4] == b'QTR5' else traw
            pdata = praw[16:] if praw[:4] == b'QTR5' else praw
            hits = water_hits = valid = 0
            for fl, flo in pts:
                tn = navigate_qtr5(tdata, fl, flo) if len(tdata) > 1 else {'is_leaf': True, 'zone': ZONE_WATER}
                pn = navigate_qtr5_pop(pdata, fl, flo) if len(pdata) > 1 else None
                if not tn or not tn.get('is_leaf'): continue
                valid += 1
                if tn.get('zone') == ZONE_WATER:
                    water_hits += 1
                    if pn and (pn.get('pop_density', 0) >= pop_threshold or pn.get('urban_zone', 0) > 0):
                        hits += 1
            if hits >= max(1, int(min_hits)):
                problems.append((lat, lon))
            elif valid > 0 and water_hits >= valid and not is_likely_ocean(lat, lon):
                problems.append((lat, lon))
        except Exception:
            continue
    return problems


def fix_population_zone_batch(pop_threshold=10.0, workers=8, max_conn=60,
                              max_rounds=2, sample_grid=3, min_hits=2,
                              max_tiles_per_round=500, idle_timeout_s=None):
    total = 0
    done = set()
    for rd in range(1, max(1, int(max_rounds)) + 1):
        tiles = [t for t in _scan_problem_tiles(pop_threshold, sample_grid, min_hits)
                 if t not in done]
        if not tiles:
            logger.info(f"[FIX-POP] Round {rd}: {'converged' if rd > 1 else 'no anomalies'}")
            break
        if max_tiles_per_round and len(tiles) > max_tiles_per_round:
            tiles = tiles[:max_tiles_per_round]
        logger.info(f"[FIX-POP] Round {rd}: {len(tiles)} problem tiles")
        _run_tile_batch(tiles, workers, max_conn, time.time(), f"FIX-R{rd}",
                        idle_timeout_s=idle_timeout_s)
        total += len(tiles)
        done.update(tiles)
    logger.info(f"[FIX-POP] Done. Total re-baked: {total}")


def fix_coastal_batch(cities_json_path="data/global_cities.json", pop_threshold=10.0,
                      workers=8, max_conn=60, idle_timeout_s=None):
    import json
    if not os.path.exists(cities_json_path):
        logger.error(f"[FIX] Cities file not found: {cities_json_path}")
        return
    with open(cities_json_path, encoding='utf-8') as f:
        cities = json.load(f)
    problems = {}
    for city in cities:
        la, lo = city.get('la', 0), city.get('lo', 0)
        if la == 0 and lo == 0: continue
        lai, loi = int(np.floor(la)), int(np.floor(lo))
        tp = os.path.join(TILE_DIR, f"{loi}_{lai}.qtree")
        pp = os.path.join(TILE_DIR, f"{loi}_{lai}.pop")
        if not os.path.exists(tp) or os.path.getsize(tp) <= 1: continue
        if not os.path.exists(pp): continue
        try:
            with open(tp, 'rb') as f: raw = f.read()
            td = raw[16:] if raw[:4] == b'QTR5' else raw
            n = navigate_qtr5(td, 0.5, 0.5)
            if not n or n.get('zone') != ZONE_WATER: continue
            with open(pp, 'rb') as f: raw = f.read()
            pd = raw[16:] if raw[:4] == b'QTR5' else raw
            pn = navigate_qtr5_pop(pd, 0.5, 0.5)
            if pn and pn.get('pop_density', 0) > pop_threshold:
                problems.setdefault((lai, loi), []).append(city.get('n', '?'))
        except Exception:
            continue
    if not problems:
        logger.info("[FIX] No coastal city problems found")
        return
    logger.info(f"[FIX] Found {len(problems)} problem tiles")
    _run_tile_batch(list(problems.keys()), workers, max_conn, time.time(), "FIX",
                    idle_timeout_s=idle_timeout_s)


# ── Open-Elevation Fallback ───────────────────────────────────────

def _download_open_elevation(lat, lon, max_conn=200):
    import asyncio

    async def _fetch(lat, lon, max_conn):
        import aiohttp
        res = 0.01
        lats = np.arange(lat, lat + 1 + res / 2, res)
        lons = np.arange(lon, lon + 1 + res / 2, res)
        elev = np.zeros((len(lats), len(lons)), dtype=np.int16)
        locs = [{"latitude": float(la), "longitude": float(lo)} for la in lats for lo in lons]
        idx = [(i, j) for i in range(len(lats)) for j in range(len(lons))]
        bs = 150
        sem = asyncio.Semaphore(max_conn)

        async def batch(session, bi):
            s, e = bi * bs, min((bi + 1) * bs, len(locs))
            async with sem:
                for att in range(3):
                    try:
                        async with session.post(OPEN_ELEVATION_URL,
                                                json={"locations": locs[s:e]},
                                                timeout=aiohttp.ClientTimeout(total=30)) as r:
                            if r.status == 200:
                                for k, v in enumerate((await r.json()).get('results', [])):
                                    if k < e - s:
                                        i, j = idx[s + k]
                                        elev[i, j] = int(v.get('elevation', 0))
                                return
                            if r.status == 429: await asyncio.sleep(2 ** att)
                    except Exception:
                        await asyncio.sleep(1)

        async with aiohttp.ClientSession() as session:
            await asyncio.gather(*(batch(session, i)
                                   for i in range((len(locs) + bs - 1) // bs)))
        return elev

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, _fetch(lat, lon, max_conn)).result()
        return loop.run_until_complete(_fetch(lat, lon, max_conn))
    except RuntimeError:
        return asyncio.run(_fetch(lat, lon, max_conn))


# ── CLI Helpers ────────────────────────────────────────────────────

def _parse_split_arg(s):
    parts = s.split('/')
    if len(parts) != 2: raise ValueError("Format: N/M")
    n, m = int(parts[0]), int(parts[1])
    if n < 1 or m < 1 or m > n: raise ValueError(f"Invalid: N={n}, M={m}")
    return (n, m)
