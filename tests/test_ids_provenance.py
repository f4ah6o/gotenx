import unittest

from gotenx import ids
from gotenx.provenance import build_graph


class TestIds(unittest.TestCase):
    def test_make_and_parse_roundtrip(self):
        self.assertEqual(ids.make_id("claude", "insight", 1), "claude:insight:001")
        p = ids.parse_id("codex:blindspot:003")
        self.assertEqual((p.source, p.kind, p.seq), ("codex", "blindspot", 3))

    def test_invalid_ids_rejected(self):  # P9
        for bad in ["Bad:id:1", "claude:insight:1", "claude::001", "x", "claude:insight:abc"]:
            self.assertFalse(ids.is_valid(bad), bad)
        with self.assertRaises(ValueError):
            ids.parse_id("nope")

    def test_make_id_validates_tokens(self):
        with self.assertRaises(ValueError):
            ids.make_id("Claude", "insight", 1)


class TestProvenance(unittest.TestCase):
    def setUp(self):
        self.panel = {"insights": [
            {"id": "claude:insight:001", "source": "claude", "kind": "insight", "content": "a"},
            {"id": "codex:blindspot:001", "source": "codex", "kind": "blindspot", "content": "b"},
        ]}

    def test_build_graph_referenced_and_dangling(self):  # P1
        judge = {"items": [
            {"id": "judge:plan:001", "source_ids": ["claude:insight:001"]},
            {"id": "judge:plan:002", "source_ids": ["ghost:x:999"]},
        ]}
        g = build_graph(self.panel, judge)
        self.assertEqual(g.referenced_panel_ids, frozenset({"claude:insight:001"}))
        self.assertEqual(g.dangling_source_ids, frozenset({"ghost:x:999"}))
        self.assertTrue(any("dangling" in w for w in g.warnings))

    def test_duplicate_panel_ids_raise(self):
        panel = {"insights": [
            {"id": "claude:insight:001", "source": "claude", "kind": "insight"},
            {"id": "claude:insight:001", "source": "claude", "kind": "insight"},
        ]}
        with self.assertRaises(ValueError):
            build_graph(panel, {"items": []})


if __name__ == "__main__":
    unittest.main()
