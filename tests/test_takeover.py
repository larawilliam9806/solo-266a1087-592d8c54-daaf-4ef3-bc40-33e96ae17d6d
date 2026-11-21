"""Rule-by-rule tests for the old-warehouse takeover ("旧仓接账").

The feature under test lets a freshly created ledger inherit the settled
weighbridge tickets of the replaced warehouse without ever counting their
settled net twice.  These tests cover, in order of the business rules:

* setup-time opt-in and the first-month sealing gate;
* normal batches still booking while a takeover is pending;
* the register format: one row per ticket, collection month before the first
  pending month, whole-file rejection otherwise;
* cross-warehouse conflicts (collection month / same-revision content) with
  every conflicting ticket number reported;
* higher revision wins, including across the provisional new-warehouse data;
* first-month old-month adjustment: effective version booked, register's
  settled net deducted; register-only tickets net to zero;
* later corrections starting from the post-takeover effective version;
* one-time, atomic takeover with resubmission after failure;
* the HTTP surface and the pre-feature schema migration.
"""

import sqlite3

import pytest

from warehouse import service
from warehouse.errors import BatchRejected, ConflictError

HEADER = "单号,清运月,街道,净重（千克）,修订号"


def csv_bytes(*rows, header=HEADER):
    lines = [header]
    for row in rows:
        lines.append(",".join(str(part) for part in row))
    return ("\n".join(lines) + "\n").encode("utf-8")


def upload(db_path, rows, target=None):
    return service.upload_batch(
        db_path, "batch.csv", csv_bytes(*rows), target_month=target
    )


def takeover(db_path, *rows):
    return service.submit_takeover(
        db_path, "takeover.csv", csv_bytes(*rows)
    )


def init_with_takeover(db_path, month="2026-10"):
    service.initialize(db_path, month, takeover=True)


def table_by_street(db_path, month):
    view = service.month_view(db_path, month)
    return {row["street"]: row for row in view["table"]["rows"]}


# ------------------------------------------------------------- setup / gate


def test_initialize_without_takeover_keeps_status_none(db_path):
    service.initialize(db_path, "2026-10")
    state = service.state_view(db_path)
    assert state["takeover"] == {
        "mode": False,
        "status": "none",
        "first_pending_month": None,
        "submission": None,
    }


def test_initialize_with_takeover_marks_pending(db_path):
    init_with_takeover(db_path)
    state = service.state_view(db_path)
    assert state["takeover"]["mode"] is True
    assert state["takeover"]["status"] == "pending"
    assert state["takeover"]["first_pending_month"] == "2026-10"
    assert state["takeover"]["submission"] is None


def test_first_month_cannot_be_sealed_while_takeover_pending(db_path):
    init_with_takeover(db_path)
    upload(db_path, [("A1", "2026-10", "城东", "100", "1")])
    with pytest.raises(ConflictError) as excinfo:
        service.seal_month(db_path, "2026-10")
    assert "接账" in excinfo.value.message
    # Calendar must not move after the refused seal.
    assert service.state_view(db_path)["current_pending_month"] == "2026-10"


def test_normal_batches_still_book_while_takeover_pending(db_path):
    init_with_takeover(db_path)
    result = upload(db_path, [("A1", "2026-10", "城东", "100", "1")])
    assert result["summary"]["new"] == 1
    row = table_by_street(db_path, "2026-10")["城东"]
    assert row["current_grams"] == 100_000


def test_takeover_endpoint_refused_when_mode_disabled(db_path):
    service.initialize(db_path, "2026-10")
    with pytest.raises(ConflictError):
        takeover(db_path, ("O1", "2026-09", "城南", "50", "1"))


# ------------------------------------------------------------ register shape


def test_register_collection_month_must_precede_first_pending_month(db_path):
    init_with_takeover(db_path)
    with pytest.raises(BatchRejected) as excinfo:
        takeover(db_path, ("O1", "2026-10", "城东", "50", "1"))
    assert "早于首个待封月" in excinfo.value.details[0]["message"]
    # Nothing written: takeover is still pending and no ticket exists.
    assert service.state_view(db_path)["takeover"]["status"] == "pending"


def test_register_duplicate_ticket_rejects_whole_file(db_path):
    init_with_takeover(db_path)
    with pytest.raises(BatchRejected) as excinfo:
        takeover(
            db_path,
            ("O1", "2026-09", "城东", "50", "1"),
            ("O1", "2026-09", "城东", "51", "2"),
        )
    assert "重复" in excinfo.value.details[0]["message"]
    assert service.state_view(db_path)["takeover"]["status"] == "pending"


