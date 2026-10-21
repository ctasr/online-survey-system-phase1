import os
import re
import unittest
from uuid import uuid4
from werkzeug.datastructures import MultiDict

from app import create_app
from app.numbering import option_label, option_style, question_label
from app.surveys import assessed_answers


class AnswerOverviewTests(unittest.TestCase):
    def test_question_numbers_stay_numeric_and_option_labels_can_use_letters(self):
        self.assertEqual(question_label(4), "4")
        self.assertEqual(option_label(4), "4")
        self.assertEqual(option_label(1, "letters"), "A")
        self.assertEqual(option_label(26, "letters"), "Z")
        self.assertEqual(option_label(27, "letters"), "AA")
        self.assertEqual(option_style({"numbering_style": "letters"}), "letters")

    def test_overview_follows_jump_and_checks_only_reached_questions(self):
        questions = [
            {"id": "first", "title": "入口", "type": "single_choice", "required": True,
             "options": ["A", "B"], "jumps": [{"kind": "single_choice", "option": "A", "target_id": "last"}]},
            {"id": "skipped", "title": "跳过", "type": "text", "required": True},
            {"id": "last", "title": "补充", "type": "text", "required": False},
        ]
        result = assessed_answers(questions, MultiDict([("q_first", "A")]))
        self.assertEqual(result["reachable"], ["first", "last"])
        self.assertEqual(result["answered"], ["first"])
        self.assertEqual(result["errors"], {})

        missing = assessed_answers(questions, MultiDict())
        self.assertEqual(missing["reachable"], ["first", "skipped", "last"])
        self.assertEqual(set(missing["errors"]), {"first", "skipped"})


class AnswerOverviewPageTests(unittest.TestCase):
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

    def test_letter_option_labels_and_assessment_endpoint(self):
        client = self.app.test_client()

        def post(path, values):
            client.get("/dashboard" if path.endswith("/assess") else path)
            with client.session_transaction() as saved:
                token = saved["csrf_token"]
            return client.post(path, data={"csrf_token": token, **values})

        client.get("/register")
        post("/register", {"username": "owner", "password": "123456", "confirmation": "123456"})
        post("/login", {"username": "owner", "password": "123456"})
        created = post("/surveys/new", {"title": "编号测试", "option_label_style": "letters"})
        self.assertEqual(created.status_code, 302)
        survey = self.db.surveys.find_one({"title": "编号测试"})
        self.assertEqual(survey["option_label_style"], "letters")
        detail = f"/surveys/{survey['_id']}"
        editor = client.get(detail + "/questions/new").get_data(as_text=True)
        self.assertRegex(editor, r'name="required" type="checkbox"[^>]*checked')
        post(detail + "/questions/new", {"type": "single_choice", "title": "入口", "options": "A\nB", "required": "on"})
        post(detail + "/questions/new", {"type": "text", "title": "补充", "required": "on"})
        questions = self.db.surveys.find_one({"_id": survey["_id"]})["questions"]
        page = client.get(detail + "/preview").get_data(as_text=True)
        self.assertIn('<span class="question-number">1</span>', page)
        self.assertIn('<span class="overview-number">2</span>', page)
        self.assertIn('<span class="answer-option-label">A.</span>', page)
        self.assertIn('<span class="answer-option-label">B.</span>', page)
        self.assertNotIn('<span class="error">*</span>', page)
        snapshot = re.search(r'name="question_snapshot" value="([^"]+)"', page).group(1)
        result = post(detail + "/preview/assess", {
            "question_snapshot": snapshot,
            "q_" + questions[0]["id"]: "A",
        })
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["reachable"], [q["id"] for q in questions])
        self.assertEqual(list(result.json["errors"]), [questions[1]["id"]])

        edit_path = detail + "/questions/" + questions[0]["id"] + "/edit"
        edit_page = client.get(edit_path).get_data(as_text=True)
        self.assertIn('data-discard-changes', edit_page)
        self.assertIn('> 必答</label>', edit_page)
        self.assertIn('data-option-style="letters"', edit_page)
        self.assertIn('选项编号使用整份问卷的设置', edit_page)
        changed = post(edit_path, {
            "option_label_style": "numeric", "type": "single_choice", "title": "入口",
            "options": "A\nB", "required": "on",
        })
        self.assertEqual(changed.status_code, 302)
        updated = self.db.surveys.find_one({"_id": survey["_id"]})
        self.assertEqual(updated["option_label_style"], "numeric")
        self.assertEqual(updated["questions"][0]["version"], questions[0]["version"])
