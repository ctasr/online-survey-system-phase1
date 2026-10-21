"""Rename the old survey numbering field without changing answers or revisions."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app


app = create_app()
database = app.extensions["mongo_client"][app.config["MONGO_DB_NAME"]]
migrated = 0
for survey in database.surveys.find({"numbering_style": {"$exists": True}}):
    style = survey.get("option_label_style", survey["numbering_style"])
    if style not in ("numeric", "letters"):
        raise ValueError(f"Invalid option label style in survey {survey['_id']}")
    update = {"$unset": {"numbering_style": ""}}
    if "option_label_style" not in survey:
        update["$set"] = {"option_label_style": style}
    result = database.surveys.update_one({"_id": survey["_id"], "numbering_style": survey["numbering_style"]}, update)
    migrated += result.modified_count

print(f"Migrated {migrated} surveys.")
app.extensions["mongo_client"].close()
