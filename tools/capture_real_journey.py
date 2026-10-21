"""Capture real phase-one pages with a headless browser and an isolated MongoDB."""

import json
import os
import sys
import threading
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import create_app


OUT = ROOT / "output" / "第一阶段_实际运行流程"
OUT.mkdir(parents=True, exist_ok=True)
DB_NAME = "survey_phase1_capture_" + uuid4().hex
os.environ["MONGO_DB_NAME"] = DB_NAME
app = create_app()
db = app.extensions["mongo_client"][DB_NAME]
server = make_server("127.0.0.1", 0, app, threaded=True)
base = f"http://127.0.0.1:{server.server_port}"
server_thread = threading.Thread(target=server.serve_forever, daemon=True)
server_thread.start()
edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
pw = None
browser = None
thumbs = []


def post(client, path, fields):
    client.get(path)
    with client.session_transaction() as saved:
        data = {"csrf_token": saved["csrf_token"], **fields}
    response = client.post(path, data=data)
    if response.status_code not in (200, 302):
        raise RuntimeError(f"POST {path} returned {response.status_code}")
    return response


def visit(page, path):
    page.goto(base + path, wait_until="networkidle")


def shot(page, number, name):
    page.wait_for_load_state("networkidle")
    filename = f"{number:02d}_{name}.png"
    path = OUT / filename
    page.screenshot(path=str(path), full_page=True, animations="disabled")
    thumbs.append({"number": number, "name": name, "file": filename})
    print(f"{number:02d} {name}: {page.url}", flush=True)


