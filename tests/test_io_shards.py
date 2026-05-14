import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from geo_baker_pkg import io
from geo_baker_pkg.core import encode_leaf_node_16, encode_pop_leaf_node


class ShardPackTests(unittest.TestCase):
    def test_pack_shards_writes_compact_manifest_and_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tiles = root / "tiles"
            out = root / "shards"
            tiles.mkdir()
            (tiles / "116_39.qtree").write_bytes(encode_leaf_node_16(50, 1, 0))
            (tiles / "116_39.pop").write_bytes(encode_pop_leaf_node(1000, 1))
            (tiles / "117_39.qtree").write_bytes(encode_leaf_node_16(60, 1, 0))
            (tiles / "117_39.pop").write_bytes(encode_pop_leaf_node(500, 1))

            with mock.patch.object(io, "TILE_DIR", str(tiles)):
                manifest = io.pack_shards(out, shard_degrees=10, include_population=True)

            manifest_path = out / "manifest.json"
            self.assertTrue(manifest_path.exists())
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["format"], "GeoShard")
            self.assertEqual(manifest["shard_degrees"], 10)
            self.assertEqual(len(loaded["shards"]), 2)
            self.assertTrue(all((out / row["path"]).exists() for row in loaded["shards"]))
            self.assertTrue(all(row["index_bytes"] == 800 for row in loaded["shards"]))


if __name__ == "__main__":
    unittest.main()
