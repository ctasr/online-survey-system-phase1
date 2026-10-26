import os
import re
import unittest
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

from app import create_app
from app.statistics import summarize_responses


class StatisticsCalculationTests(unittest.TestCase):
    def test_removed_questions_keep_real_history_without_empty_draft_rows(self):
        questions = [
            {"id": "unused", "type": "text", "title": "发布前删除", "active": False},
            {"id": "legacy", "type": "text", "title": "旧答卷包含的题", "active": False},
            {"id": "current", "type": "text", "title": "当前第一题"},
        ]
        responses = [{"answers": [{"question_id": "legacy", "value": "历史回答"}]}]
        results = summarize_responses(questions, responses)
        self.assertEqual([row['question']['id'] for row in results], ['legacy', 'current'])
        self.assertTrue(results[0]['historical'])
        self.assertEqual(results[0]['values'], ['历史回答'])
        self.assertEqual(results[1]['index'], 1)

    def test_equivalent_numeric_answers_share_one_distribution_entry(self):
        values = ["30", "30.0", "3e1", "-0", "0"]
        questions = [{"id": "number", "type": "number", "title": "数值"}]
        responses = [{"answers": [{"question_id": "number", "value": value}]} for value in values]
        result = summarize_responses(questions, responses)[0]
        self.assertEqual(result["number_frequencies"], [("30", 3), ("0", 2)])
        self.assertEqual(result["values"], values)
        self.assertEqual(result["minimum"], "0")

    def test_numeric_answers_default_to_distribution_without_mean(self):
        questions = [{"id": "calculation", "type": "number", "title": "9 的平方是多少？"}]
        responses = [{"answers": [{"question_id": "calculation", "value": value}]}
                     for value in ("81", "81", "72")]
        result = summarize_responses(questions, responses)[0]
        self.assertEqual(result["number_frequencies"], [("81", 2), ("72", 1)])
        self.assertEqual(result["average"], "78")

    def test_repeated_text_and_even_numeric_median(self):
        questions = [
            {"id": "text", "type": "text", "title": "意见"},
            {"id": "number", "type": "number", "title": "数值", "number_statistics": "measure"},
        ]
        responses = [
            {"answers": [{"question_id": "text", "value": "好"},
                         {"question_id": "number", "value": "1"}]},
            {"answers": [{"question_id": "text", "value": "好"},
                         {"question_id": "number", "value": "2"}]},
            {"answers": [{"question_id": "text", "value": None}]},
        ]
        text, number = summarize_responses(questions, responses)
        self.assertEqual(text["reached_count"], 3)
        self.assertEqual(text["blank_count"], 1)
        self.assertEqual(text["text_frequencies"], [("好", 2)])
        self.assertEqual(number["not_included_count"], 1)
        self.assertEqual(number["average"], "1.5")
        self.assertEqual(number["median"], "1.5")


