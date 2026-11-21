"""The ledger: initialization, batch upload, sealing and snapshot reads.

Rules implemented here
----------------------
* The current pending month is the only month that can be sealed.  All
  bookings land in it: rows whose collection month equals it count as
  current-month weight, older collection months count as old-month
  adjustment.  That is how late tickets and corrections to sealed months
  roll into the following report.
* A ticket number has one immutable collection month.  Its revision only
  moves forward.
* Re-sending an older revision, or the current revision with identical
  content, is a no-op.  Re-sending the current revision with changed
  content rejects the whole batch.  A higher revision is a correction:
  the previously booked version is reversed on its old street and the new
  version is booked on its new street, atomically, into the current
  pending month.
* Sealing freezes a snapshot.  Downloads and repeated seals always return
  that stored snapshot; post-seal corrections never touch it.
* Every mutation is one ``BEGIN IMMEDIATE`` transaction, so uploads and
  seals are serialised by commit order and a failed batch leaves both the
  tickets and the bottom tables exactly as they were.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3

from .csvio import parse_upload
from .errors import (
    AlreadyInitializedError,
    BatchRejected,
    ConflictError,
    NotInitializedError,
    NotFoundError,
    ValidationError,
)
from .storage import read_tx, write_tx
from .utils import InputError, next_month, now_iso, parse_month

# ---------------------------------------------------------------- helpers


def _state_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM state WHERE id = 1").fetchone()


def _require_state(conn: sqlite3.Connection) -> sqlite3.Row:
    row = _state_row(conn)
    if row is None:
        raise NotInitializedError("尚未选定首个待封月，请先完成初始化")
    return row


def _takeover_view(conn: sqlite3.Connection, state: sqlite3.Row | None) -> dict:
    """Takeover block shared by every state response."""

    if state is None or not int(state["takeover_mode"]):
        return {
            "mode": False,
            "status": "none",
            "first_pending_month": None,
            "submission": None,
        }
    submission = None
    if state["takeover_id"] is not None:
        row = conn.execute(
            "SELECT * FROM takeovers WHERE id = ?", (state["takeover_id"],)
        ).fetchone()
        if row is not None:
            submission = {
                "id": row["id"],
                "filename": row["filename"],
                "submitted_at": row["submitted_at"],
                "row_count": row["row_count"],
                "register_only_count": row["register_only_count"],
                "register_won_count": row["register_won_count"],
                "new_won_count": row["new_won_count"],
                "digest": row["digest"],
            }
    return {
        "mode": True,
        "status": state["takeover_status"],
        "first_pending_month": state["first_pending_month"],
        "submission": submission,
    }


def _sealed_months(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT month, seq, sealed_at, total_grams FROM snapshots ORDER BY seq"
    ).fetchall()


def _table_rows(conn: sqlite3.Connection, month: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT street,
               COALESCE(SUM(CASE WHEN collection_month = :m THEN grams END), 0)
                   AS current_grams,
               COALESCE(SUM(CASE WHEN collection_month <> :m THEN grams END), 0)
                   AS adjustment_grams
          FROM bookings
         WHERE target_month = :m
         GROUP BY street
         ORDER BY street
        """,
        {"m": month},
    ).fetchall()
    # Every street with any booking in the target month is listed, even if
    # its net is zero: a row silently disappearing would look like a lost
    # ticket to the clerk.  Zero-net streets therefore stay visible.
    return [
        {
            "street": row["street"],
            "current_grams": int(row["current_grams"] or 0),
            "adjustment_grams": int(row["adjustment_grams"] or 0),
            "total_grams": int(row["current_grams"] or 0)
            + int(row["adjustment_grams"] or 0),
        }
        for row in rows
    ]


def _payload(month: str, sealed_at: str | None, rows: list[dict]) -> dict:
    totals = {
        "current_grams": sum(row["current_grams"] for row in rows),
        "adjustment_grams": sum(row["adjustment_grams"] for row in rows),
        "total_grams": sum(row["total_grams"] for row in rows),
    }
    return {
        "schema_version": 1,
        "month": month,
        "sealed_at": sealed_at,
        "rows": rows,
        "street_count": len(rows),
        "totals": totals,
    }


