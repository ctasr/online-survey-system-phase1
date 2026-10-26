"""Exercise library entry, state changes, and completion without losing the origin."""
import sys
import threading
import unittest
from uuid import uuid4
from urllib.parse import urlencode, urlsplit, parse_qs
from html import unescape
import re

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from tests import test_stage2_library as library_tests


class LibraryEntryMatrixTests(unittest.TestCase):
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

    def make_owner_and_item(self):
        owner = self.flow.app.test_client()
        name = 'entry_matrix_' + uuid4().hex
        self.flow.login(owner, name)
        user = self.flow.db.users.find_one({'username': name})
        item = self.flow.api(owner, 'POST', '/api/v1/library/questions', {
            'type': 'text', 'title': '校园通勤有哪些需要改善的地方？', 'required': False}).get_json()['question']
        return owner, name, user, item

    def test_survey_entry_matrix_preserves_readonly_origin_and_permissions(self):
        owner, name, user, item = self.make_owner_and_item()
        survey = self.flow.survey(owner, name, '校园通勤调查')
        sid = str(survey['_id'])
        self.flow.api(owner, 'POST', f'/api/v1/surveys/{sid}/library-questions', {'question_id': item['id']})
        path = f"/question-library/{item['id']}?" + urlencode({'survey': sid, 'q': '通勤', 'mine_page': 2})
        for status in ('draft', 'published', 'closed', 'deleted'):
            with self.subTest(status=status):
                if status == 'published': self.flow.post(owner, f'/surveys/{sid}/publish', {})
                elif status == 'closed': self.flow.post(owner, f'/surveys/{sid}/close', {})
                elif status == 'deleted': self.flow.api(owner, 'DELETE', f'/api/v1/surveys/{sid}', {})
                response = owner.get(path)
                self.assertEqual(response.status_code, 200)
                body = response.get_data(as_text=True)
                back = unescape(re.search(r'<p class="library-back"><a href="([^"]+)"', body)[1])
                expected = f'/surveys/{sid}/question-library' if status == 'draft' else (
                    '/question-library' if status == 'deleted' else f'/surveys/{sid}')
                self.assertEqual(urlsplit(back).path, expected)
                if status in ('published', 'closed'):
                    self.assertIn('不能再添加题目', body)
                    self.assertNotIn('加入并继续选题', body)
                    self.assertIn('from_survey=' + sid, body)
                elif status == 'draft':
                    self.assertEqual(parse_qs(urlsplit(back).query)['mine_page'], ['2'])
                else:
                    self.assertIn('原问卷已不存在', body)
        other = self.flow.app.test_client(); other_name='matrix_other_'+uuid4().hex
        self.flow.login(other, other_name)
        private = self.flow.survey(other, other_name, '其他账号的私有问卷')
        for key in ('survey', 'from_survey'):
            self.assertEqual(owner.get(f"/question-library/{item['id']}?{key}={private['_id']}").status_code, 404)

        foreign_bank=self.flow.api(other,'POST','/api/v1/library/banks',{'name':'他人题库'}).get_json()['id']
        denied=self.flow.post(owner,f"/question-banks/{foreign_bank}/questions/{item['id']}",
                              {'return_to_detail':'yes','q':'通勤'})
        self.assertEqual(urlsplit(denied.location).path,'/question-library/'+item['id'])
        from bson import ObjectId
        self.assertEqual(self.flow.db.question_banks.find_one({'_id':ObjectId(foreign_bank)})['question_ids'],[])
        target_bank=self.flow.api(owner,'POST','/api/v1/library/banks',{'name':'撤回后的目标题库'}).get_json()['id']
        for used in (False,True):
            with self.subTest(withdrawn_shared=True, used=used):
                source=self.flow.api(other,'POST','/api/v1/library/questions',
                    {'type':'text','title':'曾经共享的通勤题'}).get_json()['question']
                self.flow.api(other,'PATCH',f"/api/v1/library/questions/{source['id']}/sharing",{'shared':True})
                if used:
                    kept=self.flow.survey(owner,name,'保留原题的问卷')
                    self.flow.api(owner,'POST',f"/api/v1/surveys/{kept['_id']}/library-questions",{'question_id':source['id']})
                self.flow.api(other,'PATCH',f"/api/v1/library/questions/{source['id']}/sharing",{'shared':False})
                response=self.flow.post(owner,f"/question-banks/{target_bank}/questions/{source['id']}",
                    {'return_to_detail':'yes','q':'通勤','mine_page':'2'})
                expected='/question-library/'+source['id'] if used else '/question-library'
                self.assertEqual(urlsplit(response.location).path,expected)
                self.assertEqual(owner.get(response.location).status_code,200)
                self.assertEqual(self.flow.db.question_banks.find_one({'_id':ObjectId(target_bank)})['question_ids'],[])

    def test_detail_bank_actions_keep_all_entry_contexts_and_report_stale_destination(self):
        owner, name, user, item = self.make_owner_and_item()
        draft = self.flow.survey(owner, name, '通勤草稿')
        published = self.flow.survey(owner, name, '通勤已发布')
        for survey in (draft, published):
            self.flow.api(owner, 'POST', f"/api/v1/surveys/{survey['_id']}/library-questions", {'question_id': item['id']})
        self.flow.post(owner, f"/surveys/{published['_id']}/publish", {})
        origin_bank=self.flow.api(owner, 'POST', '/api/v1/library/banks', {'name':'出发题库'}).get_json()['id']
        self.flow.api(owner, 'POST', f'/api/v1/library/banks/{origin_bank}/questions', {'question_id':item['id']})
        origins=[{'q':'通勤','mine_page':2,'shared_page':1},
                 {'bank':origin_bank,'return_bank':1},
                 {'survey':str(draft['_id']),'q':'通勤','mine_page':2,'shared_page':1},
                 {'from_survey':str(published['_id'])}]
        for width in (390, 1280):
            for index, params in enumerate(origins):
                with self.subTest(width=width, origin=index):
                    names=[f'目标 {width}-{index} {suffix}' for suffix in ('甲','乙','丙')]
                    banks=[self.flow.api(owner,'POST','/api/v1/library/banks',{'name':label}).get_json()['id'] for label in names]
                    context=self.browser.new_context(viewport={'width':width,'height':844})
                    cookie=self.flow.app.session_interface.get_signing_serializer(self.flow.app).dumps({'user_id':str(user['_id'])})
                    context.add_cookies([{'name':self.flow.app.config['SESSION_COOKIE_NAME'],'value':cookie,'url':self.base}])
                    page=context.new_page();page.set_default_timeout(6000)
                    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    try:
                        page.goto(self.base+f"/question-library/{item['id']}?"+urlencode(params))
                        original_back=page.locator('.library-back a').get_attribute('href')
                        for label in names[:2]:
                            with page.expect_navigation():
                                page.get_by_role('button',name='加入「'+label+'」',exact=True).click()
                            self.assertEqual(urlsplit(page.url).path, '/question-library/'+item['id'])
                            self.assertEqual(urlsplit(page.url).fragment, 'library-bank-actions')
                            self.assertEqual(page.evaluate('document.activeElement.id'), 'library-bank-actions')
                            self.assertEqual(page.locator('.library-back a').get_attribute('href'),original_back)
                            self.assertTrue(page.get_by_text('「'+label+'」已收录',exact=True).is_visible())
                        self.flow.api(owner,'DELETE',f'/api/v1/library/banks/{banks[2]}',{})
                        with page.expect_navigation():
                            page.get_by_role('button',name='加入「'+names[2]+'」',exact=True).click()
                        self.assertEqual(urlsplit(page.url).path, '/question-library/'+item['id'])
                        self.assertIn('本次没有加入题库',page.locator('[role="alert"]').inner_text())
                        self.assertTrue(page.locator('#library-bank-actions [role="alert"]').is_visible())
                        self.assertLess(page.locator('#library-bank-actions').bounding_box()['y'],844)
                        self.assertEqual(page.locator('.library-back a').get_attribute('href'),original_back)
                        self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                        self.assertFalse(errors,errors)
                    finally:context.close()
