import os
import re
import unittest
from uuid import uuid4

from app import create_app


class QuestionFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_name = "survey_phase1_test_" + uuid4().hex
        os.environ["MONGO_DB_NAME"] = cls.db_name
        cls.app = create_app()
        cls.db = cls.app.extensions["mongo_client"][cls.db_name]

    @classmethod
    def tearDownClass(cls):
        if cls.db_name.startswith("survey_phase1_test_"):
            cls.app.extensions["mongo_client"].drop_database(cls.db_name)
        cls.app.extensions["mongo_client"].close()
        os.environ.pop("MONGO_DB_NAME", None)

    @staticmethod
    def post(client, path, data):
        page = client.get(path[:-5] if path.endswith("/next") else path)
        with client.session_transaction() as saved:
            token = saved["csrf_token"]
        fields = {"csrf_token": token, **data}
        if path.startswith("/survey/"):
            match = re.search(r'name="question_snapshot" value="([^"]+)"', page.get_data(as_text=True))
            if match:
                fields["question_snapshot"] = match.group(1)
        return client.post(path, data=fields)

    def sign_in(self, client, name):
        self.post(client, "/register", {
            "username": name, "password": "123456", "confirmation": "123456",
        })
        self.post(client, "/login", {"username": name, "password": "123456"})

    def test_question_configuration_submission_and_statistics(self):
        owner = self.app.test_client()
        visitor = self.app.test_client()
        owner_name = "owner_" + uuid4().hex
        visitor_name = "visitor_" + uuid4().hex
        self.sign_in(owner, owner_name)
        self.sign_in(visitor, visitor_name)
        self.post(owner, "/surveys/new", {"title": "题型测试", "allow_anonymous": "on"})
        owner_id = self.db.users.find_one({"username": owner_name})["_id"]
        survey = self.db.surveys.find_one({"owner_id": owner_id})
        detail = f"/surveys/{survey['_id']}"
        add = detail + "/questions/new"
        public = f"/survey/{survey['slug']}"

        self.post(owner, detail + "/publish", {})
        self.assertIn("请先添加题目", owner.get(detail).get_data(as_text=True))
        self.assertEqual(visitor.get(add).status_code, 404)
        cases = [
            {"type": "single_choice", "title": "性别", "required": "on", "options": "男\n女\n其他"},
            {"type": "multiple_choice", "title": "水果", "required": "on", "options": "苹果\n香蕉\n西瓜\n葡萄", "min_select": "2", "max_select": "3"},
            {"type": "multiple_choice", "title": "恰好两项", "options": "甲\n乙\n丙", "exact_select": "2"},
            {"type": "text", "title": "评价", "required": "on", "min_length": "2", "max_length": "4"},
            {"type": "number", "title": "年龄", "required": "on", "min_value": "0", "max_value": "120", "integer_only": "on"},
            {"type": "number", "title": "评分", "min_value": "0", "max_value": "10"},
        ]
        for case in cases:
            self.assertEqual(self.post(owner, add, case).status_code, 302)
        bad_config = self.post(owner, add, {
            "type": "multiple_choice", "title": "错误", "options": "甲\n乙",
            "min_select": "2", "max_select": "1",
        })
        self.assertIn("最少选择的项数不能大于", bad_config.get_data(as_text=True))
        bad_config = self.post(owner, add, {
            "type": "single_choice", "title": "错误", "options": "甲\n甲",
        })
        self.assertIn("不能重复", bad_config.get_data(as_text=True))
        impossible_integer = self.post(owner, add, {
            "type": "number", "title": "无法作答的整数题", "integer_only": "on",
            "min_value": "1.1", "max_value": "1.9",
        })
        self.assertIn("没有可填写的整数", impossible_integer.get_data(as_text=True))
        survey = self.db.surveys.find_one({"_id": survey["_id"]})
        self.assertEqual(len(survey["questions"]), len(cases))
        self.post(owner, detail + "/publish", {})
        self.assertEqual(visitor.get(public).status_code, 200)

        keys = ["q_" + q["id"] for q in survey["questions"]]
        valid = {
            keys[0]: "女", keys[1]: ["苹果", "香蕉"], keys[2]: ["甲", "乙"],
            keys[3]: "很好", keys[4]: "120", keys[5]: "7.5",
        }
        invalid_cases = [
            ({keys[0]: ""}, "你还未作答本题"),
            ({keys[0]: "未知"}, "无效选项"),
            ({keys[1]: ["苹果"]}, "至少选 2 项"),
            ({keys[1]: ["苹果", "香蕉", "西瓜", "葡萄"]}, "最多选 3 项"),
            ({keys[1]: ["苹果", "苹果"]}, "无效选项"),
            ({keys[2]: []}, "你还未作答本题"),
            ({keys[2]: ["甲"]}, "请选择 2 项"),
            ({keys[3]: "好"}, "至少输入 2 字"),
            ({keys[3]: "非常非常好"}, "最多输入 4 字"),
            ({keys[4]: "3.5"}, "必须输入整数"),
            ({keys[4]: "121"}, "不能大于 120"),
            ({keys[4]: "-1"}, "不能小于 0"),
            ({keys[5]: "NaN"}, "有限数字"),
        ]
        for change, message in invalid_cases:
            attempt = self.post(visitor, public, {**valid, **change})
            self.assertIn(message, attempt.get_data(as_text=True))
            self.assertEqual(self.db.responses.count_documents({}), 0)

        result = self.post(visitor, public, valid)
        self.assertEqual(result.status_code, 302)
        self.assertEqual(result.location, public + "/submitted")
        response = self.db.responses.find_one({"survey_id": survey["_id"]})
        self.assertIsNone(response["respondent_id"])
        self.assertEqual(response["answers"][1]["value"], ["苹果", "香蕉"])
        self.assertEqual(response["answers"][4]["value"], "120")
        stats = owner.get(detail + "/statistics?view=cards").get_data(as_text=True)
        self.assertIn("已提交答卷", stats)
        self.assertIn('<b class="option-result-marker">1.</b> 苹果</span><strong>1 次 · 100%', stats)
        self.assertIn("很好", stats)
        self.post(owner, detail + "/close", {})
        self.assertEqual(self.post(visitor, public, valid).status_code, 409)
        self.assertEqual(self.db.responses.count_documents({}), 1)


if __name__ == "__main__":
    unittest.main()
