from flask import current_app
from pymongo import MongoClient


def init_db(app):
    client = MongoClient(app.config["MONGO_URI"], serverSelectionTimeoutMS=3000)
    client.admin.command("ping")
    app.extensions["mongo_client"] = client
    database = client[app.config["MONGO_DB_NAME"]]
    database.users.create_index("username", unique=True)
    database.surveys.create_index("slug", unique=True)
    database.surveys.create_index("owner_id")
    database.responses.create_index("survey_id")
    database.responses.create_index([("viewer_key", 1), ("submitted_at", -1)])
    database.responses.create_index([("respondent_id", 1), ("submitted_at", -1)])
    database.response_drafts.create_index([("survey_id", 1), ("user_id", 1)], unique=True)
    database.question_library.create_index([("root_id", 1), ("version", 1)], unique=True)
    database.question_library.create_index([("owner_id", 1), ("created_at", -1)])
    database.question_library.create_index([("shared", 1), ("created_at", -1)])
    database.question_banks.create_index([("owner_id", 1), ("name", 1)], unique=True)
    database.surveys.create_index("questions.library_question_id")
    database.responses.create_index(
        [("survey_id", 1), ("respondent_key", 1)],
        name="one_response_per_user_when_limited",
        unique=True,
        partialFilterExpression={"repeat_limited": True},
    )


def get_db():
    return current_app.extensions["mongo_client"][current_app.config["MONGO_DB_NAME"]]
