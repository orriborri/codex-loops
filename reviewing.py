"""Goal contracts, evidence records and human merge policy. No model has gate authority."""
from datetime import datetime, timezone
import fnmatch
import hashlib
import html
import json
import os
import re
import uuid
from workflow import goal_fingerprint


def text(limit=2000, empty=False):
    return {'type': 'string', 'maxLength': limit, **({} if empty else {'minLength': 1})}


def enum(*values): return {'type': 'string', 'enum': list(values)}


def obj(**fields):
    return {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}


def array(item, limit=20): return {'type': 'array', 'items': item, 'maxItems': limit}


def implementation_schema():
    return obj(outcome=enum('completed', 'needs_clarification', 'needs_human_decision'), summary=text(),
        finding_responses=array(obj(id=text(40), status=enum('fixed', 'disputed', 'needs_decision'),
            evidence=text(1000), question=text(500, True), recommendation=text(500, True))))


def review_schema():
    return obj(decision=enum('APPROVE', 'REQUEST_CHANGES', 'ESCALATE'), summary=text(),
        risk=enum('low', 'high'), risk_notes=array(text(500), 10),
        criteria=array(obj(id=text(40), result=enum('pass', 'fail', 'unclear'), evidence=text(1000),
                           tests=array(text(200), 10)), 5),
        findings=array(obj(id=text(40), severity=enum('blocking', 'minor'), status=enum('open', 'resolved'),
            description=text(1000), evidence=text(1000), recommendation=text(1000))),
        question=text(500, True), recommendation=text(500, True))


def validate_shape(data, schema):
    kind = schema['type']
    types = {'string': str, 'boolean': bool, 'object': dict, 'array': list}
    if type(data) is not types[kind]: raise ValueError('Invalid agent output type')
    if 'enum' in schema and data not in schema['enum']: raise ValueError('Invalid agent output enum')
    if kind == 'object':
        if data.keys() != schema['properties'].keys(): raise ValueError('Invalid agent output fields')
        for key, rule in schema['properties'].items(): validate_shape(data[key], rule)
    elif kind == 'array':
        if len(data) > schema.get('maxItems', 100): raise ValueError('Agent output array too large')
        for item in data: validate_shape(item, schema['items'])
    elif kind == 'string':
        if len(data) > schema.get('maxLength', 10000): raise ValueError('Agent output text too large')
        if schema.get('minLength') and not data.strip(): raise ValueError('Empty agent output text')
    return data


def section(body, name):
    match = re.search(r'^#{1,3}\s+' + re.escape(name) + r'\s*\n(.*?)(?=^#{1,3}\s|\Z)',
                      body, re.I | re.M | re.S)
    return match.group(1).strip() if match else ''


def goal_contract(goal):
    body, title = goal.get('body') or '', goal.get('title', '').strip()
    labels = {x['name'] for x in goal.get('labels', [])}
    if ('agent:large' in labels or section(body, 'Size').upper() == 'L'
            or len(body) > int(os.getenv('MAX_GOAL_CHARS', '8000'))):
        raise ValueError('Split this work into smaller issues, each with one outcome and 1–5 acceptance criteria.')
    criteria = re.findall(r'^\s*(?:[-*]\s+(?:\[[ xX]\]\s*)?|\d+[.)]\s+)(.+)',
                          section(body, 'Acceptance criteria'), re.M)
    if not title or not 1 <= len(criteria) <= 5:
        raise ValueError('Provide one outcome and a ## Acceptance criteria section with 1–5 testable bullet points.')
    if any(len(c) > 1000 for c in criteria):
        raise ValueError('Split lengthy acceptance criteria into smaller issues.')
    return {'outcome': section(body, 'Outcome') or title,
            'criteria': [{'id': f'AC{i}', 'text': c.strip()} for i, c in enumerate(criteria, 1)],
            'fingerprint': goal_fingerprint(goal)}


