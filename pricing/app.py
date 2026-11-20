"""Flask application: old-newspaper price-list entry.

Upload one page of OCR CSV, draw/confirm the ruling lines that split the page
into 品名 / 规格 / 批发价 columns, resolve every box that does not sit wholly
inside a single cell, then download the three-column CSV.
"""

from __future__ import annotations

import csv
import io
import os
from pathlib import Path
from urllib.parse import quote

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import compare
from . import db
from . import layout
from .csvparse import CsvError, parse_csv

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
        DATABASE=os.environ.get("DATABASE", "pricing.sqlite3"),
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
    )
    if config:
        app.config.update(config)

    database = app.config["DATABASE"]
    if database != ":memory:":
        Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)
    db.init_db(database)

    # ------------------------------------------------------------------
    # Per-request connection
    # ------------------------------------------------------------------
    def get_db():
        if "db" not in g:
            g.db = db.connect(app.config["DATABASE"])
        return g.db

    @app.teardown_appcontext
    def close_db(_exception=None):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    def load_job_or_404(job_id):
        job = db.get_job(get_db(), job_id)
        if job is None:
            abort(404)
        return job

    # ------------------------------------------------------------------
    # State computation shared by pages and API
    # ------------------------------------------------------------------
    def job_state(job_id):
        connection = get_db()
        # Everything below is read inside one snapshot: the version choices,
        # ruling lines and decisions behind any rendered state or exported
        # CSV must come from a single saved state, never from a mix.
        connection.execute("BEGIN")
        try:
            job = load_job_or_404(job_id)
            boxes = db.get_boxes(connection, job_id)
            new_boxes = db.get_new_boxes(connection, job_id)
            horizontal, vertical = db.get_lines(connection, job_id)
            decisions = db.get_decisions(connection, job_id)
            new_decisions = db.get_new_decisions(connection, job_id)
            variant = db.get_variant(connection, job_id)
            stored_choices = db.get_group_choices(connection, job_id)
        finally:
            connection.rollback()  # read-only snapshot, nothing to commit

        invalid = None
        grid = None
        if len(vertical) == 4 and len(horizontal) >= 2:
            grid = layout.Grid(horizontal, vertical)
        else:
            invalid = _invalid_message(len(horizontal), len(vertical))

        # Cross-version grouping.  Identical groups keep the original boxes;
        # every other group needs a stored side choice before its boxes are
        # used.  Choices pointing at vanished groups are ignored here and
        # pruned when a new version is uploaded.
        groups = compare.build_groups(boxes, new_boxes) if variant else []
        diff_groups = [group for group in groups if not group["identical"]]
        identical_count = len(groups) - len(diff_groups)
        choices = {}
        for group in diff_groups:
            side = stored_choices.get(group["key"])
            if side in ("old", "new"):
                choices[group["key"]] = side
        unchosen = len(diff_groups) - len(choices)

        old_group_of = {}
        new_group_of = {}
        for group in groups:
            for item in group["old"]:
                old_group_of[item["box_id"]] = group
            for item in group["new"]:
                new_group_of[item["box_id"]] = group
        group_key_of = {"old": {}, "new": {}}
        for group in diff_groups:
            for item in group["old"]:
                group_key_of["old"][item["box_id"]] = group["key"]
            for item in group["new"]:
                group_key_of["new"][item["box_id"]] = group["key"]

        active_old, active_new, inactive = [], [], []
        if not variant:
            active_old = list(boxes)
        else:
            for box in boxes:
                group = old_group_of[box["id"]]
                if group["identical"] or choices.get(group["key"]) == "old":
                    active_old.append(box)
                else:
                    inactive.append(_inactive_item(box, "old", group, choices))
            for box in new_boxes:
                group = new_group_of[box["id"]]
                if group["identical"]:
                    continue  # identical groups keep the original boxes only
                if choices.get(group["key"]) == "new":
                    active_new.append(box)
                else:
                    inactive.append(_inactive_item(box, "new", group, choices))

        if grid is None:
            evaluated = []
            for side, side_boxes in (("old", active_old), ("new", active_new)):
                for box in side_boxes:
                    evaluated.append({
                        "box_id": box["id"], "box_ref": box["box_ref"],
                        "text": box["text"], "x1": box["x1"], "y1": box["y1"],
                        "x2": box["x2"], "y2": box["y2"],
                        "status": "pending", "status_label": "待划格线",
                        "row": None, "col": None, "column": None,
                        "cell_key": "", "candidates": [], "decision": None,
                        "manual": False, "note": "格线无效，暂不归位",
                        "side": side,
                        "group_key": group_key_of[side].get(box["id"]),
                    })
            pending = sum(1 for item in evaluated if (item["text"] or "").strip())
            rows = []
            if not invalid:
                invalid = "尚未确认格线"
        else:
            evaluated = []
            for side, side_boxes, side_decisions in (
                    ("old", active_old, decisions),
                    ("new", active_new, new_decisions)):
                items = layout.evaluate(grid, side_boxes, side_decisions)
                for item in items:
                    item["side"] = side
                    item["group_key"] = group_key_of[side].get(item["box_id"])
                evaluated.extend(items)
            pending = layout.pending_count(evaluated)
            rows = layout.build_rows(evaluated)

        all_items = evaluated + inactive
        status_counts = {}
        for item in all_items:
            status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1

        return {
            "job": job,
            "boxes": all_items,
            "horizontal": horizontal,
            "vertical": vertical,
            "grid_valid": grid is not None,
            "invalid": invalid,
            "pending": pending,
            "rows": rows,
            "status_counts": status_counts,
            "can_export": (grid is not None and pending == 0
                           and unchosen == 0 and bool(evaluated)),
            "cells": grid.cells() if grid is not None else [],
            "variant": variant,
            "groups": [
                {
                    "key": group["key"],
                    "choice": choices.get(group["key"]),
                    "old": group["old"],
                    "new": group["new"],
                }
                for group in diff_groups
            ],
            "identical_groups": identical_count,
            "unchosen_groups": unchosen,
        }

    def _invalid_message(h_count, v_count):
        parts = []
        if v_count != 4:
            parts.append(f"竖线为 {v_count} 条，需要恰好 4 条")
        if h_count < 2:
            parts.append(f"横线为 {h_count} 条，至少需要 2 条")
        return "格线无效：" + "；".join(parts) if parts else "格线无效"

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------
    @app.get("/")
    def index():
        jobs = db.list_jobs(get_db())
        return render_template("index.html", jobs=jobs)

    @app.get("/healthz")
    def healthz():
        get_db().execute("SELECT 1")
        return jsonify({"ok": True})

    @app.post("/upload")
    def upload():
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            flash("请选择一个 CSV 文件", "error")
            return redirect(url_for("index"))
        filename = os.path.basename(uploaded.filename)
        raw = uploaded.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            flash("文件过大（上限 10MB）", "error")
            return redirect(url_for("index"))
        try:
            parsed = parse_csv(raw)
        except CsvError as error:
            for line in [error.message] + error.details:
                flash(line, "error")
            return redirect(url_for("index"))

        connection = get_db()
        job_id = db.create_job(connection, filename)
        db.add_boxes(connection, job_id, parsed.boxes)
        horizontal, vertical = _suggest_lines(parsed.boxes)
        db.set_lines(connection, job_id, horizontal, vertical)
        for warning in parsed.warnings[:20]:
            flash(warning, "warning")
        flash(f"已导入 {len(parsed.boxes)} 个文字框，请核对格线", "success")
        return redirect(url_for("job_view", job_id=job_id))

    @app.get("/jobs/<int:job_id>")
    def job_view(job_id):
        job = load_job_or_404(job_id)
        state = job_state(job_id)
        return render_template(
            "job.html", job=job, state=state, state_json=_json_state(state)
        )

    @app.post("/jobs/<int:job_id>/delete")
    def job_delete(job_id):
        load_job_or_404(job_id)
        db.delete_job(get_db(), job_id)
        flash("已删除该任务", "success")
        return redirect(url_for("index"))

    @app.post("/jobs/<int:job_id>/version")
    def upload_version(job_id):
        """Attach (or replace) the re-OCR'd new version of this page.

        One new version per job.  The whole swap is a single transaction: an
        unparseable CSV or a save error leaves the previous version, its
        decisions and the group choices exactly as they were.
        """
        load_job_or_404(job_id)
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            flash("请选择一个 CSV 文件", "error")
            return redirect(url_for("job_view", job_id=job_id))
        filename = os.path.basename(uploaded.filename)
        raw = uploaded.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            flash("文件过大（上限 10MB）", "error")
            return redirect(url_for("job_view", job_id=job_id))
        try:
            parsed = parse_csv(raw)
        except CsvError as error:
            for line in [error.message] + error.details:
                flash(line, "error")
            return redirect(url_for("job_view", job_id=job_id))

        connection = get_db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            db.replace_variant(connection, job_id, filename, parsed.boxes)
            groups = compare.build_groups(
                db.get_boxes(connection, job_id),
                db.get_new_boxes(connection, job_id),
            )
            keep = {group["key"] for group in groups if not group["identical"]}
            db.prune_group_choices(connection, job_id, keep)
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            app.logger.exception("保存新版失败")
            flash("保存新版失败，已保留操作前的状态", "error")
            return redirect(url_for("job_view", job_id=job_id))

        for warning in parsed.warnings[:20]:
            flash(warning, "warning")
        identical = sum(1 for group in groups if group["identical"])
        diff = len(groups) - identical
        flash(f"已导入新版 {len(parsed.boxes)} 个文字框：{identical} 组相同已沿用原框，"
              f"{diff} 组有差异待逐组选版", "success")
        return redirect(url_for("job_view", job_id=job_id))

    # ------------------------------------------------------------------
    # JSON API
    # ------------------------------------------------------------------
    @app.get("/api/jobs/<int:job_id>")
    def api_job(job_id):
        return jsonify(_json_state(job_state(job_id)))

    @app.post("/api/jobs/<int:job_id>/lines")
    def api_set_lines(job_id):
        load_job_or_404(job_id)
        payload = request.get_json(silent=True) or {}
        try:
            horizontal, vertical = layout.normalize_layout(
                payload.get("horizontal", []), payload.get("vertical", [])
            )
        except layout.LayoutError as error:
            # Invalid lines: keep the previous layout and its assignments.
            return jsonify({
                "ok": False,
                "error": error.message,
                "details": error.details,
            }), 400
        db.set_lines(get_db(), job_id, horizontal, vertical)
        return jsonify(_json_state(job_state(job_id)))

    @app.post("/api/jobs/<int:job_id>/suggest-lines")
    def api_suggest_lines(job_id):
        load_job_or_404(job_id)
        state = job_state(job_id)
        active = [item for item in state["boxes"] if item["status"] != "inactive"]
        horizontal, vertical = _suggest_lines(active)
        return jsonify({"ok": True, "horizontal": horizontal, "vertical": vertical})

    @app.post("/api/jobs/<int:job_id>/groups/<group_key>")
    def api_set_group_choice(job_id, group_key):
        """Pick one side of a difference group ('old' / 'new').

        Always allowed, even after everything is export-ready: the choice can
        be changed at any time.  Picking a side that has no boxes discards
        the whole group.
        """
        load_job_or_404(job_id)
        payload = request.get_json(silent=True) or {}
        side = payload.get("side")
        if side not in ("old", "new"):
            return jsonify({"ok": False, "error": "未知的选版方向", "details": []}), 400
        state = job_state(job_id)
        group = next((g for g in state["groups"] if g["key"] == group_key), None)
        if group is None:
            return jsonify({
                "ok": False,
                "error": "该对照组已不存在（新版可能已更换），请刷新页面",
                "details": [],
            }), 409
        db.set_group_choice(get_db(), job_id, group_key, side)
        return jsonify(_json_state(job_state(job_id)))

    @app.post("/api/jobs/<int:job_id>/decisions")
    def api_set_decision(job_id):
        load_job_or_404(job_id)
        payload = request.get_json(silent=True) or {}
        side = payload.get("side") or "old"
        if side not in ("old", "new"):
            return jsonify({"ok": False, "error": "未知的版本侧", "details": []}), 400
        connection = get_db()
        boxes = (db.get_new_boxes(connection, job_id) if side == "new"
                 else db.get_boxes(connection, job_id))
        box = next((item for item in boxes if item["id"] == payload.get("box_id")), None)
        if box is None:
            return jsonify({"ok": False, "error": "文字框不存在"}), 404
        state = job_state(job_id)
        if not state["grid_valid"]:
            return jsonify({"ok": False, "error": state["invalid"], "details": []}), 409
        grid = layout.Grid(state["horizontal"], state["vertical"])
        item = next((entry for entry in state["boxes"]
                     if entry["side"] == side and entry["box_id"] == box["id"]), None)
        if item is None or item["status"] == "inactive":
            # A stale window acting on a box whose group has been switched to
            # the other side (or not chosen yet) must not go through.
            return jsonify({
                "ok": False,
                "error": "该文字框当前未采用（所在对照组可能已切到另一侧），请刷新页面",
                "details": [],
            }), 409
        if item["status"] not in ("pending", "stale"):
            return jsonify({
                "ok": False,
                "error": "该文字框已完整落在一格内，无需人工归位",
                "details": [],
            }), 409
        try:
            decision = layout.make_decision(
                grid, item, payload.get("kind"), payload.get("target_cell")
            )
        except layout.LayoutError as error:
            return jsonify({"ok": False, "error": error.message, "details": error.details}), 400
        if side == "new":
            db.set_new_decision(connection, job_id, box["id"], decision["kind"],
                                decision["target_cell"], decision["overlap_cells"])
        else:
            db.set_decision(connection, job_id, box["id"], decision["kind"],
                            decision["target_cell"], decision["overlap_cells"])
        return jsonify(_json_state(job_state(job_id)))

    @app.delete("/api/jobs/<int:job_id>/decisions/<int:box_id>")
    def api_clear_decision(job_id, box_id):
        load_job_or_404(job_id)
        side = request.args.get("side") or "old"
        if side not in ("old", "new"):
            return jsonify({"ok": False, "error": "未知的版本侧", "details": []}), 400
        connection = get_db()
        boxes = (db.get_new_boxes(connection, job_id) if side == "new"
                 else db.get_boxes(connection, job_id))
        if not any(item["id"] == box_id for item in boxes):
            return jsonify({"ok": False, "error": "文字框不存在"}), 404
        state = job_state(job_id)
        item = next((entry for entry in state["boxes"]
                     if entry["side"] == side and entry["box_id"] == box_id), None)
        if item is None or item["status"] == "inactive":
            return jsonify({
                "ok": False,
                "error": "该文字框当前未采用（所在对照组可能已切到另一侧），请刷新页面",
                "details": [],
            }), 409
        if side == "new":
            db.clear_new_decision(connection, job_id, box_id)
        else:
            db.clear_decision(connection, job_id, box_id)
        return jsonify(_json_state(job_state(job_id)))

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    @app.get("/jobs/<int:job_id>/export.csv")
    def export_csv(job_id):
        # job_state reads one consistent snapshot, so this CSV's version
        # choices, ruling lines and decisions all come from the same saved
        # state even if someone keeps switching groups meanwhile.
        state = job_state(job_id)
        if not state["grid_valid"]:
            abort(409, description=state["invalid"])
        if state["unchosen_groups"] > 0:
            abort(409, description=f"还有 {state['unchosen_groups']} 个对照组未选版，不能下载")
        if state["pending"] > 0:
            abort(409, description=f"还有 {state['pending']} 个文字框待核对，不能下载")
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(["品名", "规格", "批发价"])
        for row in state["rows"]:
            writer.writerow([row["name"], row["spec"], row["price"]])
        data = "﻿" + buffer.getvalue()
        name = Path(state["job"]["filename"]).stem or "pricelist"
        return Response(
            data,
            mimetype="text/csv; charset=utf-8",
            headers={
                "Content-Disposition":
                    f"attachment; filename*=UTF-8''{_quote_filename(name + '-价目表.csv')}"
            },
        )

    @app.errorhandler(413)
    def too_large(_error):
        flash("文件过大（上限 10MB）", "error")
        return redirect(url_for("index")), 303

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", code=404, message="找不到该任务"), 404

    @app.errorhandler(409)
    def conflict(error):
        return render_template("error.html", code=409,
                               message=getattr(error, "description", "操作被拒绝")), 409

    return app