class StatisticsTests(unittest.TestCase):
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

    def sign_in(self):
        client = self.app.test_client()
        name = "student_" + uuid4().hex
        self.post(client, "/register", {
            "username": name, "password": "123456", "confirmation": "123456",
        })
        self.post(client, "/login", {"username": name, "password": "123456"})
        return client

    def test_extreme_numeric_inputs_are_rejected_and_statistics_remain_exact(self):
        from werkzeug.datastructures import MultiDict
        from app.questions import _finite_decimal, build_question, validate_answer
        from app.jumps import build_jump_rule, jump_warnings
        from app.statistics import display_number

        question = {"id": "quantity", "type": "number", "title": "本次数值",
                    "required": True, "number_statistics": "measure"}
        def summarize(values):
            return summarize_responses([question], [
                {"answers": [{"question_id": "quantity", "value": value}]} for value in values])[0]

        with self.subTest(scenario="cancellation"):
            self.assertEqual(summarize(["1e28", "1", "-1e28"])["average"], "0.33")
        with self.subTest(scenario="large_even_median"):
            base = 10 ** 40
            result = summarize([str(base + 1), str(base + 3)])
            self.assertEqual(result["median"], str(base + 2))
            self.assertEqual(result["average"], str(base + 2))
            self.assertEqual(display_number(Decimal(str(base) + ".125")), str(base) + ".13")
        with self.subTest(scenario="supported_precision_boundaries"):
            self.assertEqual(_finite_decimal("1e999", "数值"), Decimal("1e999"))
            self.assertEqual(_finite_decimal("1e-1000", "数值"), Decimal("1e-1000"))
            result = summarize(["1e999", "1", "-1e999"])
            self.assertEqual(result["average"], "0.33")
            self.assertEqual(summarize(["0e1000000", "0e-1000000"])["average"], "0")
            self.assertEqual(validate_answer(question, MultiDict({"q_quantity": "30.00"})), "30.00")

        extreme_values = ("1e1000", "1e1000000", "1e-1001", "1e-1000000", "9" * 2051)
        for value in extreme_values:
            with self.subTest(scenario="validation", value=value[:20]):
                with self.assertRaises(ValueError):
                    validate_answer(question, MultiDict({"q_quantity": value}))
                for field in ("min_value", "max_value"):
                    with self.assertRaises(ValueError):
                        build_question(MultiDict({"type": "number", "title": "边界", field: value}))
                with self.assertRaises(ValueError):
                    build_jump_rule([question, {"id": "next", "type": "text"}], 0,
                                    MultiDict({"target_id": "next", "operator": "gte", "threshold": value}))

        with self.subTest(scenario="legacy_values_do_not_break_statistics"):
            values = ["10", "20", "1e1000000", "1e-1000000", "NaN", {"bad": "shape"}]
            original = deepcopy(values)
            result = summarize(values)
            self.assertEqual(result["average"], "15")
            self.assertEqual(result["answered_count"], 2)
            self.assertEqual(result["invalid_count"], 4)
            self.assertEqual(values, original)
            warnings = jump_warnings([{**question, "integer_only": True, "jumps": [
                {"id": "bad", "kind": "number", "operator": "gte", "threshold": "1e1000000",
                 "target_id": "next"}]}, {"id": "next", "type": "text"}])
            self.assertTrue(warnings["quantity"])

        owner = self.sign_in()
        def api(method, path, data):
            token = owner.get("/api/v1/csrf").get_json()["csrf_token"]
            return owner.open(path, method=method, json=data, headers={"X-CSRF-Token": token})
        created = api("POST", "/api/v1/surveys", {"title": "精确数字统计核对"})
        self.assertEqual(created.status_code, 201)
        survey_id = created.get_json()["survey"]["id"]
        for field in ("min_value", "max_value"):
            invalid = api("POST", f"/api/v1/surveys/{survey_id}/questions",
                          {"type": "number", "title": "边界", field: "1e1000000"})
            self.assertEqual(invalid.status_code, 422)
        added = api("POST", f"/api/v1/surveys/{survey_id}/questions",
                    {"type": "number", "title": "数值", "required": True, "number_statistics": "measure"})
        self.assertEqual(added.status_code, 201)
        question_id = added.get_json()["question"]["id"]
        next_question = api("POST", f"/api/v1/surveys/{survey_id}/questions",
                            {"type": "text", "title": "补充说明"}).get_json()["question"]["id"]
        invalid_jump = api("POST", f"/api/v1/surveys/{survey_id}/questions/{question_id}/jumps",
                           {"operator": "gt", "threshold": "1e1000000", "target_id": next_question})
        self.assertEqual(invalid_jump.status_code, 422)
        self.assertEqual(api("POST", f"/api/v1/surveys/{survey_id}/publish", {}).status_code, 200)
        survey = self.db.surveys.find_one({"title": "精确数字统计核对"})
        for value in extreme_values:
            rejected = api("POST", f"/api/v1/public/surveys/{survey['slug']}/responses",
                           {"revision": survey["revision"], "answers": {question_id: value}})
            self.assertEqual(rejected.status_code, 422, rejected.get_json())
        self.assertEqual(self.db.responses.count_documents({"survey_id": survey["_id"]}), 0)
        for value in ("1e28", "1", "-1e28"):
            accepted = api("POST", f"/api/v1/public/surveys/{survey['slug']}/responses",
                           {"revision": survey["revision"], "answers": {question_id: value}})
            self.assertEqual(accepted.status_code, 201, accepted.get_json())
        # Historical malformed data remains stored, but does not take the statistics page down.
        self.db.responses.insert_one({"survey_id": survey["_id"], "respondent_id": None,
            "answers": [{"question_id": question_id, "question_version": 1, "value": "1e1000000"}]})
        before = list(self.db.responses.find({"survey_id": survey["_id"]}).sort("_id", 1))
        response = owner.get(f"/api/v1/surveys/{survey_id}/statistics/{question_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["results"][0]["average"], "0.33")
        self.assertEqual(response.get_json()["results"][0]["invalid_count"], 1)
        self.assertEqual(owner.get(f"/surveys/{survey_id}/statistics/{question_id}").status_code, 200)
        self.assertEqual(list(self.db.responses.find({"survey_id": survey["_id"]}).sort("_id", 1)), before)

    def test_many_questions_show_statistics_in_pages_without_duplicate_title_index(self):
        owner = self.sign_in()
        self.post(owner, "/surveys/new", {"title": "多题问卷"})
        survey = self.db.surveys.find_one({"title": "多题问卷"})
        detail = f"/surveys/{survey['_id']}"
        self.post(owner, detail + "/questions/new", {
            "type": "text", "title": "题目01", "required": "on",
        })
        question = self.db.surveys.find_one({"_id": survey["_id"]})["questions"][0]
        questions = [{**question, "id": uuid4().hex, "title": f"题目{index:02d}"}
                     for index in range(1, 12)]
        self.db.surveys.update_one({"_id": survey["_id"]}, {"$set": {"questions": questions}})

        detail_page = owner.get(detail).get_data(as_text=True)
        self.assertNotIn('class="question-index"', detail_page)
        self.assertIn('data-question-search', detail_page)

        table = owner.get(detail + "/statistics").get_data(as_text=True)
        self.assertIn("表格总览", table)
        self.assertRegex(table, r'<table\b[^>]*class="[^\"]*\bstatistics-table\b[^\"]*"')
        self.assertIn("题目11", table)
        self.assertNotIn('class="stat-question"', table)
        searched = owner.get(detail + "/statistics?q=题目11").get_data(as_text=True)
        self.assertIn("题目11", searched)
        self.assertNotIn("题目10", searched)

        first = owner.get(detail + "/statistics?view=cards").get_data(as_text=True)
        second = owner.get(detail + "/statistics?view=cards&page=2").get_data(as_text=True)
        self.assertNotIn('class="stat-index"', first)
        self.assertIn("显示 1–10 / 11 题", first)
        self.assertIn("题目10", first)
        self.assertNotIn("题目11", first)
        self.assertIn("显示 11–11 / 11 题", second)
        self.assertIn("题目11", second)
        self.assertNotIn("题目10", second)
        self.assertIn("下一页", first)
        self.assertIn("上一页", second)

        self.db.responses.insert_many({
            "survey_id": survey["_id"], "respondent_id": None,
            "answers": [{"question_id": questions[0]["id"], "question_version": 1,
                         "value": f"回答{index:02d}"}],
        } for index in range(1, 46))
        whole = owner.get(detail + "/statistics?view=cards").get_data(as_text=True)
        self.assertIn("查看全部原始回答", whole)
        self.assertNotIn("回答45", whole)
        question_url = detail + "/statistics/" + questions[0]["id"]
        first_answers = owner.get(question_url).get_data(as_text=True)
        second_answers = owner.get(question_url + "?answer_page=2").get_data(as_text=True)
        self.assertIn("回答01", first_answers)
        self.assertNotIn("回答45", first_answers)
        self.assertIn("回答21", second_answers)
        self.assertNotIn("<li>回答01</li>", second_answers)
        self.assertIn("第 2 / 3 页", second_answers)

    def test_whole_and_single_question_statistics_from_saved_answers(self):
        owner = self.sign_in()
        first = self.sign_in()
        second = self.sign_in()
        self.post(owner, "/surveys/new", {"title": "统计测试"})
        survey = self.db.surveys.find_one({"title": "统计测试"})
        detail = f"/surveys/{survey['_id']}"
        public = f"/survey/{survey['slug']}"
        definitions = [
            {"type": "single_choice", "title": "入口", "required": "on", "options": "A\nB"},
            {"type": "multiple_choice", "title": "水果", "options": "苹果\n香蕉"},
            {"type": "text", "title": "评价"},
            {"type": "number", "title": "分数", "number_statistics": "measure"},
        ]
        for definition in definitions:
            self.post(owner, detail + "/questions/new", definition)
        questions = self.db.surveys.find_one({"_id": survey["_id"]})["questions"]
        self.post(owner, f"{detail}/questions/{questions[0]['id']}/jumps", {
            "option": "A", "target_id": questions[2]["id"],
        })
        self.post(owner, detail + "/publish", {})
        keys = ["q_" + question["id"] for question in questions]
        stats_path = detail + "/statistics"

        self.assertEqual(self.post(first, public, {
            keys[0]: "A", keys[2]: "喜欢", keys[3]: "1",
        }).status_code, 302)
        initial = owner.get(stats_path).get_data(as_text=True)
        self.assertIn("已提交答卷</span><strong>1</strong>", initial)
        self.assertIn("<span>平均值</span><strong>1</strong>", initial)
        self.assertIn("已回答</span><strong>0</strong>", initial)

        self.assertEqual(self.post(second, public, {
            keys[0]: "B", keys[1]: ["苹果", "香蕉"], keys[2]: "一般", keys[3]: "2",
        }).status_code, 302)
        self.assertEqual(self.post(first, public, {
            keys[0]: "B", keys[1]: ["苹果"], keys[3]: "2",
        }).status_code, 302)
        whole = owner.get(stats_path).get_data(as_text=True)
        self.assertIn("已提交答卷</span><strong>3</strong>", whole)
        self.assertIn("已回答</span><strong>3</strong>", whole)
        self.assertIn('<b class="option-result-marker">1.</b> A</span><strong>1 份 · 33.33%', whole)
        self.assertIn('<b class="option-result-marker">2.</b> B</span><strong>2 份 · 66.67%', whole)
        self.assertIn('<b class="option-result-marker">1.</b> 苹果</span><strong>2 次 · 100%', whole)
        self.assertIn('<b class="option-result-marker">2.</b> 香蕉</span><strong>1 次 · 50%', whole)
        self.assertIn("喜欢", whole)
        self.assertIn("一般", whole)
        self.assertIn("<span>平均值</span><strong>1.67</strong>", whole)
        self.assertIn("匿名提交</span><strong>0</strong>", whole)
        self.assertIn("账号提交</span><strong>3</strong>", whole)
        self.assertIn("未包含本题的答卷：1 份", whole)
        self.assertIn("选答未填</span><strong>1</strong>", whole)
        self.assertIn("不同回答：<strong>2</strong> 种", whole)
        self.assertIn("<span>中位数</span><strong>2</strong>", whole)
        self.assertIn("<span>最小值</span><strong>1</strong>", whole)
        self.assertIn("<span>最大值</span><strong>2</strong>", whole)
        self.assertNotIn("填写路径分布", whole)
        overview = owner.get(stats_path + "?view=table").get_data(as_text=True)
        self.assertIn('<span class="statistics-answer-count">3 份</span>', overview)
        self.assertIn('<span class="statistics-answer-count">2 份</span>', overview)
        self.assertIn('<small class="statistics-blank-note">选答未填 1 份</small>', overview)
        self.assertNotRegex(overview, r'data-label="已回答">\s*\d+\s*/')

        for index, expected_count in [(0, 3), (1, 2), (2, 2), (3, 3)]:
            page = owner.get(stats_path + "/" + questions[index]["id"])
            self.assertEqual(page.status_code, 200)
            html = page.get_data(as_text=True)
            self.assertIn(questions[index]["title"], html)
            self.assertIn(f"已回答</span><strong>{expected_count}</strong>", html)
            if index == 0:
                self.assertNotIn("选答未填", html)
                self.assertNotIn("留空", html)
            if index == 2:
                self.assertIn("选答未填</span><strong>1</strong>", html)
            self.assertNotIn("单独查看这道题", html)
        number_page = owner.get(stats_path + "/" + questions[3]["id"]).get_data(as_text=True)
        self.assertIn("<span>平均值</span><strong>1.67</strong>", number_page)
        self.assertEqual(second.get(stats_path).status_code, 404)
        self.assertEqual(second.get(stats_path + "/" + questions[0]["id"]).status_code, 404)
        self.assertEqual(owner.get(stats_path + "/missing").status_code, 404)


if __name__ == "__main__":
    unittest.main()