def test_empty_register_is_rejected(db_path):
    init_with_takeover(db_path)
    with pytest.raises(Exception):
        service.submit_takeover(db_path, "empty.csv", b"")
    assert service.state_view(db_path)["takeover"]["status"] == "pending"


# ---------------------------------------------------------------- conflicts


def test_collection_month_conflict_reports_ticket_and_rejects_all(db_path):
    init_with_takeover(db_path)
    upload(db_path, [("A1", "2026-09", "城东", "100", "1")])
    with pytest.raises(BatchRejected) as excinfo:
        takeover(
            db_path,
            ("O9", "2026-08", "城南", "20", "1"),
            ("A1", "2026-08", "城东", "100", "2"),
        )
    details = excinfo.value.details
    assert [d["ticket_no"] for d in details] == ["A1"]
    assert "清运月不符" in details[0]["message"]
    # O9, the non-conflicting row, must not have landed either.
    state = service.state_view(db_path)
    assert state["takeover"]["status"] == "pending"
    assert "O9" not in _ticket_numbers(db_path)


def test_same_revision_different_content_conflict_rejects_all(db_path):
    init_with_takeover(db_path)
    upload(db_path, [("A1", "2026-09", "城东", "100", "3")])
    with pytest.raises(BatchRejected) as excinfo:
        takeover(
            db_path,
            ("A1", "2026-09", "城东", "120", "3"),
            ("O9", "2026-08", "城南", "20", "1"),
        )
    details = excinfo.value.details
    assert details[0]["ticket_no"] == "A1"
    assert "修订号" in details[0]["message"]
    assert service.state_view(db_path)["takeover"]["status"] == "pending"
    assert "O9" not in _ticket_numbers(db_path)


def test_multiple_conflicts_are_all_listed(db_path):
    init_with_takeover(db_path)
    upload(db_path, [("A1", "2026-09", "城东", "100", "1")])
    upload(db_path, [("A2", "2026-09", "城西", "80", "2")])
    with pytest.raises(BatchRejected) as excinfo:
        takeover(
            db_path,
            ("A1", "2026-08", "城东", "100", "1"),
            ("A2", "2026-09", "城西", "90", "2"),
        )
    assert {d["ticket_no"] for d in excinfo.value.details} == {"A1", "A2"}


# ------------------------------------------------------------------- effects


def test_register_only_ticket_nets_to_zero_in_first_month(db_path):
    init_with_takeover(db_path)
    result = takeover(db_path, ("O1", "2026-09", "城南", "70", "1"))
    assert result["summary"] == {
        "rows": 1,
        "register_only": 1,
        "register_won": 0,
        "new_won": 0,
    }
    # The settled old-warehouse net is booked then deducted on the same
    # street, so it is never counted again.
    row = table_by_street(db_path, "2026-10")["城南"]
    assert row["current_grams"] == 0
    assert row["adjustment_grams"] == 0
    assert row["total_grams"] == 0


def test_register_wins_higher_revision_applies_delta_to_register_street(db_path):
    init_with_takeover(db_path)
    # New warehouse already booked revision 1 (provisional, first month).
    upload(db_path, [("A1", "2026-09", "城东", "100", "1")])
    # Register carries revision 2 on a different street and weight.
    result = takeover(db_path, ("A1", "2026-09", "城西", "120", "2"))
    assert result["summary"]["register_won"] == 1

    table = table_by_street(db_path, "2026-10")
    # Spec formula: count the effective version's net per street, then deduct
    # the register's settled net per street.  The register settles on 城西, so
    # the provisional 100 on 城东 is reversed (-> 0), and the effective 120 on
    # 城西 is fully offset by the 120 deduction (-> 0).  The old warehouse's
    # own revision-2 correction is therefore never counted in the new ledger.
    assert table["城东"]["adjustment_grams"] == 0
    assert table["城西"]["adjustment_grams"] == 0
    assert service.month_view(db_path, "2026-10")["table"]["totals"][
        "total_grams"
    ] == 0


def test_new_warehouse_keeps_higher_revision_and_deducts_register(db_path):
    init_with_takeover(db_path)
    upload(db_path, [("A1", "2026-09", "城东", "100", "3")])
    result = takeover(db_path, ("A1", "2026-09", "城南", "80", "2"))
    assert result["summary"]["new_won"] == 1

    table = table_by_street(db_path, "2026-10")
    # Effective version stays the new-warehouse revision 3: 100 booked on
    # 城东, register's settled 80 deducted on 城南.
    assert table["城东"]["adjustment_grams"] == 100_000
    assert table["城南"]["adjustment_grams"] == -80_000
    assert service.month_view(db_path, "2026-10")["table"]["totals"][
        "total_grams"
    ] == 20_000


