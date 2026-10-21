"""Read-only inventory of survey records without printing credentials or answers."""

from app import create_app


app = create_app()
db = app.extensions["mongo_client"][app.config["MONGO_DB_NAME"]]
for owner in db.users.find({}, {"username": 1}):
    print("OWNER", owner["_id"], owner["username"])
    for survey in db.surveys.find({"owner_id": owner["_id"]}).sort("created_at", 1):
        print("SURVEY", survey["_id"], survey.get("slug"), survey["title"],
              survey.get("status"), "questions", len([q for q in survey.get("questions", []) if q.get("active", True)]),
              "responses", db.responses.count_documents({"survey_id": survey["_id"]}),
              "drafts", db.response_drafts.count_documents({"survey_id": survey["_id"]}))
app.extensions["mongo_client"].close()
