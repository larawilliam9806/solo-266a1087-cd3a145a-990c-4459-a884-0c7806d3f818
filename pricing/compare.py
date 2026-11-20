"""Cross-version grouping of OCR boxes (pure logic, no Flask/SQLite).

When a page is re-OCR'd, the new CSV is uploaded into the same job.  Boxes
from the two versions that share area overlap are linked into groups;
transitively linked boxes land in the same group, and boxes without any
counterpart form singleton groups.  A group whose two sides contain the same
coordinates and text (box numbers and row order may differ) needs no human
choice: the original boxes are kept.  Every other group is a "difference
group" and the user picks one side per group.
"""

from __future__ import annotations

import hashlib

from .layout import EPS, box_bounds, fmt, overlap_size

SIDE_LABELS = {"old": "旧版", "new": "新版"}


def _norm_box(box):
    """The identity of a box for cross-version comparison.

    Coordinates are rounded through fmt() so float noise is ignored; the box
    number (框号) and row order deliberately do not take part.
    """
    x1, y1, x2, y2 = box_bounds(box)
    return (fmt(x1), fmt(y1), fmt(x2), fmt(y2), (box["text"] or "").strip())


def _display_box(box, side):
    x1, y1, x2, y2 = box_bounds(box)
    return {
        "side": side,
        "box_id": box["id"],
        "box_ref": box["box_ref"],
        "text": (box["text"] or "").strip(),
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
    }


def _group_key(old_boxes, new_boxes):
    """Stable key for one group, derived only from its content.

    Recomputing the groups (or re-uploading an identical CSV) yields the same
    keys, so stored per-group choices keep pointing at their group.
    """
    canonical = repr((sorted(_norm_box(b) for b in old_boxes),
                      sorted(_norm_box(b) for b in new_boxes)))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def build_groups(old_boxes, new_boxes):
    """Link the two versions' boxes into overlap groups.

    Returns a deterministically ordered list of groups.  Every box of both
    versions appears in exactly one group.  Each group is a dict:

        key       stable content hash (see _group_key)
        identical True when both sides hold the same coordinates+text
        old       display dicts of the old version's boxes (may be empty)
        new       display dicts of the new version's boxes (may be empty)
    """
    old_boxes = list(old_boxes)
    new_boxes = list(new_boxes)
    count = len(old_boxes) + len(new_boxes)
    parents = list(range(count))

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(a, b):
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parents[max(root_a, root_b)] = min(root_a, root_b)

    old_bounds = [box_bounds(box) for box in old_boxes]
    new_bounds = [box_bounds(box) for box in new_boxes]
    for old_index, bounds_a in enumerate(old_bounds):
        for new_index, bounds_b in enumerate(new_bounds):
            if overlap_size(bounds_a, bounds_b) > EPS:
                union(old_index, len(old_boxes) + new_index)

    members = {}
    for old_index in range(len(old_boxes)):
        members.setdefault(find(old_index), {"old": [], "new": []})["old"].append(old_index)
    for new_index in range(len(new_boxes)):
        members.setdefault(find(len(old_boxes) + new_index),
                           {"old": [], "new": []})["new"].append(new_index)

    groups = []
    for pair in members.values():
        old_side = [old_boxes[index] for index in pair["old"]]
        new_side = [new_boxes[index] for index in pair["new"]]
        identical = (sorted(_norm_box(box) for box in old_side)
                     == sorted(_norm_box(box) for box in new_side))
        old_display = sorted(
            (_display_box(box, "old") for box in old_side),
            key=lambda item: (item["y1"], item["x1"], item["box_ref"], item["box_id"]),
        )
        new_display = sorted(
            (_display_box(box, "new") for box in new_side),
            key=lambda item: (item["y1"], item["x1"], item["box_ref"], item["box_id"]),
        )
        anchor_boxes = old_display + new_display
        groups.append({
            "key": _group_key(old_side, new_side),
            "identical": identical,
            "old": old_display,
            "new": new_display,
            "_anchor": (min(box["y1"] for box in anchor_boxes),
                        min(box["x1"] for box in anchor_boxes)),
        })

    groups.sort(key=lambda group: (group["_anchor"], group["key"]))
    for group in groups:
        del group["_anchor"]
    return groups
