"""Malformed request input must be rejected without interrupting the application."""
import os
import unittest
from uuid import uuid4

from bson import ObjectId

from app import create_app


class CsrfBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.previous_database = os.environ.get('MONGO_DB_NAME')
        cls.db_name = 'survey_phase2_csrf_test_' + uuid4().hex
        os.environ['MONGO_DB_NAME'] = cls.db_name
        cls.app = create_app()
        cls.db = cls.app.extensions['mongo_client'][cls.db_name]

    @classmethod
    def tearDownClass(cls):
        cls.app.extensions['mongo_client'].drop_database(cls.db_name)
        cls.app.extensions['mongo_client'].close()
        if cls.previous_database is None:
            os.environ.pop('MONGO_DB_NAME', None)
        else:
            os.environ['MONGO_DB_NAME'] = cls.previous_database

    def test_invalid_unicode_tokens_are_rejected_consistently_without_writes(self):
        client = self.app.test_client()
        token = client.get('/api/v1/csrf').json['csrf_token']
        username = 'csrf_' + uuid4().hex[:12]
        credentials = {'username': username, 'password': '123456', 'confirmation': '123456'}
        registered = client.post('/api/v1/auth/register', json=credentials,
                                 headers={'X-CSRF-Token': token})
        self.assertEqual(registered.status_code, 201, registered.json)
        logged_in = client.post('/api/v1/auth/login', json=credentials,
                                headers={'X-CSRF-Token': token})
        self.assertEqual(logged_in.status_code, 200, logged_in.json)
        token = logged_in.json['csrf_token']

        targets = [('/api/v1/surveys', 'json'),
                   ('/api/v1/library/questions', 'json'),
                   ('/surveys/new', 'header'), ('/surveys/new', 'form')]
        for path, transport in targets:
            for supplied in (None, 'incorrect-token', '错误令牌', 'é'):
                with self.subTest(path=path, transport=transport, token=supplied):
                    headers = {'X-CSRF-Token': supplied} if supplied is not None and transport != 'form' else {}
                    data = {'title': '不会创建的问卷', 'type': 'text', 'required': True}
                    if transport == 'form' and supplied is not None:
                        data['csrf_token'] = supplied
                    result = client.post(path, headers=headers,
                                         **({'json': data} if transport == 'json' else {'data': data}))
                    self.assertEqual(result.status_code, 400, result.get_data(as_text=True))
                    if transport == 'json':
                        self.assertIn('error', result.json)
        self.assertEqual(self.db.surveys.count_documents({}), 0)
        self.assertEqual(self.db.question_library.count_documents({}), 0)

        # Correct tokens must still follow the existing creation paths.
        survey = client.post('/api/v1/surveys', json={'title': 'API 正常创建'},
                             headers={'X-CSRF-Token': token})
        question = client.post('/api/v1/library/questions',
                               json={'type': 'text', 'title': '具体建议', 'required': False},
                               headers={'X-CSRF-Token': token})
        page = client.post('/surveys/new', data={'title': '网页正常创建', 'csrf_token': token})
        self.assertEqual(survey.status_code, 201, survey.json)
        self.assertEqual(question.status_code, 201, question.json)
        self.assertEqual(page.status_code, 302)
        self.assertEqual(self.db.surveys.count_documents({}), 2)
        self.assertEqual(self.db.question_library.count_documents({}), 1)

    def test_library_actions_require_a_json_object_before_restoring_or_deleting(self):
        client = self.app.test_client()
        token = client.get('/api/v1/csrf').json['csrf_token']
        credentials = {'username': 'body_' + uuid4().hex[:12],
                       'password': '123456', 'confirmation': '123456'}
        registered = client.post('/api/v1/auth/register', json=credentials,
                                 headers={'X-CSRF-Token': token})
        self.assertEqual(registered.status_code, 201, registered.json)
        logged_in = client.post('/api/v1/auth/login', json=credentials,
                                headers={'X-CSRF-Token': token})
        self.assertEqual(logged_in.status_code, 200, logged_in.json)
        headers = {'X-CSRF-Token': logged_in.json['csrf_token']}
        owner_id = ObjectId(registered.json['id'])
        created = client.post('/api/v1/library/questions', headers=headers,
                              json={'type': 'text', 'title': '食堂服务建议', 'required': False})
        self.assertEqual(created.status_code, 201, created.json)
        question_id = created.json['question']['id']
        bodies = [('missing', {}), ('array', {'json': []}), ('string', {'json': 'wrong'}),
                  ('null', {'data': 'null', 'content_type': 'application/json'}),
                  ('broken_json', {'data': '{', 'content_type': 'application/json'}),
                  ('wrong_media_type', {'data': '{}', 'content_type': 'text/plain'})]
        for label, body in bodies:
            bank = client.post('/api/v1/library/banks', headers=headers,
                               json={'name': '保留题库_' + label})
            self.assertEqual(bank.status_code, 201, bank.json)
            bank_id = ObjectId(bank.json['id'])
            for method, path in [('POST', f'/api/v1/library/questions/{question_id}/restore'),
                                 ('DELETE', f'/api/v1/library/banks/{bank_id}')]:
                with self.subTest(action=method, body=label):
                    questions_before = list(self.db.question_library.find({'owner_id': owner_id}))
                    bank_before = self.db.question_banks.find_one({'_id': bank_id})
                    result = client.open(path, method=method, headers=headers, **body)
                    self.assertEqual(result.status_code, 415, result.get_data(as_text=True))
                    self.assertIn('JSON', result.json['error'])
                    self.assertEqual(list(self.db.question_library.find({'owner_id': owner_id})), questions_before)
                    self.assertEqual(self.db.question_banks.find_one({'_id': bank_id}), bank_before)

        versions_before = self.db.question_library.count_documents({'owner_id': owner_id})
        restored = client.post(f'/api/v1/library/questions/{question_id}/restore', headers=headers, json={})
        self.assertEqual(restored.status_code, 201, restored.json)
        self.assertEqual(restored.json['question']['parent_id'], question_id)
        self.assertEqual(self.db.question_library.count_documents({'owner_id': owner_id}), versions_before + 1)
        valid_bank = client.post('/api/v1/library/banks', headers=headers, json={'name': '正常删除题库'})
        self.assertEqual(valid_bank.status_code, 201, valid_bank.json)
        valid_bank_id = ObjectId(valid_bank.json['id'])
        deleted = client.delete(f'/api/v1/library/banks/{valid_bank_id}', headers=headers, json={})
        self.assertEqual(deleted.status_code, 200, deleted.json)
        self.assertIsNone(self.db.question_banks.find_one({'_id': valid_bank_id}))
