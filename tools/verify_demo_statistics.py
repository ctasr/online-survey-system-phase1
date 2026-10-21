"""Check and summarize the live statistics after example responses are submitted."""

from app import create_app


app = create_app()
db = app.extensions["mongo_client"][app.config["MONGO_DB_NAME"]]
for slug in ("JbI0U8Nm", "HQriIaHd"):
    survey = db.surveys.find_one({"slug": slug})
    assert survey
    with app.test_client() as client:
        with client.session_transaction() as session:
            session["user_id"] = str(survey["owner_id"])
        response = client.get(f"/api/v1/surveys/{survey['_id']}/statistics")
        assert response.status_code == 200, (slug, response.status_code)
        payload = response.get_json()
        count = db.responses.count_documents({"survey_id": survey["_id"]})
        assert payload["response_count"] == count
        assert count >= (5 if slug == "JbI0U8Nm" else 4)
        page = client.get(f"/surveys/{survey['_id']}/statistics")
        assert page.status_code == 200
        assert "表格总览" in page.get_data(as_text=True)
        print("SURVEY", slug, survey["title"], "responses", count)
        for row in payload["results"]:
            if row["historical"]:
                continue
            question = row["question"]
            print(" ", row["index"], question["type"], question["title"],
                  "answered", row["answered_count"],
                  "options", row["option_counts"], "average", row["average"],
                  "values", row["values"] if question["type"] == "text" else "")

print("PASS live statistics API and table pages")
