import unittest

import numpy as np

from geo_baker_pkg.core import (
    ZONE_FOREST,
    ZONE_NATURAL,
    ZONE_WATER,
    build_adaptive_pop_tree,
    build_adaptive_tree,
    decode_node_16,
    decode_pop_leaf_node,
    verify_tile,
)


class QuadtreeSplitTests(unittest.TestCase):
    def test_terrain_budget_is_respected(self):
        rng = np.random.default_rng(7)
        dem = rng.normal(100.0, 50.0, size=(128, 128)).astype(np.float32)
        zone = np.full((128, 128), ZONE_NATURAL, dtype=np.uint8)

        raw = build_adaptive_tree(dem, zone, max_nodes=200)
        root = decode_node_16(raw[:2])

        self.assertFalse(root["is_leaf"])
        self.assertLessEqual(len(raw) // 2, 200)
        self.assertEqual(root["subtree_size"], len(raw) // 2)
        self.assertTrue(verify_tile(raw, decode_node_16))

    def test_flat_water_land_boundary_still_splits(self):
        dem = np.zeros((64, 64), dtype=np.float32)
        zone = np.full((64, 64), ZONE_NATURAL, dtype=np.uint8)
        zone[:, :30] = ZONE_WATER

        raw = build_adaptive_tree(dem, zone)

        self.assertGreater(len(raw) // 2, 21)
        self.assertTrue(verify_tile(raw, decode_node_16))

    def test_flat_landcover_boundary_still_splits(self):
        dem = np.full((64, 64), 25.0, dtype=np.float32)
        zone = np.full((64, 64), ZONE_NATURAL, dtype=np.uint8)
        zone[:, 30:] = ZONE_FOREST

        raw = build_adaptive_tree(dem, zone)

        self.assertGreater(len(raw) // 2, 21)
        self.assertTrue(verify_tile(raw, decode_node_16))

    def test_population_hotspot_splits_without_large_variance_gate_only(self):
        pop = np.zeros((64, 64), dtype=np.float32)
        pop[31:33, 31:33] = 40.0
        urban = np.zeros((64, 64), dtype=np.uint8)

        raw = build_adaptive_pop_tree(pop, urban)

        self.assertGreater(len(raw) // 2, 53)
        self.assertTrue(verify_tile(raw, decode_pop_leaf_node))


if __name__ == "__main__":
    unittest.main()
