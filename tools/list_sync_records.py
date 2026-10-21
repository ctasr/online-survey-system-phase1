"""List response identifiers for comparing local and public demo databases."""

from app import create_app
from bson import ObjectId

SURVEYS = ("6ad786c2e7b2bb4171cd065c", "6ad786c2e7b2bb4171cd065d")

app = create_app()
db = app.extensions["mongo_client"][app.config["MONGO_DB_NAME"]]
for survey_id in SURVEYS:
    survey = db.surveys.find_one({"_id": ObjectId(survey_id)}, {"title": 1})
    print("SURVEY", survey_id, bool(survey), survey.get("title") if survey else "")
    for response in db.responses.find({"survey_id": ObjectId(survey_id)}, {"respondent_id": 1, "submitted_at": 1}).sort("submitted_at", 1):
        print("RESPONSE", response["_id"], response.get("respondent_id"), response.get("submitted_at"))
