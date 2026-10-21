"""Print published question definitions for planning real example submissions."""

from app import create_app


app = create_app()
db = app.extensions["mongo_client"][app.config["MONGO_DB_NAME"]]
for survey in db.surveys.find({"status": "published"}):
    print("SURVEY", survey["_id"], survey["slug"], survey["title"],
          "responses", db.responses.count_documents({"survey_id": survey["_id"]}),
          "anonymous", survey.get("allow_anonymous"),
          "repeat", survey.get("allow_multiple_submissions"))
    for index, question in enumerate(survey.get("questions", []), 1):
        if not question.get("active", True):
            continue
        print(index, question["id"], question["type"], question["title"],
              "required", question.get("required"), "options", question.get("options"),
              "limits", {key: question.get(key) for key in (
                  "exact_select", "min_select", "max_select", "min_length", "max_length",
                  "min_value", "max_value", "integer_only")},
              "jumps", question.get("jumps"))