def validate_verdict(verdict, contract, previous):
    validate_shape(verdict, review_schema())
    ids = [c['id'] for c in verdict['criteria']]
    if sorted(ids) != sorted(c['id'] for c in contract['criteria']):
        raise ValueError('Review must cover each acceptance criterion exactly once')
    findings = {f['id']: f for f in verdict['findings']}
    if len(findings) != len(verdict['findings']): raise ValueError('Duplicate finding IDs')
    for old in previous:
        if old['severity'] == 'blocking' and old['status'] == 'open':
            new = findings.get(old['id'])
            if new is None or (new['status'] == 'open' and new['severity'] != 'blocking'):
                raise ValueError('Unresolved blocking finding cannot disappear or be downgraded: ' + old['id'])
    if verdict['decision'] == 'APPROVE' and (
            any(c['result'] != 'pass' for c in verdict['criteria']) or
            any(f['severity'] == 'blocking' and f['status'] == 'open' for f in findings.values())):
        raise ValueError('Approval requires evidence for every criterion and no open blockers')
    if verdict['decision'] == 'ESCALATE' and not all(verdict[k].strip() for k in ('question', 'recommendation')):
        raise ValueError('Escalation requires one question and a recommendation')


class ChangeTooLarge(RuntimeError): pass


def changed_lines(numstat):
    count = 0
    for line in numstat.splitlines():
        parts = line.split('\t', 2)
        if len(parts) != 3: continue
        added, deleted, path = parts
        if re.search(r'(^|/)(tests?|__tests__)(/|$)|\.(test|spec)\.', path): continue
        if path.rsplit('/', 1)[-1] in ('Cargo.lock', 'package-lock.json', 'pnpm-lock.yaml',
                                       'yarn.lock', 'uv.lock', 'poetry.lock'): continue
        if added.isdigit() and deleted.isdigit(): count += int(added) + int(deleted)
    return count


HIGH_RISK = ('*/auth/*', 'auth/*', 'authentication/*', 'authorization/*', '*/authentication/*', '*/authorization/*',
             'auth.*', '*/auth.*', 'authentication.*', '*/authentication.*', 'authorization.*', '*/authorization.*', '*payment*',
             '*secret*', '*migration*', '*.sql', '*.tf', '*.tfvars', '.github/*',
             'infra/*', 'infrastructure/*', 'deploy/*', '*/Dockerfile*', 'Dockerfile*',
             '*compose*.yml', '*compose*.yaml', 'k8s/*', 'helm/*')


def risk_reasons(paths, labels, reviewer_risk):
    reasons = []
    if 'agent:high-risk' in labels: reasons.append('Maintainer marked this change high-risk.')
    if reviewer_risk == 'high': reasons.append('Independent reviewer identified high risk.')
    patterns = HIGH_RISK + tuple(x.strip() for x in os.getenv('HIGH_RISK_PATHS', '').split(',') if x.strip())
    for path in paths:
        if any(fnmatch.fnmatchcase(path.lower(), p.lower()) for p in patterns):
            reasons.append('High-risk path: ' + path)
    return reasons


def risk_command(head, base, goal_hash):
    return f'/agent approve-risk {head} {base} {goal_hash}'


def risk_approved(gh, comments, approvers, head, base, goal_hash, reviewer):
    expected = risk_command(head, base, goal_hash)
    for c in comments:
        user = c.get('user') or {}
        login = user.get('login', '')
        if (user.get('type') != 'User' or login not in approvers or login == reviewer
                or c.get('body', '').strip() != expected): continue
        permission = gh.request(f'collaborators/{login}/permission').get('permission')
        if permission in ('write', 'maintain', 'admin'): return True
    return False


