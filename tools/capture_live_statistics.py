"""Render live owner statistics to PNG without driving the user's browser."""

import subprocess
from pathlib import Path

from app import create_app


app = create_app()
db = app.extensions["mongo_client"][app.config["MONGO_DB_NAME"]]
output = Path("/opt/survey-phase1/output/demo-statistics")
output.mkdir(parents=True, exist_ok=True)

for slug in ("JbI0U8Nm", "HQriIaHd"):
    survey = db.surveys.find_one({"slug": slug})
    with app.test_client() as client:
        with client.session_transaction() as session:
            session["user_id"] = str(survey["owner_id"])
        for view in ("table", "cards"):
            result = client.get(f"/surveys/{survey['_id']}/statistics?view={view}")
            assert result.status_code == 200
            html = result.get_data(as_text=True).replace(
                "<head>", f'<head><base href="{app.config["PUBLIC_BASE_URL"]}/">', 1
            )
            source = output / f"{slug}-{view}.html"
            image = output / f"{slug}-{view}.png"
            try:
                source.write_text(html, encoding="utf-8")
                subprocess.run([
                    "/usr/bin/google-chrome", "--headless=new", "--no-sandbox",
                    "--disable-dev-shm-usage", "--disable-gpu", "--hide-scrollbars",
                    "--window-size=1440,900", f"--screenshot={image}",
                    source.as_uri(),
                ], check=True, capture_output=True, timeout=35)
                assert image.is_file() and image.stat().st_size > 1000
                print(image)
            finally:
                source.unlink(missing_ok=True)
