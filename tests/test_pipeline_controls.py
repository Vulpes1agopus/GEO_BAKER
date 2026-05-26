import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from geo_baker_pkg import pipeline
from geo_baker_pkg.core import ZONE_FOREST, ZONE_NATURAL, ZONE_WATER


class PipelineControlTests(unittest.TestCase):
    def test_skip_ocean_false_bypasses_land_index_shortcut(self):
        dem = np.ones((4, 4), dtype=np.float32)
        pop = np.zeros((4, 4), dtype=np.float32)

        with mock.patch.object(pipeline, "is_likely_ocean", side_effect=AssertionError("should not call")):
            with mock.patch.object(pipeline, "_concurrent_download", return_value=(dem, pop, None)):
                with mock.patch.object(pipeline, "align_tile_data", return_value=(dem, pop, np.ones((4, 4), dtype=np.uint8), None)):
                    with mock.patch.object(pipeline, "_compute_tile", return_value={"status": "ok", "nodes": 1}) as compute:
                        result = pipeline._bake_tile_core(1, 2, skip_ocean=False)

        self.assertEqual(result["status"], "ok")
        compute.assert_called_once()

    def test_direct_rebake_manifest_respects_start_and_limit(self):
        with tempfile.TemporaryDirectory() as td:
            list_path = Path(td) / "tiles.list"
            manifest = Path(td) / "manifest.jsonl"
            list_path.write_text("1,1,30017\n2 2 30017\n3,3\n", encoding="utf-8")

            with mock.patch.object(pipeline, "bake_tile", return_value={"status": "ok", "nodes": 7}):
                stats = pipeline.rebake_from_lonlat_file(
                    list_path,
                    direct=True,
                    start=1,
                    limit=2,
                    workers=1,
                    manifest_path=manifest,
                )

            rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(stats["ok"], 2)
            self.assertEqual([(r["lon"], r["lat"]) for r in rows], [(2, 2), (3, 3)])

    def test_tile_node_budgets_prioritize_semantic_complexity(self):
        pop_empty = np.zeros((16, 16), dtype=np.float32)
        plain = np.full((16, 16), ZONE_NATURAL, dtype=np.uint8)
        mixed_land = plain.copy()
        mixed_land[:, 8:] = ZONE_FOREST
        coast = plain.copy()
        coast[:, :8] = ZONE_WATER
        busy_pop = np.full((16, 16), 80.0, dtype=np.float32)

        self.assertEqual(pipeline._tile_node_budgets(plain, pop_empty)[0], 10000)
        self.assertEqual(pipeline._tile_node_budgets(mixed_land, pop_empty)[0], 14000)
        self.assertEqual(pipeline._tile_node_budgets(coast, pop_empty)[0], 20000)
        self.assertEqual(pipeline._tile_node_budgets(coast, busy_pop)[0], 24000)


if __name__ == "__main__":
    unittest.main()
