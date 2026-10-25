"""Usable HTML failure pages keep HTTP semantics and the previous form."""
import sys
import threading
import unittest
from uuid import uuid4
from unittest.mock import patch
from pymongo.errors import DuplicateKeyError

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from tests import test_stage2_library as library_tests


class ErrorPageExperienceTests(unittest.TestCase):
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

    def test_html_failures_offer_return_while_api_errors_and_allow_headers_stay_correct(self):
        owner=self.flow.app.test_client();name='error_return_'+uuid4().hex
        self.flow.login(owner,name);user=self.flow.db.users.find_one({'username':name})
        item=self.flow.api(owner,'POST','/api/v1/library/questions',{'type':'text','title':'错误页返回检查'}).get_json()['question']
        before=self.flow.db.question_library.count_documents({})
        checks=[(owner.post('/question-library/new',data={'csrf_token':'expired'}),400,'请求未能完成'),
                (owner.get('/question-banks'),405,'此页面不支持该操作'),
                (owner.get('/missing-page-for-check'),404,'页面不存在')]
        with patch('app.question_bank.make_version',side_effect=DuplicateKeyError('concurrent version')):
            checks.append((self.flow.post(owner,f"/question-library/{item['id']}/restore",{}),409,'操作状态已变化'))
        checks.append((owner.post('/question-library/new',data={'title':'x'*600001},content_type='multipart/form-data'),413,'内容超出请求限制'))
        for response,status,title in checks:
            with self.subTest(status=status):
                self.assertEqual(response.status_code,status)
                text=response.get_data(as_text=True)
                self.assertIn(title,text);self.assertIn('data-error-back',text)
                self.assertIn('返回个人首页',text)
        self.assertIn('POST',checks[1][0].headers['Allow'])
        api_error=owner.get('/api/v1/auth/logout')
        self.assertEqual(api_error.status_code,405)
        self.assertTrue(api_error.is_json)
        self.assertIn('error',api_error.get_json())
        self.assertIn('POST',api_error.headers['Allow'])
        self.assertEqual(self.flow.db.question_library.count_documents({}),before)
        context=self.browser.new_context(viewport={'width':390,'height':844})
        cookie=self.flow.app.session_interface.get_signing_serializer(self.flow.app).dumps({'user_id':str(user['_id'])})
        context.add_cookies([{'name':self.flow.app.config['SESSION_COOKIE_NAME'],'value':cookie,'url':self.base}])
        page=context.new_page();page.set_default_timeout(6000)
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        try:
            page.goto(self.base+'/question-library/new')
            page.get_by_label('题目内容',exact=True).fill('失效提交后应保留的题目')
            page.locator('[data-options-rows] input').nth(0).fill('校内')
            page.locator('[data-options-rows] input').nth(1).fill('校外')
            page.locator('[data-question-form] [name="csrf_token"]').evaluate("el=>el.value='expired'")
            page.get_by_role('button',name='保存到题目库',exact=True).click()
            page.get_by_role('heading',name='请求未能完成',exact=True).wait_for()
            self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
            page.get_by_role('button',name='返回上一页',exact=True).click()
            page.wait_for_url(self.base+'/question-library/new')
            self.assertEqual(page.get_by_label('题目内容',exact=True).input_value(),'失效提交后应保留的题目')
            self.assertEqual(page.locator('[data-options-rows] input').nth(0).input_value(),'校内')
            self.assertEqual(self.flow.db.question_library.count_documents({}),before)
            page.get_by_role('button',name='保存到题目库',exact=True).click()
            page.wait_for_url('**/question-library/*')
            self.assertNotIn('/new',page.url)
            self.assertEqual(self.flow.db.question_library.count_documents({}),before+1)
            fresh=context.new_page()
            fresh.goto(self.base+'/missing-page-for-check')
            self.assertEqual(fresh.evaluate('document.referrer'),'')
            fresh.get_by_role('button',name='返回上一页',exact=True).click()
            fresh.wait_for_url(self.base+'/dashboard')
            fresh.close()
            self.assertFalse(errors,errors)
        finally:context.close()
