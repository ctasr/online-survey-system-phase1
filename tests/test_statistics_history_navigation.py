"""Historical raw-answer paging keeps the selected question version."""
import sys
import threading
import unittest
from uuid import uuid4
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from tests import test_stage2_library as library_tests


class StatisticsHistoryNavigationTests(unittest.TestCase):
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

    def test_history_pagination_and_account_return_keep_selected_version(self):
        owner = self.flow.app.test_client()
        username = 'history_pages_' + uuid4().hex
        self.flow.login(owner, username)
        user = self.flow.db.users.find_one({'username': username})
        survey = self.flow.survey(owner, username, '历史意见统计')
        questions = [{'id': 'opinion', 'title': f'活动建议第 {v} 版', 'type': 'text',
                      'version': v, 'required': True, 'active': True} for v in (1, 2, 3)]
        self.flow.db.surveys.update_one({'_id': survey['_id']}, {'$set': {
            'questions': questions[-1:], 'question_history': questions[:-1]}})
        self.flow.db.responses.insert_many([
            {'survey_id': survey['_id'], 'survey_title': survey['title'],
             'respondent_id': user['_id'], 'submitted_at': datetime.now(timezone.utc),
             'answers': [{'question_id': 'opinion', 'question_version': v,
                          'value': f'版本 {v} 建议 {i:02d}'}]}
            for v in (1, 2, 3) for i in range(1, 26)])
        for width in (390, 1280):
            with self.subTest(width=width):
                context = self.browser.new_context(viewport={'width': width, 'height': 844})
                cookie = self.flow.app.session_interface.get_signing_serializer(self.flow.app).dumps(
                    {'user_id': str(user['_id'])})
                context.add_cookies([{'name': self.flow.app.config['SESSION_COOKIE_NAME'],
                                     'value': cookie, 'url': self.base}])
                page = context.new_page()
                page.set_default_timeout(6000)
                try:
                    page.goto(self.base + f"/surveys/{survey['_id']}/statistics/opinion")
                    page.locator('.historical-statistics > summary').click()
                    old = page.locator('#statistics-version-1-opinion')
                    old.get_by_role('link', name='下一页 →', exact=True).click()
                    self.assertEqual(parse_qs(urlsplit(page.url).query)['version'], ['1'])
                    self.assertEqual(urlsplit(page.url).fragment, 'statistics-version-1-opinion')
                    self.assertTrue(page.locator('.historical-statistics').get_attribute('open') is not None)
                    self.assertEqual(old.locator('.stat-raw-answers li').count(), 5)
                    self.assertEqual(page.locator('#statistics-version-2-opinion .stat-raw-answers li').count(), 20)
                    self.assertEqual(page.locator('#statistics-version-3-opinion .stat-raw-answers li').count(), 20)
                    self.assertIn('第 1 版', old.locator('h2').inner_text())
                    old.locator('.stat-answer-preview a').first.click()
                    self.assertIn('第 1 版', page.locator('.eyebrow').inner_text())
                    page.get_by_role('link', name='← 返回单题统计', exact=True).click()
                    self.assertEqual(parse_qs(urlsplit(page.url).query)['version'], ['1'])
                    self.assertTrue(old.is_visible())
                    self.assertEqual(parse_qs(urlsplit(page.url).query).get('answer_page'), ['2'])
                    self.assertEqual(old.locator('.stat-raw-answers li').count(), 5)
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                    # The source answer page and whole-survey filters are independent.
                    for version in (1, 3):
                        page.goto(self.base + f"/surveys/{survey['_id']}/statistics/opinion"
                                  f"?version={version}&answer_page=2&stats_view=cards&stats_q=活动&stats_page=2&stats_hpage=3")
                        section = page.locator(f'#statistics-version-{version}-opinion')
                        section.locator('.stat-answer-preview a').first.click()
                        self.assertEqual(parse_qs(urlsplit(page.url).query)['stats_answer_page'], ['2'])
                        page.get_by_role('link', name='清除筛选', exact=True).click()
                        page.locator('input[name=q]').fill(f'版本 {version}')
                        page.get_by_role('button', name='查找', exact=True).click()
                        page.get_by_role('navigation', name='本题账号回答分页').get_by_role('link', name='下一页 →').click()
                        page.get_by_role('link', name='查看答卷 →').first.click()
                        page.get_by_role('link', name='← 本题账号回答', exact=True).click()
                        self.assertEqual(parse_qs(urlsplit(page.url).query)['page'], ['2'])
                        self.assertEqual(page.locator('.account-responses-table tbody tr').count(), 5)
                        page.get_by_role('link', name='← 返回单题统计', exact=True).click()
                        returned = parse_qs(urlsplit(page.url).query)
                        self.assertEqual(returned['version'], [str(version)])
                        self.assertEqual(returned['answer_page'], ['2'])
                        self.assertEqual(section.locator('.stat-raw-answers li').count(), 5)
                        overview = parse_qs(urlsplit(page.get_by_role('link', name='← 返回整卷统计').get_attribute('href')).query)
                        self.assertEqual(overview, {'view': ['cards'], 'q': ['活动'], 'page': ['2'], 'hpage': ['3']})
                        self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                    # An unknown version must not silently show an unrelated version.
                    denied = owner.get(f"/surveys/{survey['_id']}/statistics/opinion?version=99")
                    self.assertEqual(denied.status_code, 404)
                finally:
                    context.close()
