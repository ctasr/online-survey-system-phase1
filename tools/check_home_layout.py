"""Capture the guest home page at desktop and phone widths for visual review."""

from pathlib import Path

from playwright.sync_api import sync_playwright


output = Path(__file__).resolve().parents[1] / "output" / "home-layout"
output.mkdir(parents=True, exist_ok=True)
url = "http://127.0.0.1:5001/"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        headless=True,
        executable_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        args=["--no-first-run"],
    )
    for name, width, height in (("desktop", 1440, 900), ("phone", 390, 844)):
        page = browser.new_page(viewport={"width": width, "height": height}, locale="zh-CN")
        response = page.goto(url, wait_until="networkidle")
        assert response and response.status == 200
        assert page.get_by_role("button", name="登录", exact=True).is_visible()
        assert page.locator(".welcome-capabilities").count() == 0
        dimensions = page.evaluate("({ width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight })")
        assert dimensions["width"] <= width, (name, dimensions)
        page.screenshot(path=str(output / f"{name}.png"), full_page=True, animations="disabled")
        print(name, dimensions, page.locator(".welcome-layout").bounding_box())
        page.close()
    browser.close()