class ReviewLog:
    def __init__(self, db):
        self.db = db
        db.executescript('''
            CREATE TABLE IF NOT EXISTS review_reports (
                seq INTEGER PRIMARY KEY, id TEXT UNIQUE, issue INTEGER, pr INTEGER,
                head TEXT, base TEXT, goal_hash TEXT, contract TEXT, verdict TEXT,
                risks TEXT, review_id INTEGER, response_seq INTEGER, at TEXT);
            CREATE TABLE IF NOT EXISTS review_resets (pr INTEGER PRIMARY KEY, report_seq INTEGER, response_seq INTEGER);
            CREATE TABLE IF NOT EXISTS finding_responses (
                seq INTEGER PRIMARY KEY, pr INTEGER, report_id TEXT UNIQUE, goal_hash TEXT,
                payload TEXT, at TEXT);
        ''')

    @staticmethod
    def decode(row):
        if row is None: return None
        data = dict(zip(('seq', 'id', 'issue', 'pr', 'head', 'base', 'goal_hash', 'contract',
                         'verdict', 'risks', 'review_id', 'response_seq', 'at'), row))
        for key in ('contract', 'verdict', 'risks'): data[key] = json.loads(data[key])
        data['marker'] = f"<!-- codex-review-report:{data['id']} -->"
        return data

    def latest(self, pr):
        return self.decode(self.db.execute('SELECT * FROM review_reports WHERE pr=? ORDER BY seq DESC LIMIT 1', (pr,)).fetchone())

    def history(self, pr):
        return [self.decode(r) for r in self.db.execute('SELECT * FROM review_reports WHERE pr=? ORDER BY seq DESC LIMIT 10', (pr,))]

    def response_history(self, pr):
        return [{'seq': row[0], 'report_id': row[1], 'responses': json.loads(row[2])}
                for row in self.db.execute('SELECT seq,report_id,payload FROM finding_responses WHERE pr=? ORDER BY seq DESC LIMIT 10', (pr,))]

    def unreviewed_responses(self, pr, report_id=None):
        row = self.db.execute('SELECT MAX(seq) FROM finding_responses WHERE pr=?', (pr,)).fetchone()
        latest = self.latest(pr)
        if report_id is not None:
            # Used while checking whether the matching response already exists.
            return self.db.execute('SELECT 1 FROM finding_responses WHERE report_id=?', (report_id,)).fetchone() is None
        return bool(row[0] and (latest is None or row[0] > latest['response_seq']))

    def record(self, issue, pr, head, base, contract, verdict, risks):
        previous = self.latest(pr)
        old = previous['verdict']['findings'] if previous and previous['goal_hash'] == contract['fingerprint'] else []
        validate_verdict(verdict, contract, old)
        # An agent cannot lower a risk raised earlier in this PR, even after goal/source edits.
        prior_risks = [risk for row in self.db.execute('SELECT risks FROM review_reports WHERE pr=? ORDER BY seq', (pr,))
                       for risk in json.loads(row[0])]
        risks = list(dict.fromkeys(prior_risks + risks))
        identifier = uuid.uuid4().hex
        response_seq = self.db.execute('SELECT COALESCE(MAX(seq),0) FROM finding_responses WHERE pr=?', (pr,)).fetchone()[0]
        with self.db:
            self.db.execute('''INSERT INTO review_reports
                (id,issue,pr,head,base,goal_hash,contract,verdict,risks,review_id,response_seq,at)
                VALUES (?,?,?,?,?,?,?,?,?,NULL,?,?)''',
                (identifier, issue, pr, head, base, contract['fingerprint'], json.dumps(contract),
                 json.dumps(verdict), json.dumps(risks), response_seq, datetime.now(timezone.utc).isoformat()))
        return self.latest(pr)

    def published(self, identifier, review_id):
        with self.db:
            self.db.execute('UPDATE review_reports SET review_id=? WHERE id=?', (review_id, identifier))

    def approved(self, pr, head, base, goal_hash, review_id):
        report = self.latest(pr)
        return bool(report and report['head'] == head and report['base'] == base and report['goal_hash'] == goal_hash
                    and report['review_id'] is not None and report['review_id'] == review_id
                    and report['verdict']['decision'] == 'APPROVE' and self.active(report) and not self.unreviewed_responses(pr))

    def active(self, report):
        reset = self.db.execute('SELECT report_seq FROM review_resets WHERE pr=?', (report['pr'],)).fetchone()
        return reset is None or report['seq'] > reset[0]

    def reset_disputes(self, pr):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO review_resets VALUES (?,?,?)', (pr,
                self.db.execute('SELECT COALESCE(MAX(seq),0) FROM review_reports WHERE pr=?', (pr,)).fetchone()[0],
                self.db.execute('SELECT COALESCE(MAX(seq),0) FROM finding_responses WHERE pr=?', (pr,)).fetchone()[0]))

    def responses(self, pr, identifier, responses):
        report = self.latest(pr)
        if not report or report['id'] != identifier: raise ValueError('Stale finding responses')
        validate_shape(responses, implementation_schema()['properties']['finding_responses'])
        blockers = {f['id'] for f in report['verdict']['findings'] if f['severity'] == 'blocking' and f['status'] == 'open'}
        ids = [r['id'] for r in responses]
        if len(set(ids)) != len(ids) or set(ids) != blockers:
            raise ValueError('Respond to every blocking finding exactly once')
        for item in responses:
            if item['status'] != 'fixed' and not all(item[k].strip() for k in ('question', 'recommendation')):
                raise ValueError('Disputes require one question and a recommendation')
        existing = self.db.execute('SELECT payload FROM finding_responses WHERE report_id=?', (identifier,)).fetchone()
        if existing and json.loads(existing[0]) != responses:
            raise ValueError('Finding responses are immutable; wait for a new independent review')
        # Lost controller responses must not count the same repair as a second dispute.
        previous = self.db.execute('SELECT payload FROM finding_responses WHERE pr=? AND goal_hash=? AND report_id<>? AND seq>COALESCE((SELECT response_seq FROM review_resets WHERE pr=?),0) ORDER BY seq DESC LIMIT 1',
                                   (pr, report['goal_hash'], identifier, pr)).fetchone()
        old = {x['id']: x for x in json.loads(previous[0])} if previous else {}
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO finding_responses (pr,report_id,goal_hash,payload,at) VALUES (?,?,?,?,?)',
                            (pr, identifier, report['goal_hash'], json.dumps(responses), datetime.now(timezone.utc).isoformat()))
        for item in responses:
            repeated = item['status'] == 'disputed' and old.get(item['id'], {}).get('status') == 'disputed'
            if repeated or item['status'] == 'needs_decision':
                return f"Finding {item['id']}: {item['question']} Recommendation: {item['recommendation']}"
        return None

    def metrics(self, pr):
        reports = self.history(pr)
        history = self.response_history(pr)
        return {'review_rounds': self.db.execute('SELECT COUNT(*) FROM review_reports WHERE pr=?', (pr,)).fetchone()[0],
                'open_blockers': sum(f['severity'] == 'blocking' and f['status'] == 'open'
                                     for f in reports[0]['verdict']['findings']) if reports else 0,
                'disputes': sum(r['status'] == 'disputed' for h in history for r in h['responses'])}


