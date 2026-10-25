"""Keep long question-bank operations near the item being changed."""
import sys
import threading
import unittest
from uuid import uuid4
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from tests import test_stage2_library as library_tests


class BankNavigationTests(unittest.TestCase):
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

    def test_update_remove_and_unavailable_target_keep_bank_position(self):
        owner = self.flow.app.test_client()
        username = 'bank_navigation_' + uuid4().hex
        self.flow.login(owner, username)
        user = self.flow.db.users.find_one({'username': username})
        bank_id = self.flow.api(owner, 'POST', '/api/v1/library/banks', {'name': '校园服务常用题'}).get_json()['id']
        items = []
        for index in range(15):
            item = self.flow.api(owner, 'POST', '/api/v1/library/questions', {
                'type': 'text', 'title': f'校园服务改进建议 {index + 1}', 'required': False}).get_json()['question']
            self.flow.api(owner, 'POST', f'/api/v1/library/banks/{bank_id}/questions', {'question_id': item['id']})
            items.append(item)
        last = items[-1]
        newer = self.flow.api(owner, 'POST', f"/api/v1/library/questions/{last['id']}/versions", {
            'type': 'text', 'title': '校园服务改进建议（补充说明）', 'required': False}).get_json()['question']
        for width in (390, 1280):
            with self.subTest(width=width):
                # Restore only this isolated bank between screen widths.
                from bson import ObjectId
                self.flow.db.question_banks.update_one({'_id': ObjectId(bank_id)},
                    {'$set': {'question_ids': [ObjectId(item['id']) for item in items]}})
                context = self.browser.new_context(viewport={'width': width, 'height': 844})
                cookie = self.flow.app.session_interface.get_signing_serializer(self.flow.app).dumps({'user_id': str(user['_id'])})
                context.add_cookies([{'name': self.flow.app.config['SESSION_COOKIE_NAME'], 'value': cookie, 'url': self.base}])
                page = context.new_page()
                page.set_default_timeout(6000)
                try:
                    page.goto(self.base + f'/question-banks/{bank_id}')
                    source = page.locator('.library-bank-item').filter(has_text='校园服务改进建议 15')
                    source.get_by_role('button', name='更新至 v2', exact=True).click()
                    page.locator('[data-confirm-proceed]').click()
                    page.wait_for_url(lambda u: urlsplit(str(u)).fragment == 'bank-question-' + newer['id'])
                    target = page.locator('#bank-question-' + newer['id'])
                    self.assertTrue(target.is_visible())
                    self.assertIn('题库版本已更新', target.locator('[role="status"]').inner_text())
                    self.assertEqual(page.evaluate('document.activeElement.id'), 'bank-question-' + newer['id'])
                    self.assertGreater(page.evaluate('scrollY'), 500)
                    target.get_by_role('button', name='从题库移除', exact=True).click()
                    page.wait_for_url(lambda u: urlsplit(str(u)).fragment == 'bank-question-' + items[-2]['id'])
                    self.assertEqual(page.locator('.library-bank-item').count(), 14)
                    self.assertGreater(page.evaluate('scrollY'), 500)
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                finally:
                    context.close()
        # Missing target is a conflict, and must return to the existing source card.
        response = self.flow.post(owner, f"/question-banks/{bank_id}/questions/{items[0]['id']}",
                                  {'action': 'update', 'target_id': '000000000000000000000000'})
        self.assertEqual(urlsplit(response.location).fragment, 'bank-question-' + items[0]['id'])
        self.assertEqual(owner.get(response.location).status_code, 200)
