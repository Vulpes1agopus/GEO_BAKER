import unittest

import numpy as np

from geo_baker_pkg.core import (
    ESA_WATER_CLASS,
    ZONE_NATURAL,
    ZONE_WATER,
    build_adaptive_tree,
    decode_node_qtr6,
    navigate_qtr6,
    verify_tile,
)
from geo_baker_pkg.pipeline import _build_zone_layers, fix_water_consistency


class WaterLogicTests(unittest.TestCase):
    def test_explicit_esa_water_survives_positive_dem(self):
        esa = np.full((8, 8), ESA_WATER_CLASS, dtype=np.uint8)
        zone, urban, explicit, *_ = _build_zone_layers(esa)
        dem = np.full((8, 8), 455.0, dtype=np.float32)
        pop = np.zeros((8, 8), dtype=np.float32)

        fix_water_consistency(dem, pop, zone, urban, explicit)

        self.assertTrue(np.all(zone == ZONE_WATER))
        self.assertTrue(np.all(dem == 0.0))

    def test_esa_class_zero_is_not_rescued_into_fake_land(self):
        esa = np.zeros((8, 8), dtype=np.uint8)
        zone, urban, explicit, *_ = _build_zone_layers(esa)
        dem = np.full((8, 8), 120.0, dtype=np.float32)
        pop = np.full((8, 8), 100.0, dtype=np.float32)

        fix_water_consistency(dem, pop, zone, urban, explicit)

        self.assertTrue(np.all(zone == ZONE_WATER))
        self.assertTrue(np.all(dem == 0.0))
        self.assertTrue(np.all(pop == 0.0))

    def test_explicit_esa_land_survives_negative_dem(self):
        esa = np.full((8, 8), 30, dtype=np.uint8)
        zone, urban, explicit, *_ = _build_zone_layers(esa)
        dem = np.full((8, 8), -28.0, dtype=np.float32)
        pop = np.zeros((8, 8), dtype=np.float32)

        fix_water_consistency(dem, pop, zone, urban, explicit)

        self.assertTrue(np.all(zone == ZONE_NATURAL))
        self.assertTrue(np.all(dem == -28.0))

    def test_inferred_no_coverage_negative_dem_stays_water(self):
        esa = np.zeros((8, 8), dtype=np.uint8)
        zone, urban, explicit, *_ = _build_zone_layers(esa)
        dem = np.full((8, 8), -28.0, dtype=np.float32)
        pop = np.zeros((8, 8), dtype=np.float32)

        fix_water_consistency(dem, pop, zone, urban, explicit)

        self.assertTrue(np.all(zone == ZONE_WATER))
        self.assertTrue(np.all(dem == 0.0))

    def test_quadtree_does_not_turn_positive_water_into_land(self):
        dem = np.full((32, 32), 455.0, dtype=np.float32)
        zone = np.full((32, 32), ZONE_WATER, dtype=np.uint8)

        raw = build_adaptive_tree(dem, zone, max_nodes=128)
        self.assertTrue(verify_tile(raw, decode_node_qtr6))
        node = navigate_qtr6(raw, 0.5, 0.5)

        self.assertIsNotNone(node)
        self.assertEqual(node.get("zone"), ZONE_WATER)
        self.assertEqual(node.get("elevation"), 0)

    def test_qtr6_preserves_negative_land_elevation(self):
        dem = np.full((32, 32), -28.0, dtype=np.float32)
        zone = np.full((32, 32), ZONE_NATURAL, dtype=np.uint8)

        raw = build_adaptive_tree(dem, zone, max_nodes=128, codec='qtr6')
        node = navigate_qtr6(raw, 0.5, 0.5)

        self.assertIsNotNone(node)
        self.assertEqual(node.get("zone"), ZONE_NATURAL)
        self.assertLess(node.get("elevation"), 0)


if __name__ == "__main__":
    unittest.main()
