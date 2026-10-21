"""Import a small BSON snapshot into an empty survey database."""

import sys
from pathlib import Path
from zipfile import ZipFile

from bson import decode_all

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app


COLLECTIONS = ("users", "surveys", "responses", "response_drafts")


def main(archive_path):
    app = create_app()
    database = app.extensions["mongo_client"][app.config["MONGO_DB_NAME"]]
    if any(database[name].count_documents({}) for name in COLLECTIONS):
        raise SystemExit("Target database is not empty; refusing to overwrite data.")

    with ZipFile(archive_path) as archive:
        documents = {
            name: decode_all(archive.read(f"{name}.bson"))
            for name in COLLECTIONS
        }

    for name, items in documents.items():
        if items:
            database[name].insert_many(items)
    print({name: database[name].count_documents({}) for name in COLLECTIONS})


if __name__ == "__main__":
    main(sys.argv[1])
