"""Read-only legacy answer presentation and deterministic list ordering."""
import re
import sys
import threading
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from tests import test_stage2_library as library_tests
from tests import test_creator_navigation as creator_tests


class HistoricalReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        library_tests.LibraryFlowTests.setUpClass()
        cls.flow = library_tests.LibraryFlowTests()
        cls.server = make_server('127.0.0.1', 0, cls.flow.app, threaded=True)
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel='msedge' if sys.platform == 'win32' else None, headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        library_tests.LibraryFlowTests.tearDownClass()

    def test_removed_questions_do_not_duplicate_numbers_in_personal_or_owner_review(self):
        for width in (390, 1280):
            with self.subTest(width=width):
                owner, user, survey, context, page = creator_tests.CreatorNavigationTests.scenario(self, width)
                questions = self.flow.db.surveys.find_one({'_id': survey['_id']})['questions']
                questions[0]['active'] = False
                self.flow.db.surveys.update_one({'_id': survey['_id']}, {'$set': {
                    'questions': questions, 'status': 'published'}})
                configurations = [
                    (questions, list(range(1, 25)), True),
                    (questions[1:], list(range(1, 24)), False),
                    ([questions[0], questions[-1]], [1, 24], True),
                    ([questions[1], questions[-1]], [1, 23], False),
                ]
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                try:
                    for answered, expected, historical in configurations:
                        document = {'survey_id': survey['_id'], 'respondent_id': user['_id'],
                            'submitted_at': datetime.now(timezone.utc),
                            'answers': [{'question_id': q['id'], 'question_version': 1,
                                         'value': '安静阅览' if q['type'] == 'single_choice' else '保留的原始回答'}
                                        for q in answered]}
                        rid = self.flow.db.responses.insert_one(document).inserted_id
                        original = self.flow.db.responses.find_one({'_id': rid})
                        for path in (f'/responses/{rid}', f"/surveys/{survey['_id']}/responses/{rid}"):
                            page.goto(self.base + path)
                            numbers = [int(value) for value in page.locator('.answer-review .stat-question-number').all_text_contents()]
                            self.assertEqual(numbers, expected[:20])
                            self.assertEqual(page.get_by_text('这份旧答卷包含已移除的题目，题号按原问卷顺序显示。', exact=True).count(), int(historical))
                            if len(expected) > 20:
                                page.get_by_role('navigation', name='答卷分页').get_by_role('link', name='下一页 →').click()
                                numbers += [int(value) for value in page.locator('.answer-review .stat-question-number').all_text_contents()]
                            self.assertEqual(numbers, expected)
                            self.assertEqual(len(numbers), len(set(numbers)))
                            self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                        self.assertEqual(self.flow.db.responses.find_one({'_id': rid}), original)
                    self.assertEqual(errors, [])
                finally:
                    context.close()

    def test_tied_timestamps_use_same_stable_order_in_lists_dashboard_and_json(self):
        owner = self.flow.app.test_client()
        username = 'same_time_' + uuid4().hex
        self.flow.login(owner, username)
        user = self.flow.db.users.find_one({'username': username})
        seed = self.flow.survey(owner, username, '同秒创建')
        self.flow.db.surveys.delete_one({'_id': seed['_id']})
        now = datetime.now(timezone.utc)
        ids = self.flow.db.surveys.insert_many([
            {**{key: value for key, value in seed.items() if key != '_id'},
             'slug': uuid4().hex, 'title': f'同秒创建 {i:02d}', 'created_at': now}
            for i in range(25)]).inserted_ids
        expected = [str(key) for key in reversed(ids)]
        found = []
        for page_number in (1, 2):
            html = owner.get(f'/surveys?q=同秒创建&page={page_number}').get_data(as_text=True)
            found += re.findall(r'class="survey-manage-link" href="/surveys/([0-9a-f]+)', html)
        self.assertEqual(found, expected)
        api = owner.get('/api/v1/surveys').get_json()['surveys']
        self.assertEqual([row['id'] for row in api], expected)
        dashboard = owner.get('/dashboard').get_data(as_text=True)
        self.assertIn('同秒创建 24', dashboard)
        self.assertNotIn('同秒创建 00', dashboard)
        response_ids = self.flow.db.responses.insert_many([
            {'survey_id': ids[0], 'respondent_id': user['_id'], 'submitted_at': now, 'answers': []}
            for _ in range(25)]).inserted_ids
        api = owner.get('/api/v1/responses').get_json()['responses']
        self.assertEqual([row['id'] for row in api], [str(key) for key in reversed(response_ids)])
