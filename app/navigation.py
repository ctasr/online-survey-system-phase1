"""Carry an explicit survey-list return location through management pages."""
from flask import g, has_request_context, request


def list_context():
    if not has_request_context():
        return {}
    if hasattr(g, 'survey_list_context'):
        return g.survey_list_context
    if not any(key in request.args for key in ('survey_list_q', 'survey_list_status', 'survey_list_page')):
        return {}
    status = request.args.get('survey_list_status', '')
    if status not in ('草稿', '已发布', '已截止', '已关闭'):
        status = ''
    return {'survey_list_q': request.args.get('survey_list_q', '').strip()[:100],
            'survey_list_status': status,
            'survey_list_page': min(100000, max(1, request.args.get('survey_list_page', 1, type=int) or 1))}


def init_navigation(app):
    @app.url_defaults
    def retain_list_context(endpoint, values):
        if values.pop('_reset_list', False):
            return
        context = list_context()
        if not context:
            return
        if endpoint == 'surveys.my_surveys':
            for key, value in context.items():
                values.setdefault(key.removeprefix('survey_list_'), value)
        elif (endpoint.startswith('bank.') or endpoint.startswith('surveys.')
              and endpoint not in ('surveys.my_responses', 'surveys.response_detail',
                                   'surveys.public_survey', 'surveys.public_assess',
                                   'surveys.response_draft', 'surveys.next_question', 'surveys.submitted')):
            for key, value in context.items():
                values.setdefault(key, value)

    @app.context_processor
    def list_form_context():
        return {'survey_list_context': list_context()}
