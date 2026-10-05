"""Exercise real workerd + SQLite + multipart with the local Telegram stub only."""
import json
import secrets
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import argparse
import copy


def run(url):
    parsed = urlparse(url)
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError("Runtime test requires localhost")
    def call(path, payload=None, headers=None, expected=200):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        request = Request(url + path, data, {"Content-Type": "application/json", **(headers or {})})
        try:
            response = urlopen(request, timeout=120)
        except HTTPError as exc:
            assert exc.code == expected, (path, exc.code)
            return None
        with response:
            assert response.status == expected
            body = response.read()
            if path.startswith("/guide/"):
                assert body.startswith(b"\x89PNG\r\n\x1a\n")
                return body
            return json.loads(body) if body.startswith(b"{") else body.decode()
    assert call("/health").get("test_mode") is True, "Use wrangler.test.toml with Telegram stub"
    call("/admin/export", {}, expected=403)
    call("/webhook", {}, expected=403)
    admin_headers = {"Authorization": "Bearer test-admin"}
    webhook_headers = {"X-Telegram-Bot-Api-Secret-Token": "test-webhook"}
    def admin(action, payload=None, expected=200):
        return call("/admin/" + action, payload or {}, admin_headers, expected)
    for page in range(1, 8):
        call(f"/guide/page-{page:02d}.png")
    uid = secrets.randbelow(2**31) + 100000
    other = uid + 1
    counter, screen = 0, None
    def snapshot():
        return admin("export")
    def rows(table):
        return [row for row in snapshot()["tables"][table] if row.get("user_id", row.get("id")) == uid]
    def send(text=None, callback=None, user=uid, update=None):
        nonlocal counter, screen
        counter += 1
        if update is None:
            message = {"message_id": counter, "chat": {"id": user, "type": "private"}, "from": {"id": user, "is_bot": False}, "text": text or ""}
            update = {"update_id": counter, "message": message}
            if callback is not None:
                if screen and screen.get("photo"):
                    message["photo"] = [{"file_id": "test"}]
                update = {"update_id": counter, "callback_query": {"id": f"test-{uid}-{counter}", "from": message["from"], "data": callback, "message": message}}
        assert call("/webhook", update, webhook_headers) == "ok"
        events = [row for row in snapshot()["tables"]["events"] if row["user_id"] == user]
        if events:
            screen = json.loads(events[-1]["response"])
        return screen, update
    def click(label):
        route = next(route for row in screen["buttons"] for text, route in row if label in text)
        return send(callback=route)[0]
    send("/start")
    click("Пройти гайд")
    for page in range(1, 8):
        assert f"{page}/7" in screen["text"] and screen["photo"]
        if page < 7:
            click("Далее")
    click("Завершить гайд")
    send("Тест Банк")
    send("тб")
    send("10000")
    assert "Теперь настрой первый кэшбэк" in screen["text"]
    user = rows("users")[0]
    assert user["guide_completed"] == user["cashback_intro_shown"] == 1
    bank = rows("banks")[0]["id"]
    click("Настроить первый кэшбэк")
    click("Прочее")
    send("3")
    click("Только для новых")
    _, purchase = send("250")
    assert "9 750,00" in screen["text"] and "7,50" in screen["text"]
    send(update=purchase)
    operations = rows("operations")
    assert len([r for r in operations if r["kind"] == "expense"]) == 1
    assert type(operations[0]["amount"]) is int
    month = operations[0]["day"][:7]
    assert rows("events")[-1]["created_at"].endswith("+00:00")
    send(callback=f"stats:{month}")
    assert "Доходы: 10 000,00 ₸" in screen["text"] and "Расходы: 250,00 ₸" in screen["text"]
    click("CSV")
    assert screen["document"] and screen["filename"].endswith(".csv")
    send(callback=f"paynow:{month}:{bank}")
    assert len([r for r in rows("operations") if r["payout_month"] == month]) == 1
    send(callback=f"paynow:{month}:{bank}")
    assert len([r for r in rows("operations") if r["payout_month"] == month]) == 1
    oid = next(r["id"] for r in rows("operations") if r["kind"] == "expense")
    send(callback=f"op:{oid}")
    click("Сумма")
    send("500")
    assert "500,00" in screen["text"]
    send(callback=f"op:{oid}")
    click("Удалить")
    click("Подтвердить")
    assert next(r for r in rows("operations") if r["id"] == oid)["deleted"] == 1
    send("/start", user=other)
    send(callback=f"obj:bank:{bank}", user=other)
    assert "Тест Банк" not in screen["text"]
    send("/settings")
    send("/help")
    send("/banks")
    send("/categories")
    send("/ops")
    backup = admin("backup")["backup"]
    before = snapshot()
    preview = admin("reset-user", {"uid": uid})
    assert preview["counts"]["users"] == 1 and preview["backup"] is None
    reset = admin("reset-user", {"uid": uid, "confirm": True})
    assert reset["backup"].startswith("before-reset-") and rows("users") == []
    admin("restore", {"name": backup, "confirm": True})
    assert snapshot() == before
    # A bad restore rolls back every deletion and insertion.
    admin("import", {"snapshot": before}, expected=400)
    assert snapshot() == before
    downloaded = admin("download", {"name": backup})
    assert downloaded == before
    call("/cdn-cgi/local/scheduled?cron=*+*+*+*+*")
    assert any(meta["daily"] for meta in admin("backups").values())
    admin("reset-user", {"uid": uid, "confirm": True})
    admin("reset-user", {"uid": other, "confirm": True})
    empty = snapshot()
    if not empty["tables"]["users"]:
        broken = copy.deepcopy(before)
        broken["tables"]["banks"][0]["user_id"] = 2**40
        admin("import", {"snapshot": broken}, expected=503)
        assert snapshot() == empty
        admin("import", {"snapshot": before})
        assert snapshot() == before
        admin("reset-user", {"uid": uid, "confirm": True})
        admin("reset-user", {"uid": other, "confirm": True})
    print("Runtime OK: 7 illustrations, onboarding, cashback, opening income, CSV, editing, deletion, replay, isolation, snapshots, reset, restore, cron, authentication.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8790")
    run(parser.parse_args().url.rstrip("/"))