def _inactive_item(box, side, group, choices):
    """Display record for a box that is currently not in use.

    Either its difference group has no side choice yet (待选版) or the group
    was switched to the other side (未采用).
    """
    x1, y1, x2, y2 = layout.box_bounds(box)
    choice = choices.get(group["key"])
    if choice is None:
        note = "所在对照组尚未选版，暂不归位"
    else:
        note = "所在对照组已采用" + compare.SIDE_LABELS[choice]
    return {
        "box_id": box["id"],
        "box_ref": box["box_ref"],
        "text": (box["text"] or "").strip(),
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
        "status": "inactive",
        "status_label": layout.STATUS_LABELS["inactive"],
        "row": None,
        "col": None,
        "column": None,
        "cell_key": "",
        "candidates": [],
        "decision": None,
        "manual": False,
        "note": note,
        "side": side,
        "group_key": group["key"],
    }


def _json_state(state):
    job = state["job"]
    variant = state["variant"]
    return {
        "ok": True,
        "job": {"id": job["id"], "filename": job["filename"]},
        "boxes": state["boxes"],
        "horizontal": state["horizontal"],
        "vertical": state["vertical"],
        "grid_valid": state["grid_valid"],
        "invalid": state["invalid"],
        "pending": state["pending"],
        "rows": state["rows"],
        "status_counts": state["status_counts"],
        "can_export": state["can_export"],
        "cells": state["cells"],
        "variant": (
            {"filename": variant["filename"], "created_at": variant["created_at"]}
            if variant else None
        ),
        "groups": state["groups"],
        "identical_groups": state["identical_groups"],
        "unchosen_groups": state["unchosen_groups"],
    }


