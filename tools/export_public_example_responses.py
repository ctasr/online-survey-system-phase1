"""Export only the two demonstration surveys' answer records for local sync.

The payload is base64 encoded for piping directly to the matching importer.
"""

import base64
import sys

from bson import ObjectId, json_util

from app import create_app

SURVEY_IDS = [ObjectId(value) for value in (
    "6ad786c2e7b2bb4171cd065c",
    "6ad786c2e7b2bb4171cd065d",
)]


def main():
    app = create_app()
    db = app.extensions["mongo_client"][app.config["MONGO_DB_NAME"]]
    responses = list(db.responses.find({"survey_id": {"$in": SURVEY_IDS}}))
    user_ids = {item.get("respondent_id") for item in responses if item.get("respondent_id")}
    users = list(db.users.find({"_id": {"$in": list(user_ids)}}))
    payload = json_util.dumps({"responses": responses, "users": users}).encode("utf-8")
    sys.stdout.write(base64.b64encode(payload).decode("ascii") + "\n")


if __name__ == "__main__":
    main()
