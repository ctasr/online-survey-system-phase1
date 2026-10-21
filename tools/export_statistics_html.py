"""Export two read-only owner statistics views for local headless screenshots."""

import re
from pathlib import Path

from app import create_app


app = create_app()
db = app.extensions["mongo_client"][app.config["MONGO_DB_NAME"]]
output = Path("/opt/survey-phase1/output/demo-statistics")
output.mkdir(parents=True, exist_ok=True)

for slug, view in (("JbI0U8Nm", "table"), ("HQriIaHd", "cards")):
    survey = db.surveys.find_one({"slug": slug})
    with app.test_client() as client:
        with client.session_transaction() as session:
            session["user_id"] = str(survey["owner_id"])
        response = client.get(f"/surveys/{survey['_id']}/statistics?view={view}")
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        html = re.sub(r'(<input[^>]*name="csrf_token"[^>]*value=")[^"]+', r'\1', html)
        html = html.replace("<head>", f'<head><base href="{app.config["PUBLIC_BASE_URL"]}/">', 1)
        target = output / f"{slug}-{view}-export.html"
        target.write_text(html, encoding="utf-8")
        print(target)
