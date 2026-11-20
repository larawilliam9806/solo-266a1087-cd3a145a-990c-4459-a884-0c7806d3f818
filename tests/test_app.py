import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pricing import create_app  # noqa: E402

SAMPLE = """框号,文字,x1,y1,x2,y2
1,白砂糖,10,10,90,40
2,一级,110,10,190,40
3,5.00,210,10,290,40
4,红糖,10,110,90,140
5,1.20,210,110,290,140
6,跨格,90,10,110,40
7,出界,400,400,450,430
"""


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.sqlite3")
        self.app = create_app({
            "TESTING": True,
            "DATABASE": self.db_path,
            "SECRET_KEY": "test",
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, body=SAMPLE, filename="page.csv"):
        return self.client.post(
            "/upload",
            data={"file": (io.BytesIO(body.encode("utf-8")), filename)},
            content_type="multipart/form-data",
            follow_redirects=False,
        )

    def upload_and_get_job(self):
        response = self.upload()
        self.assertEqual(response.status_code, 302)
        return int(response.headers["Location"].rstrip("/").split("/")[-1])

    def set_lines(self, job_id, horizontal, vertical):
        return self.client.post(
            f"/api/jobs/{job_id}/lines",
            json={"horizontal": horizontal, "vertical": vertical},
        )

    def state(self, job_id):
        response = self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def box_id(self, state, ref):
        return next(item["box_id"] for item in state["boxes"] if item["box_ref"] == ref)


class UploadTests(AppTestCase):
    def test_upload_creates_job_and_redirects(self):
        job_id = self.upload_and_get_job()
        state = self.state(job_id)
        self.assertEqual(len(state["boxes"]), 7)
        self.assertIn(state["boxes"][0]["status"],
                      ("assigned_auto", "pending", "stale"))

    def test_upload_rejects_empty_file(self):
        response = self.client.post(
            "/upload",
            data={"file": (io.BytesIO(b""), "empty.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn("文件为空".encode("utf-8"), response.data)

    def test_upload_without_file_redirects_with_error(self):
        response = self.client.post("/upload", data={}, follow_redirects=True)
        self.assertIn("请选择一个 CSV 文件".encode("utf-8"), response.data)

    def test_job_page_renders(self):
        job_id = self.upload_and_get_job()
        response = self.client.get(f"/jobs/{job_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"grid-lines", response.data)


class LineTests(AppTestCase):
    def test_invalid_lines_keep_previous_layout_and_assignments(self):
        job_id = self.upload_and_get_job()
        self.assertEqual(self.set_lines(job_id, [0, 100, 200], [0, 100, 200, 300])
                         .status_code, 200)
        before = self.state(job_id)
        response = self.set_lines(job_id, [0, 100], [0, 100, 200])
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        after = self.state(job_id)
        self.assertEqual(before["horizontal"], after["horizontal"])
        self.assertEqual(before["vertical"], after["vertical"])
        self.assertEqual([(b["box_id"], b["status"]) for b in before["boxes"]],
                         [(b["box_id"], b["status"]) for b in after["boxes"]])

    def test_stale_pending_decisions_after_boundary_move(self):
        job_id = self.upload_and_get_job()
        self.set_lines(job_id, [0, 100, 200], [0, 100, 200, 300])
        state = self.state(job_id)
        box = self.box_id(state, "6")
        decision = self.client.post(
            f"/api/jobs/{job_id}/decisions",
            json={"box_id": box, "kind": "cell", "target_cell": "100|0|200|100"},
        )
        self.assertEqual(decision.status_code, 200)
        self.assertEqual(self.state(job_id)["status_counts"].get("assigned_manual"), 1)

        self.set_lines(job_id, [0, 100, 200], [0, 95, 200, 300])
        state = self.state(job_id)
        item = next(entry for entry in state["boxes"] if entry["box_id"] == box)
        self.assertEqual(item["status"], "stale")

    def test_decision_survives_unrelated_line_change(self):
        job_id = self.upload_and_get_job()
        self.set_lines(job_id, [0, 100, 200], [0, 100, 200, 300])
        box = self.box_id(self.state(job_id), "6")
        self.client.post(f"/api/jobs/{job_id}/decisions",
                         json={"box_id": box, "kind": "cell",
                               "target_cell": "100|0|200|100"})
        # Moving the bottom border y=200 does not touch box 6's cells.
        self.set_lines(job_id, [0, 100, 260], [0, 100, 200, 300])
        item = next(entry for entry in self.state(job_id)["boxes"]
                    if entry["box_id"] == box)
        self.assertEqual(item["status"], "assigned_manual")


class DecisionTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.job = self.upload_and_get_job()
        self.set_lines(self.job, [0, 100, 200], [0, 100, 200, 300])
        self.current = self.state(self.job)

    def test_accepts_overlapping_cell(self):
        box = self.box_id(self.current, "6")
        response = self.client.post(
            f"/api/jobs/{self.job}/decisions",
            json={"box_id": box, "kind": "cell", "target_cell": "0|0|100|100"},
        )
        self.assertEqual(response.status_code, 200)
        item = next(entry for entry in response.get_json()["boxes"]
                    if entry["box_id"] == box)
        self.assertEqual(item["status"], "assigned_manual")
        self.assertEqual(item["column"], "name")

    def test_rejects_non_overlapping_cell(self):
        box = self.box_id(self.current, "6")
        response = self.client.post(
            f"/api/jobs/{self.job}/decisions",
            json={"box_id": box, "kind": "cell", "target_cell": "200|100|300|200"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])

    def test_rejects_decision_for_auto_box(self):
        box = self.box_id(self.current, "1")
        response = self.client.post(
            f"/api/jobs/{self.job}/decisions",
            json={"box_id": box, "kind": "cell", "target_cell": "0|0|100|100"},
        )
        self.assertEqual(response.status_code, 409)

    def test_exclude_box(self):
        box = self.box_id(self.current, "7")
        response = self.client.post(
            f"/api/jobs/{self.job}/decisions",
            json={"box_id": box, "kind": "exclude"},
        )
        self.assertEqual(response.status_code, 200)
        item = next(entry for entry in response.get_json()["boxes"]
                    if entry["box_id"] == box)
        self.assertEqual(item["status"], "excluded")

    def test_unknown_box_404(self):
        response = self.client.post(
            f"/api/jobs/{self.job}/decisions",
            json={"box_id": 99999, "kind": "exclude"},
        )
        self.assertEqual(response.status_code, 404)

    def test_clear_decision_returns_to_pending(self):
        box = self.box_id(self.current, "7")
        self.client.post(f"/api/jobs/{self.job}/decisions",
                         json={"box_id": box, "kind": "exclude"})
        response = self.client.delete(f"/api/jobs/{self.job}/decisions/{box}")
        self.assertEqual(response.status_code, 200)
        item = next(entry for entry in response.get_json()["boxes"]
                    if entry["box_id"] == box)
        self.assertEqual(item["status"], "pending")


class ExportTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.job = self.upload_and_get_job()
        self.set_lines(self.job, [0, 100, 200], [0, 100, 200, 300])

    def resolve_all(self):
        state = self.state(self.job)
        box6 = self.box_id(state, "6")
        box7 = self.box_id(state, "7")
        self.client.post(f"/api/jobs/{self.job}/decisions",
                         json={"box_id": box6, "kind": "cell",
                               "target_cell": "100|0|200|100"})
        self.client.post(f"/api/jobs/{self.job}/decisions",
                         json={"box_id": box7, "kind": "exclude"})

    def test_export_blocked_while_pending(self):
        response = self.client.get(f"/jobs/{self.job}/export.csv")
        self.assertEqual(response.status_code, 409)

    def test_export_blocked_without_valid_lines(self):
        self.set_lines(self.job, [0, 100], [0, 100, 200])
        response = self.client.get(f"/jobs/{self.job}/export.csv")
        self.assertEqual(response.status_code, 409)

    def test_export_succeeds_when_all_resolved(self):
        self.resolve_all()
        response = self.client.get(f"/jobs/{self.job}/export.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.headers["Content-Type"])
        text = response.data.decode("utf-8-sig")
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertEqual(lines[0], "品名,规格,批发价")
        self.assertEqual(lines[1], "白砂糖,跨格一级,5.00")
        self.assertEqual(lines[2], "红糖,,1.20")

    def test_export_filename_is_utf8_encoded(self):
        self.resolve_all()
        response = self.client.get(f"/jobs/{self.job}/export.csv")
        disposition = response.headers["Content-Disposition"]
        self.assertIn("filename*=UTF-8''", disposition)
        self.assertIn("%E4%BB%B7%E7%9B%AE%E8%A1%A8", disposition)

    def test_export_uses_bom(self):
        self.resolve_all()
        response = self.client.get(f"/jobs/{self.job}/export.csv")
        self.assertTrue(response.data.startswith(b"\xef\xbb\xbf"))


class JobManagementTests(AppTestCase):
    def test_delete_job_removes_it(self):
        job_id = self.upload_and_get_job()
        response = self.client.post(f"/jobs/{job_id}/delete", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}").status_code, 404)

    def test_missing_job_returns_404_page(self):
        response = self.client.get("/jobs/99999")
        self.assertEqual(response.status_code, 404)

    def test_suggest_lines_returns_three_columns(self):
        job_id = self.upload_and_get_job()
        response = self.client.post(f"/api/jobs/{job_id}/suggest-lines")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(len(data["vertical"]), 4)
        self.assertGreaterEqual(len(data["horizontal"]), 2)


# A re-OCR'd version of SAMPLE: five boxes identical, box 6 recognised with
# different text, box 7 dropped, one extra box far outside the table.
SAMPLE_V2 = """框号,文字,x1,y1,x2,y2
1,白砂糖,10,10,90,40
2,一级,110,10,190,40
3,5.00,210,10,290,40
4,红糖,10,110,90,140
5,1.20,210,110,290,140
6,跨格准,90,10,110,40
8,新品,500,500,540,520
"""


class VersionTests(AppTestCase):
    """Re-OCR new-version upload, grouping, side choice and export."""

    def setUp(self):
        super().setUp()
        self.job = self.upload_and_get_job()
        self.set_lines(self.job, [0, 100, 200], [0, 100, 200, 300])

    def upload_version(self, body=SAMPLE_V2, filename="page-v2.csv", follow=False):
        return self.client.post(
            f"/jobs/{self.job}/version",
            data={"file": (io.BytesIO(body.encode("utf-8")), filename)},
            content_type="multipart/form-data",
            follow_redirects=follow,
        )

    def choose(self, group_key, side):
        return self.client.post(
            f"/api/jobs/{self.job}/groups/{group_key}", json={"side": side}
        )

    def group_with_refs(self, state, old_refs=(), new_refs=()):
        for group in state["groups"]:
            old = {item["box_ref"] for item in group["old"]}
            new = {item["box_ref"] for item in group["new"]}
            if old == set(old_refs) and new == set(new_refs):
                return group
        raise AssertionError(f"no group with old={old_refs} new={new_refs}"
                             f" in {state['groups']}")

    def item(self, state, side, ref):
        return next(entry for entry in state["boxes"]
                    if entry["side"] == side and entry["box_ref"] == ref)

    def decide(self, side, box_id, payload):
        return self.client.post(
            f"/api/jobs/{self.job}/decisions",
            json=dict(payload, box_id=box_id, side=side),
        )

    def test_upload_variant_groups_boxes(self):
        response = self.upload_version()
        self.assertEqual(response.status_code, 302)
        state = self.state(self.job)
        self.assertEqual(state["variant"]["filename"], "page-v2.csv")
        self.assertEqual(state["identical_groups"], 5)
        self.assertEqual(len(state["groups"]), 3)
        self.assertEqual(state["unchosen_groups"], 3)
        # Diff groups: {old6,new6}, {old7}, {new8}.
        self.group_with_refs(state, old_refs={"6"}, new_refs={"6"})
        self.group_with_refs(state, old_refs={"7"})
        self.group_with_refs(state, new_refs={"8"})
        # Unchosen groups keep their boxes out of placement.
        self.assertEqual(self.item(state, "old", "6")["status"], "inactive")
        self.assertEqual(self.item(state, "old", "7")["status"], "inactive")
        self.assertEqual(self.item(state, "new", "6")["status"], "inactive")
        self.assertEqual(self.item(state, "new", "8")["status"], "inactive")
        # Identical groups keep the original boxes; their new copies are not
        # listed at all.
        self.assertEqual(self.item(state, "old", "1")["status"], "assigned_auto")
        self.assertFalse(any(entry["side"] == "new" and entry["box_ref"] == "1"
                             for entry in state["boxes"]))
        self.assertFalse(state["can_export"])

    def test_upload_without_file_keeps_previous_state(self):
        self.upload_version()
        before = self.state(self.job)
        response = self.client.post(f"/jobs/{self.job}/version", data={},
                                    follow_redirects=True)
        self.assertIn("请选择一个 CSV 文件".encode("utf-8"), response.data)
        after = self.state(self.job)
        self.assertEqual(before["variant"], after["variant"])
        self.assertEqual(before["groups"], after["groups"])

    def test_unparseable_csv_keeps_previous_version(self):
        self.upload_version()
        group = self.group_with_refs(self.state(self.job),
                                     old_refs={"6"}, new_refs={"6"})
        self.choose(group["key"], "old")
        response = self.client.post(
            f"/jobs/{self.job}/version",
            data={"file": (io.BytesIO("框号,文字\n1,只有两列\n".encode("utf-8")),
                            "broken.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn("找不到矩形坐标列".encode("utf-8"), response.data)
        state = self.state(self.job)
        # The previous version and the stored choice survive the failure.
        self.assertEqual(state["variant"]["filename"], "page-v2.csv")
        again = self.group_with_refs(state, old_refs={"6"}, new_refs={"6"})
        self.assertEqual(again["choice"], "old")

    def test_save_error_rolls_back_to_previous_version(self):
        self.upload_version()
        state = self.state(self.job)
        key = self.group_with_refs(state, old_refs={"6"}, new_refs={"6"})["key"]
        self.choose(key, "old")
        before = self.state(self.job)
        with mock.patch("pricing.app.db.replace_variant",
                        side_effect=RuntimeError("disk full")):
            response = self.client.post(
                f"/jobs/{self.job}/version",
                data={"file": (io.BytesIO(SAMPLE_V2.replace("跨格准", "跨格好")
                                           .encode("utf-8")), "page-v3.csv")},
                content_type="multipart/form-data",
                follow_redirects=True,
            )
        self.assertIn("保存新版失败".encode("utf-8"), response.data)
        after = self.state(self.job)
        # Everything is exactly as before the failed upload.
        self.assertEqual(after["variant"]["filename"], "page-v2.csv")
        self.assertEqual(self.group_with_refs(after, old_refs={"6"},
                                              new_refs={"6"})["choice"], "old")
        self.assertEqual([b["box_ref"] for b in after["boxes"]
                          if b["side"] == "new"],
                         [b["box_ref"] for b in before["boxes"]
                          if b["side"] == "new"])

    def test_unchosen_groups_block_export(self):
        self.upload_version()
        response = self.client.get(f"/jobs/{self.job}/export.csv")
        self.assertEqual(response.status_code, 409)
        self.assertIn("未选版".encode("utf-8"), response.data)
        self.assertFalse(self.state(self.job)["can_export"])

    def resolve_chosen_side(self, state, side, ref, target="100|0|200|100"):
        entry = self.item(state, side, ref)
        if entry["status"] in ("pending", "stale"):
            if entry["candidates"]:
                response = self.decide(side, entry["box_id"],
                                       {"kind": "cell", "target_cell": target})
            else:
                response = self.decide(side, entry["box_id"], {"kind": "exclude"})
            self.assertEqual(response.status_code, 200, response.get_json())

    def choose_all_and_resolve(self, main_side):
        state = self.state(self.job)
        self.choose(self.group_with_refs(state, old_refs={"6"}, new_refs={"6"})["key"],
                    main_side)
        self.choose(self.group_with_refs(state, old_refs={"7"})["key"], "new")
        self.choose(self.group_with_refs(state, new_refs={"8"})["key"], "old")
        state = self.state(self.job)
        self.resolve_chosen_side(state, main_side, "6")
        return self.state(self.job)

    def test_choose_old_side_exports_old_text(self):
        self.upload_version()
        state = self.choose_all_and_resolve("old")
        self.assertTrue(state["can_export"])
        self.assertEqual(state["unchosen_groups"], 0)
        # Empty-side choices discarded the singleton groups.
        self.assertEqual(self.item(state, "old", "7")["status"], "inactive")
        self.assertEqual(self.item(state, "new", "8")["status"], "inactive")
        text = self.client.get(f"/jobs/{self.job}/export.csv").data.decode("utf-8-sig")
        self.assertIn("白砂糖,跨格一级,5.00", text)
        self.assertNotIn("跨格准", text)

    def test_choose_new_side_exports_new_text(self):
        self.upload_version()
        state = self.choose_all_and_resolve("new")
        self.assertTrue(state["can_export"])
        self.assertEqual(self.item(state, "old", "6")["status"], "inactive")
        text = self.client.get(f"/jobs/{self.job}/export.csv").data.decode("utf-8-sig")
        self.assertIn("白砂糖,跨格准一级,5.00", text)

    def test_choice_can_be_changed(self):
        self.upload_version()
        state = self.state(self.job)
        key = self.group_with_refs(state, old_refs={"6"}, new_refs={"6"})["key"]
        self.choose(key, "old")
        state = self.state(self.job)
        self.assertEqual(self.item(state, "old", "6")["status"], "pending")
        self.assertEqual(self.item(state, "new", "6")["status"], "inactive")
        self.choose(key, "new")
        state = self.state(self.job)
        self.assertEqual(self.item(state, "old", "6")["status"], "inactive")
        self.assertEqual(self.item(state, "new", "6")["status"], "pending")

    def test_empty_side_choice_discards_group(self):
        self.upload_version()
        state = self.state(self.job)
        key = self.group_with_refs(state, old_refs={"7"})["key"]
        self.choose(key, "new")  # new side is empty → discard old 7
        state = self.state(self.job)
        entry = self.item(state, "old", "7")
        self.assertEqual(entry["status"], "inactive")
        self.assertIn("新版", entry["note"])
        self.assertFalse(any(row["name"] == "出界" for row in state["rows"]))

    def test_decisions_are_kept_per_side(self):
        self.upload_version()
        state = self.state(self.job)
        key = self.group_with_refs(state, old_refs={"6"}, new_refs={"6"})["key"]
        self.choose(key, "new")
        state = self.state(self.job)
        new6 = self.item(state, "new", "6")
        response = self.decide("new", new6["box_id"],
                               {"kind": "cell", "target_cell": "100|0|200|100"})
        self.assertEqual(response.status_code, 200)
        # Switch away, decide the old box too, then switch back.
        self.choose(key, "old")
        state = self.state(self.job)
        old6 = self.item(state, "old", "6")
        self.assertEqual(old6["status"], "pending")
        self.decide("old", old6["box_id"],
                    {"kind": "cell", "target_cell": "0|0|100|100"})
        self.choose(key, "new")
        state = self.state(self.job)
        # The new side's decision is still valid under the unchanged grid.
        self.assertEqual(self.item(state, "new", "6")["status"], "assigned_manual")
        self.assertEqual(self.item(state, "new", "6")["column"], "spec")
        self.choose(key, "old")
        state = self.state(self.job)
        self.assertEqual(self.item(state, "old", "6")["status"], "assigned_manual")
        self.assertEqual(self.item(state, "old", "6")["column"], "name")

    def test_side_decision_revalidated_against_current_grid(self):
        self.upload_version()
        state = self.state(self.job)
        key = self.group_with_refs(state, old_refs={"6"}, new_refs={"6"})["key"]
        self.choose(key, "new")
        new6 = self.item(self.state(self.job), "new", "6")
        self.decide("new", new6["box_id"],
                    {"kind": "cell", "target_cell": "100|0|200|100"})
        # While the group sits on the old side, move a boundary through the
        # new box's cells; switching back must re-confirm the decision.
        self.choose(key, "old")
        self.set_lines(self.job, [0, 100, 200], [0, 95, 200, 300])
        self.choose(key, "new")
        state = self.state(self.job)
        self.assertEqual(self.item(state, "new", "6")["status"], "stale")

    def test_stale_window_decision_rejected(self):
        self.upload_version()
        state = self.state(self.job)
        key = self.group_with_refs(state, old_refs={"6"}, new_refs={"6"})["key"]
        self.choose(key, "new")
        state = self.state(self.job)
        old6 = self.item(state, "old", "6")
        # An old window still showing old 6 as actionable must be refused.
        response = self.decide("old", old6["box_id"], {"kind": "exclude"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("刷新", response.get_json()["error"])
        response = self.client.delete(
            f"/api/jobs/{self.job}/decisions/{old6['box_id']}?side=old")
        self.assertEqual(response.status_code, 409)
        self.assertIn("刷新", response.get_json()["error"])
        # The active side still works.
        new6 = self.item(self.state(self.job), "new", "6")
        response = self.decide("new", new6["box_id"], {"kind": "exclude"})
        self.assertEqual(response.status_code, 200)

    def test_unknown_group_key_rejected(self):
        self.upload_version()
        response = self.choose("no-such-group", "old")
        self.assertEqual(response.status_code, 409)
        self.assertIn("刷新", response.get_json()["error"])

    def test_group_choice_requires_valid_side(self):
        self.upload_version()
        state = self.state(self.job)
        key = self.group_with_refs(state, old_refs={"6"}, new_refs={"6"})["key"]
        response = self.choose(key, "sideways")
        self.assertEqual(response.status_code, 400)

    def test_reupload_replaces_and_prunes_choices(self):
        self.upload_version()
        state = self.state(self.job)
        key = self.group_with_refs(state, old_refs={"6"}, new_refs={"6"})["key"]
        self.choose(key, "old")
        # Re-uploading the identical CSV keeps the group keys, so the choice
        # survives; a changed CSV recomputes groups and stale choices go away.
        self.upload_version(filename="page-v2-again.csv")
        state = self.state(self.job)
        self.assertEqual(state["variant"]["filename"], "page-v2-again.csv")
        self.assertEqual(self.group_with_refs(state, old_refs={"6"},
                                              new_refs={"6"})["choice"], "old")
        changed = SAMPLE_V2.replace("跨格准", "跨格好")
        self.upload_version(body=changed, filename="page-v3.csv")
        state = self.state(self.job)
        self.assertEqual(state["variant"]["filename"], "page-v3.csv")
        group = self.group_with_refs(state, old_refs={"6"}, new_refs={"6"})
        self.assertIsNone(group["choice"])
        self.assertEqual(state["unchosen_groups"], 3)

    def test_new_box_decision_validated_against_grid(self):
        self.upload_version()
        state = self.state(self.job)
        key = self.group_with_refs(state, old_refs={"6"}, new_refs={"6"})["key"]
        self.choose(key, "new")
        state = self.state(self.job)
        new6 = self.item(state, "new", "6")
        response = self.decide("new", new6["box_id"],
                               {"kind": "cell", "target_cell": "200|100|300|200"})
        self.assertEqual(response.status_code, 400)
        response = self.decide("new", new6["box_id"], {"kind": "wiggle"})
        self.assertEqual(response.status_code, 400)

    def test_unknown_side_rejected(self):
        self.upload_version()
        response = self.client.post(
            f"/api/jobs/{self.job}/decisions",
            json={"box_id": 1, "side": "middle", "kind": "exclude"})
        self.assertEqual(response.status_code, 400)

    def test_job_page_lists_variant_and_groups(self):
        self.upload_version()
        response = self.client.get(f"/jobs/{self.job}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("page-v2.csv".encode("utf-8"), response.data)
        self.assertIn("对照组".encode("utf-8"), response.data)


if __name__ == "__main__":
    unittest.main()