def _quote_filename(name):
    return quote(name)


def _suggest_lines(boxes):
    """Propose ruling lines from the OCR boxes.

    Vertical: page borders plus two separators chosen so the boxes cluster
    into three columns (splits are only allowed inside empty horizontal gaps).
    Horizontal: page margins plus a separator between adjacent text bands
    that do not overlap vertically.
    """
    if not boxes:
        return [], []
    x1 = min(box["x1"] for box in boxes)
    x2 = max(box["x2"] for box in boxes)
    y1 = min(box["y1"] for box in boxes)
    y2 = max(box["y2"] for box in boxes)

    separators = _column_separators(boxes, x1, x2)
    vertical = sorted({round(x1, 2), round(x2, 2), *separators})
    if len(vertical) != 4 or x2 - x1 <= layout.EPS:
        # Degenerate page (a single point, or no usable x-spread): propose a
        # nominal 1000-unit-wide page instead of duplicate lines.
        left = x1
        step = 1000.0 / 3.0
        vertical = [round(left, 2), round(left + step, 2),
                    round(left + 2 * step, 2), round(left + 1000.0, 2)]

    # Horizontal: group boxes into visual bands first.
    ordered = sorted(boxes, key=lambda box: (box["y1"], box["x1"]))
    bands = []
    for box in ordered:
        placed = False
        for band in bands:
            if layout._same_visual_line(
                {"x1": box["x1"], "y1": box["y1"], "x2": box["x2"], "y2": box["y2"]},
                {"x1": band[0], "y1": band[1], "x2": band[2], "y2": band[3]},
            ):
                band[0] = min(band[0], box["x1"])
                band[1] = min(band[1], box["y1"])
                band[2] = max(band[2], box["x2"])
                band[3] = max(band[3], box["y2"])
                placed = True
                break
        if not placed:
            bands.append([box["x1"], box["y1"], box["x2"], box["y2"]])

    bands.sort(key=lambda band: band[1])
    horizontal = [round(y1, 2), round(y2, 2)]
    for previous, current in zip(bands, bands[1:]):
        if current[1] > previous[3]:
            horizontal.append(round((previous[3] + current[1]) / 2.0, 2))
    # Merge duplicate/near-duplicate lines.
    merged = []
    for value in sorted(horizontal):
        if not merged or abs(value - merged[-1]) > layout.EPS:
            merged.append(value)
    if len(merged) < 2:
        # A single point (or no vertical spread) still needs a page height.
        merged = [round(y1, 2), round(y1 + 1000.0, 2)]
    return merged, vertical


