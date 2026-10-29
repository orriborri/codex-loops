def goal(body=None):
    return {'number': 1, 'title': 'Retry exports', 'state': 'open', 'labels': [],
            'body': body or '## Outcome\nExports survive restarts.\n\n## Acceptance criteria\n- Pending exports survive restart.\n- Retries reuse the trace ID.'}


def verdict(decision='APPROVE'):
    return {'decision': decision, 'summary': 'Reviewed source and tests.', 'risk': 'low', 'risk_notes': [],
            'criteria': [{'id': 'AC1', 'result': 'pass', 'evidence': 'Restart test passes.', 'tests': ['test_restart']},
                         {'id': 'AC2', 'result': 'pass', 'evidence': 'Retry test asserts same ID.', 'tests': ['test_retry']}],
            'findings': [], 'question': '', 'recommendation': ''}


def finding(id='F1', severity='blocking', status='open'):
    return {'id': id, 'severity': severity, 'status': status, 'description': 'Retry duplicates data.',
            'evidence': 'retry.py:42 regenerates the ID.', 'recommendation': 'Persist the ID before sending.'}


def response(status='disputed', id='F1'):
    return {'id': id, 'status': status, 'evidence': 'The backend upserts by ID.',
            'question': 'Should retries preserve the existing ID?', 'recommendation': 'Keep the stable ID requirement.'}
