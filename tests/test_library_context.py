"""Keep independent library edits usable when their return destination disappears."""
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from unittest.mock import patch
from pymongo.collection import Collection

from bson import ObjectId
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from tests import test_stage2_library as library_tests


class LibraryContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        library_tests.LibraryFlowTests.setUpClass()
        cls.flow = library_tests.LibraryFlowTests()
        cls.server = make_server('127.0.0.1', 0, cls.flow.app, threaded=True)
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(
            channel='msedge' if sys.platform == 'win32' else None, headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        library_tests.LibraryFlowTests.tearDownClass()

    def test_raw_answer_paging_and_numeric_mode_keep_context_and_privacy(self):
        owner, colleague = self.flow.app.test_client(), self.flow.app.test_client()
        name = "library_values_" + uuid4().hex
        self.flow.login(owner, name)
        self.flow.login(colleague, "library_external_" + uuid4().hex)
        user = self.flow.db.users.find_one({"username": name})
        source = self.flow.api(owner, "POST", "/api/v1/library/questions", {
            "type": "text", "title": "你希望自习区优先改善什么？", "required": True
        }).get_json()["question"]
        self.flow.api(owner, "PATCH", f"/api/v1/library/questions/{source['id']}/sharing",
                      {"shared": True})
        bank = self.flow.api(owner, "POST", "/api/v1/library/banks",
                             {"name": "学习环境常用题"}).get_json()
        self.flow.api(owner, "POST", f"/api/v1/library/banks/{bank['id']}/questions",
                      {"question_id": source["id"]})

        def make_survey(client, title, question_id=source["id"]):
            created = self.flow.api(client, "POST", "/api/v1/surveys", {
                "title": title, "allow_multiple_submissions": True})
            self.assertEqual(created.status_code, 201, created.get_json())
            item = created.get_json()["survey"]
            self.flow.api(client, "POST", f"/api/v1/surveys/{item['id']}/library-questions",
                          {"question_id": question_id})
            self.flow.api(client, "POST", f"/api/v1/surveys/{item['id']}/publish", {})
            return self.flow.db.surveys.find_one({"_id": ObjectId(item["id"])})

        own_survey = make_survey(owner, "自习区改善反馈")
        other_survey = make_survey(colleague, "其他账号的私有问卷标题")
        answers = [f"自习反馈{index:03d}：希望改善照明和插座。" for index in range(1, 46)]
        for answer in answers:
            response = self.flow.api(owner, "POST",
                f"/api/v1/public/surveys/{own_survey['slug']}/responses",
                {"revision": own_survey["revision"],
                 "answers": {own_survey["questions"][0]["id"]: answer}})
            self.assertEqual(response.status_code, 201, response.get_json())
        # Deliberately make submission order differ from insertion order, including tied timestamps.
        rows = list(self.flow.db.responses.find({"survey_id": own_survey["_id"]}).sort("_id", 1))
        ordered = []
        for index, row in enumerate(rows):
            submitted = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(minutes=(44 - index) // 3)
            self.flow.db.responses.update_one({"_id": row["_id"]}, {"$set": {"submitted_at": submitted}})
            ordered.append((submitted, row["_id"], answers[index]))
        answers = [row[2] for row in sorted(ordered)]
        external = self.flow.api(colleague, "POST",
            f"/api/v1/public/surveys/{other_survey['slug']}/responses",
            {"revision": other_survey["revision"],
             "answers": {other_survey["questions"][0]["id"]: "其他用户不可公开的原始填空"}})
        self.assertEqual(external.status_code, 201)
        stats = owner.get(f"/api/v1/library/questions/{source['id']}/statistics").get_json()
        self.assertEqual(stats["answered_count"], 46)
        self.assertEqual(len(stats["values"]), 45)
        self.assertEqual(stats["values"], answers)

        numeric = {}
        for mode in ("distribution", "measure"):
            numeric[mode] = self.flow.api(owner, "POST", "/api/v1/library/questions", {
                "type": "number", "title": "本周到馆次数" if mode == "distribution" else "单次自习时长",
                "required": True, "number_statistics": mode}).get_json()["question"]
            survey = make_survey(owner, "数字展示核对", numeric[mode]["id"])
            for value in (10, 20):
                submitted = self.flow.api(owner, "POST",
                    f"/api/v1/public/surveys/{survey['slug']}/responses",
                    {"revision": survey["revision"], "answers": {survey["questions"][0]["id"]: value}})
                self.assertEqual(submitted.status_code, 201, submitted.get_json())
        cookie = self.flow.app.session_interface.get_signing_serializer(self.flow.app).dumps(
            {"user_id": str(user["_id"])})
        saved = self.flow.db.responses.count_documents({})
        for width in (1280, 390):
            with self.subTest(width=width):
                context = self.browser.new_context(viewport={"width": width, "height": 844})
                context.add_cookies([{"name": self.flow.app.config["SESSION_COOKIE_NAME"],
                                      "value": cookie, "url": self.base}])
                page = context.new_page()
                page.set_default_timeout(6000)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                try:
                    url = (self.base + f"/question-library/{source['id']}?bank={bank['id']}"
                           "&return_bank=1&q=自习&mine_page=2")
                    page.goto(url)
                    values = page.locator("#library-own-values")
                    values.locator("summary").click()
                    self.assertEqual(values.locator("li").count(), 20)
                    self.assertIn(answers[0], values.inner_text())
                    self.assertNotIn(answers[20], values.inner_text())
                    self.assertNotIn("其他用户不可公开的原始填空", page.locator("body").inner_text())
                    self.assertNotIn("其他账号的私有问卷标题", page.locator("body").inner_text())
                    self.assertEqual(page.get_by_role("link", name="创建题目新版本 →", exact=True).count(), 1)
                    values.get_by_role("link", name="下一页 →", exact=True).click()
                    self.assertEqual(page.locator("#library-own-values[open]").count(), 1)
                    self.assertEqual(values.locator("li").count(), 20)
                    self.assertIn(answers[20], values.inner_text())
                    self.assertNotIn(answers[0], values.inner_text())
                    args = parse_qs(urlsplit(page.url).query)
                    self.assertEqual(args["values_page"], ["2"])
                    self.assertEqual(args["bank"], [bank["id"]])
                    self.assertEqual(args["q"], ["自习"])
                    self.assertEqual(args["mine_page"], ["2"])
                    self.assertEqual(urlsplit(page.url).fragment, "library-own-values")
                    values.get_by_role("link", name="下一页 →", exact=True).click()
                    self.assertEqual(values.locator("li").count(), 5)
                    self.assertIn(answers[-1], values.inner_text())
                    self.assertEqual(values.get_by_role("link", name="下一页 →").count(), 0)
                    page.goto(url + "&values_page=999")
                    self.assertIn("第 3 / 3 页", values.inner_text())
                    page.goto(url + "&values_page=invalid")
                    self.assertIn(answers[0], values.inner_text())
                    self.assertEqual(values.locator("li").count(), 20)
                    page.get_by_role("link", name="← 返回题库", exact=True).click()
                    self.assertIn(f"/question-banks/{bank['id']}", page.url)
                    for mode, item in numeric.items():
                        page.goto(self.base + f"/question-library/{item['id']}")
                        measures = page.locator(".library-number-overview")
                        self.assertEqual(measures.is_visible(), mode == "measure")
                        if mode == "distribution":
                            page.get_by_text("查看数值概览", exact=True).click()
                            self.assertTrue(measures.is_visible())
                        self.assertIn("15", measures.inner_text())
                    self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                    self.assertFalse(errors, errors)
                finally:
                    context.close()
        self.assertEqual(self.flow.db.responses.count_documents({}), saved)

    def test_deleted_source_survey_keeps_library_edit_and_return_links_usable(self):
        owner = self.flow.app.test_client()
        owner_name = 'library_context_' + uuid4().hex
        self.flow.login(owner, owner_name)
        user = self.flow.db.users.find_one({'username': owner_name})
        source = self.flow.api(owner, 'POST', '/api/v1/library/questions', {
            'type': 'text', 'title': '图书馆自习环境有什么需要改进的地方？',
            'required': False}).get_json()['question']
        survey = self.flow.survey(owner, owner_name, '图书馆环境反馈')
        attached = self.flow.api(owner, 'POST',
            f"/api/v1/surveys/{survey['_id']}/library-questions",
            {'question_id': source['id']}).get_json()
        source_before = self.flow.db.question_library.find_one({'_id': ObjectId(source['id'])})
        context = self.browser.new_context(viewport={'width': 390, 'height': 844})
        cookie = self.flow.app.session_interface.get_signing_serializer(self.flow.app).dumps(
            {'user_id': str(user['_id'])})
        context.add_cookies([{'name': self.flow.app.config['SESSION_COOKIE_NAME'],
                             'value': cookie, 'url': self.base}])
        page = context.new_page()
        page.set_default_timeout(6000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        new_title = '你希望图书馆优先改善哪些自习设施？'
        try:
            page.goto(self.base + f"/question-library/{source['id']}?from_survey={survey['_id']}&from_question={attached['question_id']}")
            page.get_by_role('link', name='创建题目新版本 →', exact=True).click()
            page.get_by_label('题目内容', exact=True).fill(new_title)
            page.get_by_label('最少字数', exact=True).fill('10')
            page.get_by_label('最多字数', exact=True).fill('2')
            deleted = self.flow.api(owner, 'DELETE', f"/api/v1/surveys/{survey['_id']}", {})
            self.assertEqual(deleted.status_code, 200)

            # Validation still works, and an invalid edit remains on the form after its source is deleted.
            page.get_by_role('button', name='保存新版本', exact=True).click()
            self.assertEqual(page.get_by_label('题目内容', exact=True).input_value(), new_title)
            self.assertEqual(page.get_by_label('最少字数', exact=True).input_value(), '10')
            self.assertEqual(page.get_by_label('最多字数', exact=True).input_value(), '2')
            self.assertIn('原问卷已不存在或无法访问', page.locator('body').inner_text())
            self.assertEqual(page.locator('input[name="from_survey"]').count(), 0)
            self.assertEqual(page.locator('input[name="from_question"]').count(), 0)
            self.assertEqual(self.flow.db.question_library.count_documents(
                {'root_id': ObjectId(source['root_id'])}), 1)

            page.get_by_label('最多字数', exact=True).fill('100')
            real_insert = Collection.insert_one
            raced = []
            def concurrent_version(collection, document, *args, **kwargs):
                if collection.name == 'question_library' and document.get('root_id') == ObjectId(source['root_id']) and not raced:
                    raced.append(True)
                    real_insert(collection, {**document, '_id': ObjectId(), 'title': '另一页面保存的版本'})
                return real_insert(collection, document, *args, **kwargs)
            with patch.object(Collection, 'insert_one', concurrent_version):
                with page.expect_response(lambda r: r.request.method == 'POST' and '/version' in r.url) as collision:
                    page.get_by_role('button', name='保存新版本', exact=True).click()
            self.assertTrue(raced)
            self.assertEqual(collision.value.status, 409)
            self.assertIn('直接重试保存', page.get_by_role('alert').inner_text())
            self.assertEqual(page.get_by_label('题目内容', exact=True).input_value(), new_title)
            self.assertEqual(page.get_by_label('最少字数', exact=True).input_value(), '10')
            self.assertEqual(page.get_by_label('最多字数', exact=True).input_value(), '100')
            self.assertEqual(self.flow.db.question_library.count_documents({'root_id': ObjectId(source['root_id'])}), 2)
            self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            page.get_by_role('button', name='保存新版本', exact=True).click()
            page.wait_for_url('**/question-library/*', wait_until='domcontentloaded')
            self.assertEqual(page.locator('h1').inner_text(), new_title)
            self.assertNotIn('from_survey', parse_qs(urlsplit(page.url).query))
            saved = self.flow.db.question_library.find_one(
                {'root_id': ObjectId(source['root_id']), 'version': 3})
            self.assertEqual(saved['title'], new_title)
            self.assertEqual(saved['min_length'], 10)
            self.assertEqual(saved['max_length'], 100)
            self.assertEqual(saved['parent_id'], ObjectId(source['id']))
            self.assertEqual(self.flow.db.question_library.find_one(
                {'_id': ObjectId(source['id'])}), source_before)
            page.get_by_role('link', name='← 返回题目库', exact=True).click()
            self.assertEqual(urlsplit(page.url).path, '/question-library')
            self.assertIn(new_title, page.locator('body').inner_text())

            # Both original-survey and question-selection return contexts may expire independently.
            for key in ('from_survey', 'survey'):
                result = page.goto(self.base + f"/question-library/{source['id']}?{key}={survey['_id']}")
                self.assertEqual(result.status, 200)
                self.assertIn('原问卷已不存在或无法访问', page.locator('body').inner_text())
                self.assertEqual(page.get_by_role('button', name='加入并继续选题').count(), 0)
                page.get_by_role('link', name='← 返回题目库', exact=True).click()
                self.assertEqual(urlsplit(page.url).path, '/question-library')

            # A foreign return destination conveys no access to that survey or a private question.
            outsider = self.flow.app.test_client()
            other_name = 'library_other_' + uuid4().hex
            self.flow.login(outsider, other_name)
            other_survey = self.flow.survey(outsider, other_name, '不可展示的他人问卷名称')
            result = page.goto(self.base + f"/question-library/{source['id']}?from_survey={other_survey['_id']}")
            self.assertEqual(result.status, 404)
            self.assertNotIn(other_survey['title'], page.locator('body').inner_text())
            self.assertEqual(owner.get(f"/question-library/{source['id']}/version?from_survey={other_survey['_id']}").status_code, 404)
            self.assertEqual(outsider.get(f"/question-library/{source['id']}?from_survey={other_survey['_id']}").status_code, 404)
            self.assertEqual(outsider.get(f"/question-library/{source['id']}/version?from_survey={other_survey['_id']}").status_code, 404)
            self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            self.assertFalse(errors, errors)
        finally:
            context.close()


if __name__ == '__main__':
    unittest.main()