def _load_json(text: str) -> dict:
    return json.loads(text)


def _safe_filename(name: str) -> str:
    base = os.path.basename(name or "").strip() or "upload.csv"
    base = re.sub(r"[\x00-\x1f\x7f]", "", base)
    return base[:200]


def _batch_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "filename": row["filename"],
        "target_month": row["pending_month"],
        "uploaded_at": row["uploaded_at"],
        "row_count": row["row_count"],
        "new_count": row["new_count"],
        "corrected_count": row["corrected_count"],
        "noop_count": row["noop_count"],
        "digest": row["digest"],
    }


# ------------------------------------------------------------ state / init


def state_view(db_path: str) -> dict:
    with read_tx(db_path) as conn:
        state = _state_row(conn)
        if state is None:
            return {
                "initialized": False,
                "first_pending_month": None,
                "current_pending_month": None,
                "sealed_months": [],
                "batches": [],
                "takeover": _takeover_view(conn, state),
            }
        sealed = [
            {
                "month": row["month"],
                "seq": row["seq"],
                "sealed_at": row["sealed_at"],
                "total_grams": row["total_grams"],
            }
            for row in _sealed_months(conn)
        ]
        batches = [
            _batch_dict(row)
            for row in conn.execute(
                "SELECT * FROM batches ORDER BY id DESC LIMIT 20"
            ).fetchall()
        ]
        return {
            "initialized": True,
            "first_pending_month": state["first_pending_month"],
            "current_pending_month": state["current_pending_month"],
            "created_at": state["created_at"],
            "updated_at": state["updated_at"],
            "sealed_months": sealed,
            "batches": batches,
            "takeover": _takeover_view(conn, state),
        }


def initialize(
    db_path: str, month: object, takeover: bool = False
) -> dict:
    try:
        first = parse_month(month)
    except InputError as exc:
        raise ValidationError(str(exc)) from None
    stamp = now_iso()
    takeover_status = "pending" if takeover else "none"
    with write_tx(db_path) as conn:
        if _state_row(conn) is not None:
            raise AlreadyInitializedError("已经选定过首个待封月，不能重复初始化")
        conn.execute(
            """
            INSERT INTO state (id, first_pending_month, current_pending_month,
                               takeover_mode, takeover_status, created_at,
                               updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?)
            """,
            (first, first, 1 if takeover else 0, takeover_status, stamp, stamp),
        )
    return state_view(db_path)


# ------------------------------------------------------------------ upload


