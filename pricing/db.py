"""SQLite storage for uploaded boxes, ruling lines and human decisions."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path, timeout=15.0):
    connection = sqlite3.connect(path, timeout=timeout)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    # WAL + a busy timeout keep two gunicorn workers from tripping over each
    # other's short writes.
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def init_db(path):
    connection = connect(path)
    try:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.commit()
    finally:
        connection.close()


def _rows_to_dicts(rows):
    return [dict(row) for row in rows]


def get_job(connection, job_id):
    row = connection.execute(
        "SELECT * FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    return dict(row) if row else None


def list_jobs(connection):
    rows = connection.execute(
        "SELECT j.*, (SELECT COUNT(*) FROM boxes b WHERE b.job_id = j.id) AS box_count,"
        " (SELECT COUNT(*) FROM lines l WHERE l.job_id = j.id) AS line_count"
        " FROM jobs j ORDER BY j.id DESC"
    ).fetchall()
    return _rows_to_dicts(rows)


def create_job(connection, filename):
    cursor = connection.execute(
        "INSERT INTO jobs (filename) VALUES (?)", (filename,)
    )
    connection.commit()
    return cursor.lastrowid


def delete_job(connection, job_id):
    connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    connection.commit()


def replace_box(connection, job_id, row):
    """Insert one OCR box; UNIQUE(job_id,row_no) makes re-upload idempotent."""
    connection.execute(
        "INSERT OR REPLACE INTO boxes"
        " (job_id, row_no, box_ref, text, x1, y1, x2, y2)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, row["row_no"], row["box_ref"], row["text"],
         row["x1"], row["y1"], row["x2"], row["y2"]),
    )


def add_boxes(connection, job_id, rows):
    for row in rows:
        replace_box(connection, job_id, row)
    connection.commit()


def get_boxes(connection, job_id):
    rows = connection.execute(
        "SELECT * FROM boxes WHERE job_id = ? ORDER BY row_no", (job_id,)
    ).fetchall()
    return _rows_to_dicts(rows)


def set_lines(connection, job_id, horizontal, vertical):
    connection.execute("DELETE FROM lines WHERE job_id = ?", (job_id,))
    for position in horizontal:
        connection.execute(
            "INSERT INTO lines (job_id, orientation, position) VALUES (?, 'h', ?)",
            (job_id, position),
        )
    for position in vertical:
        connection.execute(
            "INSERT INTO lines (job_id, orientation, position) VALUES (?, 'v', ?)",
            (job_id, position),
        )
    connection.commit()


def get_lines(connection, job_id):
    rows = connection.execute(
        "SELECT orientation, position FROM lines WHERE job_id = ?"
        " ORDER BY orientation, position", (job_id,)
    ).fetchall()
    horizontal = [row["position"] for row in rows if row["orientation"] == "h"]
    vertical = [row["position"] for row in rows if row["orientation"] == "v"]
    return horizontal, vertical


def get_decisions(connection, job_id):
    rows = connection.execute(
        "SELECT * FROM decisions WHERE job_id = ?", (job_id,)
    ).fetchall()
    decisions = {}
    for row in rows:
        record = dict(row)
        try:
            record["overlap_cells"] = json.loads(record["overlap_cells"] or "[]")
        except ValueError:
            record["overlap_cells"] = []
        decisions[record["box_id"]] = record
    return decisions


def set_decision(connection, job_id, box_id, kind, target_cell, overlap_cells):
    connection.execute(
        "INSERT INTO decisions"
        " (job_id, box_id, kind, target_cell, overlap_cells)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (job_id, box_id) DO UPDATE SET"
        " kind = excluded.kind, target_cell = excluded.target_cell,"
        " overlap_cells = excluded.overlap_cells,"
        " created_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
        (job_id, box_id, kind, target_cell,
         json.dumps(sorted(overlap_cells), ensure_ascii=False)),
    )
    connection.commit()


def clear_decision(connection, job_id, box_id):
    connection.execute(
        "DELETE FROM decisions WHERE job_id = ? AND box_id = ?", (job_id, box_id)
    )
    connection.commit()


# ---------------------------------------------------------------------------
# Re-OCR'd new version (one per job) and per-group side choices
# ---------------------------------------------------------------------------

def get_variant(connection, job_id):
    row = connection.execute(
        "SELECT * FROM variants WHERE job_id = ?", (job_id,)
    ).fetchone()
    return dict(row) if row else None


def replace_variant(connection, job_id, filename, rows):
    """Swap in a new-version CSV.  Does NOT commit: the caller wraps this in
    one transaction together with the choice pruning, so a failure anywhere
    leaves the previous version untouched."""
    connection.execute("DELETE FROM new_boxes WHERE job_id = ?", (job_id,))
    # new_decisions rows disappear through ON DELETE CASCADE.
    for row in rows:
        connection.execute(
            "INSERT INTO new_boxes"
            " (job_id, row_no, box_ref, text, x1, y1, x2, y2)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, row["row_no"], row["box_ref"], row["text"],
             row["x1"], row["y1"], row["x2"], row["y2"]),
        )
    connection.execute(
        "INSERT INTO variants (job_id, filename, created_at)"
        " VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
        " ON CONFLICT (job_id) DO UPDATE SET"
        " filename = excluded.filename, created_at = excluded.created_at",
        (job_id, filename),
    )


def get_new_boxes(connection, job_id):
    rows = connection.execute(
        "SELECT * FROM new_boxes WHERE job_id = ? ORDER BY row_no", (job_id,)
    ).fetchall()
    return _rows_to_dicts(rows)


def get_new_decisions(connection, job_id):
    rows = connection.execute(
        "SELECT * FROM new_decisions WHERE job_id = ?", (job_id,)
    ).fetchall()
    decisions = {}
    for row in rows:
        record = dict(row)
        try:
            record["overlap_cells"] = json.loads(record["overlap_cells"] or "[]")
        except ValueError:
            record["overlap_cells"] = []
        decisions[record["box_id"]] = record
    return decisions


def set_new_decision(connection, job_id, box_id, kind, target_cell, overlap_cells):
    connection.execute(
        "INSERT INTO new_decisions"
        " (job_id, box_id, kind, target_cell, overlap_cells)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (job_id, box_id) DO UPDATE SET"
        " kind = excluded.kind, target_cell = excluded.target_cell,"
        " overlap_cells = excluded.overlap_cells,"
        " created_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
        (job_id, box_id, kind, target_cell,
         json.dumps(sorted(overlap_cells), ensure_ascii=False)),
    )
    connection.commit()


def clear_new_decision(connection, job_id, box_id):
    connection.execute(
        "DELETE FROM new_decisions WHERE job_id = ? AND box_id = ?", (job_id, box_id)
    )
    connection.commit()


def get_group_choices(connection, job_id):
    rows = connection.execute(
        "SELECT group_key, side FROM group_choices WHERE job_id = ?", (job_id,)
    ).fetchall()
    return {row["group_key"]: row["side"] for row in rows}


def set_group_choice(connection, job_id, group_key, side):
    connection.execute(
        "INSERT INTO group_choices (job_id, group_key, side) VALUES (?, ?, ?)"
        " ON CONFLICT (job_id, group_key) DO UPDATE SET"
        " side = excluded.side,"
        " created_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
        (job_id, group_key, side),
    )
    connection.commit()


def prune_group_choices(connection, job_id, keep_keys):
    """Drop choices whose group no longer exists.  Does NOT commit (see
    replace_variant)."""
    keys = list(keep_keys)
    if keys:
        placeholders = ",".join("?" for _ in keys)
        connection.execute(
            f"DELETE FROM group_choices WHERE job_id = ?"
            f" AND group_key NOT IN ({placeholders})",
            (job_id, *keys),
        )
    else:
        connection.execute(
            "DELETE FROM group_choices WHERE job_id = ?", (job_id,)
        )
