"""Append missing public example answers locally without replacing existing data."""

import base64
import sys

from bson import ObjectId, json_util

from app import create_app

SURVEY_IDS = {ObjectId(value) for value in (
    "6ad786c2e7b2bb4171cd065c",
    "6ad786c2e7b2bb4171cd065d",
)}


def main():
    payload = json_util.loads(base64.b64decode(sys.stdin.read().strip()).decode("utf-8"))
    app = create_app()
    db = app.extensions["mongo_client"][app.config["MONGO_DB_NAME"]]
    if {item["survey_id"] for item in payload["responses"]} != SURVEY_IDS:
        raise SystemExit("Unexpected survey IDs in transfer")
    if db.surveys.count_documents({"_id": {"$in": list(SURVEY_IDS)}}) != 2:
        raise SystemExit("Local example surveys are missing")

    users_by_id = {item["_id"]: item for item in payload["users"]}
    missing = [item for item in payload["responses"] if not db.responses.find_one({"_id": item["_id"]})]
    for item in missing:
        user_id = item.get("respondent_id")
        if user_id and not db.users.find_one({"_id": user_id}):
            user = users_by_id.get(user_id)
            if not user:
                raise SystemExit(f"Missing account for response {item['_id']}")
            same_name = db.users.find_one({"username": user["username"]}, {"_id": 1})
            if same_name and same_name["_id"] != user_id:
                raise SystemExit("Username conflict; local database unchanged")

    users_added = 0
    for item in missing:
        user_id = item.get("respondent_id")
        if user_id and not db.users.find_one({"_id": user_id}):
            db.users.insert_one(users_by_id[user_id])
            users_added += 1
    for item in missing:
        db.responses.insert_one(item)

    print(f"Added {users_added} accounts and {len(missing)} responses; replaced 0 existing records.")
    for survey_id in SURVEY_IDS:
        print(f"{survey_id}: {db.responses.count_documents({'survey_id': survey_id})} responses")


if __name__ == "__main__":
    main()