def upload_batch(
    db_path: str,
    filename: str,
    data: bytes,
    target_month: object | None = None,
) -> dict:
    # Cheap precondition check so "not initialised" wins over CSV errors.
    with read_tx(db_path) as conn:
        _require_state(conn)

    try:
        rows = parse_upload(data, filename)
        requested_target = parse_month(target_month) if target_month else None
    except InputError as exc:
        raise ValidationError(str(exc)) from None

    digest = hashlib.sha256(data).hexdigest()
    stamp = now_iso()

    with write_tx(db_path) as conn:
        state = _require_state(conn)
        pending = state["current_pending_month"]

        if requested_target is not None:
            if requested_target > pending:
                raise ConflictError(
                    f"批次月份 {requested_target} 尚未开启；当前待封月为 {pending}"
                )
            sealed = {
                row["month"] for row in _sealed_months(conn)
            }
            if requested_target != pending and requested_target not in sealed:
                raise ConflictError(
                    f"批次月份 {requested_target} 既不是当前待封月，也不是已封月"
                )

        seen: dict[str, int] = {}
        duplicates: list[dict] = []
        for row in rows:
            if row["ticket_no"] in seen:
                duplicates.append(
                    {
                        "line": row["line"],
                        "ticket_no": row["ticket_no"],
                        "message": (
                            f"单号 {row['ticket_no']} 在同一批次中重复"
                            f"（首次出现在第 {seen[row['ticket_no']]} 行）"
                        ),
                    }
                )
            else:
                seen[row["ticket_no"]] = row["line"]
        if duplicates:
            raise BatchRejected("批次内单号重复，整批退回", duplicates)

        existing = {}
        ticket_numbers = list(seen)
        # SQLite's default variable limit is high enough for 50k rows on
        # modern builds, but chunk to stay safely under older limits.
        for start in range(0, len(ticket_numbers), 500):
            chunk = ticket_numbers[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            for record in conn.execute(
                f"SELECT * FROM tickets WHERE ticket_no IN ({placeholders})",
                chunk,
            ):
                existing[record["ticket_no"]] = record

        errors: list[dict] = []
        new_count = 0
        corrected_count = 0
        noop_count = 0

        # The batch row exists from the start so every booking can point at
        # it directly; counts are filled in before commit.
        batch_cursor = conn.execute(
            """
            INSERT INTO batches (filename, pending_month, uploaded_at,
                                 row_count, new_count, corrected_count,
                                 noop_count, digest)
            VALUES (?, ?, ?, ?, 0, 0, 0, ?)
            """,
            (_safe_filename(filename), pending, stamp, len(rows), digest),
        )
        batch_id = int(batch_cursor.lastrowid)

        for row in rows:
            collection_month = row["collection_month"]
            if collection_month > pending:
                errors.append(
                    {
                        "line": row["line"],
                        "ticket_no": row["ticket_no"],
                        "message": (
                            f"清运月 {collection_month} 晚于当前待封月 {pending}，"
                            "不能提前入账"
                        ),
                    }
                )
                continue

            record = existing.get(row["ticket_no"])
            if record is None:
                conn.execute(
                    """
                    INSERT INTO tickets (ticket_no, collection_month,
                                         current_revision, street,
                                         weight_grams, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["ticket_no"],
                        collection_month,
                        row["revision"],
                        row["street"],
                        row["grams"],
                        stamp,
                        stamp,
                    ),
                )
                _insert_booking(
                    conn,
                    batch_id=batch_id,
                    row=row,
                    target_month=pending,
                    street=row["street"],
                    grams=row["grams"],
                    kind="book",
                    stamp=stamp,
                )
                new_count += 1
                continue

            if record["collection_month"] != collection_month:
                errors.append(
                    {
                        "line": row["line"],
                        "ticket_no": row["ticket_no"],
                        "message": (
                            f"单号 {row['ticket_no']} 的清运月固定为 "
                            f"{record['collection_month']}，不能改为 {collection_month}"
                        ),
                    }
                )
                continue

            current_revision = int(record["current_revision"])
            if row["revision"] < current_revision:
                # An older version is superseded; keep the current values.
                noop_count += 1
                continue
            if row["revision"] == current_revision:
                if (
                    row["street"] == record["street"]
                    and row["grams"] == int(record["weight_grams"])
                ):
                    noop_count += 1
                else:
                    errors.append(
                        {
                            "line": row["line"],
                            "ticket_no": row["ticket_no"],
                            "message": (
                                f"单号 {row['ticket_no']} 提交的修订号 "
                                f"{current_revision} 已是当前版本，但街道或净重与已入账"
                                "内容不一致，整批退回"
                            ),
                        }
                    )
                continue

            # A genuinely newer revision.
            if (
                row["street"] == record["street"]
                and row["grams"] == int(record["weight_grams"])
            ):
                conn.execute(
                    "UPDATE tickets SET current_revision = ?, updated_at = ? "
                    "WHERE ticket_no = ?",
                    (row["revision"], stamp, row["ticket_no"]),
                )
                noop_count += 1
                continue

            _insert_booking(
                conn,
                batch_id=batch_id,
                row=row,
                target_month=pending,
                street=record["street"],
                grams=-int(record["weight_grams"]),
                kind="reverse",
                stamp=stamp,
                revision=current_revision,
            )
            _insert_booking(
                conn,
                batch_id=batch_id,
                row=row,
                target_month=pending,
                street=row["street"],
                grams=row["grams"],
                kind="book",
                stamp=stamp,
            )
            conn.execute(
                """
                UPDATE tickets
                   SET current_revision = ?, street = ?, weight_grams = ?,
                       updated_at = ?
                 WHERE ticket_no = ?
                """,
                (
                    row["revision"],
                    row["street"],
                    row["grams"],
                    stamp,
                    row["ticket_no"],
                ),
            )
            corrected_count += 1

        if errors:
            # Nothing has been committed: the transaction is rolled back by
            # write_tx, preserving every ticket and every bottom table.
            raise BatchRejected("批次校验未通过，整批退回，未写入任何数据", errors)

        conn.execute(
            """
            UPDATE batches
               SET new_count = ?, corrected_count = ?, noop_count = ?
             WHERE id = ?
            """,
            (new_count, corrected_count, noop_count, batch_id),
        )

        batch = _batch_dict(
            conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        )
        payload = _payload(pending, None, _table_rows(conn, pending))

    return {
        "batch": batch,
        "pending_month": pending,
        "target_month": batch["target_month"],
        "summary": {
            "rows": len(rows),
            "new": new_count,
            "corrected": corrected_count,
            "noop": noop_count,
        },
        "table": payload,
    }


def _insert_booking(
    conn: sqlite3.Connection,
    *,
    batch_id: int | None,
    row: dict,
    target_month: str,
    street: str,
    grams: int,
    kind: str,
    stamp: str,
    revision: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO bookings (batch_id, ticket_no, revision, target_month,
                              collection_month, street, grams, kind, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            row["ticket_no"],
            row["revision"] if revision is None else revision,
            target_month,
            row["collection_month"],
            street,
            grams,
            kind,
            stamp,
        ),
    )


def _insert_takeover_booking(
    conn: sqlite3.Connection,
    *,
    takeover_id: int,
    ticket_no: str,
    revision: int,
    target_month: str,
    collection_month: str,
    street: str,
    grams: int,
    kind: str,
    stamp: str,
) -> None:
    conn.execute(
        """
        INSERT INTO bookings (batch_id, takeover_id, ticket_no, revision,
                              target_month, collection_month, street, grams,
                              kind, created_at)
        VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            takeover_id,
            ticket_no,
            revision,
            target_month,
            collection_month,
            street,
            grams,
            kind,
            stamp,
        ),
    )


