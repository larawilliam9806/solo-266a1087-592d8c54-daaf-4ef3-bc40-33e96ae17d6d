"""HTTP-level tests for the old-warehouse takeover flow."""

import io

from warehouse import service


def csrf(client):
    client.get("/")
    with client.session_transaction() as sess:
        return sess["csrf"]


def csv_bytes(rows):
    text = "单号,清运月,街道,净重（千克）,修订号\n"
    text += "".join(",".join(str(p) for p in row) + "\n" for row in rows)
    return text.encode("utf-8")


def post_upload(client, rows):
    return client.post(
        "/api/upload",
        data={
            "file": (io.BytesIO(csv_bytes(rows)), "batch.csv"),
        },
        headers={"X-CSRF-Token": csrf(client)},
        content_type="multipart/form-data",
    )


def post_takeover(client, rows):
    return client.post(
        "/api/takeover",
        data={"file": (io.BytesIO(csv_bytes(rows)), "takeover.csv")},
        headers={"X-CSRF-Token": csrf(client)},
        content_type="multipart/form-data",
    )


def initialize(client, month="2026-10", takeover=False):
    data = {"csrf": csrf(client), "month": month}
    if takeover:
        data["takeover"] = "1"
    return client.post("/initialize", data=data, follow_redirects=False)


def test_setup_page_offers_takeover_choice(client):
    page = client.get("/").get_data(as_text=True)
    assert "从旧仓接账" in page
    assert 'name="takeover"' in page


def test_initialize_with_takeover_shows_pending_banner(client, db_path):
    response = initialize(client, takeover=True)
    assert response.status_code == 302
    page = client.get("/").get_data(as_text=True)
    assert "旧仓接账" in page
    assert "待接" in page
    assert "提交接账清册" in page
    # The first-month seal button must not be offered while pending.
    assert "待接账" in page
    state = service.state_view(db_path)
    assert state["takeover"]["mode"] is True
    assert state["takeover"]["status"] == "pending"


def test_takeover_requires_csrf(client, db_path):
    initialize(client, takeover=True)
    response = client.post(
        "/api/takeover",
        data={"file": (io.BytesIO(csv_bytes([("O1", "2026-09", "城东", 70, 1)])), "t.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400


def test_normal_upload_still_works_while_takeover_pending(client, db_path):
    initialize(client, takeover=True)
    response = post_upload(client, [("A1", "2026-10", "城东", 100, 1)])
    assert response.status_code == 200
    assert response.get_json()["summary"]["new"] == 1


def test_seal_first_month_blocked_over_http(client, db_path):
    initialize(client, takeover=True)
    post_upload(client, [("A1", "2026-10", "城东", 100, 1)])
    response = client.post(
        "/seal",
        data={"csrf": csrf(client), "month": "2026-10"},
        follow_redirects=False,
    )
    # LedgerError surfaces as the rendered error page (non-JSON form post).
    assert response.status_code == 409
    assert "接账" in response.get_data(as_text=True)
    assert service.state_view(db_path)["current_pending_month"] == "2026-10"


def test_takeover_conflict_returns_details_and_keeps_ledger(client, db_path):
    initialize(client, takeover=True)
    post_upload(client, [("A1", "2026-09", "城东", 100, 2)])
    response = post_takeover(client, [("A1", "2026-08", "城东", 100, 2)])
    assert response.status_code == 409
    payload = response.get_json()
    assert payload["code"] == "batch_rejected"
    assert payload["details"][0]["ticket_no"] == "A1"
    assert "清运月不符" in payload["details"][0]["message"]
    assert service.state_view(db_path)["takeover"]["status"] == "pending"

    # Resubmitting a corrected register succeeds.
    response = post_takeover(client, [("A1", "2026-09", "城西", 120, 3)])
    assert response.status_code == 200, response.get_data(as_text=True)
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["summary"]["register_won"] == 1
    assert service.state_view(db_path)["takeover"]["status"] == "done"


def test_same_revision_content_conflict_over_http(client, db_path):
    initialize(client, takeover=True)
    post_upload(client, [("A1", "2026-09", "城东", 100, 2)])
    response = post_takeover(client, [("A1", "2026-09", "城东", 101, 2)])
    assert response.status_code == 409
    assert response.get_json()["details"][0]["ticket_no"] == "A1"


def test_successful_takeover_unblocks_seal_and_renders_done(client, db_path):
    initialize(client, takeover=True)
    post_upload(client, [("N1", "2026-10", "城东", 60, 1)])
    response = post_takeover(client, [("O1", "2026-09", "城南", 70, 1)])
    assert response.status_code == 200

    page = client.get("/").get_data(as_text=True)
    assert "已接" in page
    assert "清册独有 1" in page

    response = client.post(
        "/seal",
        data={"csrf": csrf(client), "month": "2026-10"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert service.state_view(db_path)["current_pending_month"] == "2026-11"
    snapshot = service.get_snapshot(db_path, "2026-10")
    # O1 nets zero; N1's current-month 60 remains.
    assert snapshot["totals"]["total_grams"] == 60_000


def test_second_takeover_is_refused(client, db_path):
    initialize(client, takeover=True)
    post_takeover(client, [("O1", "2026-09", "城南", 70, 1)])
    response = post_takeover(client, [("O2", "2026-09", "城北", 30, 1)])
    assert response.status_code == 409
    assert "只接一次" in response.get_json()["error"]


def test_takeover_refused_when_mode_disabled(client, db_path):
    initialize(client, takeover=False)
    response = post_takeover(client, [("O1", "2026-09", "城南", 70, 1)])
    assert response.status_code == 409
    assert "未选择从旧仓接账" in response.get_json()["error"]


def test_state_api_reports_takeover_block(client, db_path):
    initialize(client, takeover=True)
    pending = client.get("/api/state").get_json()
    assert pending["takeover"] == {
        "mode": True,
        "status": "pending",
        "first_pending_month": "2026-10",
        "submission": None,
    }
    post_takeover(client, [("O1", "2026-09", "城南", 70, 1)])
    done = client.get("/api/state").get_json()
    assert done["takeover"]["status"] == "done"
    assert done["takeover"]["submission"]["row_count"] == 1
    assert done["takeover"]["submission"]["register_only_count"] == 1
