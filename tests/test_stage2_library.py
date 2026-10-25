import os
import re
import unittest
from uuid import uuid4
from unittest.mock import patch

from bson import ObjectId

from app import create_app


class LibraryFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_name = "survey_phase2_test_" + uuid4().hex
        os.environ["MONGO_DB_NAME"] = cls.db_name
        cls.app = create_app()
        cls.db = cls.app.extensions["mongo_client"][cls.db_name]

    @classmethod
    def tearDownClass(cls):
        cls.app.extensions["mongo_client"].drop_database(cls.db_name)
        cls.app.extensions["mongo_client"].close()
        os.environ.pop("MONGO_DB_NAME", None)

    def post(self, client, path, data):
        client.get("/api/v1/csrf")
        with client.session_transaction() as saved:
            token = saved["csrf_token"]
        return client.post(path, data={"csrf_token": token, **data})

    def api(self, client, method, path, data):
        client.get("/api/v1/csrf")
        with client.session_transaction() as saved:
            token = saved["csrf_token"]
        return client.open(path, method=method, json=data, headers={"X-CSRF-Token": token})

    def login(self, client, name):
        self.post(client, "/register", {"username": name, "password": "123456", "confirmation": "123456"})
        self.post(client, "/login", {"username": name, "password": "123456"})

    def survey(self, client, owner_name, title):
        self.post(client, "/surveys/new", {"title": title, "allow_anonymous": "on"})
        owner = self.db.users.find_one({"username": owner_name})
        return self.db.surveys.find_one({"owner_id": owner["_id"], "title": title})

    def test_reuse_sharing_versions_banks_statistics_and_legacy(self):
        maker = self.app.test_client()
        colleague = self.app.test_client()
        maker_name = "maker_" + uuid4().hex
        colleague_name = "colleague_" + uuid4().hex
        self.login(maker, maker_name)
        self.login(colleague, colleague_name)

        source = self.api(maker, "POST", "/api/v1/library/questions", {
            "type": "single_choice", "title": "喜欢的季节", "required": True,
            "options": ["春", "夏", "秋", "冬"]}).get_json()["question"]
        question_id = source["id"]
        self.assertEqual(maker.get("/question-library").status_code, 200)
        self.assertEqual(maker.get("/question-library/new").status_code, 200)
        self.assertEqual(maker.get(f"/question-library/{question_id}").status_code, 200)
        self.assertEqual(colleague.get(f"/api/v1/library/questions/{question_id}").status_code, 404)
        survey_one = self.survey(maker, maker_name, "旧版问卷")
        self.db.surveys.update_one({"_id": survey_one["_id"]}, {"$set": {"option_label_style": "letters"}})
        choice_html = maker.get(f"/surveys/{survey_one['_id']}/question-library").get_data(as_text=True)
        self.assertIn('class="library-option-marker">A.', choice_html)
        attached = self.api(maker, "POST", f"/api/v1/surveys/{survey_one['_id']}/library-questions",
                            {"question_id": question_id})
        self.assertEqual(attached.status_code, 201)
        linked = self.db.surveys.find_one({"_id": survey_one["_id"]})["questions"][0]
        self.post(maker, f"/surveys/{survey_one['_id']}/questions/{linked['id']}/edit", {
            "type": "single_choice", "title": "喜欢的季节", "required": "on",
            "options": "春\n夏\n秋\n冬"})
        unchanged = self.db.surveys.find_one({"_id": survey_one["_id"]})["questions"][0]
        self.assertEqual(str(unchanged["library_question_id"]), question_id)
        self.assertEqual(unchanged["version"], linked["version"])
        no_op = self.api(maker, "PATCH", f"/api/v1/surveys/{survey_one['_id']}/questions/{linked['id']}", {})
        self.assertEqual(no_op.status_code, 200)
        self.assertEqual(str(self.db.surveys.find_one({"_id": survey_one['_id']})["questions"][0]["library_question_id"]), question_id)
        self.assertEqual(maker.get(f"/api/v1/surveys/{survey_one['_id']}").status_code, 200)
        self.post(maker, f"/surveys/{survey_one['_id']}/publish", {})

        version_two = self.api(maker, "POST", f"/api/v1/library/questions/{question_id}/versions", {
            "type": "single_choice", "title": "你最喜欢哪个季节？", "required": True,
            "options": ["春天", "夏天", "秋天", "冬天"]}).get_json()["question"]
        self.assertEqual(maker.get(f"/question-library/{question_id}/version").status_code, 200)
        self.assertIn("与来源版本比较", maker.get(f"/question-library/{version_two['id']}").get_data(as_text=True))
        self.assertEqual(maker.get(f"/api/v1/library/questions/{version_two['id']}/statistics").get_json()["answered_count"], 0)
        old = self.db.surveys.find_one({"_id": survey_one["_id"]})
        self.assertEqual(old["questions"][0]["title"], "喜欢的季节")
        self.assertEqual(old["questions"][0]["options"][0], "春")
        self.assertEqual(maker.get(f"/surveys/{survey_one['_id']}/questions/{old['questions'][0]['id']}/edit").status_code, 409)
        self.assertEqual(self.api(maker, "POST", f"/api/v1/surveys/{survey_one['_id']}/library-questions",
                                  {"question_id": version_two["id"]}).status_code, 409)

        self.api(maker, "PATCH", f"/api/v1/library/questions/{question_id}/sharing", {"shared": True})
        self.assertEqual(colleague.get(f"/api/v1/library/questions/{question_id}").status_code, 200)
        survey_two = self.survey(colleague, colleague_name, "共享题问卷")
        attached_two = self.api(colleague, "POST", f"/api/v1/surveys/{survey_two['_id']}/library-questions",
                                {"question_id": question_id})
        self.assertEqual(attached_two.status_code, 201)
        self.post(colleague, f"/surveys/{survey_two['_id']}/publish", {})

        for client, survey, answer in ((colleague, old, "春"), (maker, survey_two, "夏")):
            public_path = f"/survey/{survey['slug']}"
            page = client.get(public_path).get_data(as_text=True)
            token = re.search(r'name="question_snapshot" value="([^"]+)"', page).group(1)
            current = self.db.surveys.find_one({"_id": survey["_id"]})
            response = self.post(client, public_path, {
                "question_snapshot": token, "q_" + current["questions"][0]["id"]: answer})
            self.assertEqual(response.status_code, 302)
        stats = maker.get(f"/api/v1/library/questions/{question_id}/statistics").get_json()
        self.assertEqual(stats["survey_count"], 2)
        self.assertEqual(stats["answered_count"], 2)
        self.assertEqual(stats["option_counts"]["春"], 1)
        self.assertEqual(stats["option_counts"]["夏"], 1)

        bank = self.api(maker, "POST", "/api/v1/library/banks", {"name": "常用调查"}).get_json()
        self.assertEqual(self.api(maker, "POST", f"/api/v1/library/banks/{bank['id']}/questions",
                                  {"question_id": question_id}).status_code, 200)
        self.assertIn("比较新旧版内容", maker.get(f"/question-banks/{bank['id']}").get_data(as_text=True))
        self.post(maker, f"/question-banks/{bank['id']}/questions/{question_id}",
                  {"action": "update", "target_id": version_two["id"]})
        bank_doc = self.db.question_banks.find_one({"name": "常用调查"})
        self.assertEqual(str(bank_doc["question_ids"][0]), version_two["id"])
        self.assertEqual(maker.get(f"/question-banks/{bank['id']}").status_code, 200)
        renamed = self.api(maker, "PATCH", f"/api/v1/library/banks/{bank['id']}", {"name": "常用题"})
        self.assertEqual(renamed.get_json()["name"], "常用题")
        restored = self.api(maker, "POST", f"/api/v1/library/questions/{question_id}/restore", {}).get_json()["question"]
        self.assertEqual(restored["version"], 3)
        self.assertEqual(restored["parent_id"], question_id)
        self.assertEqual(self.db.surveys.find_one({"_id": survey_one["_id"]})["questions"][0]["title"], "喜欢的季节")
        self.assertEqual(self.api(maker, "DELETE", f"/api/v1/library/banks/{bank['id']}", {}).status_code, 200)
        self.assertIsNotNone(self.db.question_library.find_one({"_id": self.db.surveys.find_one({"_id": survey_one["_id"]})["questions"][0]["library_question_id"]}))

    def test_existing_question_can_be_saved_without_changing_old_answers(self):
        owner = self.app.test_client()
        visitor = self.app.test_client()
        name = "legacy_" + uuid4().hex
        self.login(owner, name)
        self.login(visitor, "legacy_visitor_" + uuid4().hex)
        survey = self.survey(owner, name, "第一阶段旧问卷")
        detail = f"/surveys/{survey['_id']}"
        self.post(owner, detail + "/questions/new", {
            "type": "text", "title": "原有问题", "required": "on"})
        question = self.db.surveys.find_one({"_id": survey["_id"]})["questions"][0]
        self.post(owner, detail + "/publish", {})
        public = f"/survey/{survey['slug']}"
        page = visitor.get(public).get_data(as_text=True)
        snapshot = re.search(r'name="question_snapshot" value="([^"]+)"', page).group(1)
        self.assertEqual(self.post(visitor, public, {"question_snapshot": snapshot,
            "q_" + question["id"]: "旧回答"}).status_code, 302)
        initial_revision = self.db.surveys.find_one({"_id": survey["_id"]})["revision"]
        saved = self.post(owner, detail + f"/questions/{question['id']}/save-to-library", {})
        self.assertEqual(saved.status_code, 302)
        self.assertIn(f"from_survey={survey['_id']}", saved.headers["Location"])
        self.assertIn(f"from_question={question['id']}", saved.headers["Location"])
        source_page = owner.get(saved.headers["Location"]).get_data(as_text=True)
        self.assertIn(f'{detail}#question-{question["id"]}', source_page)
        current = self.db.surveys.find_one({"_id": survey["_id"]})
        self.assertEqual(current["revision"], initial_revision)
        self.assertEqual(current["questions"][0]["title"], "原有问题")
        item_id = current["questions"][0]["library_question_id"]
        self.assertEqual(owner.get(f"/question-library/{item_id}").status_code, 200)
        self.assertEqual(owner.get(f"/api/v1/library/questions/{item_id}/statistics").get_json()["answered_count"], 1)
        self.assertEqual(self.post(owner, detail + f"/questions/{question['id']}/save-to-library", {}).status_code, 302)
        self.assertEqual(self.db.question_library.count_documents({"_id": item_id}), 1)

    def test_removed_draft_question_keeps_filling_review_and_statistics_numbering_consistent(self):
        owner, visitor = self.app.test_client(), self.app.test_client()
        name = "numbering_" + uuid4().hex
        self.login(owner, name)
        self.login(visitor, "numbering_visitor_" + uuid4().hex)
        survey = self.survey(owner, name, "删题后的编号与答卷")
        detail = f"/surveys/{survey['_id']}"
        questions = [self.api(owner, "POST", f"/api/v1/surveys/{survey['_id']}/questions", definition).get_json()["question"]
                     for definition in (
                         {"type": "text", "title": "不再使用的问题", "required": True},
                         {"type": "single_choice", "title": "你喜欢哪种学习方式？", "required": True,
                          "options": ["阅读资料", "动手实践"]},
                         {"type": "text", "title": "还有什么建议？", "required": True})]
        self.post(owner, detail + f"/questions/{questions[0]['id']}/delete", {})
        self.db.surveys.update_one({"_id": survey['_id']}, {"$set": {"option_label_style": "letters"}})
        self.post(owner, detail + "/publish", {})
        survey = self.db.surveys.find_one({"_id": survey['_id']})
        submitted = self.api(visitor, "POST", f"/api/v1/public/surveys/{survey['slug']}/responses", {
            "revision": survey['revision'], "identity_mode": "named",
            "answers": {questions[1]['id']: "动手实践", questions[2]['id']: "希望增加案例"}})
        self.assertEqual(submitted.status_code, 201)
        response_id = submitted.get_json()['response_id']
        review = visitor.get(f"/responses/{response_id}").get_data(as_text=True)
        with self.subTest("review numbering"):
            headings = [re.sub(r'<[^>]+>', '', heading).strip()
                        for heading in re.findall(r'<h2\b[^>]*>(.*?)</h2>', review, re.S)]
            self.assertIn('1你喜欢哪种学习方式？', headings)
            self.assertIn('2还有什么建议？', headings)
        with self.subTest("option labels"):
            self.assertIn('class="review-option-marker">B.</span>', review)
        with self.subTest("statistics numbering"):
            results = owner.get(f"/api/v1/surveys/{survey['_id']}/statistics").get_json()['results']
            self.assertEqual([row['index'] for row in results if not row['historical']], [1, 2])
            self.assertEqual(len(results), 2)
        # A page containing only free-text answers must not offer an ineffective options toggle.
        text_survey = self.survey(owner, name, "只有填空题的问卷")
        text_question = self.api(owner, "POST", f"/api/v1/surveys/{text_survey['_id']}/questions", {
            "type": "text", "title": "请留下建议", "required": True}).get_json()['question']
        self.post(owner, f"/surveys/{text_survey['_id']}/publish", {})
        text_survey = self.db.surveys.find_one({"_id": text_survey['_id']})
        text_response = self.api(visitor, "POST", f"/api/v1/public/surveys/{text_survey['slug']}/responses", {
            "revision": text_survey['revision'], "answers": {text_question['id']: "建议增加案例"}})
        text_response_id = text_response.get_json()['response_id']
        with self.subTest("text-only review"):
            self.assertNotIn('data-review-toggle', visitor.get(f"/responses/{text_response_id}").get_data(as_text=True))

    def test_save_to_library_rejects_a_question_changed_during_saving(self):
        owner = self.app.test_client()
        name = "save_source_" + uuid4().hex
        self.login(owner, name)
        for via_api in (True, False):
            with self.subTest(via_api=via_api):
                survey = self.survey(owner, name, "入库时另一页改题_" + str(via_api))
                question = self.api(owner, "POST", f"/api/v1/surveys/{survey['_id']}/questions", {
                    "type": "text", "title": "原始问题", "required": True}).get_json()['question']
                initial_count = self.db.question_library.count_documents({})
                collection_class = type(self.db.surveys)
                real_update = collection_class.update_one
                injected = []

                def edit_before_link(collection, query, update, *args, **kwargs):
                    if collection.name == "surveys" and "questions.$.library_question_id" in update.get("$set", {}) and not injected:
                        injected.append(True)
                        real_update(collection, {"_id": survey['_id']}, {
                            "$set": {"questions.0.title": "另一页保存的新问题", "questions.0.version": 2},
                            "$inc": {"revision": 1}})
                    return real_update(collection, query, update, *args, **kwargs)

                path = f"/surveys/{survey['_id']}/questions/{question['id']}/save-to-library"
                with patch.object(collection_class, "update_one", new=edit_before_link):
                    result = (self.api(owner, "POST", "/api/v1" + path, {}) if via_api
                              else self.post(owner, path, {}))
                self.assertTrue(injected)
                with self.subTest("no wrong source"):
                    stored = self.db.surveys.find_one({"_id": survey['_id']})['questions'][0]
                    self.assertEqual(stored['title'], "另一页保存的新问题")
                    self.assertNotIn('library_question_id', stored)
                    self.assertEqual(self.db.question_library.count_documents({}), initial_count)
                with self.subTest("usable conflict feedback"):
                    self.assertEqual(result.status_code, 409 if via_api else 302)
                    if via_api:
                        self.assertIn("其他页面", result.get_json()['error'])
                    else:
                        self.assertEqual(result.headers['Location'], f"/surveys/{survey['_id']}#question-{question['id']}")
                        self.assertIn("本次未入库", owner.get(result.headers['Location']).get_data(as_text=True))

    def test_withdrawn_shared_question_during_bank_add_preserves_selection(self):
        maker, colleague = self.app.test_client(), self.app.test_client()
        self.login(maker, "bank_maker_" + uuid4().hex)
        self.login(colleague, "bank_user_" + uuid4().hex)
        item = self.api(maker, "POST", "/api/v1/library/questions", {
            "type": "text", "title": "课程建议共享题", "required": True}).get_json()['question']
        self.api(maker, "PATCH", f"/api/v1/library/questions/{item['id']}/sharing", {"shared": True})
        bank = self.api(colleague, "POST", "/api/v1/library/banks", {"name": "同事共享题库"}).get_json()
        page = colleague.get(f"/question-library?bank={bank['id']}&q=课程").get_data(as_text=True)
        self.assertIn("课程建议共享题", page)
        self.api(maker, "PATCH", f"/api/v1/library/questions/{item['id']}/sharing", {"shared": False})
        result = self.post(colleague, f"/question-banks/{bank['id']}/questions/{item['id']}", {
            "continue": "yes", "q": "课程", "mine_page": "1", "shared_page": "2"})
        self.assertEqual(result.status_code, 302)
        self.assertIn(f"bank={bank['id']}", result.headers['Location'])
        self.assertIn("shared_page=2", result.headers['Location'])
        following = colleague.get(result.headers['Location']).get_data(as_text=True)
        self.assertIn('value="课程"', following)
        self.assertIn("本次没有加入题库", following)
        self.assertEqual(self.db.question_banks.find_one({"_id": ObjectId(bank['id'])})['question_ids'], [])

    def test_bank_name_validation_retains_input_and_opens_the_correct_form(self):
        owner = self.app.test_client()
        self.login(owner, "bank_names_" + uuid4().hex)
        first = self.api(owner, "POST", "/api/v1/library/banks", {"name": "课堂调查"}).get_json()
        self.api(owner, "POST", "/api/v1/library/banks", {"name": "活动调查"})
        for path, value, form_id, details_class in (
            ("/question-banks", "课堂调查", "bank-name", "library-bank-create"),
            (f"/question-banks/{first['id']}/rename", "活动调查", "rename-bank", "library-bank-settings"),
        ):
            with self.subTest(path=path):
                result = self.post(owner, path, {"name": value})
                self.assertEqual(result.status_code, 422)
                html = result.get_data(as_text=True)
                self.assertIn("你已经有同名题库", html)
                self.assertIn(f'id="{form_id}" name="name" value="{value}"', html)
                self.assertIn(f'class="{details_class}" open', html)
                self.assertIn('role="alert"', html)
        self.assertEqual(self.db.question_banks.find_one({"_id": ObjectId(first['id'])})['name'], "课堂调查")

    def test_continuous_selection_duplicate_guard_and_shared_answer_privacy(self):
        maker = self.app.test_client()
        colleague = self.app.test_client()
        maker_name = "ux_maker_" + uuid4().hex
        colleague_name = "ux_colleague_" + uuid4().hex
        self.login(maker, maker_name)
        self.login(colleague, colleague_name)
        item = self.api(maker, "POST", "/api/v1/library/questions", {
            "type": "text", "title": "你有什么建议？", "required": True}).get_json()["question"]
        item_id = item["id"]
        self.api(maker, "PATCH", f"/api/v1/library/questions/{item_id}/sharing", {"shared": True})
        own_survey = self.survey(maker, maker_name, "我的建议调查")
        other_survey = self.survey(colleague, colleague_name, "同事的建议调查")
        selection = f"/surveys/{own_survey['_id']}/question-library"
        page = maker.get(selection)
        self.assertIn("加入并继续选题", page.get_data(as_text=True))
        detail = maker.get(f"/question-library/{item_id}?survey={own_survey['_id']}")
        self.assertIn("返回当前问卷选题", detail.get_data(as_text=True))
        added = self.post(maker, f"{selection}/{item_id}", {"continue": "yes"})
        self.assertIn(selection, added.headers["Location"])
        self.assertIn("已加入这份问卷", maker.get(selection).get_data(as_text=True))
        self.post(maker, f"{selection}/{item_id}", {"continue": "yes"})
        self.assertEqual(len(self.db.surveys.find_one({"_id": own_survey["_id"]})["questions"]), 1)
        duplicate = self.api(maker, "POST", f"/api/v1/surveys/{own_survey['_id']}/library-questions",
                             {"question_id": item_id})
        self.assertEqual(duplicate.status_code, 409)
        self.api(colleague, "POST", f"/api/v1/surveys/{other_survey['_id']}/library-questions",
                 {"question_id": item_id})
        for client, survey, answer in ((maker, own_survey, "只给我的问卷的内容"),
                                       (colleague, other_survey, "只给同事问卷的内容")):
            self.post(client, f"/surveys/{survey['_id']}/publish", {})
            public = f"/survey/{survey['slug']}"
            page = client.get(public).get_data(as_text=True)
            snapshot = re.search(r'name="question_snapshot" value="([^"]+)"', page).group(1)
            question = self.db.surveys.find_one({"_id": survey["_id"]})["questions"][0]
            self.assertEqual(self.post(client, public, {"question_snapshot": snapshot,
                             "q_" + question["id"]: answer}).status_code, 302)
        maker_stats = maker.get(f"/api/v1/library/questions/{item_id}/statistics").get_json()
        colleague_stats = colleague.get(f"/api/v1/library/questions/{item_id}/statistics").get_json()
        self.assertEqual(maker_stats["answered_count"], 2)
        self.assertEqual(colleague_stats["answered_count"], 2)
        self.assertEqual(maker_stats["values"], ["只给我的问卷的内容"])
        self.assertEqual(colleague_stats["values"], ["只给同事问卷的内容"])
        self.assertEqual(maker_stats["values_scope"], "own_surveys")
        self.assertNotIn("只给同事问卷的内容", maker.get(f"/question-library/{item_id}").get_data(as_text=True))

    def test_survey_specific_jump_does_not_break_library_statistics(self):
        owner = self.app.test_client()
        visitor = self.app.test_client()
        name = "jump_owner_" + uuid4().hex
        self.login(owner, name)
        self.login(visitor, "jump_visitor_" + uuid4().hex)
        item = self.api(owner, "POST", "/api/v1/library/questions", {
            "type": "single_choice", "title": "是否参加", "required": True,
            "options": ["是", "否"]}).get_json()["question"]
        survey = self.survey(owner, name, "题库跳转问卷")
        detail = f"/surveys/{survey['_id']}"
        self.api(owner, "POST", f"/api/v1/surveys/{survey['_id']}/library-questions",
                 {"question_id": item["id"]})
        self.post(owner, detail + "/questions/new", {"type": "text", "title": "说明"})
        questions = self.db.surveys.find_one({"_id": survey["_id"]})["questions"]
        self.post(owner, f"{detail}/questions/{questions[0]['id']}/jumps", {
            "option": "是", "target_id": questions[1]["id"]})
        self.post(owner, detail + "/publish", {})
        public = f"/survey/{survey['slug']}"
        page = visitor.get(public).get_data(as_text=True)
        snapshot = re.search(r'name="question_snapshot" value="([^"]+)"', page).group(1)
        submitted = self.post(visitor, public, {
            "question_snapshot": snapshot, "q_" + questions[0]["id"]: "是",
            "q_" + questions[1]["id"]: "会参加"})
        self.assertEqual(submitted.status_code, 302)
        stats = owner.get(f"/api/v1/library/questions/{item['id']}/statistics").get_json()
        self.assertEqual(stats["answered_count"], 1)
        self.assertEqual(stats["option_counts"]["是"], 1)

    def test_create_and_attach_reports_partial_save_and_allows_recovery(self):
        import app.question_bank as bank_module

        owner = self.app.test_client()
        name = "creation_race_" + uuid4().hex
        self.login(owner, name)
        survey = self.survey(owner, name, "正在编辑的问卷")
        real_make_version = bank_module.make_version

        def change_survey_after_save(source, form):
            item = real_make_version(source, form)
            self.db.surveys.update_one({"_id": survey["_id"]}, {
                "$set": {"title": "另一页保存的问卷名称"}, "$inc": {"revision": 1}})
            return item

        with patch.object(bank_module, "make_version", side_effect=change_survey_after_save):
            saved = self.post(owner, "/question-library/new", {
                "survey": str(survey["_id"]), "type": "text", "title": "这节课哪些内容最有帮助？"})
        item = self.db.question_library.find_one({"owner_id": survey["owner_id"], "title": "这节课哪些内容最有帮助？"})
        self.assertIsNotNone(item)
        self.assertEqual(saved.status_code, 302)
        self.assertIn(f"/question-library/{item['_id']}", saved.headers["Location"])
        page = owner.get(saved.headers["Location"]).get_data(as_text=True)
        self.assertIn("题目已保存到题目库，但问卷已发生变化，本次没有加入", page)
        self.assertIn("加入并继续选题", page)
        self.assertEqual(self.db.surveys.find_one({"_id": survey["_id"]})["questions"], [])
        retry = self.post(owner, f"/surveys/{survey['_id']}/question-library/{item['_id']}", {"continue": "yes"})
        self.assertEqual(retry.status_code, 302)
        self.assertEqual(len(self.db.surveys.find_one({"_id": survey["_id"]})["questions"]), 1)
        self.assertEqual(self.db.question_library.count_documents({"_id": item["_id"]}), 1)

        bank = self.api(owner, "POST", "/api/v1/library/banks", {"name": "待整理题库"}).get_json()

        def remove_bank_after_save(source, form):
            item = real_make_version(source, form)
            self.db.question_banks.delete_one({"_id": ObjectId(bank["id"])})
            return item

        with patch.object(bank_module, "make_version", side_effect=remove_bank_after_save):
            saved = self.post(owner, "/question-library/new", {
                "bank": bank["id"], "type": "text", "title": "还希望了解哪些内容？"})
        item = self.db.question_library.find_one({"owner_id": survey["owner_id"], "title": "还希望了解哪些内容？"})
        self.assertEqual(saved.status_code, 302)
        self.assertIn(f"/question-library/{item['_id']}", saved.headers["Location"])
        page = owner.get(saved.headers["Location"])
        self.assertEqual(page.status_code, 200)
        self.assertIn("题目已保存到题目库，但原题库已被删除，本次没有加入", page.get_data(as_text=True))
        self.assertNotIn("题目已保存并加入题库", page.get_data(as_text=True))

    def test_version_preview_keeps_source_survey_option_labels(self):
        owner = self.app.test_client()
        name = "preview_labels_" + uuid4().hex
        self.login(owner, name)
        survey = self.survey(owner, name, "字母选项问卷")
        self.db.surveys.update_one({"_id": survey["_id"]}, {"$set": {"option_label_style": "letters"}})
        item = self.api(owner, "POST", "/api/v1/library/questions", {
            "type": "single_choice", "title": "学习方式", "required": True,
            "options": ["阅读", "实践"]}).get_json()["question"]
        self.api(owner, "POST", f"/api/v1/surveys/{survey['_id']}/library-questions", {"question_id": item["id"]})
        linked = self.db.surveys.find_one({"_id": survey["_id"]})["questions"][0]
        for context in (f"survey={survey['_id']}", f"from_survey={survey['_id']}&from_question={linked['id']}"):
            with self.subTest(context=context):
                detail = owner.get(f"/question-library/{item['id']}?{context}").get_data(as_text=True)
                self.assertIn('class="library-option-marker">A.', detail)
                form = owner.get(f"/question-library/{item['id']}/version?{context}").get_data(as_text=True)
                self.assertIn('data-option-style="letters"', form)
                error = self.post(owner, f"/question-library/{item['id']}/version?{context}", {
                    "type": "single_choice", "title": "学习方式", "required": "on", "options": "阅读\n实践"})
                self.assertIn("题目内容没有变化", error.get_data(as_text=True))
                self.assertIn('data-option-style="letters"', error.get_data(as_text=True))
        standalone = owner.get(f"/question-library/{item['id']}/version").get_data(as_text=True)
        self.assertIn('data-option-style="numeric"', standalone)
        colleague = self.app.test_client()
        self.login(colleague, "preview_colleague_" + uuid4().hex)
        private_item = self.api(colleague, "POST", "/api/v1/library/questions", {
            "type": "text", "title": "自己的题目"}).get_json()["question"]
        self.assertEqual(colleague.get(f"/question-library/{private_item['id']}/version?from_survey={survey['_id']}").status_code, 404)

    def test_published_question_old_form_keeps_input_for_separate_save(self):
        owner = self.app.test_client()
        name = "published_form_" + uuid4().hex
        self.login(owner, name)
        for linked_source in (False, True):
            with self.subTest(linked_source=linked_source):
                survey = self.survey(owner, name, "发布后旧表单" + str(linked_source))
                if linked_source:
                    item = self.api(owner, "POST", "/api/v1/library/questions", {
                        "type": "text", "title": "原始问题", "required": True}).get_json()["question"]
                    self.api(owner, "POST", f"/api/v1/surveys/{survey['_id']}/library-questions", {"question_id": item["id"]})
                else:
                    self.api(owner, "POST", f"/api/v1/surveys/{survey['_id']}/questions", {
                        "type": "text", "title": "原始问题", "required": True})
                current = self.db.surveys.find_one({"_id": survey["_id"]})
                question = current["questions"][0]
                path = f"/surveys/{survey['_id']}/questions/{question['id']}/edit"
                self.assertEqual(owner.get(path).status_code, 200)
                self.assertEqual(self.post(owner, f"/surveys/{survey['_id']}/publish", {}).status_code, 302)
                edited = {"type": "text", "title": "已经输入但还未保存的内容", "required": "on", "min_length": "3"}
                result = self.post(owner, path, edited)
                self.assertEqual(result.status_code, 409)
                html = result.get_data(as_text=True)
                self.assertIn("问卷已发布，不能再修改其中的题目", html)
                self.assertIn('value="已经输入但还未保存的内容"', html)
                self.assertIn('value="3"', html)
                self.assertNotIn("保存后，本题的新版本用于之后的填写", html)
                action = f"/question-library/{item['id']}/version" if linked_source else "/question-library/new"
                self.assertIn(f'action="{action}', html)
                saved = self.post(owner, action, edited)
                self.assertEqual(saved.status_code, 302)
                self.assertIn("/question-library/", saved.headers["Location"])
                original = self.db.surveys.find_one({"_id": survey["_id"]})["questions"][0]
                self.assertEqual(original, question)
                self.assertEqual(self.db.question_library.count_documents({
                    "owner_id": survey["owner_id"], "title": edited["title"]}), int(linked_source) + 1)

    def test_contextual_creation_paging_and_stopped_sharing(self):
        owner = self.app.test_client()
        colleague = self.app.test_client()
        name = "context_owner_" + uuid4().hex
        other = "context_other_" + uuid4().hex
        self.login(owner, name)
        self.login(colleague, other)
        bank_id = self.api(owner, "POST", "/api/v1/library/banks", {"name": "课堂常用"}).get_json()["id"]
        bank_form = owner.get(f"/question-library/new?bank={bank_id}").get_data(as_text=True)
        self.assertIn("保存并加入题库", bank_form)
        saved = self.post(owner, "/question-library/new", {
            "type": "text", "title": "题库中新建的问题", "required": "on", "bank": bank_id})
        self.assertIn(f"/question-banks/{bank_id}", saved.headers["Location"])
        bank = self.db.question_banks.find_one({"name": "课堂常用"})
        self.assertEqual(len(bank["question_ids"]), 1)

        survey = self.survey(owner, name, "当前正在编辑的问卷")
        survey_id = str(survey["_id"])
        choose = owner.get(f"/surveys/{survey_id}/question-library").get_data(as_text=True)
        self.assertIn(f"/question-library/new?survey={survey_id}", choose)
        survey_form = owner.get(f"/question-library/new?survey={survey_id}").get_data(as_text=True)
        self.assertIn("保存并加入问卷", survey_form)
        self.db.surveys.update_one({"_id": survey["_id"]}, {"$set": {"option_label_style": "letters"}})
        survey_form = owner.get(f"/question-library/new?survey={survey_id}").get_data(as_text=True)
        self.assertIn('data-option-style="letters"', survey_form)
        attached = self.post(owner, "/question-library/new", {
            "type": "text", "title": "直接加入当前问卷", "required": "on", "survey": survey_id})
        self.assertIn(f"/surveys/{survey_id}/question-library", attached.headers["Location"])
        self.assertEqual(len(self.db.surveys.find_one({"_id": survey["_id"]})["questions"]), 1)
        self.assertEqual(colleague.get(f"/question-library/new?survey={survey_id}").status_code, 404)

        for number in range(14):
            created = self.api(owner, "POST", "/api/v1/library/questions", {
                "type": "text", "title": f"分页查找_{number:02d}", "required": True})
            self.assertEqual(created.status_code, 201)
        first = owner.get("/question-library?q=分页查找").get_data(as_text=True)
        second = owner.get("/question-library?q=分页查找&mine_page=2").get_data(as_text=True)
        self.assertIn("第 1 / 2 页", first)
        self.assertIn("第 2 / 2 页", second)
        self.assertIn("分页查找_00", second)
        self.assertNotIn("分页查找_13", second)
        self.assertIn("mine_page=2", first)
        bank_add = owner.get(f"/question-library?bank={bank_id}&q=题库中新建的问题").get_data(as_text=True)
        self.assertIn('value="题库中新建的问题"', bank_add)
        selected = self.api(owner, "POST", "/api/v1/library/questions", {
            "type": "text", "title": "题库中新建的问题扩展版", "required": True}).get_json()["question"]
        chosen = self.post(owner, f"/surveys/{survey_id}/question-library/{selected['id']}", {
            "continue": "yes", "q": "扩展版", "mine_page": "2"})
        self.assertTrue(chosen.headers["Location"].endswith(f"#library-question-{selected['root_id']}"))
        chosen_html = owner.get(f"/surveys/{survey_id}/question-library?q=扩展版").get_data(as_text=True)
        self.assertIn(f'id="library-question-{selected["root_id"]}"', chosen_html)
        self.assertIn("已从题目库选用 2 道题", chosen_html)
        linked = self.db.surveys.find_one({"_id": survey["_id"]})["questions"][-1]
        source_page = owner.get(f"/surveys/{survey_id}").get_data(as_text=True)
        self.assertIn(f"from_survey={survey_id}", source_page)
        version_page = owner.get(
            f"/question-library/{selected['id']}?from_survey={survey_id}&from_question={linked['id']}")
        self.assertIn(f"/surveys/{survey_id}#question-{linked['id']}",
                      version_page.get_data(as_text=True))
        self.assertIn("只修改这份问卷", version_page.get_data(as_text=True))
        self.assertIn("创建题库新版本", version_page.get_data(as_text=True))
        self.assertIn(f"/surveys/{survey_id}/questions/{linked['id']}/edit",
                      version_page.get_data(as_text=True))
        back = self.post(owner, f"/question-banks/{bank_id}/questions/{selected['id']}", {
            "continue": "yes", "q": "扩展版", "mine_page": "2"})
        self.assertIn("q=%E6%89%A9%E5%B1%95%E7%89%88", back.headers["Location"])
        self.assertIn("mine_page=2", back.headers["Location"])
        self.assertTrue(back.headers["Location"].endswith(f"#library-question-{selected['root_id']}"))
        bank_list = owner.get(f"/question-library?bank={bank_id}&q=扩展版").get_data(as_text=True)
        self.assertIn(f'id="library-question-{selected["root_id"]}"', bank_list)
        self.post(owner, f"/surveys/{survey_id}/questions/{linked['id']}/edit", {
            "type": "text", "title": "只在当前问卷修改", "required": "on"})
        changed = next(row for row in self.db.surveys.find_one({"_id": survey["_id"]})["questions"]
                       if row["id"] == linked["id"])
        self.assertEqual(changed["title"], "只在当前问卷修改")
        self.assertNotIn("library_question_id", changed)
        self.assertEqual(self.db.question_library.find_one({"_id": ObjectId(selected["id"])})["title"],
                         "题库中新建的问题扩展版")
        self.assertEqual(owner.get("/question-library?q=分页查找&mine_page=99").status_code, 200)

        shared_id = str(bank["question_ids"][0])
        self.api(owner, "PATCH", f"/api/v1/library/questions/{shared_id}/sharing", {"shared": True})
        used = self.survey(colleague, other, "已经使用共享题的问卷")
        self.api(colleague, "POST", f"/api/v1/surveys/{used['_id']}/library-questions",
                 {"question_id": shared_id})
        self.api(owner, "PATCH", f"/api/v1/library/questions/{shared_id}/sharing", {"shared": False})
        another = self.survey(colleague, other, "新问卷")
        detail = colleague.get(f"/question-library/{shared_id}?survey={another['_id']}")
        self.assertEqual(detail.status_code, 200)
        html = detail.get_data(as_text=True)
        self.assertIn("已停止共享，仅供查看已有问卷", html)
        self.assertIn("不能加入新问卷", html)
        self.assertNotIn("加入并继续选题", html)

    def test_version_actions_keep_context_and_reject_unchanged_content(self):
        owner = self.app.test_client()
        name = "version_context_" + uuid4().hex
        self.login(owner, name)
        item = self.api(owner, "POST", "/api/v1/library/questions", {
            "type": "number", "title": "每周学习时长", "required": True,
            "min_value": "0", "max_value": "100", "number_statistics": "measure"}).get_json()["question"]
        survey = self.survey(owner, name, "修改版本时的原问卷")
        self.api(owner, "POST", f"/api/v1/surveys/{survey['_id']}/library-questions", {"question_id": item["id"]})
        linked = self.db.surveys.find_one({"_id": survey["_id"]})["questions"][0]
        context = f"?from_survey={survey['_id']}&from_question={linked['id']}&q=时长&mine_page=2"
        version_path = f"/question-library/{item['id']}/version" + context
        form = owner.get(version_path).get_data(as_text=True)
        self.assertIn(f'from_survey={survey["_id"]}', form)
        self.assertIn(f'name="from_question" value="{linked["id"]}"', form)
        unchanged = {"type": "number", "title": "每周学习时长", "required": "on",
                     "min_value": "0.0", "max_value": "100.0", "number_statistics": "measure"}
        rejected = self.post(owner, version_path, unchanged)
        self.assertIn("题目内容没有变化", rejected.get_data(as_text=True))
        self.assertEqual(self.db.question_library.count_documents({"root_id": ObjectId(item["root_id"])}), 1)
        unchanged_api = self.api(owner, "POST", f"/api/v1/library/questions/{item['id']}/versions", {
            "type": "number", "title": "每周学习时长", "required": True,
            "min_value": 0, "max_value": 100, "number_statistics": "measure"})
        self.assertEqual(unchanged_api.status_code, 422)
        saved = self.post(owner, version_path, {**unchanged, "title": "每周课外学习时长"})
        self.assertEqual(saved.status_code, 302)
        self.assertIn(f"from_survey={survey['_id']}", saved.headers["Location"])
        new_detail = owner.get(saved.headers["Location"]).get_data(as_text=True)
        self.assertIn(f"/surveys/{survey['_id']}#question-{linked['id']}", new_detail)
        self.assertNotIn("只修改这份问卷", new_detail)
        shared = self.post(owner, f"/question-library/{item['id']}/share" + context, {"shared": "yes"})
        self.assertIn(f"from_question={linked['id']}", shared.headers["Location"])
        restored = self.post(owner, f"/question-library/{item['id']}/restore" + context, {})
        self.assertIn(f"from_survey={survey['_id']}", restored.headers["Location"])
        self.assertEqual(self.db.surveys.find_one({"_id": survey["_id"]})["questions"][0]["title"], "每周学习时长")

    def test_bank_update_uses_the_version_that_the_user_confirmed(self):
        owner = self.app.test_client()
        name = "pinned_bank_" + uuid4().hex
        self.login(owner, name)
        source = self.api(owner, "POST", "/api/v1/library/questions", {
            "type": "text", "title": "原版问题", "required": True}).get_json()["question"]
        bank = self.api(owner, "POST", "/api/v1/library/banks", {"name": "固定更新版本"}).get_json()
        self.api(owner, "POST", f"/api/v1/library/banks/{bank['id']}/questions", {"question_id": source["id"]})
        second = self.api(owner, "POST", f"/api/v1/library/questions/{source['id']}/versions", {
            "type": "text", "title": "我已查看的第二版", "required": True}).get_json()["question"]
        page = owner.get(f"/question-banks/{bank['id']}").get_data(as_text=True)
        self.assertIn("我已查看的第二版", page)
        third = self.api(owner, "POST", f"/api/v1/library/questions/{source['id']}/versions", {
            "type": "text", "title": "稍后新增的第三版", "required": True}).get_json()["question"]
        stale_form = self.post(owner, f"/question-banks/{bank['id']}/questions/{source['id']}", {"action": "update"})
        self.assertEqual(stale_form.status_code, 302)
        self.assertEqual(self.db.question_banks.find_one({"_id": ObjectId(bank["id"])})["question_ids"],
                         [ObjectId(source["id"])])
        result = self.post(owner, f"/question-banks/{bank['id']}/questions/{source['id']}", {
            "action": "update", "target_id": second["id"]})
        self.assertEqual(result.status_code, 302)
        self.assertEqual(self.db.question_banks.find_one({"_id": ObjectId(bank["id"])})["question_ids"],
                         [ObjectId(second["id"])])
        extra = self.api(owner, "POST", "/api/v1/library/questions", {
            "type": "text", "title": "另一个标签页加入的题", "required": True}).get_json()["question"]
        unrelated = self.api(owner, "PATCH", f"/api/v1/library/banks/{bank['id']}/questions/{second['id']}",
                             {"target_id": extra["id"]})
        self.assertEqual(unrelated.status_code, 422)
        collection_class = type(self.db.question_banks)
        real_update = collection_class.update_one
        injected = []

        def concurrent_add(collection, query, update, *args, **kwargs):
            if collection.name == "question_banks" and "question_ids" in query and not injected:
                injected.append(True)
                real_update(collection, {"_id": ObjectId(bank["id"])},
                            {"$addToSet": {"question_ids": ObjectId(extra["id"])}})
            return real_update(collection, query, update, *args, **kwargs)

        with patch.object(collection_class, "update_one", new=concurrent_add):
            updated = self.api(owner, "PATCH", f"/api/v1/library/banks/{bank['id']}/questions/{second['id']}",
                               {"target_id": third["id"]})
        self.assertTrue(injected)
        self.assertEqual(updated.status_code, 200, updated.get_json())
        self.assertEqual(updated.get_json()["question_ids"], [third["id"], extra["id"]])
        self.assertEqual(self.db.question_banks.find_one({"_id": ObjectId(bank["id"])})["question_ids"],
                         [ObjectId(third["id"]), ObjectId(extra["id"])])

    def test_bank_can_replace_a_withdrawn_version_with_a_shared_new_version(self):
        maker, colleague = self.app.test_client(), self.app.test_client()
        self.login(maker, "withdraw_maker_" + uuid4().hex)
        self.login(colleague, "withdraw_colleague_" + uuid4().hex)
        source = self.api(maker, "POST", "/api/v1/library/questions", {
            "type": "text", "title": "原共享版", "required": True}).get_json()["question"]
        self.api(maker, "PATCH", f"/api/v1/library/questions/{source['id']}/sharing", {"shared": True})
        bank = self.api(colleague, "POST", "/api/v1/library/banks", {"name": "团队问卷题库"}).get_json()
        self.api(colleague, "POST", f"/api/v1/library/banks/{bank['id']}/questions", {"question_id": source["id"]})
        second = self.api(maker, "POST", f"/api/v1/library/questions/{source['id']}/versions", {
            "type": "text", "title": "新的共享版", "required": True}).get_json()["question"]
        self.api(maker, "PATCH", f"/api/v1/library/questions/{second['id']}/sharing", {"shared": True})
        self.api(maker, "PATCH", f"/api/v1/library/questions/{source['id']}/sharing", {"shared": False})
        before = colleague.get(f"/question-banks/{bank['id']}").get_data(as_text=True)
        self.assertIn("新的共享版", before)
        self.assertIn(f'name="target_id" value="{second["id"]}"', before)
        result = self.api(colleague, "PATCH", f"/api/v1/library/banks/{bank['id']}/questions/{source['id']}",
                          {"target_id": second["id"]})
        self.assertEqual(result.status_code, 200, result.get_json())
        self.assertEqual(result.get_json()["question_ids"], [second["id"]])
        third = self.api(maker, "POST", f"/api/v1/library/questions/{second['id']}/versions", {
            "type": "text", "title": "后来撤回的第三版", "required": True}).get_json()["question"]
        self.api(maker, "PATCH", f"/api/v1/library/questions/{third['id']}/sharing", {"shared": True})
        self.api(maker, "PATCH", f"/api/v1/library/questions/{third['id']}/sharing", {"shared": False})
        rejected = self.post(colleague, f"/question-banks/{bank['id']}/questions/{second['id']}",
                             {"action": "update", "target_id": third["id"]})
        self.assertEqual(rejected.status_code, 302)
        self.assertIn("所选版本已无法选用", colleague.get(rejected.headers["Location"]).get_data(as_text=True))
        self.assertEqual(self.db.question_banks.find_one({"_id": ObjectId(bank["id"])})["question_ids"],
                         [ObjectId(second["id"])])

    def test_removed_draft_question_does_not_count_as_current_usage(self):
        owner = self.app.test_client()
        name = "removed_usage_" + uuid4().hex
        self.login(owner, name)
        item = self.api(owner, "POST", "/api/v1/library/questions", {
            "type": "text", "title": "准备移除的共享题", "required": True}).get_json()["question"]
        removed = self.survey(owner, name, "已从草稿移除该题")
        kept = self.survey(owner, name, "仍在使用该题")
        for survey in (removed, kept):
            self.api(owner, "POST", f"/api/v1/surveys/{survey['_id']}/library-questions", {"question_id": item["id"]})
        linked = self.db.surveys.find_one({"_id": removed["_id"]})["questions"][0]
        self.post(owner, f"/surveys/{removed['_id']}/questions/{linked['id']}/delete", {})
        stats = owner.get(f"/api/v1/library/questions/{item['id']}/statistics").get_json()
        self.assertEqual(stats["survey_count"], 1)
        self.assertEqual([usage["title"] for usage in stats["usages"]], ["仍在使用该题"])

    def test_publish_rechecks_the_draft_after_another_tab_changes_it(self):
        owner = self.app.test_client()
        name = "publish_race_" + uuid4().hex
        self.login(owner, name)
        survey = self.survey(owner, name, "发布时另一标签页移除题目")
        self.api(owner, "POST", f"/api/v1/surveys/{survey['_id']}/questions", {
            "type": "text", "title": "唯一一道题", "required": True})
        current = self.db.surveys.find_one({"_id": survey["_id"]})
        collection_class = type(self.db.surveys)
        real_update = collection_class.update_one
        injected = []

        def remove_before_publish(collection, query, update, *args, **kwargs):
            if collection.name == "surveys" and update.get("$set", {}).get("status") == "published" and not injected:
                injected.append(True)
                real_update(collection, {"_id": survey["_id"]}, {
                    "$set": {"questions": [{**current["questions"][0], "active": False}]},
                    "$inc": {"revision": 1}})
            return real_update(collection, query, update, *args, **kwargs)

        with patch.object(collection_class, "update_one", new=remove_before_publish):
            result = self.post(owner, f"/surveys/{survey['_id']}/publish", {})
        self.assertTrue(injected)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.db.surveys.find_one({"_id": survey["_id"]})["status"], "draft")
        self.assertIn("尚未发布", result.get_data(as_text=True))

    def test_library_attach_api_rejects_a_stale_survey_revision(self):
        owner = self.app.test_client()
        name = "attach_revision_" + uuid4().hex
        self.login(owner, name)
        survey = self.survey(owner, name, "库题接口版本校验")
        items = [self.api(owner, "POST", "/api/v1/library/questions", {
            "type": "text", "title": title, "required": True}).get_json()["question"]
            for title in ("先加入的题", "旧页面准备加入的题")]
        first = self.api(owner, "POST", f"/api/v1/surveys/{survey['_id']}/library-questions",
                         {"question_id": items[0]["id"], "revision": survey["revision"]})
        self.assertEqual(first.status_code, 201)
        stale = self.api(owner, "POST", f"/api/v1/surveys/{survey['_id']}/library-questions",
                         {"question_id": items[1]["id"], "revision": survey["revision"]})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(len(self.db.surveys.find_one({"_id": survey["_id"]})["questions"]), 1)

    def test_old_edit_forms_preserve_input_without_overwriting_newer_changes(self):
        owner = self.app.test_client()
        name = "form_revision_" + uuid4().hex
        self.login(owner, name)
        survey = self.survey(owner, name, "原问卷标题")
        added = self.api(owner, "POST", f"/api/v1/surveys/{survey['_id']}/questions", {
            "type": "text", "title": "原题目", "required": True}).get_json()
        key = added["question"]["id"]
        loaded_revision = self.db.surveys.find_one({"_id": survey["_id"]})["revision"]
        changed = self.api(owner, "PATCH", f"/api/v1/surveys/{survey['_id']}/questions/{key}",
                           {"title": "另一个页面保存的新题目"})
        self.assertEqual(changed.status_code, 200)
        rejected = self.post(owner, f"/surveys/{survey['_id']}/questions/{key}/edit", {
            "revision": str(loaded_revision), "type": "text", "title": "当前页面尚未保存的题目", "required": "on"})
        self.assertEqual(rejected.status_code, 409)
        self.assertIn('value="当前页面尚未保存的题目"', rejected.get_data(as_text=True))
        self.assertIn("查看最新问卷", rejected.get_data(as_text=True))
        current = self.db.surveys.find_one({"_id": survey["_id"]})
        self.assertEqual(current["questions"][0]["title"], "另一个页面保存的新题目")
        info_revision = current["revision"]
        self.api(owner, "PATCH", f"/api/v1/surveys/{survey['_id']}", {"title": "另一个页面保存的问卷标题"})
        info = self.post(owner, f"/surveys/{survey['_id']}/edit", {
            "revision": str(info_revision), "title": "当前页面保留的问卷标题", "description": "未丢失的说明"})
        self.assertEqual(info.status_code, 409)
        self.assertIn("未丢失的说明", info.get_data(as_text=True))
        self.assertIn('value="当前页面保留的问卷标题"', info.get_data(as_text=True))
        latest = self.db.surveys.find_one({"_id": survey["_id"]})
        self.assertEqual(latest["title"], "另一个页面保存的问卷标题")

        # A second tab can save after the initial revision check but before the write.
        from pymongo.collection import Collection
        original_update = Collection.update_one
        pending_title = '保存瞬间发生冲突时仍保留的标题'

        def change_between_check_and_write(collection, query, update, *args, **kwargs):
            if (collection.name == 'surveys' and query.get('_id') == survey['_id']
                    and update.get('$set', {}).get('title') == pending_title):
                original_update(collection, {'_id': survey['_id']}, {
                    '$set': {'description': '另一页面刚保存的说明'}, '$inc': {'revision': 1}})
            return original_update(collection, query, update, *args, **kwargs)

        with patch.object(Collection, 'update_one', change_between_check_and_write):
            raced = self.post(owner, f"/surveys/{survey['_id']}/edit", {
                'revision': str(latest['revision']), 'title': pending_title,
                'description': '当前页面还未保存的说明', 'repeat_policy': 'once',
                'allow_anonymous': 'on', 'option_label_style': 'letters'})
        self.assertEqual(raced.status_code, 409)
        raced_html = raced.get_data(as_text=True)
        self.assertIn(f'value="{pending_title}"', raced_html)
        self.assertIn('当前页面还未保存的说明', raced_html)
        self.assertIn('查看最新问卷', raced_html)
        self.assertIn('value="letters" checked', raced_html)
        self.assertIn('value="once" checked', raced_html)
        actual = self.db.surveys.find_one({'_id': survey['_id']})
        self.assertEqual(actual['description'], '另一页面刚保存的说明')
        self.assertEqual(actual['title'], latest['title'])
        confirmed = self.post(owner, f"/surveys/{survey['_id']}/edit", {
            "revision": str(actual["revision"]), "title": "当前页面保留的问卷标题", "description": "未丢失的说明"})
        self.assertEqual(confirmed.status_code, 302)
        self.assertEqual(self.db.surveys.find_one({"_id": survey["_id"]})["title"], "当前页面保留的问卷标题")

    def test_numeric_distribution_drilldown_matches_equivalent_named_answers(self):
        owner = self.app.test_client()
        name = "numeric_drilldown_" + uuid4().hex
        self.login(owner, name)
        survey = self.survey(owner, name, "数字等值筛选")
        item = self.api(owner, "POST", "/api/v1/library/questions", {
            "type": "number", "title": "填写数字", "required": True}).get_json()["question"]
        self.api(owner, "POST", f"/api/v1/surveys/{survey['_id']}/library-questions", {"question_id": item["id"]})
        self.api(owner, "POST", f"/api/v1/surveys/{survey['_id']}/publish", {})
        current = self.db.surveys.find_one({"_id": survey["_id"]})
        key = current["questions"][0]["id"]
        for value, identity in (("30", "named"), ("30.0", "named"), ("3e1", "named"), ("30", "anonymous")):
            submitted = self.api(owner, "POST", f"/api/v1/public/surveys/{survey['slug']}/responses", {
                "revision": current["revision"], "answers": {key: value}, "identity_mode": identity})
            self.assertEqual(submitted.status_code, 201)
        page = owner.get(f"/surveys/{survey['_id']}/statistics/{key}/accounts?q=30&exact=1")
        self.assertEqual(page.status_code, 200)
        self.assertIn("符合条件的账号答卷：3 份", page.get_data(as_text=True))
        self.assertIn("30.0", page.get_data(as_text=True))
        self.assertIn("3e1", page.get_data(as_text=True))
        stats = owner.get(f"/api/v1/library/questions/{item['id']}/statistics").get_json()
        self.assertEqual(stats["number_distribution"], [{"value": "30", "count": 4, "percent": "100"}])

    def test_published_jump_overview_links_to_readable_question_instead_of_editor(self):
        owner = self.app.test_client()
        name = "published_overview_" + uuid4().hex
        self.login(owner, name)
        survey = self.survey(owner, name, "发布后的跳转总览")
        question_ids = []
        for definition in ({"type": "single_choice", "title": "是否参加", "options": ["是", "否"]},
                           {"type": "text", "title": "参加感受"}, {"type": "text", "title": "其他建议"}):
            result = self.api(owner, "POST", f"/api/v1/surveys/{survey['_id']}/questions",
                              {**definition, "required": True})
            question_ids.append(result.get_json()["question"]["id"])
        self.api(owner, "POST", f"/api/v1/surveys/{survey['_id']}/questions/{question_ids[0]}/jumps",
                 {"option": "否", "target_id": question_ids[2]})
        self.post(owner, f"/surveys/{survey['_id']}/publish", {})
        html = owner.get(f"/surveys/{survey['_id']}").get_data(as_text=True)
        self.assertNotIn(f"/questions/{question_ids[0]}/jumps/new", html)
        self.assertIn(f'href="#question-{question_ids[0]}"', html)

    def test_shared_question_withdrawn_after_loading_selection_keeps_user_on_selection(self):
        maker, colleague = self.app.test_client(), self.app.test_client()
        self.login(maker, "selection_maker_" + uuid4().hex)
        other = "selection_colleague_" + uuid4().hex
        self.login(colleague, other)
        item = self.api(maker, "POST", "/api/v1/library/questions", {
            "type": "text", "title": "选题期间撤回的题目", "required": True}).get_json()["question"]
        self.api(maker, "PATCH", f"/api/v1/library/questions/{item['id']}/sharing", {"shared": True})
        survey = self.survey(colleague, other, "继续选择其他题目")
        self.assertIn("选题期间撤回的题目", colleague.get(
            f"/surveys/{survey['_id']}/question-library?q=撤回").get_data(as_text=True))
        self.api(maker, "PATCH", f"/api/v1/library/questions/{item['id']}/sharing", {"shared": False})
        result = self.post(colleague, f"/surveys/{survey['_id']}/question-library/{item['id']}",
                           {"continue": "yes", "q": "撤回"})
        self.assertEqual(result.status_code, 302)
        self.assertIn(f"/surveys/{survey['_id']}/question-library", result.headers["Location"])
        self.assertIn("已停止共享", colleague.get(result.headers["Location"]).get_data(as_text=True))
        self.assertEqual(self.db.surveys.find_one({"_id": survey["_id"]})["questions"], [])

    def test_selection_open_during_publish_returns_to_survey_without_adding(self):
        owner = self.app.test_client()
        name = 'stale_selection_' + uuid4().hex
        self.login(owner, name)
        survey = self.survey(owner, name, '另一页发布的问卷')
        sid = str(survey['_id'])
        self.api(owner, 'POST', f'/api/v1/surveys/{sid}/questions', {'type': 'text', 'title': '原有题目'})
        item = self.api(owner, 'POST', '/api/v1/library/questions', {'type': 'text', 'title': '还没加入的题目'}).get_json()['question']
        self.assertEqual(owner.get(f'/surveys/{sid}/question-library').status_code, 200)
        self.post(owner, f'/surveys/{sid}/publish', {})
        for method in ('GET', 'POST'):
            with self.subTest(method=method):
                result = (owner.get(f'/surveys/{sid}/question-library') if method == 'GET' else
                          self.post(owner, f"/surveys/{sid}/question-library/{item['id']}", {'continue': 'yes'}))
                self.assertEqual(result.status_code, 302)
                self.assertEqual(result.headers['Location'], f'/surveys/{sid}')
                page = owner.get(result.headers['Location'])
                self.assertIn('已发布，不能再添加题目', page.get_data(as_text=True))
        self.assertEqual(len(self.db.surveys.find_one({'_id': survey['_id']})['questions']), 1)
        self.assertEqual(self.api(owner, 'POST', f'/api/v1/surveys/{sid}/library-questions', {'question_id': item['id']}).status_code, 409)

    def test_deleted_bank_filter_keeps_search_and_shows_available_questions(self):
        owner = self.app.test_client()
        name = 'bank_filter_' + uuid4().hex
        self.login(owner, name)
        survey = self.survey(owner, name, '题库筛选恢复')
        bank = self.api(owner, 'POST', '/api/v1/library/banks', {'name': '已删除的筛选题库'}).get_json()
        self.api(owner, 'POST', '/api/v1/library/questions', {'type': 'text', 'title': '可继续选择的学习建议'})
        self.api(owner, 'DELETE', f"/api/v1/library/banks/{bank['id']}", {})
        for path in ('/question-library', f"/surveys/{survey['_id']}/question-library"):
            with self.subTest(path=path):
                result = owner.get(path, query_string={'bank': bank['id'], 'q': '学习建议'})
                self.assertEqual(result.status_code, 302)
                self.assertNotIn('bank=', result.headers['Location'])
                page = owner.get(result.headers['Location']).get_data(as_text=True)
                self.assertIn('value="学习建议"', page)
                self.assertIn('可继续选择的学习建议', page)
                self.assertIn('已显示全部可用题目', page)

    def test_existing_question_bank_add_checks_deleted_destination(self):
        import app.question_bank as bank_module
        owner = self.app.test_client()
        name = 'bank_destination_' + uuid4().hex
        self.login(owner, name)
        item = self.api(owner, 'POST', '/api/v1/library/questions', {'type': 'text', 'title': '保留在题目库的题目'}).get_json()['question']
        real_accessible = bank_module.accessible_question
        for during in (False, True):
            with self.subTest(during=during):
                bank = self.api(owner, 'POST', '/api/v1/library/banks', {'name': '即将删除' + str(during)}).get_json()

                def delete_before_write(question_id, **kwargs):
                    result = real_accessible(question_id, **kwargs)
                    self.db.question_banks.delete_one({'_id': ObjectId(bank['id'])})
                    return result

                if not during:
                    self.db.question_banks.delete_one({'_id': ObjectId(bank['id'])})
                with patch.object(bank_module, 'accessible_question', side_effect=delete_before_write if during else real_accessible):
                    result = self.post(owner, f"/question-banks/{bank['id']}/questions/{item['id']}",
                                       {'continue': 'yes', 'q': '保留'})
                self.assertEqual(result.status_code, 302)
                self.assertNotIn('bank=', result.headers['Location'])
                page = owner.get(result.headers['Location']).get_data(as_text=True)
                self.assertIn('本次没有加入题库', page)
                self.assertIn('value="保留"', page)
                self.assertNotIn('已加入题库。', page)
                self.assertIsNotNone(self.db.question_library.find_one({'_id': ObjectId(item['id'])}))

    def test_bank_api_does_not_report_success_when_destination_is_deleted_during_write(self):
        owner = self.app.test_client()
        self.login(owner, 'bank_api_race_' + uuid4().hex)
        item = self.api(owner, 'POST', '/api/v1/library/questions',
                        {'type': 'text', 'title': '题库删除后仍保留的题目'}).get_json()['question']
        collection_class = type(self.db.question_banks)
        real_update = collection_class.update_one
        for action in ('add', 'rename', 'remove', 'web_rename'):
            with self.subTest(action=action):
                bank = self.api(owner, 'POST', '/api/v1/library/banks', {'name': '并发删除_' + action}).get_json()
                bank_id = ObjectId(bank['id'])
                self.db.question_banks.update_one({'_id': bank_id}, {'$addToSet': {'question_ids': ObjectId(item['id'])}})

                def delete_before_update(collection, query, update, *args, **kwargs):
                    if collection.full_name == self.db.question_banks.full_name and query.get('_id') == bank_id:
                        collection.delete_one({'_id': bank_id})
                    return real_update(collection, query, update, *args, **kwargs)

                with patch.object(collection_class, 'update_one', new=delete_before_update):
                    if action == 'add':
                        response = self.api(owner, 'POST', f"/api/v1/library/banks/{bank['id']}/questions",
                                            {'question_id': item['id']})
                    elif action == 'rename':
                        response = self.api(owner, 'PATCH', f"/api/v1/library/banks/{bank['id']}", {'name': '新名称'})
                    elif action == 'remove':
                        response = self.api(owner, 'DELETE', f"/api/v1/library/banks/{bank['id']}/questions/{item['id']}", {})
                    else:
                        response = self.post(owner, f"/question-banks/{bank['id']}/rename", {'name': '新名称'})
                if action == 'web_rename':
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.location, '/question-library')
                    self.assertIn('本次没有修改名称', owner.get(response.location).get_data(as_text=True))
                else:
                    self.assertEqual(response.status_code, 404)
                    self.assertIn('error', response.get_json())
                self.assertIsNone(self.db.question_banks.find_one({'_id': bank_id}))
                self.assertIsNotNone(self.db.question_library.find_one({'_id': ObjectId(item['id'])}))

    def test_shared_catalog_refreshes_when_sharing_changes_between_queries(self):
        maker, colleague = self.app.test_client(), self.app.test_client()
        self.login(maker, 'catalog_maker_' + uuid4().hex)
        self.login(colleague, 'catalog_reader_' + uuid4().hex)
        collection_class = type(self.db.question_library)
        original_aggregate = collection_class.aggregate
        for keep_older in (False, True):
            with self.subTest(keep_older=keep_older):
                tag = uuid4().hex
                source = self.api(maker, 'POST', '/api/v1/library/questions',
                                  {'type':'text', 'title':'旧题_' + tag}).get_json()['question']
                if keep_older:
                    self.api(maker, 'PATCH', f"/api/v1/library/questions/{source['id']}/sharing", {'shared':True})
                    selected = self.api(maker, 'POST', f"/api/v1/library/questions/{source['id']}/versions",
                                        {'type':'text', 'title':'新版_' + tag}).get_json()['question']
                else:
                    selected = source
                self.api(maker, 'PATCH', f"/api/v1/library/questions/{selected['id']}/sharing", {'shared':True})
                changed=[]

                def withdraw_after_catalog_query(collection, pipeline, *args, **kwargs):
                    rows=list(original_aggregate(collection, pipeline, *args, **kwargs))
                    if (collection.full_name == self.db.question_library.full_name
                            and pipeline[0]['$match'].get('shared') is True and not changed):
                        self.db.question_library.update_one({'_id':ObjectId(selected['id'])}, {'$set':{'shared':False}})
                        changed.append(True)
                    return iter(rows)

                with patch.object(collection_class, 'aggregate', new=withdraw_after_catalog_query):
                    response = colleague.get('/question-library', query_string={'q':selected['definition']['title']})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(changed)
                self.assertNotIn('id="library-question-' + source['root_id'] + '"', response.get_data(as_text=True))

    def test_existing_library_attach_conflict_keeps_selection_and_allows_retry(self):
        import app.question_bank as bank_module
        owner = self.app.test_client()
        name = 'attach_recovery_' + uuid4().hex
        self.login(owner, name)
        survey = self.survey(owner, name, '加入期间问卷变化')
        item = self.api(owner, 'POST', '/api/v1/library/questions', {'type': 'text', 'title': '学习建议'}).get_json()['question']
        real_add = bank_module.add_to_survey

        def change_before_add(survey, item):
            self.db.surveys.update_one({'_id': survey['_id']}, {'$inc': {'revision': 1}})
            return real_add(survey, item)

        path = f"/surveys/{survey['_id']}/question-library/{item['id']}"
        with patch.object(bank_module, 'add_to_survey', side_effect=change_before_add):
            result = self.post(owner, path, {'continue': 'yes', 'q': '学习建议'})
        self.assertEqual(result.status_code, 302)
        page = owner.get(result.headers['Location']).get_data(as_text=True)
        self.assertIn('本次没有加入问卷', page)
        self.assertIn('value="学习建议"', page)
        self.assertEqual(self.db.surveys.find_one({'_id': survey['_id']})['questions'], [])
        self.assertEqual(self.post(owner, path, {'continue': 'yes', 'q': '学习建议'}).status_code, 302)
        self.assertEqual(len(self.db.surveys.find_one({'_id': survey['_id']})['questions']), 1)

    def test_survey_statistics_api_serializes_reused_question_sources(self):
        owner, visitor = self.app.test_client(), self.app.test_client()
        name = 'reused_stats_' + uuid4().hex
        self.login(owner, name)
        self.login(visitor, 'reused_stats_visitor_' + uuid4().hex)
        source = self.api(owner, 'POST', '/api/v1/library/questions', {
            'type': 'number', 'title': '你的年龄是多少？', 'required': True,
            'min_value': 0, 'max_value': 120, 'integer_only': True, 'number_statistics': 'measure'}).get_json()['question']
        survey = self.survey(owner, name, '从题库复用后查看JSON统计')
        sid = str(survey['_id'])
        linked = self.api(owner, 'POST', f'/api/v1/surveys/{sid}/library-questions', {'question_id': source['id']}).get_json()
        qid = linked['question_id']
        self.post(owner, f'/surveys/{sid}/publish', {})
        for expected_count in (0, 1):
            if expected_count:
                current = self.db.surveys.find_one({'_id': survey['_id']})
                submitted = self.api(visitor, 'POST', f"/api/v1/public/surveys/{survey['slug']}/responses", {
                    'revision': current['revision'], 'answers': {qid: 22}, 'identity_mode': 'named'})
                self.assertEqual(submitted.status_code, 201)
            for endpoint in (f'/api/v1/surveys/{sid}/statistics', f'/api/v1/surveys/{sid}/statistics/{qid}'):
                with self.subTest(count=expected_count, endpoint=endpoint):
                    result = owner.get(endpoint)
                    self.assertEqual(result.status_code, 200)
                    data = result.get_json()
                    self.assertEqual(data['response_count'], expected_count)
                    self.assertEqual(data['results'][0]['question']['library_question_id'], source['id'])
                    self.assertEqual(data['results'][0]['question']['library_root_id'], source['root_id'])
                    self.assertEqual(data['results'][0]['answered_count'], expected_count)
                    if expected_count:
                        self.assertEqual(data['results'][0]['average'], '22')

    def test_legacy_question_definitions_without_optional_limits_render_cleanly(self):
        owner = self.app.test_client()
        name = "legacy_limits_" + uuid4().hex
        self.login(owner, name)
        owner_id = self.db.users.find_one({"username": name})["_id"]
        old_questions = []
        for kind in ("text", "number", "multiple_choice"):
            document = {"root_id": ObjectId(), "owner_id": owner_id, "version": 1,
                        "parent_id": None, "shared": False, "required": True,
                        "type": kind, "title": "旧格式题目_" + kind}
            if kind == "multiple_choice":
                document["options"] = ["是", "否"]
            key = self.db.question_library.insert_one(document).inserted_id
            detail = owner.get(f"/question-library/{key}")
            self.assertEqual(detail.status_code, 200)
            html = detail.get_data(as_text=True)
            self.assertNotIn('class="library-definition-rule"', html)
            old_questions.append({"id": uuid4().hex[:16], "version": 1, "active": True, "jumps": [],
                                  "type": kind, "title": document["title"], "required": True,
                                  **({"options": document["options"]} if kind == "multiple_choice" else {})})
        survey = self.survey(owner, name, "旧格式问卷缺少可选限制")
        self.db.surveys.update_one({"_id": survey["_id"]}, {"$set": {"questions": old_questions}})
        self.post(owner, f"/surveys/{survey['_id']}/publish", {})
        for url in (f"/surveys/{survey['_id']}", f"/survey/{survey['slug']}"):
            page = owner.get(url)
            self.assertEqual(page.status_code, 200)
            for incorrect in ("至少填写", "最多填写", "需选", "请选择  项", "不能小于", "不能大于"):
                self.assertNotIn(incorrect, page.get_data(as_text=True))

    def test_three_accounts_numeric_distribution_versions_and_revoked_bank_item(self):
        owner, colleague, visitor = (self.app.test_client() for _ in range(3))
        owner_name = "age_owner_" + uuid4().hex
        colleague_name = "age_colleague_" + uuid4().hex
        visitor_name = "age_visitor_" + uuid4().hex
        for client, name in ((owner, owner_name), (colleague, colleague_name),
                             (visitor, visitor_name)):
            self.login(client, name)

        first = self.api(owner, "POST", "/api/v1/library/questions", {
            "type": "number", "title": "你的年龄", "required": True,
            "integer_only": True, "min_value": 0, "max_value": 120,
            "number_statistics": "measure"}).get_json()["question"]
        first_id = first["id"]
        self.assertEqual(self.api(owner, "PATCH", f"/api/v1/library/questions/{first_id}/sharing",
                                  {"shared": True}).status_code, 200)
        bank_id = self.api(colleague, "POST", "/api/v1/library/banks",
                           {"name": "团队常用"}).get_json()["id"]
        self.api(colleague, "POST", f"/api/v1/library/banks/{bank_id}/questions",
                 {"question_id": first_id})

        colleague_survey = self.survey(colleague, colleague_name, "同事的年龄调查")
        visitor_survey = self.survey(visitor, visitor_name, "访客的年龄调查")
        for client, survey in ((colleague, colleague_survey), (visitor, visitor_survey)):
            attached = self.api(client, "POST", f"/api/v1/surveys/{survey['_id']}/library-questions",
                                {"question_id": first_id})
            self.assertEqual(attached.status_code, 201)
            self.assertEqual(self.post(client, f"/surveys/{survey['_id']}/publish", {}).status_code, 302)

        for respondent, survey, age in ((owner, colleague_survey, "20"),
                                        (visitor, colleague_survey, "30"),
                                        (colleague, visitor_survey, "30.0")):
            current = self.db.surveys.find_one({"_id": survey["_id"]})
            question_id = current["questions"][0]["id"]
            submitted = self.api(respondent, "POST", f"/api/v1/public/surveys/{survey['slug']}/responses",
                                 {"revision": current["revision"], "answers": {question_id: age}})
            self.assertEqual(submitted.status_code, 201, submitted.get_json())

        stats = owner.get(f"/api/v1/library/questions/{first_id}/statistics").get_json()
        self.assertEqual(stats["survey_count"], 2)
        self.assertEqual(stats["answered_count"], 3)
        self.assertEqual(len(stats["usages"]), 2)
        self.assertTrue(all(not usage["own"] and len(usage["reference"]) == 10 for usage in stats["usages"]))
        self.assertNotEqual(stats["usages"][0]["reference"], stats["usages"][1]["reference"])
        self.assertEqual(stats["number_distribution"], [
            {"value": "30", "count": 2, "percent": "66.67"},
            {"value": "20", "count": 1, "percent": "33.33"}])
        detail_html = owner.get(f"/question-library/{first_id}").get_data(as_text=True)
        self.assertIn("各数值的回答份数", detail_html)
        self.assertIn("其他用户的问卷 ·", detail_html)
        self.assertNotIn("同事的年龄调查", detail_html)
        self.assertNotIn("访客的年龄调查", detail_html)

        second = self.api(owner, "POST", f"/api/v1/library/questions/{first_id}/versions", {
            "type": "number", "title": "你的年龄（新版）", "required": True,
            "integer_only": True, "min_value": 0, "max_value": 120,
            "number_statistics": "measure"}).get_json()["question"]
        owner_survey = self.survey(owner, owner_name, "新版本问卷")
        self.assertEqual(self.api(owner, "POST", f"/api/v1/surveys/{owner_survey['_id']}/library-questions",
                                  {"question_id": second["id"]}).status_code, 201)
        self.post(owner, f"/surveys/{owner_survey['_id']}/publish", {})
        latest = self.db.surveys.find_one({"_id": owner_survey["_id"]})
        self.assertEqual(self.api(visitor, "POST", f"/api/v1/public/surveys/{owner_survey['slug']}/responses",
                                  {"revision": latest["revision"],
                                   "answers": {latest["questions"][0]["id"]: "40"}}).status_code, 201)
        self.assertEqual(owner.get(f"/api/v1/library/questions/{first_id}/statistics").get_json()["answered_count"], 3)
        self.assertEqual(owner.get(f"/api/v1/library/questions/{second['id']}/statistics").get_json()["answered_count"], 1)

        self.api(owner, "PATCH", f"/api/v1/library/questions/{first_id}/sharing", {"shared": False})
        bank_html = colleague.get(f"/question-banks/{bank_id}").get_data(as_text=True)
        self.assertIn("你的年龄 · v1", bank_html)
        self.assertIn("已停止共享", bank_html)
        another = self.survey(colleague, colleague_name, "停止共享后的新问卷")
        self.assertEqual(self.api(colleague, "POST", f"/api/v1/surveys/{another['_id']}/library-questions",
                                  {"question_id": first_id}).status_code, 404)
        self.assertEqual(visitor.get(f"/survey/{colleague_survey['slug']}").status_code, 200)


if __name__ == "__main__":
    unittest.main()
