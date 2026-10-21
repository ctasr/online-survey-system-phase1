import os
import re
import unittest
from datetime import datetime, timedelta
from uuid import uuid4

from app import create_app
from app.surveys import CHINA_TIME


class FullJourneyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_name = "survey_phase1_test_" + uuid4().hex
        os.environ["MONGO_DB_NAME"] = cls.db_name
        cls.app = create_app()
        cls.db = cls.app.extensions["mongo_client"][cls.db_name]

    @classmethod
    def tearDownClass(cls):
        cls.app.extensions["mongo_client"].drop_database(cls.db_name)
        cls.app.extensions["mongo_client"].close()
        os.environ.pop("MONGO_DB_NAME", None)

    @staticmethod
    def post(client, path, data):
        page = client.get(path[:-5] if path.endswith("/next") else path)
        with client.session_transaction() as saved:
            fields = {"csrf_token": saved["csrf_token"], **data}
        if path.startswith("/survey/"):
            match = re.search(r'name="question_snapshot" value="([^"]+)"', page.get_data(as_text=True))
            if match:
                fields["question_snapshot"] = match.group(1)
        return client.post(path, data=fields)

    def sign_in(self):
        client = self.app.test_client()
        username = "journey_" + uuid4().hex
        self.assertEqual(self.post(client, "/register", {
            "username": username, "password": "123456", "confirmation": "123456",
        }).status_code, 302)
        self.assertEqual(self.post(client, "/login", {
            "username": username, "password": "123456",
        }).status_code, 302)
        return client, self.db.users.find_one({"username": username})

    def test_from_registration_to_deletion(self):
        owner, owner_user = self.sign_in()
        respondent, _ = self.sign_in()
        deadline = (datetime.now(CHINA_TIME) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        self.assertEqual(self.post(owner, "/surveys/new", {
            "title": "Full journey survey", "description": "Course survey", "deadline": deadline,
        }).status_code, 302)
        survey = self.db.surveys.find_one({"owner_id": owner_user["_id"]})
        detail = f"/surveys/{survey['_id']}"
        public = f"/survey/{survey['slug']}"
        self.assertEqual(respondent.get(detail).status_code, 404)
        self.assertIn("Full journey survey", owner.get("/surveys").get_data(as_text=True))

        definitions = (
            {"type": "single_choice", "title": "Mode", "options": "A\nB", "required": "on"},
            {"type": "text", "title": "B followup", "required": "on"},
            {"type": "multiple_choice", "title": "Fruit", "options": "Apple\nBanana\nMelon",
             "required": "on", "min_select": "2", "max_select": "2"},
            {"type": "number", "title": "Age", "required": "on", "integer_only": "on",
             "min_value": "0", "max_value": "120"},
        )
        for question in definitions:
            self.assertEqual(self.post(owner, detail + "/questions/new", question).status_code, 302)
        questions = self.db.surveys.find_one({"_id": survey["_id"]})["questions"]
        jump_path = f"{detail}/questions/{questions[0]['id']}/jumps/new"
        self.assertEqual(owner.get(jump_path).status_code, 200)
        self.assertEqual(self.post(owner, f"{detail}/questions/{questions[0]['id']}/jumps", {
            "option": "A", "target_id": questions[2]["id"], "return_to": "editor",
        }).status_code, 302)
        self.assertIn("已有规则", owner.get(jump_path).get_data(as_text=True))
        self.assertEqual(self.post(owner, detail + "/publish", {}).status_code, 302)
        self.assertEqual(respondent.get(public).status_code, 200)

        keys = ["q_" + question["id"] for question in questions]
        next_step = self.post(respondent, public + "/next", {
            "question_id": questions[0]["id"], keys[0]: "A",
        })
        self.assertEqual(next_step.json["next_question_id"], questions[2]["id"])
        invalid = self.post(respondent, public + "/next", {
            "question_id": questions[2]["id"], keys[2]: ["Apple"],
        })
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(self.db.responses.count_documents({"survey_id": survey["_id"]}), 0)
        submitted = self.post(respondent, public, {
            keys[0]: "A", keys[2]: ["Apple", "Banana"], keys[3]: "20",
        })
        self.assertEqual(submitted.status_code, 302)
        saved = self.db.responses.find_one({"survey_id": survey["_id"]})
        self.assertEqual([answer["question_id"] for answer in saved["answers"]],
                         [questions[0]["id"], questions[2]["id"], questions[3]["id"]])
        self.assertIn("Full journey survey", respondent.get("/responses").get_data(as_text=True))
        statistics = owner.get(detail + "/statistics").get_data(as_text=True)
        self.assertIn("Apple", statistics)
        self.assertIn("20", statistics)

        self.assertEqual(self.post(owner, detail + "/edit", {
            "title": "Full journey survey updated", "description": "Updated",
            "repeat_policy": "multiple", "deadline": "",
        }).status_code, 302)
        updated = self.db.surveys.find_one({"_id": survey["_id"]})
        self.assertEqual(updated["title"], "Full journey survey updated")
        self.assertIsNone(updated["deadline"])
        self.assertEqual(self.post(owner, detail + "/delete", {}).status_code, 302)
        self.assertEqual(self.db.responses.count_documents({"survey_id": survey["_id"]}), 0)
        self.assertEqual(respondent.get(public).status_code, 404)
        self.assertNotIn("Full journey survey updated", owner.get("/surveys").get_data(as_text=True))
