"""Capture current demo statistics with headless Edge, without desktop UI input."""

from pathlib import Path

from playwright.sync_api import sync_playwright

from app import create_app


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "当前演示问卷"
OUTPUT.mkdir(parents=True, exist_ok=True)

app = create_app()
db = app.extensions["mongo_client"][app.config["MONGO_DB_NAME"]]
owner = db.users.find_one({"username": "yyx"})
if owner is None:
    raise RuntimeError("Owner account not found")

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        headless=True,
        executable_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        args=["--no-first-run"],
    )
    for title, prefix in (("基础数学题问卷", "数学"), ("足球兴趣与规则认知问卷", "足球")):
        survey = db.surveys.find_one({"title": title, "owner_id": owner["_id"], "status": "published"})
        if survey is None:
            raise RuntimeError(f"Survey not found: {title}")
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = str(owner["_id"])
        targets = [("表格总览", f"/surveys/{survey['_id']}/statistics?view=table")]
        if prefix == "数学":
            number = next(q for q in survey["questions"] if q["type"] == "number")
            targets.append(("数字题详情", f"/surveys/{survey['_id']}/statistics/{number['id']}"))
        else:
            targets.append(("逐题详情", f"/surveys/{survey['_id']}/statistics?view=cards"))
        for label, route in targets:
            result = client.get(route)
            if result.status_code != 200:
                raise RuntimeError(f"Statistics unavailable: {route}: {result.status_code}")
            html = result.get_data(as_text=True).replace(
                "<head>", '<head><base href="http://127.0.0.1:5001/">', 1
            )
            page = browser.new_page(viewport={"width": 1440, "height": 900}, locale="zh-CN")
            page.set_content(html, wait_until="networkidle")
            page.get_by_text("已提交答卷").first.wait_for()
            target = OUTPUT / f"{prefix}_{label}.png"
            page.screenshot(path=str(target), full_page=label != "逐题详情", animations="disabled")
            print(target)
            page.close()
    browser.close()

app.extensions["mongo_client"].close()