# --------------------------------------------------------------- takeover


def submit_takeover(db_path: str, filename: str, data: bytes) -> dict:
    """Apply one old-warehouse settled-ticket register (the "接账清册").

    The register reuses the ordinary five-column CSV.  Every ticket number
    appears once and its collection month must be strictly earlier than the
    first pending month.  For ticket numbers already collected in the new
    warehouse, two conflicts reject the whole register: differing collection
    months, or identical revision numbers carrying different content.

    On success, all inside one serialised transaction:

    * provisional bookings the new warehouse made before the takeover for any
      shared ticket number are reversed (ordinary batches themselves stay);
    * register-only tickets become effective tickets; their effective net is
      booked (``takeover_book``) and their settled net deducted
      (``takeover_deduct``) on the register's street, so the two cancel;
    * shared tickets keep the higher revision; the effective version is booked
      on its street (``takeover_book``) and the register's settled net deducted
      on the register's street (``takeover_deduct``);
    * takeover state, the effective ticket set and the first-month bottom
      table take effect together.

    Any failure rolls everything back, so a rejected register leaves the
    ledger exactly as it was and the clerk can simply resubmit.
    """

    # Cheap precondition check so "not initialised" wins over CSV errors.
    with read_tx(db_path) as conn:
        state = _require_state(conn)
        if not int(state["takeover_mode"]):
            raise ConflictError("本账套建账时未选择从旧仓接账，不能提交接账清册")
        if state["takeover_status"] == "done":
            raise ConflictError("旧仓接账已完成，期初只接一次，不能再次提交清册")
        first_month = state["first_pending_month"]

    try:
        rows = parse_upload(data, filename)
    except InputError as exc:
        raise ValidationError(str(exc)) from None

    digest = hashlib.sha256(data).hexdigest()
    stamp = now_iso()

    with write_tx(db_path) as conn:
        state = _require_state(conn)
        if not int(state["takeover_mode"]):
            raise ConflictError("本账套建账时未选择从旧仓接账，不能提交接账清册")
        if state["takeover_status"] == "done":
            raise ConflictError("旧仓接账已完成，期初只接一次，不能再次提交清册")
        first_month = state["first_pending_month"]

        # One row per ticket number inside the register itself.
        seen: dict[str, int] = {}
        duplicates: list[dict] = []
        for row in rows:
            if row["ticket_no"] in seen:
                duplicates.append(
                    {
                        "line": row["line"],
                        "ticket_no": row["ticket_no"],
                        "message": (
                            f"单号 {row['ticket_no']} 在清册中重复"
                            f"（首次出现在第 {seen[row['ticket_no']]} 行），"
                            "每个单号只能填一行"
                        ),
                    }
                )
            else:
                seen[row["ticket_no"]] = row["line"]
        if duplicates:
            raise BatchRejected("接账清册内单号重复，整份退回", duplicates)

        errors: list[dict] = []
        for row in rows:
            if not row["collection_month"] < first_month:
                errors.append(
                    {
                        "line": row["line"],
                        "ticket_no": row["ticket_no"],
                        "message": (
                            f"单号 {row['ticket_no']} 的清运月 "
                            f"{row['collection_month']} 必须早于首个待封月 "
                            f"{first_month}"
                        ),
                    }
                )
        if errors:
            raise BatchRejected(
                "接账清册存在清运月不早于首个待封月的单据，整份退回", errors
            )

        existing: dict[str, sqlite3.Row] = {}
        ticket_numbers = list(seen)
        for start in range(0, len(ticket_numbers), 500):
            chunk = ticket_numbers[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            for record in conn.execute(
                f"SELECT * FROM tickets WHERE ticket_no IN ({placeholders})",
                chunk,
            ):
                existing[record["ticket_no"]] = record

        # Cross-warehouse conflicts: collection month mismatch, or the same
        # revision number carrying different content.  Every conflicting
        # ticket number is reported before the whole register is rejected.
        conflicts: list[dict] = []
        for row in rows:
            record = existing.get(row["ticket_no"])
            if record is None:
                continue
            if record["collection_month"] != row["collection_month"]:
                conflicts.append(
                    {
                        "line": row["line"],
                        "ticket_no": row["ticket_no"],
                        "message": (
                            f"单号 {row['ticket_no']} 两仓清运月不符："
                            f"新仓 {record['collection_month']}，"
                            f"旧仓清册 {row['collection_month']}"
                        ),
                    }
                )
                continue
            if row["revision"] == int(record["current_revision"]) and (
                row["street"] != record["street"]
                or row["grams"] != int(record["weight_grams"])
            ):
                conflicts.append(
                    {
                        "line": row["line"],
                        "ticket_no": row["ticket_no"],
                        "message": (
                            f"单号 {row['ticket_no']} 的修订号 "
                            f"{row['revision']} 在两仓内容不符"
                            "（街道或净重不一致）"
                        ),
                    }
                )
        if conflicts:
            raise BatchRejected(
                "接账清册与新仓已收单据存在冲突，整份退回，未写入任何数据",
                conflicts,
            )

        cursor = conn.execute(
            """
            INSERT INTO takeovers (filename, submitted_at, row_count,
                                   register_only_count, register_won_count,
                                   new_won_count, digest)
            VALUES (?, ?, ?, 0, 0, 0, ?)
            """,
            (_safe_filename(filename), stamp, len(rows), digest),
        )
        takeover_id = int(cursor.lastrowid)

        register_only_count = 0
        register_won_count = 0
        new_won_count = 0

        for row in rows:
            record = existing.get(row["ticket_no"])

            if record is None:
                # Register-only ticket: it becomes the effective ticket.
                conn.execute(
                    """
                    INSERT INTO tickets (ticket_no, collection_month,
                                         current_revision, street,
                                         weight_grams, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["ticket_no"],
                        row["collection_month"],
                        row["revision"],
                        row["street"],
                        row["grams"],
                        stamp,
                        stamp,
                    ),
                )
                # First-month old-month adjustment: count the effective
                # version's net on its street, then deduct the register's
                # settled net on that same street.  The two entries cancel, so
                # the old-warehouse settled ticket is never counted again.
                _insert_takeover_booking(
                    conn,
                    takeover_id=takeover_id,
                    ticket_no=row["ticket_no"],
                    revision=row["revision"],
                    target_month=first_month,
                    collection_month=row["collection_month"],
                    street=row["street"],
                    grams=row["grams"],
                    kind="takeover_book",
                    stamp=stamp,
                )
                _insert_takeover_booking(
                    conn,
                    takeover_id=takeover_id,
                    ticket_no=row["ticket_no"],
                    revision=row["revision"],
                    target_month=first_month,
                    collection_month=row["collection_month"],
                    street=row["street"],
                    grams=-row["grams"],
                    kind="takeover_deduct",
                    stamp=stamp,
                )
                conn.execute(
                    """
                    INSERT INTO takeover_tickets
                        (ticket_no, takeover_id, collection_month,
                         register_revision, register_street, register_grams,
                         effective_revision, effective_source, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'register', ?)
                    """,
                    (
                        row["ticket_no"],
                        takeover_id,
                        row["collection_month"],
                        row["revision"],
                        row["street"],
                        row["grams"],
                        row["revision"],
                        stamp,
                    ),
                )
                register_only_count += 1
                continue

            # Shared ticket number.  First undo every provisional booking the
            # new warehouse had made for it (all land in the first month
            # before any seal); the booked batch records themselves remain.
            provisional = conn.execute(
                "SELECT * FROM bookings WHERE ticket_no = ? ORDER BY id",
                (row["ticket_no"],),
            ).fetchall()
            for booking in provisional:
                _insert_takeover_booking(
                    conn,
                    takeover_id=takeover_id,
                    ticket_no=booking["ticket_no"],
                    revision=int(booking["revision"]),
                    target_month=first_month,
                    collection_month=booking["collection_month"],
                    street=booking["street"],
                    grams=-int(booking["grams"]),
                    kind="reverse",
                    stamp=stamp,
                )

            if row["revision"] >= int(record["current_revision"]):
                # The register carries the higher (or equal) revision: it is
                # the effective version.  Equal revisions already proved
                # identical during conflict checking.
                if row["revision"] > int(record["current_revision"]):
                    conn.execute(
                        """
                        UPDATE tickets
                           SET current_revision = ?, street = ?, weight_grams = ?,
                               updated_at = ?
                         WHERE ticket_no = ?
                        """,
                        (
                            row["revision"],
                            row["street"],
                            row["grams"],
                            stamp,
                            row["ticket_no"],
                        ),
                    )
                effective_revision = row["revision"]
                effective_source = "register"
                effective_street = row["street"]
                effective_grams = row["grams"]
                register_won_count += 1
            else:
                # The new warehouse already held a newer revision: it stays
                # effective and later corrections start from it.
                effective_revision = int(record["current_revision"])
                effective_source = "new"
                effective_street = record["street"]
                effective_grams = int(record["weight_grams"])
                new_won_count += 1

            # First-month old-month adjustment: count the effective version's
            # net on its street, then deduct the register's settled net on the
            # register's street.  Tickets absent from the register keep their
            # ordinary provisional booking untouched.
            _insert_takeover_booking(
                conn,
                takeover_id=takeover_id,
                ticket_no=row["ticket_no"],
                revision=effective_revision,
                target_month=first_month,
                collection_month=row["collection_month"],
                street=effective_street,
                grams=effective_grams,
                kind="takeover_book",
                stamp=stamp,
            )
            _insert_takeover_booking(
                conn,
                takeover_id=takeover_id,
                ticket_no=row["ticket_no"],
                revision=row["revision"],
                target_month=first_month,
                collection_month=row["collection_month"],
                street=row["street"],
                grams=-row["grams"],
                kind="takeover_deduct",
                stamp=stamp,
            )
            conn.execute(
                """
                INSERT INTO takeover_tickets
                    (ticket_no, takeover_id, collection_month,
                     register_revision, register_street, register_grams,
                     effective_revision, effective_source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["ticket_no"],
                    takeover_id,
                    row["collection_month"],
                    row["revision"],
                    row["street"],
                    row["grams"],
                    effective_revision,
                    effective_source,
                    stamp,
                ),
            )

        conn.execute(
            """
            UPDATE takeovers
               SET register_only_count = ?, register_won_count = ?,
                   new_won_count = ?
             WHERE id = ?
            """,
            (
                register_only_count,
                register_won_count,
                new_won_count,
                takeover_id,
            ),
        )
        conn.execute(
            "UPDATE state SET takeover_status = 'done', takeover_id = ?, "
            "updated_at = ? WHERE id = 1",
            (takeover_id, stamp),
        )

        submission = _takeover_view(
            conn, _state_row(conn)
        )["submission"]
        payload = _payload(first_month, None, _table_rows(conn, first_month))

    return {
        "takeover": submission,
        "first_pending_month": first_month,
        "summary": {
            "rows": len(rows),
            "register_only": register_only_count,
            "register_won": register_won_count,
            "new_won": new_won_count,
        },
        "table": payload,
    }


# -------------------------------------------------------------------- seal


def seal_month(db_path: str, month: object) -> tuple[dict, bool]:
    try:
        target = parse_month(month)
    except InputError as exc:
        raise ValidationError(str(exc)) from None

    stamp = now_iso()
    with write_tx(db_path) as conn:
        state = _require_state(conn)
        pending = state["current_pending_month"]

        stored = conn.execute(
            "SELECT seq, sealed_at, payload_json FROM snapshots WHERE month = ?",
            (target,),
        ).fetchone()
        if stored is not None:
            # Re-sealing a sealed month returns the original table and does
            # not move the calendar again.
            return _load_json(stored["payload_json"]), False

        if target > pending:
            raise ConflictError(
                f"不能封存 {target}：当前待封月为 {pending}，只能封存当前待封月"
            )
        if target != pending:
            raise ConflictError(f"{target} 未处于待封状态，无法封存")

        if (
            target == state["first_pending_month"]
            and int(state["takeover_mode"])
            and state["takeover_status"] != "done"
        ):
            raise ConflictError(
                f"首月 {target} 必须等旧仓接账成功后才能封存；"
                "接账清册尚未通过，请先提交或重交清册"
            )

        payload = _payload(target, stamp, _table_rows(conn, pending))
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM snapshots"
        ).fetchone()["next"]
        conn.execute(
            """
            INSERT INTO snapshots (month, seq, sealed_at, total_current_grams,
                                   total_adjustment_grams, total_grams,
                                   payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target,
                seq,
                stamp,
                payload["totals"]["current_grams"],
                payload["totals"]["adjustment_grams"],
                payload["totals"]["total_grams"],
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.execute(
            "UPDATE state SET current_pending_month = ?, updated_at = ? WHERE id = 1",
            (next_month(target), stamp),
        )
        return payload, True


# ------------------------------------------------------------------- reads


def get_snapshot(db_path: str, month: object) -> dict | None:
    try:
        target = parse_month(month)
    except InputError as exc:
        raise ValidationError(str(exc)) from None
    with read_tx(db_path) as conn:
        row = conn.execute(
            "SELECT payload_json FROM snapshots WHERE month = ?", (target,)
        ).fetchone()
        return _load_json(row["payload_json"]) if row else None


def month_view(db_path: str, month: object) -> dict:
    try:
        target = parse_month(month)
    except InputError as exc:
        raise ValidationError(str(exc)) from None
    with read_tx(db_path) as conn:
        state = _require_state(conn)
        row = conn.execute(
            "SELECT payload_json FROM snapshots WHERE month = ?", (target,)
        ).fetchone()
        if row is not None:
            payload = _load_json(row["payload_json"])
            return {"status": "sealed", "month": target, "table": payload}
        if target == state["current_pending_month"]:
            payload = _payload(target, None, _table_rows(conn, target))
            return {"status": "pending", "month": target, "table": payload}
        if target > state["current_pending_month"]:
            raise NotFoundError(f"{target} 尚未开启")
        raise NotFoundError(f"{target} 没有封存记录")


def snapshot_csv(db_path: str, month: object):
    from .csvio import snapshot_csv as render

    payload = get_snapshot(db_path, month)
    if payload is None:
        try:
            target = parse_month(month)
        except InputError as exc:
            raise ValidationError(str(exc)) from None
        raise ConflictError(f"{target} 尚未封存，暂无原表可下载")
    return render(payload)


def recent_batches(db_path: str, limit: int = 20) -> list[dict]:
    with read_tx(db_path) as conn:
        return [
            _batch_dict(row)
            for row in conn.execute(
                "SELECT * FROM batches ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        ]
