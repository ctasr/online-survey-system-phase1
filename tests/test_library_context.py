"""Keep independent library edits usable when their return destination disappears."""
import sys
import threading
import unittest
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

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
            page.get_by_role('link', name='创建题库新版本 →', exact=True).click()
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
            page.get_by_role('button', name='保存新版本', exact=True).click()
            page.wait_for_url('**/question-library/*', wait_until='domcontentloaded')
            self.assertEqual(page.locator('h1').inner_text(), new_title)
            self.assertNotIn('from_survey', parse_qs(urlsplit(page.url).query))
            saved = self.flow.db.question_library.find_one(
                {'root_id': ObjectId(source['root_id']), 'version': 2})
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
