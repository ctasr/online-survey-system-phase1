"""Capture owner views of named answers using a disposable database and headless Edge."""

import os
import re
import sys
import threading
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import create_app


database_name = "survey_phase1_capture_" + uuid4().hex
os.environ["MONGO_DB_NAME"] = database_name
app = create_app()
db = app.extensions["mongo_client"][database_name]
server = make_server("127.0.0.1", 0, app, threaded=True)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base = f"http://127.0.0.1:{server.server_port}"
out = ROOT / "output" / "第一阶段_实际运行流程"
out.mkdir(parents=True, exist_ok=True)


def post(client, path, fields):
    page = client.get(path)
    with client.session_transaction() as saved:
        data = {"csrf_token": saved["csrf_token"], **fields}
    if path.startswith("/survey/"):
        match = re.search(r'name="question_snapshot" value="([^"]+)"', page.get_data(as_text=True))
        if not match:
            raise RuntimeError("Question snapshot missing from public form")
        data["question_snapshot"] = match.group(1)
    response = client.post(path, data=data)
    if response.status_code != 302:
        raise RuntimeError(f"POST {path}: {response.status_code}")
    return response


def registered(name):
    client = app.test_client()
    post(client, "/register", {"username": name, "password": "12345678", "confirmation": "12345678"})
    post(client, "/login", {"username": name, "password": "12345678"})
    return client


browser = None
try:
    owner = registered("演示创建者")
    named = registered("演示账号填写者")
    anonymous = registered("演示匿名填写者")
    post(owner, "/surveys/new", {"title": "账号答卷查看验证", "allow_anonymous": "on"})
    survey = db.surveys.find_one({"title": "账号答卷查看验证"})
    detail = f"/surveys/{survey['_id']}"
    post(owner, detail + "/questions/new", {
        "type": "single_choice", "title": "你更喜欢哪类活动？",
        "options": "室内活动\n户外活动", "required": "on",
    })
    post(owner, detail + "/questions/new", {
        "type": "text", "title": "请写下原因", "required": "on",
        "min_length": "2", "max_length": "80",
    })
    post(owner, detail + "/publish", {})
    survey = db.surveys.find_one({"_id": survey["_id"]})
    q1, q2 = survey["questions"]
    public = "/survey/" + survey["slug"]
    post(named, public, {
        "identity_mode": "named", "q_" + q1["id"]: "户外活动",
        "q_" + q2["id"]: "喜欢在户外运动。",
    })
    post(anonymous, public, {
        "identity_mode": "anonymous", "q_" + q1["id"]: "室内活动",
        "q_" + q2["id"]: "室内活动比较方便。",
    })
    assert db.responses.count_documents({"survey_id": survey["_id"]}) == 2
    named_row = db.responses.find_one({"survey_id": survey["_id"], "respondent_id": {"$ne": None}})
    anonymous_row = db.responses.find_one({"survey_id": survey["_id"], "respondent_id": None})
    assert named_row and anonymous_row

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            args=["--no-first-run"],
        )
        page = browser.new_page(viewport={"width": 1360, "height": 900}, locale="zh-CN")
        page.goto(base + "/login", wait_until="networkidle")
        page.locator("#username").fill("演示创建者")
        page.locator("#password").fill("12345678")
        page.get_by_role("button", name="登录", exact=True).click()
        page.wait_for_url("**/dashboard")
        targets = [
            ("31_创建者查看账号答卷.png", detail + "/responses", "演示账号填写者"),
            ("32_创建者查看完整答卷.png", detail + "/responses/" + str(named_row["_id"]), "喜欢在户外运动"),
            ("33_按选项查看填写账号.png", detail + f"/statistics/{q1['id']}/accounts?version=1&option=1", "演示账号填写者"),
        ]
        for filename, route, expected in targets:
            page.goto(base + route, wait_until="networkidle")
            if expected not in page.locator("body").inner_text():
                raise RuntimeError(f"Expected content missing from {route}")
            if str(anonymous_row["_id"]) in page.locator("body").inner_text():
                raise RuntimeError("Anonymous response leaked into named review")
            page.screenshot(path=str(out / filename), full_page=True, animations="disabled")
            print(filename, route, flush=True)
        browser.close()
        browser = None
finally:
    server.shutdown()
    thread.join(timeout=10)
    app.extensions["mongo_client"].drop_database(database_name)
    app.extensions["mongo_client"].close()
    os.environ.pop("MONGO_DB_NAME", None)
