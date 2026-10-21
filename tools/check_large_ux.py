"""Headless regression check for a survey with many long questions."""

import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from bson import ObjectId
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import create_app


database_name = "survey_phase1_large_ux_" + uuid4().hex
os.environ["MONGO_DB_NAME"] = database_name
app = create_app()
db = app.extensions["mongo_client"][database_name]
server = make_server("127.0.0.1", 0, app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_port}"
screenshots = ROOT / "output" / "大题量体验检查"
screenshots.mkdir(parents=True, exist_ok=True)


def post(client, path, data):
    client.get(path)
    with client.session_transaction() as saved:
        token = saved["csrf_token"]
    result = client.post(path, data={"csrf_token": token, **data})
    assert result.status_code == 302, (path, result.status_code)
    return result


try:
    client = app.test_client()
    username = "大问卷体验_" + uuid4().hex[:10]
    post(client, "/register", {"username": username, "password": "12345678", "confirmation": "12345678"})
    post(client, "/login", {"username": username, "password": "12345678"})
    post(client, "/surveys/new", {"title": "长问卷操作检查"})
    survey = db.surveys.find_one({"title": "长问卷操作检查"})
    detail = f"/surveys/{survey['_id']}"
    post(client, detail + "/questions/new", {
        "type": "single_choice", "title": "示例题目", "required": "on", "options": "是\n否",
    })
    survey = db.surveys.find_one({"_id": survey["_id"]})
    seed = survey["questions"][0]
    questions = [
        {**seed, "id": uuid4().hex, "title": f"第{index:02d}题：这是一道较长的题目，用来检查查找与翻页时标题是否仍然清楚"}
        for index in range(1, 46)
    ]
    db.surveys.update_one({"_id": survey["_id"]}, {"$set": {"questions": questions}})
    user = db.users.find_one({"username": username})
    db.responses.insert_one({
        "survey_id": survey["_id"], "respondent_id": user["_id"],
        "submitted_at": datetime.now(timezone.utc),
        "survey_title": survey["title"],
        "answers": [{"question_id": q["id"], "question_version": 1, "value": "是"} for q in questions],
    })
    response = db.responses.find_one({"survey_id": survey["_id"]})
    for index in range(2, 25):
        duplicate = {**survey, "_id": ObjectId(), "title": f"分页样本问卷{index:02d}",
                     "slug": uuid4().hex[:12], "questions": []}
        db.surveys.insert_one(duplicate)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            args=["--no-first-run"],
        )
        page = browser.new_page(viewport={"width": 1360, "height": 900}, locale="zh-CN")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/login")
        page.locator("#username").fill(username)
        page.locator("#password").fill("12345678")
        page.get_by_role("button", name="登录", exact=True).click()
        page.wait_for_url("**/dashboard")

        page.goto(base + "/surveys")
        assert page.locator("[data-survey-item]:visible").count() == 20
        page.get_by_role("link", name="下一页").click()
        assert page.locator("[data-survey-item]:visible").count() == 4
        page.get_by_role("link", name="上一页").click()
        assert page.locator("[data-survey-item]:visible").count() == 20
        page.locator("#survey-search").fill("分页样本问卷24")
        page.get_by_role("button", name="查找").click()
        assert page.locator("[data-survey-item]:visible").count() == 1
        print("问卷列表：翻页与查找通过", flush=True)

        page.goto(base + detail)
        page.screenshot(path=str(screenshots / "题目管理.png"), full_page=False)
        assert page.locator("[data-question-list] > li:visible").count() == 20
        assert page.locator("[data-question-list] > li:first-child .question-actions").is_hidden()
        page.locator("[data-question-list] > li:first-child [data-question-expand]").click()
        assert page.locator("[data-question-list] > li:first-child .question-actions").is_visible()
        page.locator("[data-question-next]").click()
        assert page.locator("[data-question-list] > li:visible").count() == 20
        page.locator("[data-question-page-input]").fill("3")
        page.locator("[data-question-page-go]").click()
        assert page.locator("[data-question-list] > li:visible").count() == 5
        page.locator("[data-question-search]").fill("第45题")
        assert page.locator("[data-question-list] > li:visible").count() == 1
        assert "第45题" in page.locator("[data-question-list] > li:visible").inner_text()
        page.locator('[data-question-view="cards"]').click()
        assert not page.locator("[data-question-list]").get_attribute("class").endswith("is-condensed")
        page.goto(base + detail + "#question-" + questions[44]["id"])
        assert page.locator(f'#question-{questions[44]["id"]}').is_visible()
        print("题目管理：翻页、查找、切换视图和锚点通过", flush=True)

        page.goto(base + detail + "/statistics")
        page.screenshot(path=str(screenshots / "统计表格.png"), full_page=False)
        assert page.locator(".statistics-table tbody tr").count() == 25
        page.get_by_role("link", name="下一页").click()
        assert page.locator(".statistics-table tbody tr").count() == 20
        page.goto(base + detail + "/statistics?q=45")
        assert page.locator(".statistics-table tbody tr").count() == 1
        assert "第45题" in page.locator(".statistics-table tbody").inner_text()
        page.goto(base + detail + "/statistics?view=cards&page=5")
        assert page.locator(".stat-question").count() == 5
        page.locator('input[aria-label="统计页码"]').fill("1")
        page.get_by_role("button", name="跳转").click()
        assert page.locator(".stat-question").count() == 10
        print("统计结果：表格、精确题号查找及逐题分页通过", flush=True)

        page.goto(base + f"/surveys/{survey['_id']}/questions/{questions[0]['id']}/jumps/new")
        page.locator("[data-jump-target-search]").fill("第45题")
        assert page.locator("[data-jump-target-grid] .jump-target:visible").count() == 1
        page.goto(base + detail + "/preview")
        assert page.locator("[data-question-id]").count() == 45
        assert page.locator("[data-overview-question]").count() == 45
        page.wait_for_selector('[data-response-page-controls]:visible')
        page.screenshot(path=str(screenshots / "长问卷填写_桌面.png"), full_page=False)
        assert page.locator('[data-question-id]:visible').count() == 8
        assert '1–8' in page.locator('[data-response-page-status]').inner_text()
        page.locator('[data-response-next]').click()
        assert page.locator('[data-question-id]:visible').count() == 8
        assert '9–16' in page.locator('[data-response-page-status]').inner_text()
        page.locator(f'[data-overview-question="{questions[44]["id"]}"]').click()
        assert page.locator(f'[data-question-id="{questions[44]["id"]}"]').is_visible()
        assert '41–45' in page.locator('[data-response-page-status]').inner_text()
        page.locator('[data-flow-submit]').click()
        page.wait_for_timeout(600)
        assert page.locator(f'[data-question-id="{questions[0]["id"]}"]').is_visible()
        assert page.locator('[data-question-error]:visible').first.inner_text()
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(screenshots / "长问卷填写_窄屏.png"), full_page=False)
        assert page.locator('[data-question-id]:visible').count() == 8
        assert page.locator('.response-overview').evaluate("element => element.classList.contains('is-collapsed')")
        page.locator('[data-overview-toggle]').click()
        assert page.locator('[data-overview-question]:visible').count() == 45
        page.locator('[data-overview-toggle]').click()
        assert not errors, errors
        print("跳转设置与填写预览：分段浏览、跨组定位、提交校验通过", flush=True)

        page.goto(base + f"/responses/{response['_id']}")
        page.screenshot(path=str(screenshots / "答卷查看.png"), full_page=False)
        assert page.locator(".answer-review > li").count() == 20
        page.get_by_role("link", name="下一页").click()
        assert page.locator(".answer-review > li").count() == 20
        page.locator('input[aria-label="答卷页码"]').fill("3")
        page.get_by_role("button", name="跳转").click()
        assert page.locator(".answer-review > li").count() == 5
        assert "第45题" in page.locator(".answer-review").inner_text()
        assert not errors, errors
        print("查看答卷：45题分页、完整题目与浏览器脚本通过", flush=True)
        browser.close()
finally:
    server.shutdown()
    app.extensions["mongo_client"].drop_database(database_name)
    app.extensions["mongo_client"].close()
    os.environ.pop("MONGO_DB_NAME", None)
