"""One-time setup for an isolated MongoDB account and server environment."""

import os
import pwd
import secrets
import sys
from pathlib import Path

from pymongo import MongoClient


ROOT = Path(__file__).resolve().parent.parent
DATABASE = "survey_phase1"


def main(public_origin):
    if os.geteuid() != 0:
        raise SystemExit("Run this script as root on the deployment server.")

    app_env = ROOT / ".env"
    admin_env = Path("/root/survey-phase1-mongo-admin.env")
    if app_env.exists() or admin_env.exists():
        raise SystemExit("Secret files already exist; refusing to replace them.")

    client = MongoClient("mongodb://127.0.0.1:27017/", serverSelectionTimeoutMS=3000)
    database = client[DATABASE]
    app_password = secrets.token_urlsafe(32)
    admin_password = secrets.token_urlsafe(32)
    database.command("createUser", "survey_app", pwd=app_password,
                     roles=[{"role": "readWrite", "db": DATABASE}])
    client.admin.command("createUser", "survey_admin", pwd=admin_password,
                         roles=[{"role": "root", "db": "admin"}])

    uri = (f"mongodb://survey_app:{app_password}@127.0.0.1:27017/"
           f"{DATABASE}?authSource={DATABASE}")
    app_env.write_text(
        f"MONGO_URI={uri}\nMONGO_DB_NAME={DATABASE}\n"
        f"PUBLIC_BASE_URL={public_origin.rstrip('/')}\n"
        f"SECRET_KEY={secrets.token_hex(32)}\n",
        encoding="utf-8",
    )
    user = pwd.getpwnam("ubuntu")
    os.chown(app_env, user.pw_uid, user.pw_gid)
    app_env.chmod(0o600)
    admin_env.write_text(
        f"MONGO_URI=mongodb://survey_admin:{admin_password}"
        "@127.0.0.1:27017/admin?authSource=admin\n",
        encoding="utf-8",
    )
    admin_env.chmod(0o600)
    print("MongoDB users and protected environment files created.")


if __name__ == "__main__":
    main(sys.argv[1])
