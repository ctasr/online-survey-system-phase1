"""Read-only verification of answer hints and the default date shown in answers."""

from datetime import datetime

from app import create_app
from app.surveys import CHINA_TIME


app = create_app()
db = app.extensions["mongo_client"][app.config["MONGO_DB_NAME"]]
survey = db.surveys.find_one({"slug": "JbI0U8Nm"})
with app.test_client() as client:
    with client.session_transaction() as session:
        session["user_id"] = str(survey["owner_id"])
    form = client.get("/survey/JbI0U8Nm")
    assert form.status_code == 200
    text = form.get_data(as_text=True)
    assert "至少选 1 项，最多选 3 项。" in text
    assert "请填写整数，不能小于 0，不能大于 12。" in text
    answers = client.get("/responses")
    assert answers.status_code == 200
    today = datetime.now(CHINA_TIME).date().isoformat()
    answer_html = answers.get_data(as_text=True)
    assert f'name="date" value="{today}"' in answer_html
    assert 'data-date-active="false"' in answer_html
    print("PASS answer hints use commas, and answer date shows", today)