def test_equal_revision_identical_content_is_not_a_conflict(db_path):
    init_with_takeover(db_path)
    upload(db_path, [("A1", "2026-09", "城东", "100", "2")])
    result = takeover(db_path, ("A1", "2026-09", "城东", "100", "2"))
    assert result["summary"]["register_won"] == 1
    # Booked effective 100, deducted settled 100: nothing counted twice.
    row = table_by_street(db_path, "2026-10")["城东"]
    assert row["adjustment_grams"] == 0


def test_tickets_outside_register_keep_their_ordinary_booking(db_path):
    init_with_takeover(db_path)
    upload(db_path, [("N1", "2026-09", "城东", "40", "1")])
    takeover(db_path, ("O1", "2026-09", "城南", "70", "1"))
    table = table_by_street(db_path, "2026-10")
    # N1 is not on the register: its provisional booking stays untouched.
    assert table["城东"]["adjustment_grams"] == 40_000
    assert table["城南"]["adjustment_grams"] == 0


def test_takeover_includes_new_warehouse_revisions_received_before_effect(db_path):
    """The register must be checked against everything the new warehouse
    already collected, not just the very first booking."""

    init_with_takeover(db_path)
    upload(db_path, [("A1", "2026-09", "城东", "100", "1")])
    upload(db_path, [("A1", "2026-09", "城西", "130", "3")])  # correction
    # Register revision 2 is older than the new-warehouse revision 3.
    result = takeover(db_path, ("A1", "2026-09", "城南", "90", "2"))
    assert result["summary"]["new_won"] == 1
    # Effective version is revision 3 (130 on 城西); the register settled
    # 90 on 城南.  Both provisional bookings were reversed first, then the
    # effective 130 is booked on 城西 and 90 deducted on 城南.
    table = table_by_street(db_path, "2026-10")
    # 城东 stays visible as an explicit zero-net row (the ledger never hides
    # streets that held bookings); provisional 100 was fully reversed.
    assert table["城东"]["adjustment_grams"] == 0
    assert table["城西"]["adjustment_grams"] == 130_000
    assert table["城南"]["adjustment_grams"] == -90_000
    assert service.month_view(db_path, "2026-10")["table"]["totals"][
        "total_grams"
    ] == 40_000


def test_first_month_current_month_bookings_remain_current(db_path):
    init_with_takeover(db_path)
    # A ticket whose collection month equals the first pending month stays in
    # the "current month" column even after the takeover.
    upload(db_path, [("C1", "2026-10", "城东", "60", "1")])
    takeover(db_path, ("O1", "2026-09", "城南", "70", "1"))
    row = table_by_street(db_path, "2026-10")["城东"]
    assert row["current_grams"] == 60_000
    assert row["adjustment_grams"] == 0


# --------------------------------------------------------------- post-takeover


def test_seal_allowed_after_takeover_and_snapshot_freezes_effect(db_path):
    init_with_takeover(db_path)
    upload(db_path, [("A1", "2026-09", "城东", "100", "1")])
    takeover(db_path, ("A1", "2026-09", "城西", "120", "2"))
    payload, advanced = service.seal_month(db_path, "2026-10")
    assert advanced is True
    # Register-won ticket: effective 120 booked and 120 deducted, provisional
    # 100 reversed -- the settled ticket contributes nothing to the new ledger.
    assert payload["totals"]["total_grams"] == 0
    assert service.state_view(db_path)["current_pending_month"] == "2026-11"


def test_later_correction_starts_from_effective_version(db_path):
    init_with_takeover(db_path)
    upload(db_path, [("A1", "2026-09", "城东", "100", "1")])
    takeover(db_path, ("A1", "2026-09", "城西", "120", "2"))
    service.seal_month(db_path, "2026-10")

    # Revision 3 must reverse the post-takeover effective version (120/城西),
    # not the pre-takeover provisional one.
    upload(db_path, [("A1", "2026-09", "城北", "150", "3")])
    table = table_by_street(db_path, "2026-11")
    assert table["城西"]["adjustment_grams"] == -120_000
    assert table["城北"]["adjustment_grams"] == 150_000