def display(value):
    # Model/source text must not create headings, tables, hidden markers or mentions.
    return html.escape(str(value)).replace('@', '@\u200b').replace('|', '&#124;').replace('\n', '<br>')


def render_pack(report, history, responses):
    verdict, contract = report['verdict'], report['contract']
    lines = ['## Review pack', f"Goal: #{report['issue']} · Review record: {report['seq']}",
        f"Head: `{report['head']}` · Base: `{report['base']}`", f"Goal fingerprint: `{report['goal_hash']}`",
        '', display(verdict['summary']), '', '| Criterion | Result | Evidence | Tests/checks |', '|---|---|---|---|']
    facts = {c['id']: c['text'] for c in contract['criteria']}
    for c in verdict['criteria']:
        lines.append(f"| {display(c['id'])}: {display(facts[c['id']])} | {c['result']} | {display(c['evidence'])} | {display(', '.join(c['tests']))} |")
    lines += ['', '### Risk', 'High risk: human approval required.' if report['risks'] else 'Low risk under the configured policy.']
    lines += ['- ' + display(r) for r in report['risks'] + verdict['risk_notes']]
    if report['risks']:
        lines += ['', 'An allowlisted maintainer can approve this exact revision and goal with a PR comment:',
                  '```text', risk_command(report['head'], report['base'], report['goal_hash']), '```']
    lines += ['', '### Findings']
    lines += [f"- **{display(f['id'])} ({f['severity']}, {f['status']}):** {display(f['description'])} Evidence: {display(f['evidence'])} Recommendation: {display(f['recommendation'])}" for f in verdict['findings']]
    if not verdict['findings']: lines.append('No findings recorded.')
    if responses:
        lines += ['', '### Implementation responses']
        for batch in responses[:3]:
            lines += [f"- {display(r['id'])}: **{r['status']}** — {display(r['evidence'])}" for r in batch['responses']]
    lines += ['', '### Review history']
    lines += [f"- {r['at']}: {r['verdict']['decision']} on `{r['head']}:{r['base']}`" for r in history]
    if verdict['decision'] == 'ESCALATE':
        lines += ['', '**Decision needed:** ' + display(verdict['question']), 'Recommendation: ' + display(verdict['recommendation'])]
    lines += ['', 'Evidence is the reviewer’s assessment; required GitHub CI and branch protection remain authoritative. Merging does not imply deployment.']
    return '\n'.join(lines)[:55000] + '\n\n' + report['marker']