try:
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True, executable_path=edge, args=["--no-first-run"])
    owner_context = browser.new_context(viewport={"width": 1360, "height": 900}, device_scale_factor=1, locale="zh-CN")
    owner = owner_context.new_page()

    visit(owner, "/")
    shot(owner, 1, "首页")
    visit(owner, "/register")
    shot(owner, 2, "注册")
    owner.locator("#username").fill("演示创建者")
    owner.locator("#password").fill("12345678")
    owner.locator("#confirmation").fill("12345678")
    owner.get_by_role("button", name="注册", exact=True).click()
    owner.wait_for_url("**/login")
    shot(owner, 3, "注册后登录")
    owner.locator("#username").fill("演示创建者")
    owner.locator("#password").fill("12345678")
    owner.get_by_role("button", name="登录", exact=True).click()
    owner.wait_for_url("**/dashboard")
    shot(owner, 4, "个人首页")

    visit(owner, "/surveys/new")
    owner.locator("#title").fill("周末活动调查")
    owner.locator("#description").fill("了解大家喜欢的活动，并收集参加意愿。")
    owner.locator('input[name="allow_anonymous"]').check()
    shot(owner, 5, "创建问卷填写信息")
    owner.get_by_role("button", name="创建问卷").click()
    owner.wait_for_url("**/surveys/*")
    survey = db.surveys.find_one({"title": "周末活动调查"})
    if not survey:
        raise RuntimeError("Survey creation failed")
    detail = f"/surveys/{survey['_id']}"
    shot(owner, 6, "问卷草稿")
    visit(owner, detail + "/questions/new")
    shot(owner, 7, "添加题目页面")

    seed = app.test_client()
    post(seed, "/login", {"username": "演示创建者", "password": "12345678"})
    questions = [
        {"type": "single_choice", "title": "你更喜欢哪种活动？", "options": "室内\n室外", "required": "on"},
        {"type": "single_choice", "title": "你想参加哪种室内活动？", "options": "电影\n桌游", "required": "on"},
        {"type": "single_choice", "title": "你想参加哪种户外活动？", "options": "徒步\n骑行", "required": "on"},
        {"type": "number", "title": "你愿意参加几次？", "required": "on", "integer_only": "on", "min_value": "0", "max_value": "10"},
        {"type": "text", "title": "还有其他建议吗？", "max_length": "100"},
    ]
    for definition in questions:
        post(seed, detail + "/questions/new", definition)
    ids = [q["id"] for q in db.surveys.find_one({"_id": survey["_id"]})["questions"]]
    post(seed, f"{detail}/questions/{ids[0]}/jumps", {"option": "室外", "target_id": ids[2], "return_to": "editor"})
    for option in ("电影", "桌游"):
        post(seed, f"{detail}/questions/{ids[1]}/jumps", {"option": option, "target_id": ids[3], "return_to": "editor"})
    visit(owner, f"{detail}/questions/{ids[0]}/edit")
    owner.locator("#question-title").fill("临时修改后取消")
    owner.get_by_role("link", name="取消").click()
    owner.wait_for_url(base + detail)
    visit(owner, detail)
    shot(owner, 8, "题目与跳转规则")
    if owner.get_by_role("link", name="继续添加题目").count() != 1:
        raise RuntimeError("Long question list lacks an add-question shortcut at the bottom")
    owner.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    owner.locator("[data-page-back-top]").wait_for(state="visible")
    owner.locator("[data-page-back-top]").click()
    owner.wait_for_function("window.scrollY < 30")
    visit(owner, "/dashboard")
    shot(owner, 26, "有问卷的个人首页")
    visit(owner, f"{detail}/questions/{ids[0]}/jumps/new")
    shot(owner, 9, "跳转设置页面")
    visit(owner, detail + "/preview")
    owner.locator(".preview-back-link").click()
    owner.wait_for_url(base + detail)
    visit(owner, detail + "/preview")
    shot(owner, 10, "填写预览")
    post(seed, detail + "/publish", {})
    visit(owner, detail)
    shot(owner, 11, "发布与分享链接")

    respondent_seed = app.test_client()
    post(respondent_seed, "/register", {"username": "演示填写者", "password": "12345678", "confirmation": "12345678"})
    public = "/survey/" + db.surveys.find_one({"_id": survey["_id"]})["slug"]
    respondent_context = browser.new_context(viewport={"width": 1360, "height": 900}, device_scale_factor=1, locale="zh-CN")
    respondent = respondent_context.new_page()
    visit(respondent, public)
    shot(respondent, 12, "打开链接先登录")
    respondent.locator("#username").fill("演示填写者")
    respondent.locator("#password").fill("12345678")
    respondent.get_by_role("button", name="登录", exact=True).click()
    respondent.wait_for_url("**/survey/*")
    shot(respondent, 13, "开始填写第一题")
    respondent.locator("[data-flow-submit]").click()
    respondent.wait_for_function("document.querySelector('[data-question-id] [data-question-error]').textContent.trim().length > 0")
    shot(respondent, 14, "必答校验提示")
    respondent.locator(f'input[name="q_{ids[0]}"][value="室外"]').check()
    respondent.locator(f'[data-question-id="{ids[1]}"]').wait_for(state="hidden")
    respondent.locator(f'[data-question-id="{ids[2]}"]').wait_for(state="visible")
    shot(respondent, 15, "按答案跳到第三题")
    respondent.locator(f'input[name="q_{ids[2]}"][value="徒步"]').check()
    respondent.locator(f'[data-question-id="{ids[3]}"]').wait_for(state="visible")
    shot(respondent, 16, "数字题填写")
    respondent.locator(f'input[name="q_{ids[3]}"]').fill("2")
    respondent.locator(f'[data-question-id="{ids[4]}"]').wait_for(state="visible")
    respondent.wait_for_function("document.querySelector('[role=progressbar]').getAttribute('aria-valuenow') === '100'")
    if "可提交" not in respondent.locator("[data-flow-status]").inner_text():
        raise RuntimeError("An unanswered optional question must not block submission")
    respondent.locator(f'textarea[name="q_{ids[4]}"]').fill("希望提前通知活动地点。")
    shot(respondent, 17, "文本题填写")
    respondent.locator("[data-flow-submit]").wait_for(state="visible")
    shot(respondent, 18, "提交前确认")
    respondent.locator("[data-flow-submit]").click()
    submit_confirmation = respondent.locator("[data-confirm-dialog]")
    submit_confirmation.wait_for(state="visible")
    if "提交后这份答卷不可修改" not in submit_confirmation.inner_text():
        raise RuntimeError("Submission confirmation does not explain that an answer cannot be edited")
    shot(respondent, 28, "答卷提交确认")
    submit_confirmation.get_by_role("button", name="取消").click()
    if db.responses.count_documents({"survey_id": survey["_id"]}) != 0:
        raise RuntimeError("Canceling submission saved a response")
    respondent.locator("[data-flow-submit]").click()
    submit_confirmation.get_by_role("button", name="确认提交").click()
    respondent.wait_for_url("**/submitted")
    shot(respondent, 19, "提交成功")
    if db.responses.count_documents({"survey_id": survey["_id"]}) != 1:
        raise RuntimeError("Response was not saved")
    visit(respondent, "/responses")
    shot(respondent, 20, "我的答卷")
    respondent.locator(".response-list a").first.click()
    if respondent.locator(".review-options .review-option").count() < 2:
        raise RuntimeError("Saved answer does not show the complete choice question")
    if respondent.locator(".review-options .review-option.is-selected").count() < 2:
        raise RuntimeError("Saved choices are not marked on the answer review")
    shot(respondent, 21, "查看本次回答")
    visit(owner, detail + "/statistics")
    shot(owner, 22, "整份问卷统计")
    visit(owner, f"{detail}/statistics/{ids[0]}")
    shot(owner, 23, "单题统计")
    visit(owner, detail + "/edit")
    owner.locator('label:has(input[name="option_label_style"][value="letters"])').click()
    owner.get_by_role("button", name="保存修改").click()
    owner.wait_for_url(base + detail)
    if db.surveys.find_one({"_id": survey["_id"]}).get("option_label_style") != "letters":
        raise RuntimeError("Option numbering was not saved")
    visit(owner, detail + "/preview")
    if owner.locator(".response-question .question-number").first.inner_text() != "1":
        raise RuntimeError("Question number changed to a letter")
    if owner.locator(".response-question .answer-option-label").first.inner_text() != "A.":
        raise RuntimeError("Choice did not get a letter label")
    shot(owner, 29, "数字题号和字母选项")
    visit(owner, detail + "/edit")
    shot(owner, 24, "编辑问卷信息")
    owner.set_viewport_size({"width": 390, "height": 844})
    if owner.evaluate("document.documentElement.scrollWidth > window.innerWidth"):
        raise RuntimeError("Survey edit page overflows a narrow viewport")
    shot(owner, 25, "窄屏编辑问卷信息")

    for path in ("/dashboard", "/surveys", "/surveys/new", detail,
                 detail + "/questions/new", f"{detail}/questions/{ids[0]}/jumps/new",
                 detail + "/statistics", f"{detail}/statistics/{ids[0]}"):
        visit(owner, path)
        if owner.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"):
            raise RuntimeError(f"Narrow viewport overflows: {path}")
    respondent.set_viewport_size({"width": 390, "height": 844})
    for path in (public, "/responses"):
        visit(respondent, path)
        if respondent.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"):
            raise RuntimeError(f"Narrow viewport overflows: {path}")
    visit(respondent, public)
    respondent.locator(f'input[name="q_{ids[0]}"][value="室外"]').check()
    respondent.locator('[data-draft-state]').get_by_text('答案已暂存', exact=False).wait_for(timeout=10000)
    visit(respondent, public)
    if not respondent.locator(f'input[name="q_{ids[0]}"][value="室外"]').is_checked():
        raise RuntimeError("An unfinished answer was not restored after reopening the survey")
    respondent.evaluate("window.scrollTo(0, document.documentElement.scrollHeight / 2)")
    respondent.locator("[data-response-shortcuts]").wait_for(state="visible")
    respondent.locator("[data-response-shortcuts] [data-exit-response]").click()
    leave_confirmation = respondent.locator("[data-confirm-dialog]")
    leave_confirmation.wait_for(state="visible")
    if "已填写的答案会暂存" not in leave_confirmation.inner_text():
        raise RuntimeError("Leaving an unfinished survey has no clear warning")
    leave_confirmation.get_by_role("button", name="取消").click()
    if respondent.url != base + public:
        raise RuntimeError("Canceling exit left the survey")
    respondent.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    respondent.locator(".flow-controls [data-exit-response]").wait_for(state="visible")
    if respondent.locator("[data-response-shortcuts]").is_visible():
        raise RuntimeError("Floating shortcuts obscure the bottom actions")
    respondent.locator(".flow-controls [data-exit-response]").click()
    leave_confirmation.get_by_role("button", name="取消").click()
    respondent.evaluate("window.scrollTo(0, document.documentElement.scrollHeight / 2)")
    respondent.locator("[data-response-shortcuts] [data-shortcut-overview]").click()
    respondent.wait_for_function("document.querySelector('.response-overview').getBoundingClientRect().top < 100")

    db.surveys.update_one(
        {"_id": survey["_id"]},
        {"$set": {"questions.2.title": "你每周用于这门课程的课外学习时间（小时）？"}},
    )
    owner.set_viewport_size({"width": 960, "height": 900})
    visit(owner, "/dashboard")
    third_number = owner.locator(".home-question-list li:nth-child(3) span").first
    if third_number.inner_text() != "03" or third_number.bounding_box()["width"] < 15:
        raise RuntimeError("The third question number wraps in the dashboard card")
    shot(owner, 30, "长题目在首页的排版")

    owner.set_viewport_size({"width": 1360, "height": 900})
    visit(owner, "/surveys")
    native_dialogs = []
    owner.on("dialog", lambda dialog: (native_dialogs.append(dialog.type), dialog.dismiss()))
    survey_row = owner.locator("[data-survey-item]").filter(has_text="周末活动调查")
    survey_row.get_by_role("button", name="删除", exact=True).click()
    confirm = owner.locator("[data-confirm-dialog]")
    confirm.wait_for(state="visible")
    if "周末活动调查" not in confirm.inner_text() or native_dialogs:
        raise RuntimeError("Survey deletion did not open the page confirmation")
    shot(owner, 27, "删除问卷确认")
    confirm.get_by_role("button", name="取消").click()
    if not db.surveys.find_one({"_id": survey["_id"]}):
        raise RuntimeError("Cancel deleted the survey")
    survey_row.get_by_role("button", name="删除", exact=True).click()
    confirm.get_by_role("button", name="删除问卷").click()
    owner.wait_for_url(base + "/surveys")
    if db.surveys.find_one({"_id": survey["_id"]}) or native_dialogs:
        raise RuntimeError("Confirmed deletion failed or used a native dialog")

    (OUT / "步骤清单.json").write_text(json.dumps(thumbs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {len(thumbs)} real screenshots in {OUT}", flush=True)
finally:
    if browser is not None:
        browser.close()
    if pw is not None:
        pw.stop()
    server.shutdown()
    server_thread.join(timeout=10)
    app.extensions["mongo_client"].drop_database(DB_NAME)
    app.extensions["mongo_client"].close()
    os.environ.pop("MONGO_DB_NAME", None)