def test_older_revision_after_takeover_is_noop(db_path):
    init_with_takeover(db_path)
    takeover(db_path, ("O1", "2026-09", "城南", "70", "2"))
    result = upload(db_path, [("O1", "2026-09", "城南", "999", "1")])
    assert result["summary"]["noop"] == 1
    row = table_by_street(db_path, "2026-10")["城南"]
    assert row["total_grams"] == 0


def test_takeover_is_one_time_only(db_path):
    init_with_takeover(db_path)
    takeover(db_path, ("O1", "2026-09", "城南", "70", "1"))
    with pytest.raises(ConflictError):
        takeover(db_path, ("O2", "2026-09", "城北", "30", "1"))


def test_failed_takeover_can_be_resubmitted(db_path):
    init_with_takeover(db_path)
    upload(db_path, [("A1", "2026-09", "城东", "100", "1")])
    with pytest.raises(BatchRejected):
        takeover(db_path, ("A1", "2026-08", "城东", "100", "2"))  # month conflict
    # Ordinary batch and bottom table are retained.
    before = service.month_view(db_path, "2026-10")["table"]
    assert table_by_street(db_path, "2026-10")["城东"]["adjustment_grams"] == 100_000

    result = takeover(db_path, ("A1", "2026-09", "城西", "120", "2"))
    assert result["summary"]["register_won"] == 1
    assert service.state_view(db_path)["takeover"]["status"] == "done"
    after = service.month_view(db_path, "2026-10")["table"]
    assert before != after  # the successful resubmission did take effect
    assert service.state_view(db_path)["takeover"]["submission"][
        "filename"
    ] == "takeover.csv"


def test_failed_takeover_leaves_no_takeover_rows(db_path):
    init_with_takeover(db_path)
    with pytest.raises(BatchRejected):
        takeover(
            db_path,
            ("O1", "2026-10", "城南", "70", "1"),  # bad month
        )
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM takeovers").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM takeover_tickets").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE kind LIKE 'takeover%'"
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0
    finally:
        conn.close()


def test_full_workflow_matches_spec_example(db_path):
    """End-to-end: ordinary batches, conflicts fixed, mixed register, seal."""

    init_with_takeover(db_path)
    # New warehouse collected a few tickets before the takeover is ready.
    upload(db_path, [("A1", "2026-09", "城东", "100", "1")])
    upload(db_path, [("N1", "2026-09", "城北", "50", "1")])  # not on register

    result = takeover(
        db_path,
        ("O1", "2026-08", "城南", "70", "1"),    # register-only
        ("A1", "2026-09", "城西", "120", "2"),   # register wins
    )
    assert result["summary"] == {
        "rows": 2,
        "register_only": 1,
        "register_won": 1,
        "new_won": 0,
    }

    table = table_by_street(db_path, "2026-10")
    # O1 nets zero (booked and deducted); A1's provisional 100 on 城东 is
    # reversed (zero-net row stays visible) and the register-won revision 2
    # nets zero on 城西; N1 keeps its ordinary 50 城北 adjustment because it is
    # not on the register.
    assert table["城东"]["adjustment_grams"] == 0
    assert table["城西"]["adjustment_grams"] == 0
    assert table["城南"]["adjustment_grams"] == 0
    assert table["城北"]["adjustment_grams"] == 50_000
    assert service.month_view(db_path, "2026-10")["table"]["totals"][
        "total_grams"
    ] == 50_000

    # Ordinary bookings and batches are retained alongside takeover records.
    state = service.state_view(db_path)
    assert len(state["batches"]) == 2
    assert state["takeover"]["status"] == "done"

    payload, advanced = service.seal_month(db_path, "2026-10")
    assert advanced is True
    assert payload["totals"]["total_grams"] == 50_000
    # Snapshot is immutable under later corrections.
    upload(db_path, [("A1", "2026-09", "城北", "200", "3")])
    assert service.get_snapshot(db_path, "2026-10")["totals"][
        "total_grams"
    ] == 50_000


# --------------------------------------------------------------- persistence


def _ticket_numbers(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[0]
            for row in conn.execute("SELECT ticket_no FROM tickets").fetchall()
        }
    finally:
        conn.close()


