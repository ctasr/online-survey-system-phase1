import os

from app import create_app


app = create_app()


if __name__ == "__main__":
    app.run(host=os.getenv("SURVEY_HOST", "0.0.0.0"), port=int(os.getenv("SURVEY_PORT", "5000")))
