"""Read-only smoke check for a deployed survey server and its public links."""

from app import create_app


app = create_app()
db = app.extensions["mongo_client"][app.config["MONGO_DB_NAME"]]
origin = app.config["PUBLIC_BASE_URL"]
assert origin.startswith("https://"), "PUBLIC_BASE_URL must be HTTPS"

with app.test_client() as client:
    for survey in db.surveys.find({"status": "published"}):
        with client.session_transaction() as session:
            session["user_id"] = str(survey["owner_id"])
        detail = client.get(f"/surveys/{survey['_id']}")
        link = f"{origin}/survey/{survey['slug']}"
        assert detail.status_code == 200, (survey["slug"], detail.status_code)
        assert link.encode() in detail.data, (survey["slug"], "share link absent")
        form = client.get(f"/survey/{survey['slug']}")
        assert form.status_code == 200, (survey["slug"], form.status_code)
        api = client.get(f"/api/v1/public/surveys/{survey['slug']}")
        assert api.status_code == 200, (survey["slug"], api.status_code)
        print(f"PASS {link}: detail, form and API")

print("PASS read-only public deployment checks")
