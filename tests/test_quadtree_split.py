import unittest

import numpy as np

from geo_baker_pkg.core import (
    ZONE_FOREST,
    ZONE_NATURAL,
    ZONE_WATER,
    build_adaptive_pop_tree,
    build_adaptive_tree,
    decode_node_qtr6,
    decode_pop_leaf_node,
    navigate_qtr6,
    verify_tile,
    wrap_terrain_tile,
    unwrap_terrain_tile,
)


class QuadtreeSplitTests(unittest.TestCase):
    def test_terrain_budget_is_respected(self):
        rng = np.random.default_rng(7)
        dem = rng.normal(100.0, 50.0, size=(128, 128)).astype(np.float32)
        zone = np.full((128, 128), ZONE_NATURAL, dtype=np.uint8)

        raw = build_adaptive_tree(dem, zone, max_nodes=200)
        root = decode_node_qtr6(raw[:2])

        self.assertFalse(root["is_leaf"])
        self.assertLessEqual(len(raw) // 2, 200)
        self.assertEqual(root["subtree_size"], len(raw) // 2)
        self.assertTrue(verify_tile(raw, decode_node_qtr6))

    def test_flat_water_land_boundary_still_splits(self):
        dem = np.zeros((64, 64), dtype=np.float32)
        zone = np.full((64, 64), ZONE_NATURAL, dtype=np.uint8)
        zone[:, :30] = ZONE_WATER

        raw = build_adaptive_tree(dem, zone)

        self.assertGreater(len(raw) // 2, 21)
        self.assertTrue(verify_tile(raw, decode_node_qtr6))

    def test_flat_landcover_boundary_still_splits(self):
        dem = np.full((64, 64), 25.0, dtype=np.float32)
        zone = np.full((64, 64), ZONE_NATURAL, dtype=np.uint8)
        zone[:, 30:] = ZONE_FOREST

        raw = build_adaptive_tree(dem, zone)

        self.assertGreater(len(raw) // 2, 21)
        self.assertTrue(verify_tile(raw, decode_node_qtr6))

    def test_flat_noise_does_not_explode_nodes(self):
        rng = np.random.default_rng(11)
        dem = rng.normal(100.0, 1.5, size=(128, 128)).astype(np.float32)
        zone = np.full((128, 128), ZONE_NATURAL, dtype=np.uint8)
        stats = {}

        raw = build_adaptive_tree(dem, zone, max_nodes=30000, stats=stats)

        self.assertLess(len(raw) // 2, 1000)
        self.assertEqual(stats.get("split_elevation", 0), 0)
        self.assertTrue(verify_tile(raw, decode_node_qtr6))

    def test_single_dem_spike_does_not_force_budget(self):
        dem = np.full((128, 128), 120.0, dtype=np.float32)
        dem[64, 64] = 5000.0
        zone = np.full((128, 128), ZONE_NATURAL, dtype=np.uint8)
        stats = {}

        raw = build_adaptive_tree(dem, zone, max_nodes=30000, stats=stats)

        self.assertLess(len(raw) // 2, 1000)
        self.assertEqual(stats.get("split_elevation", 0), 0)
        self.assertTrue(verify_tile(raw, decode_node_qtr6))

    def test_high_relief_ridge_splits_but_not_to_cap(self):
        dem = np.zeros((128, 128), dtype=np.float32)
        dem[:, 70:] = 900.0
        zone = np.full((128, 128), ZONE_NATURAL, dtype=np.uint8)
        stats = {}

        raw = build_adaptive_tree(dem, zone, max_nodes=20000, stats=stats)

        self.assertGreater(stats.get("split_elevation", 0), 0)
        self.assertGreater(len(raw) // 2, 21)
        self.assertLess(len(raw) // 2, 5000)
        self.assertTrue(verify_tile(raw, decode_node_qtr6))

    def test_split_stats_records_zone_and_leaf_reasons(self):
        dem = np.zeros((64, 64), dtype=np.float32)
        zone = np.full((64, 64), ZONE_NATURAL, dtype=np.uint8)
        zone[:, :30] = ZONE_WATER
        stats = {}

        raw = build_adaptive_tree(dem, zone, max_nodes=30000, stats=stats)
        node = navigate_qtr6(raw, 0.5, 0.2)

        self.assertIsNotNone(node)
        self.assertGreater(stats.get("split_zone_water", 0), 0)
        self.assertGreater(stats.get("leaf", 0), 0)
        self.assertTrue(verify_tile(raw, decode_node_qtr6))

    def test_qtr6_tile_header_round_trips_codec(self):
        dem = np.full((16, 16), -28.0, dtype=np.float32)
        zone = np.full((16, 16), ZONE_NATURAL, dtype=np.uint8)

        raw = build_adaptive_tree(dem, zone, max_nodes=64, codec='qtr6')
        wrapped = wrap_terrain_tile(raw, codec='qtr6')
        payload, codec = unwrap_terrain_tile(wrapped)

        self.assertEqual(codec, 'qtr6')
        self.assertEqual(payload, raw)
        self.assertTrue(verify_tile(payload, decode_node_qtr6))

    def test_population_hotspot_splits_without_large_variance_gate_only(self):
        pop = np.zeros((64, 64), dtype=np.float32)
        pop[31:33, 31:33] = 40.0
        urban = np.zeros((64, 64), dtype=np.uint8)

        raw = build_adaptive_pop_tree(pop, urban)

        self.assertGreater(len(raw) // 2, 53)
        self.assertTrue(verify_tile(raw, decode_pop_leaf_node))


if __name__ == "__main__":
    unittest.main()
