"""Publication, closing and deletion act on the state the owner reviewed."""
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from unittest.mock import patch

from pymongo.collection import Collection
from tests import test_deadline_experience as deadline_tests


class LifecycleExperienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parent = deadline_tests.DeadlineExperienceTests
        parent.setUpClass()
        cls.flow, cls.browser, cls.base = parent.flow, parent.browser, parent.base

    @classmethod
    def tearDownClass(cls):
        deadline_tests.DeadlineExperienceTests.tearDownClass()

    def scenario(self):
        return deadline_tests.DeadlineExperienceTests.create_survey(self)

    def test_owner_reviews_latest_state_before_publish_close_and_delete(self):
        for width in (390, 1280):
            with self.subTest(width=width):
                owner, user, survey = self.scenario()
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
                    self.flow.post(owner, path + '/questions/new', {'type': 'text', 'title': '另一页新增的活动建议'})
                    with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/publish')) as stale:
                        page.get_by_role('button', name='发布问卷', exact=True).click()
                    self.assertEqual(stale.value.status, 409)
                    self.assertIn('尚未发布', page.get_by_role('alert').inner_text())
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                    self.assertIn('另一页新增的活动建议', page.locator('[data-question-list]').inner_text())
                    self.assertEqual(self.flow.db.surveys.find_one({'_id': survey['_id']})['status'], 'draft')
                    page.get_by_role('button', name='发布问卷', exact=True).click()
                    page.wait_for_url(self.base + path)
                    self.assertEqual(self.flow.db.surveys.find_one({'_id': survey['_id']})['status'], 'published')
                    page.get_by_role('button', name='关闭问卷', exact=True).click()
                    page.locator('[data-confirm-cancel]').click()
                    self.assertEqual(self.flow.db.surveys.find_one({'_id': survey['_id']})['status'], 'published')
                    self.flow.post(owner, path + '/edit', {'title': survey['title'], 'description': '另一页改好的活动说明'})
                    page.get_by_role('button', name='关闭问卷', exact=True).click()
                    with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/close')) as close:
                        page.locator('[data-confirm-proceed]').click()
                    self.assertEqual(close.value.status, 409)
                    self.assertIn('尚未关闭', page.get_by_role('alert').inner_text())
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                    self.assertEqual(self.flow.db.surveys.find_one({'_id': survey['_id']})['status'], 'published')
                    page.get_by_role('button', name='关闭问卷', exact=True).click()
                    page.locator('[data-confirm-proceed]').click()
                    page.wait_for_url(self.base + path)
                    saved = self.flow.db.surveys.find_one({'_id': survey['_id']})
                    self.assertEqual(saved['status'], 'closed')
                    self.assertEqual(saved['description'], '另一页改好的活动说明')
                    self.flow.db.responses.insert_one({'survey_id': survey['_id'], 'respondent_id': user['_id'],
                        'submitted_at': datetime.now(timezone.utc), 'answers': []})
                    self.flow.db.response_drafts.insert_one({'survey_id': survey['_id'], 'user_id': user['_id'], 'answers': {}})
                    self.flow.post(owner, path + '/edit', {'title': '另一页修订的阅读活动', 'description': saved['description']})
                    page.locator('.survey-danger-zone > summary').click()
                    page.get_by_role('button', name='删除问卷', exact=True).click()
                    with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/delete')) as deleted:
                        page.locator('[data-confirm-proceed]').click()
                    self.assertEqual(deleted.value.status, 409)
                    self.assertIn('尚未删除', page.get_by_role('alert').inner_text())
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                    self.assertEqual(page.locator('h1').inner_text(), '另一页修订的阅读活动')
                    self.assertEqual(self.flow.db.responses.count_documents({'survey_id': survey['_id']}), 1)
                    self.assertEqual(self.flow.db.response_drafts.count_documents({'survey_id': survey['_id']}), 1)
                    page.locator('.survey-danger-zone > summary').click()
                    page.get_by_role('button', name='删除问卷', exact=True).click()
                    page.locator('[data-confirm-proceed]').click()
                    page.wait_for_url(self.base + '/surveys')
                    self.assertIsNone(self.flow.db.surveys.find_one({'_id': survey['_id']}))
                    self.assertEqual(self.flow.db.responses.count_documents({'survey_id': survey['_id']}), 0)
                    self.assertEqual(self.flow.db.response_drafts.count_documents({'survey_id': survey['_id']}), 0)
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                    self.assertEqual(errors, [])
                finally:
                    context.close()

    def test_lifecycle_compare_and_write_guards_web_and_json_without_false_success(self):
        for channel in ('web', 'api'):
            for action in ('publish', 'close', 'delete'):
                for timing in ('stale_form', 'final_write'):
                    with self.subTest(channel=channel, action=action, timing=timing):
                        owner, user, survey = self.scenario()
                        sid = survey['_id']
                        initial_state = 'published' if action == 'close' else 'draft'
                        self.flow.db.surveys.update_one({'_id': sid}, {'$set': {'status': initial_state}})
                        self.flow.db.responses.insert_one({'survey_id': sid, 'respondent_id': user['_id'],
                            'submitted_at': datetime.now(timezone.utc), 'answers': []})
                        self.flow.db.response_drafts.insert_one({'survey_id': sid, 'user_id': user['_id'], 'answers': {}})
                        original_update, original_delete = Collection.update_one, Collection.delete_one
                        def other_edit():
                            original_update(self.flow.db.surveys, {'_id': sid}, {
                                '$set': {'description': '最后由另一页保存的说明'}, '$inc': {'revision': 1}})
                        def collision(collection, query, update=None, *args, **kwargs):
                            if collection.name == 'surveys' and query.get('_id') == sid:
                                other_edit()
                            if action == 'delete':
                                return original_delete(collection, query, *args, **kwargs)
                            return original_update(collection, query, update, *args, **kwargs)
                        if timing == 'stale_form': other_edit()
                        method = 'delete_one' if action == 'delete' else 'update_one'
                        manager = patch.object(Collection, method, collision) if timing == 'final_write' else nullcontext()
                        with manager:
                            if channel == 'web':
                                response = self.flow.post(owner, f'/surveys/{sid}/{action}',
                                    {'revision': '1', 'expected_status': initial_state})
                            else:
                                endpoint = f'/api/v1/surveys/{sid}' + ('' if action == 'delete' else '/' + action)
                                response = self.flow.api(owner, 'DELETE' if action == 'delete' else 'POST', endpoint, {'revision': 1})
                        self.assertEqual(response.status_code, 409)
                        saved = self.flow.db.surveys.find_one({'_id': sid})
                        self.assertIsNotNone(saved)
                        self.assertEqual(saved['status'], initial_state)
                        self.assertEqual(saved['description'], '最后由另一页保存的说明')
                        self.assertEqual(self.flow.db.responses.count_documents({'survey_id': sid}), 1)
                        self.assertEqual(self.flow.db.response_drafts.count_documents({'survey_id': sid}), 1)
        # A status change need not change the question/settings revision.
        owner, user, survey = self.scenario()
        self.flow.db.surveys.update_one({'_id': survey['_id']}, {'$set': {'status': 'published'}})
        result = self.flow.post(owner, f"/surveys/{survey['_id']}/delete", {'revision': '1', 'expected_status': 'draft'})
        self.assertEqual(result.status_code, 409)
        self.assertIsNotNone(self.flow.db.surveys.find_one({'_id': survey['_id']}))
        # Repeated completed actions must not rewrite their timestamps.
        for action, initial in (('publish', 'draft'), ('close', 'published')):
            with self.subTest(repeat=action):
                owner, user, survey = self.scenario()
                sid = survey['_id']
                self.flow.db.surveys.update_one({'_id': sid}, {'$set': {'status': initial}})
                first = self.flow.post(owner, f'/surveys/{sid}/{action}', {})
                self.assertEqual(first.status_code, 302)
                owner.get(first.location)
                saved = self.flow.db.surveys.find_one({'_id': sid})
                again = self.flow.post(owner, f'/surveys/{sid}/{action}', {})
                self.assertEqual(again.status_code, 302)
                self.assertIn('已经发布' if action == 'publish' else '已经关闭', owner.get(again.location).get_data(as_text=True))
                self.assertEqual(self.flow.db.surveys.find_one({'_id': sid}), saved)
        # A final status change is independent of content revision.
        for channel in ('web', 'api'):
            with self.subTest(final_status=channel):
                owner, user, survey = self.scenario()
                sid = survey['_id']
                original_delete = Collection.delete_one
                def publish_before_delete(collection, query, *args, **kwargs):
                    if collection.name == 'surveys' and query.get('_id') == sid:
                        self.flow.db.surveys.update_one({'_id': sid}, {'$set': {'status': 'published'}})
                    return original_delete(collection, query, *args, **kwargs)
                with patch.object(Collection, 'delete_one', publish_before_delete):
                    result = (self.flow.post(owner, f'/surveys/{sid}/delete', {'revision': '1', 'expected_status': 'draft'})
                              if channel == 'web' else self.flow.api(owner, 'DELETE', f'/api/v1/surveys/{sid}', {'revision': 1}))
                self.assertEqual(result.status_code, 409)
                self.assertEqual(self.flow.db.surveys.find_one({'_id': sid})['status'], 'published')
        # Another page can complete an action while this request is in flight.
        for action, initial, desired in (('publish', 'draft', 'published'), ('close', 'published', 'closed'),
                                         ('delete', 'draft', None)):
            with self.subTest(completed_elsewhere=action):
                owner, user, survey = self.scenario()
                sid = survey['_id']
                self.flow.db.surveys.update_one({'_id': sid}, {'$set': {'status': initial}})
                original_update, original_delete = Collection.update_one, Collection.delete_one
                sentinel = datetime(2026, 1, 1)
                def completed_elsewhere(collection, query, update=None, *args, **kwargs):
                    if collection.name == 'surveys' and query.get('_id') == sid:
                        if action == 'delete': original_delete(collection, {'_id': sid})
                        else: original_update(collection, {'_id': sid}, {'$set': {
                            'status': desired, 'published_at' if action == 'publish' else 'closed_at': sentinel}})
                    if action == 'delete': return original_delete(collection, query, *args, **kwargs)
                    return original_update(collection, query, update, *args, **kwargs)
                with patch.object(Collection, 'delete_one' if action == 'delete' else 'update_one', completed_elsewhere):
                    result = self.flow.post(owner, f'/surveys/{sid}/{action}', {'revision': '1', 'expected_status': initial})
                self.assertEqual(result.status_code, 302)
                message = owner.get(result.location).get_data(as_text=True)
                self.assertIn('已经发布' if action == 'publish' else '已经关闭' if action == 'close' else '其他页面删除', message)
                saved = self.flow.db.surveys.find_one({'_id': sid})
                if desired:
                    self.assertEqual(saved['published_at' if action == 'publish' else 'closed_at'], sentinel)
                else:
                    self.assertIsNone(saved)

    def jump_scenario(self):
        owner, user, survey = self.scenario()
        questions = [
            {'id': 'start', 'type': 'number', 'title': '每周阅读时长', 'required': True,
             'active': True, 'version': 1, 'integer_only': False, 'min_value': None,
             'max_value': None, 'number_statistics': 'distribution', 'jumps': [
                 {'id': 'r1', 'kind': 'number', 'operator': 'gt', 'threshold': 8, 'target_id': 'last'},
                 {'id': 'r2', 'kind': 'number', 'operator': 'lt', 'threshold': 2, 'target_id': 'next'}]},
            {'id': 'next', 'type': 'text', 'title': '阅读建议', 'required': True, 'active': True, 'version': 1},
            {'id': 'last', 'type': 'text', 'title': '补充意见', 'required': False, 'active': True, 'version': 1}]
        self.flow.db.surveys.update_one({'_id': survey['_id']}, {'$set': {'questions': questions}})
        return owner, user, survey

    def test_jump_and_bank_rename_keep_pending_inputs_on_conflicts(self):
        for width in (390, 1280):
            with self.subTest(width=width):
                owner, user, survey = self.jump_scenario()
                sid = survey['_id']
                path = f'/surveys/{sid}/questions/start/jumps/new'
                context = self.browser.new_context(viewport={'width': width, 'height': 900})
                cookie = self.flow.app.session_interface.get_signing_serializer(self.flow.app).dumps({'user_id': str(user['_id'])})
                context.add_cookies([{'name': self.flow.app.config['SESSION_COOKIE_NAME'], 'value': cookie, 'url': self.base}])
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                try:
                    page.goto(self.base + path)
                    page.locator('[name=operator]').select_option('gt')
                    page.locator('[name=threshold]').fill('3.5')
                    page.locator('[name=target_id][value=last]').check()
                    self.flow.db.surveys.update_one({'_id': sid}, {'$set': {'description': '另一页修改'}, '$inc': {'revision': 1}})
                    with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/jumps')) as conflict:
                        page.get_by_role('button', name='保存跳转规则', exact=True).click()
                    self.assertEqual(conflict.value.status, 409)
                    self.assertEqual(page.locator('[name=threshold]').input_value(), '3.5')
                    self.assertEqual(page.locator('[name=operator]').input_value(), 'gt')
                    self.assertTrue(page.locator('[name=target_id][value=last]').is_checked())
                    self.assertIn('输入已保留', page.get_by_role('alert').inner_text())
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                    page.get_by_role('button', name='保存跳转规则', exact=True).click()
                    page.wait_for_url(self.base + path)
                    self.assertEqual(len(self.flow.db.surveys.find_one({'_id': sid})['questions'][0]['jumps']), 3)
                    bank = self.flow.api(owner, 'POST', '/api/v1/library/banks', {'name': '阅读活动题库'}).get_json()
                    bank_path = f"/question-banks/{bank['id']}"
                    page.goto(self.base + bank_path)
                    page.locator('.library-bank-settings > summary').click()
                    page.locator('#rename-bank').fill('下学期活动题库')
                    self.flow.api(owner, 'PATCH', f"/api/v1/library/banks/{bank['id']}", {'name': '另一页的新名称'})
                    with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/rename')) as rename:
                        page.get_by_role('button', name='保存名称', exact=True).click()
                    self.assertEqual(rename.value.status, 409)
                    self.assertEqual(page.locator('#rename-bank').input_value(), '下学期活动题库')
                    self.assertIn('另一页的新名称', page.locator('h1').inner_text())
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                    page.get_by_role('button', name='保存名称', exact=True).click()
                    page.wait_for_url(self.base + bank_path)
                    self.assertEqual(page.locator('h1').inner_text(), '下学期活动题库')
                    self.assertEqual(errors, [])
                finally:
                    context.close()

    def test_question_rule_and_bank_preconditions_preserve_other_writes(self):
        for action in ('question_delete', 'add', 'delete', 'move'):
            for timing in ('stale_form', 'final_write'):
                with self.subTest(action=action, timing=timing):
                    owner, user, survey = self.jump_scenario()
                    sid = survey['_id']
                    prefix = f'/surveys/{sid}/questions/start'
                    paths = {'question_delete': prefix + '/delete', 'add': prefix + '/jumps',
                             'delete': prefix + '/jumps/r1/delete', 'move': prefix + '/jumps/r1/move'}
                    original_update = Collection.update_one
                    def other_edit():
                        original_update(self.flow.db.surveys, {'_id': sid}, {
                            '$set': {'description': '另一页保留的内容'}, '$inc': {'revision': 1}})
                    def collision(collection, query, update, *args, **kwargs):
                        if collection.name == 'surveys' and query.get('_id') == sid:
                            other_edit()
                        return original_update(collection, query, update, *args, **kwargs)
                    if timing == 'stale_form': other_edit()
                    with patch.object(Collection, 'update_one', collision) if timing == 'final_write' else nullcontext():
                        response = self.flow.post(owner, paths[action], {'revision': '1', 'operator': 'gt',
                            'threshold': '3.5', 'target_id': 'last', 'direction': 'down', 'return_to': 'editor'})
                    self.assertEqual(response.status_code, 409)
                    saved = self.flow.db.surveys.find_one({'_id': sid})
                    self.assertTrue(saved['questions'][0]['active'])
                    self.assertEqual([r['id'] for r in saved['questions'][0]['jumps']], ['r1', 'r2'])
                    self.assertEqual(saved['description'], '另一页保留的内容')
                    if action == 'add': self.assertIn('value="3.5"', response.get_data(as_text=True))
        # Legacy first-stage documents can lack revision; rendered tokens use the same default as the server.
        import re
        for action in ('add', 'question_delete'):
            owner, user, survey = self.jump_scenario()
            sid = survey['_id']
            self.flow.db.surveys.update_one({'_id': sid}, {'$unset': {'revision': ''}})
            page_path = f'/surveys/{sid}/questions/start/jumps/new' if action == 'add' else f'/surveys/{sid}'
            html = owner.get(page_path).get_data(as_text=True)
            tokens = re.findall(r'name="revision" value="([^"]+)"', html)
            self.assertTrue(tokens)
            self.assertEqual(set(tokens), {'1'})
            endpoint = f'/surveys/{sid}/questions/start/' + ('jumps' if action == 'add' else 'delete')
            saved = self.flow.post(owner, endpoint, {'revision': tokens[0], 'operator': 'gt',
                'threshold': '3.5', 'target_id': 'last', 'return_to': 'editor'})
            self.assertEqual(saved.status_code, 302)
        owner, user, survey = self.jump_scenario()
        invalid = self.flow.post(owner, f"/surveys/{survey['_id']}/questions/start/jumps", {
            'operator': 'gt', 'threshold': '3.5', 'target_id': 'start'})
        self.assertEqual(invalid.status_code, 422)
        self.assertIn('value="3.5"', invalid.get_data(as_text=True))
        from bson import ObjectId
        for channel in ('web', 'api'):
            for timing in ('stale_form', 'final_write'):
                if channel == 'api' and timing == 'stale_form': continue
                with self.subTest(channel=channel, timing=timing):
                    bank = self.flow.api(owner, 'POST', '/api/v1/library/banks', {'name': channel + timing}).get_json()
                    key = ObjectId(bank['id'])
                    original_update = Collection.update_one
                    def rename_elsewhere():
                        original_update(self.flow.db.question_banks, {'_id': key}, {'$set': {'name': '其他页' + bank['name']}})
                    def collide_bank(collection, query, update, *args, **kwargs):
                        if collection.name == 'question_banks' and query.get('_id') == key: rename_elsewhere()
                        return original_update(collection, query, update, *args, **kwargs)
                    if timing == 'stale_form': rename_elsewhere()
                    with patch.object(Collection, 'update_one', collide_bank) if timing == 'final_write' else nullcontext():
                        response = (self.flow.post(owner, f"/question-banks/{key}/rename", {'name': '待保存的新名称', 'original_name': bank['name']})
                            if channel == 'web' else self.flow.api(owner, 'PATCH', f'/api/v1/library/banks/{key}', {'name': '待保存的新名称'}))
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(self.flow.db.question_banks.find_one({'_id': key})['name'], '其他页' + bank['name'])
                    if channel == 'web': self.assertIn('value="待保存的新名称"', response.get_data(as_text=True))
