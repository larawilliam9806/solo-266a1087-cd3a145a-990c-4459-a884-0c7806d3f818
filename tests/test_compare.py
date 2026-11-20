import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pricing import compare  # noqa: E402


def box(box_id, ref, text, x1, y1, x2, y2):
    return dict(id=box_id, box_ref=ref, text=text,
                x1=x1, y1=y1, x2=x2, y2=y2)


def group_of(groups, side, box_id):
    return next(group for group in groups
                if any(item["box_id"] == box_id for item in group[side]))


class GroupingTests(unittest.TestCase):
    def test_no_overlap_makes_singleton_groups(self):
        old = [box(1, "1", "甲", 0, 0, 10, 10)]
        new = [box(9, "9", "乙", 100, 100, 110, 110)]
        groups = compare.build_groups(old, new)
        self.assertEqual(len(groups), 2)
        for group in groups:
            self.assertEqual(len(group["old"]) + len(group["new"]), 1)
            self.assertFalse(group["identical"])

    def test_overlapping_pair_forms_one_group(self):
        old = [box(1, "1", "甲", 0, 0, 10, 10)]
        new = [box(9, "9", "甲", 5, 5, 15, 15)]
        groups = compare.build_groups(old, new)
        self.assertEqual(len(groups), 1)
        self.assertEqual([item["box_id"] for item in groups[0]["old"]], [1])
        self.assertEqual([item["box_id"] for item in groups[0]["new"]], [9])

    def test_indirect_links_share_one_group(self):
        # old 1 overlaps new 9; new 9 overlaps old 2; old 1 and old 2 never
        # touch, yet all four boxes belong to one group.
        old = [box(1, "1", "甲", 0, 0, 10, 10),
               box(2, "2", "乙", 20, 0, 30, 10)]
        new = [box(9, "9", "新", 5, 0, 25, 10),
               box(8, "8", "远", 500, 500, 510, 510)]
        groups = compare.build_groups(old, new)
        self.assertEqual(len(groups), 2)
        merged = group_of(groups, "new", 9)
        self.assertEqual(sorted(item["box_id"] for item in merged["old"]), [1, 2])
        lonely = group_of(groups, "new", 8)
        self.assertEqual(lonely["old"], [])

    def test_one_old_box_can_bridge_two_new_boxes(self):
        old = [box(1, "1", "长框", 0, 0, 100, 10)]
        new = [box(9, "9", "左", 0, 0, 40, 10),
               box(10, "10", "右", 60, 0, 100, 10)]
        groups = compare.build_groups(old, new)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["new"]), 2)

    def test_edge_touch_is_not_overlap(self):
        old = [box(1, "1", "甲", 0, 0, 10, 10)]
        new = [box(9, "9", "乙", 10, 0, 20, 10)]
        groups = compare.build_groups(old, new)
        self.assertEqual(len(groups), 2)

    def test_identical_ignores_box_ref_and_row_order(self):
        old = [box(1, "1", "白砂糖", 10, 10, 90, 40),
               box(2, "2", "一级", 110, 10, 190, 40)]
        # Same content, different ids/refs and reversed row order.
        new = [box(7, "乙", "一级", 110, 10, 190, 40),
               box(8, "甲", "白砂糖", 10, 10, 90, 40)]
        groups = compare.build_groups(old, new)
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(group["identical"] for group in groups))

    def test_text_difference_breaks_identity(self):
        old = [box(1, "1", "统货", 0, 0, 10, 10)]
        new = [box(9, "9", "上等", 0, 0, 10, 10)]
        groups = compare.build_groups(old, new)
        self.assertEqual(len(groups), 1)
        self.assertFalse(groups[0]["identical"])

    def test_coordinate_difference_breaks_identity(self):
        old = [box(1, "1", "甲", 0, 0, 10, 10)]
        new = [box(9, "9", "甲", 0, 0, 12, 10)]
        groups = compare.build_groups(old, new)
        self.assertEqual(len(groups), 1)
        self.assertFalse(groups[0]["identical"])

    def test_group_key_is_stable_and_content_based(self):
        old = [box(1, "1", "甲", 0, 0, 10, 10)]
        new = [box(9, "9", "乙", 0, 0, 10, 10)]
        first = compare.build_groups(old, new)[0]["key"]
        # Different ids / refs, same content: the key must not change.
        again = compare.build_groups(
            [box(41, "x", "甲", 0, 0, 10, 10)],
            [box(42, "y", "乙", 0, 0, 10, 10)],
        )[0]["key"]
        self.assertEqual(first, again)
        other = compare.build_groups(
            [box(1, "1", "甲", 0, 0, 10, 10)],
            [box(9, "9", "丙", 0, 0, 10, 10)],
        )[0]["key"]
        self.assertNotEqual(first, other)

    def test_groups_are_sorted_top_to_bottom(self):
        old = [box(1, "1", "下", 0, 100, 10, 110),
               box(2, "2", "上", 0, 0, 10, 10)]
        groups = compare.build_groups(old, [])
        self.assertEqual([item["box_id"] for item in groups[0]["old"]], [2])
        self.assertEqual([item["box_id"] for item in groups[1]["old"]], [1])

    def test_empty_new_version_groups_everything_as_old_only(self):
        old = [box(1, "1", "甲", 0, 0, 10, 10)]
        groups = compare.build_groups(old, [])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["new"], [])
        self.assertFalse(groups[0]["identical"])


if __name__ == "__main__":
    unittest.main()