def _column_separators(boxes, low, high):
    """Two x separators that split the boxes into three column clusters.

    Splits are restricted to gaps between the boxes' x-spans, so no box is
    ever cut by a suggested separator.  The pair that minimises within-column
    spread (with a heavy penalty for crossing a box) wins; if the boxes do not
    form three clusters, equal thirds of the page width are used.
    """
    spans = sorted((box["x1"], box["x2"], (box["x1"] + box["x2"]) / 2.0)
                   for box in boxes)
    gaps = [index for index in range(len(spans) - 1)
            if spans[index + 1][0] - spans[index][1] > 0]
    best = None
    for first in gaps:
        for second in gaps:
            if second <= first:
                continue
            groups = [spans[:first + 1], spans[first + 1:second + 1], spans[second + 1:]]
            if any(not group for group in groups):
                continue
            split_a = (spans[first][1] + spans[first + 1][0]) / 2.0
            split_b = (spans[second][1] + spans[second + 1][0]) / 2.0
            score = 0.0
            for group in groups:
                centre = sum(entry[2] for entry in group) / len(group)
                score += sum((entry[2] - centre) ** 2 for entry in group)
                for start, end, _mid in group:
                    if start < split_a < end or start < split_b < end:
                        score += 1e9
            if best is None or score < best[0]:
                best = (score, split_a, split_b)
    if best is None:
        step = (high - low) / 3.0
        return [round(low + step, 2), round(low + 2 * step, 2)]
    separators = sorted({
        round(value, 2) for value in (best[1], best[2])
        if low + layout.EPS < value < high - layout.EPS
    })
    if len(separators) != 2:
        step = (high - low) / 3.0
        return [round(low + step, 2), round(low + 2 * step, 2)]
    return separators

