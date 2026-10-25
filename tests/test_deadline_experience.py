"""Deadline correction, no-op saves and concurrent state changes."""
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from playwright.sync_api import sync_playwright
from pymongo.collection import Collection
from werkzeug.serving import make_server

from tests import test_stage2_library as library_tests
from app.surveys import CHINA_TIME


class DeadlineExperienceTests(unittest.TestCase):
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

    def create_survey(self):
        owner = self.flow.app.test_client()
        username = 'deadline_' + uuid4().hex
        self.flow.login(owner, username)
        user = self.flow.db.users.find_one({'username': username})
        survey = self.flow.survey(owner, username, '校园阅读活动报名')
        self.flow.db.surveys.update_one({'_id': survey['_id']}, {'$set': {'questions': [
            {'id': 'reading', 'title': '你希望阅读哪类书籍？', 'type': 'text',
             'required': True, 'version': 1, 'active': True}]}})
        return owner, user, survey

    def test_inline_deadline_correction_conflict_retry_and_closed_form(self):
        for width in (390, 1280):
            with self.subTest(width=width):
                owner, user, survey = self.create_survey()
                path = f"/surveys/{survey['_id']}"
                context = self.browser.new_context(viewport={'width': width, 'height': 900})
                cookie = self.flow.app.session_interface.get_signing_serializer(self.flow.app).dumps({'user_id': str(user['_id'])})
                context.add_cookies([{'name': self.flow.app.config['SESSION_COOKIE_NAME'], 'value': cookie, 'url': self.base}])
                page = context.new_page()
                page.set_default_timeout(6000)
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                try:
                    page.goto(self.base + path)
                    panel = page.locator('.deadline-settings')
                    panel.locator('summary').click()
                    today = datetime.now(CHINA_TIME).strftime('%Y-%m-%d')
                    panel.locator('[data-deadline-date]').fill(today)
                    panel.locator('[data-deadline-time]').fill('00:00')
                    with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/deadline')) as rejected:
                        panel.get_by_role('button', name='保存', exact=True).click()
                    self.assertEqual(rejected.value.status, 422)
                    self.assertIsNotNone(panel.get_attribute('open'))
                    self.assertEqual(panel.locator('[data-deadline-date]').input_value(), today)
                    self.assertEqual(panel.locator('[data-deadline-time]').input_value(), '00:00')
                    self.assertIn('晚于当前时间', panel.get_by_role('alert').inner_text())
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                    self.assertIsNone(self.flow.db.surveys.find_one({'_id': survey['_id']})['deadline'])
                    panel.locator('[data-deadline-days="1"]').click()
                    pending = panel.locator('[data-deadline-value]').input_value()
                    self.flow.db.surveys.update_one({'_id': survey['_id']}, {
                        '$set': {'description': '另一页已保存的说明'}, '$inc': {'revision': 1}})
                    with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/deadline')) as conflict:
                        panel.get_by_role('button', name='保存', exact=True).click()
                    self.assertEqual(conflict.value.status, 409)
                    self.assertEqual(panel.locator('[data-deadline-value]').input_value(), pending)
                    self.assertIn('输入已保留', panel.get_by_role('alert').inner_text())
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                    panel.get_by_role('button', name='保存', exact=True).click()
                    page.wait_for_url(self.base + path + '#deadline-settings')
                    self.assertIn('截止时间已更新', panel.locator('.notice').inner_text())
                    saved = self.flow.db.surveys.find_one({'_id': survey['_id']})
                    self.assertEqual(saved['description'], '另一页已保存的说明')
                    original_deadline = saved['deadline']
                    panel.locator('[data-deadline-days="7"]').click()
                    final_attempt = panel.locator('[data-deadline-value]').input_value()
                    original_update = Collection.update_one
                    def collide(collection, query, update, *args, **kwargs):
                        if collection.name == 'surveys' and query.get('_id') == survey['_id'] and 'deadline' in update.get('$set', {}):
                            original_update(collection, {'_id': survey['_id']}, {
                                '$set': {'description': '最终写入前另一页再次修改'}, '$inc': {'revision': 1}})
                        return original_update(collection, query, update, *args, **kwargs)
                    with patch.object(Collection, 'update_one', collide):
                        with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/deadline')) as final_conflict:
                            panel.get_by_role('button', name='保存', exact=True).click()
                        self.assertEqual(final_conflict.value.status, 409)
                    self.assertEqual(panel.locator('[data-deadline-value]').input_value(), final_attempt)
                    self.assertEqual(self.flow.db.surveys.find_one({'_id': survey['_id']})['deadline'], original_deadline)
                    self.flow.post(owner, path + '/publish', {})
                    self.flow.post(owner, path + '/close', {})
                    with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/deadline')) as closed:
                        panel.get_by_role('button', name='保存', exact=True).click()
                    self.assertEqual(closed.value.status, 409)
                    self.assertIn('已关闭', page.get_by_role('alert').inner_text())
                    self.assertEqual(page.locator('.deadline-settings').count(), 0)
                    self.assertEqual(self.flow.db.surveys.find_one({'_id': survey['_id']})['deadline'], original_deadline)
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                    self.assertEqual(errors, [])
                finally:
                    context.close()

    def test_expired_unchanged_deadline_is_noop_but_new_past_dates_and_closed_writes_are_rejected(self):
        owner, user, survey = self.create_survey()
        path = f"/surveys/{survey['_id']}"
        api_path = '/api/v1' + path
        old = (datetime.now(timezone.utc) - timedelta(days=2)).replace(second=0, microsecond=0)
        local_old = old.astimezone(CHINA_TIME).strftime('%Y-%m-%dT%H:%M')
        new_past = (old + timedelta(hours=1)).astimezone(CHINA_TIME).strftime('%Y-%m-%dT%H:%M')
        self.flow.db.surveys.update_one({'_id': survey['_id']}, {'$set': {'deadline': old, 'status': 'published'}})
        original = self.flow.db.surveys.find_one({'_id': survey['_id']})
        with self.subTest(operation='unchanged_web'):
            result = self.flow.post(owner, path + '/deadline', {'deadline': local_old, 'revision': '1'})
            self.assertEqual(result.status_code, 302)
            self.assertEqual(self.flow.db.surveys.find_one({'_id': survey['_id']}), original)
        with self.subTest(operation='unchanged_api'):
            result = self.flow.api(owner, 'PATCH', api_path, {'deadline': local_old, 'revision': 1})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(self.flow.db.surveys.find_one({'_id': survey['_id']}), original)
        for raw in (new_past, '不是日期'):
            with self.subTest(operation='invalid', raw=raw):
                self.assertEqual(self.flow.post(owner, path + '/deadline', {'deadline': raw, 'revision': '1'}).status_code, 422)
                self.assertEqual(self.flow.api(owner, 'PATCH', api_path, {'deadline': raw, 'revision': 1}).status_code, 422)
                self.assertEqual(self.flow.db.surveys.find_one({'_id': survey['_id']}), original)
        self.flow.db.surveys.update_one({'_id': survey['_id']}, {'$set': {'status': 'closed'}})
        closed = self.flow.db.surveys.find_one({'_id': survey['_id']})
        future = (datetime.now(CHINA_TIME) + timedelta(days=2)).strftime('%Y-%m-%dT%H:%M')
        with self.subTest(operation='closed'):
            self.assertEqual(self.flow.post(owner, path + '/deadline', {'deadline': future, 'revision': '1'}).status_code, 409)
            self.assertEqual(self.flow.api(owner, 'PATCH', api_path, {'deadline': future, 'revision': 1}).status_code, 409)
            self.assertEqual(self.flow.db.surveys.find_one({'_id': survey['_id']}), closed)