def test_takeover_state_and_effective_tickets_persist(db_path):
    init_with_takeover(db_path)
    upload(db_path, [("A1", "2026-09", "城东", "100", "1")])
    takeover(
        db_path,
        ("O1", "2026-08", "城南", "70", "1"),
        ("A1", "2026-09", "城西", "120", "2"),
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        state = conn.execute("SELECT * FROM state WHERE id = 1").fetchone()
        assert state["takeover_status"] == "done"
        assert state["takeover_id"] == 1
        tickets = {
            row["ticket_no"]: row
            for row in conn.execute("SELECT * FROM takeover_tickets")
        }
        assert set(tickets) == {"O1", "A1"}
        assert tickets["O1"]["effective_source"] == "register"
        assert tickets["A1"]["effective_source"] == "register"
        assert tickets["A1"]["effective_revision"] == 2
        kinds = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT kind FROM bookings WHERE takeover_id = 1"
            )
        }
        assert kinds == {"reverse", "takeover_book", "takeover_deduct"}
        # Ordinary batch bookings are preserved.
        assert conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE batch_id IS NOT NULL"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_concurrent_takeover_and_uploads_are_serialised(db_path):
    """The write lock serialises the takeover with ordinary uploads; the
    register is checked against every batch that committed before it, and a
    later upload sees the post-takeover effective version."""

    import threading

    init_with_takeover(db_path)
    barrier = threading.Barrier(4)
    errors = []

    def uploader(index):
        try:
            barrier.wait(timeout=10)
            upload(db_path, [(f"U{index}", "2026-09", "城东", 10 + index, 1)])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def register_submitter():
        try:
            barrier.wait(timeout=10)
            takeover(db_path, ("O1", "2026-08", "城南", "50", "1"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=uploader, args=(i,)) for i in range(1, 4)
    ]
    threads.append(threading.Thread(target=register_submitter))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    state = service.state_view(db_path)
    assert state["takeover"]["status"] == "done"
    # All three ordinary batches survived alongside the takeover.
    assert len(state["batches"]) == 3
    # The register-only ticket nets zero in the first month regardless of
    # commit interleaving.
    table = table_by_street(db_path, "2026-10")
    assert table["城南"]["total_grams"] == 0
    # Sealing is now unblocked.
    payload, advanced = service.seal_month(db_path, "2026-10")
    assert advanced is True


def test_migration_from_legacy_schema(tmp_path):
    """A database created before the takeover feature migrates in place."""

    from warehouse.storage import connect, ensure_schema

    legacy = tmp_path / "legacy.db"
    conn = connect(str(legacy))
    try:
        conn.executescript(
            """
            CREATE TABLE state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                first_pending_month TEXT NOT NULL,
                current_pending_month TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE tickets (
                ticket_no TEXT PRIMARY KEY,
                collection_month TEXT NOT NULL,
                current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
                street TEXT NOT NULL,
                weight_grams INTEGER NOT NULL CHECK (weight_grams >= 0),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                pending_month TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                new_count INTEGER NOT NULL,
                corrected_count INTEGER NOT NULL,
                noop_count INTEGER NOT NULL,
                digest TEXT NOT NULL
            );
            CREATE TABLE bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL REFERENCES batches(id),
                ticket_no TEXT NOT NULL REFERENCES tickets(ticket_no),
                revision INTEGER NOT NULL,
                target_month TEXT NOT NULL,
                collection_month TEXT NOT NULL,
                street TEXT NOT NULL,
                grams INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('book', 'reverse')),
                created_at TEXT NOT NULL
            );
            CREATE TABLE snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month TEXT NOT NULL UNIQUE,
                seq INTEGER NOT NULL UNIQUE,
                sealed_at TEXT NOT NULL,
                total_current_grams INTEGER NOT NULL,
                total_adjustment_grams INTEGER NOT NULL,
                total_grams INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            );
            INSERT INTO state VALUES (1, '2026-10', '2026-10', 't', 't');
            INSERT INTO batches VALUES (1, 'b.csv', '2026-10', 't', 1, 1, 0, 0, 'd');
            INSERT INTO tickets VALUES ('A1', '2026-09', 1, '城东', 100000, 't', 't');
            INSERT INTO bookings VALUES
                (1, 1, 'A1', 1, '2026-10', '2026-09', '城东', 100000, 'book', 't');
            """
        )
    finally:
        conn.close()

    ensure_schema(str(legacy))
    # Migration is idempotent.
    ensure_schema(str(legacy))

    db_path = str(legacy)
    state = service.state_view(db_path)
    assert state["initialized"] is True
    assert state["takeover"]["mode"] is False
    row = table_by_street(db_path, "2026-10")["城东"]
    assert row["adjustment_grams"] == 100_000
    # The upgraded ledger can opt into a takeover on a fresh book only; here
    # the mode stays off and sealing works as before.
    payload, advanced = service.seal_month(db_path, "2026-10")
    assert advanced is True
    assert payload["totals"]["total_grams"] == 100_000
