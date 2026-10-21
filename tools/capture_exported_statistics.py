"""Capture exported live statistics with local Chinese fonts; no GUI interaction."""

from pathlib import Path

from playwright.sync_api import sync_playwright


output = Path(__file__).resolve().parents[1] / "output" / "demo-statistics"
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        headless=True,
        executable_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        args=["--no-first-run"],
    )
    for slug, view in (("JbI0U8Nm", "table"), ("HQriIaHd", "cards")):
        page = browser.new_page(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page.goto((output / f"{slug}-{view}-export.html").as_uri(), wait_until="networkidle")
        assert page.get_by_text("已提交答卷").first.is_visible()
        target = output / f"{slug}-{view}.png"
        page.screenshot(path=str(target), full_page=view == "table", animations="disabled")
        print(target)
        page.close()
    browser.close()
