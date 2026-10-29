"""Optional, metadata-only Opik export. Never participates in workflow decisions."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.parse
import urllib.request
import uuid

LOG = logging.getLogger('loops.telemetry')


def uuid7():
    # Python 3.12 has no uuid.uuid7. RFC 9562: 48-bit milliseconds + 74 random bits.
    value = (int(time.time() * 1000) << 80) | (7 << 76)
    value |= secrets.randbits(12) << 64
    value |= (2 << 62) | secrets.randbits(62)
    return str(uuid.UUID(int=value))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward trace payloads or the API key to another URL.
        return None


class Telemetry:
    def __init__(self, db):
        self.db = db
        self.enabled = os.getenv('OPIK_ENABLED', 'false').lower() == 'true'
        if not self.enabled:
            return
        try:
            self.url = os.getenv('OPIK_URL_OVERRIDE', '').rstrip('/')
            parsed = urllib.parse.urlsplit(self.url)
            if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError('Invalid Opik URL')
            self.project = os.getenv('OPIK_PROJECT_NAME', 'codex-loops')
            self.headers = {'Content-Type': 'application/json', 'User-Agent': 'codex-loops'}
            for variable, header in [('OPIK_API_KEY', 'Authorization'), ('OPIK_WORKSPACE', 'Comet-Workspace')]:
                if os.getenv(variable):
                    self.headers[header] = os.environ[variable]
            self.timeout = min(10, max(0.1, float(os.getenv('OPIK_TIMEOUT_SECONDS', '2'))))
            self.capacity = max(1, int(os.getenv('OPIK_MAX_PENDING', '10000')))
            self.opener = urllib.request.build_opener(NoRedirect())
            db.executescript('''
                CREATE TABLE IF NOT EXISTS opik_outbox (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, scores TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS opik_cursor (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), event_id INTEGER);
            ''')
            # Start from enablement; do not automatically upload pre-existing history.
            with db:
                db.execute('''INSERT OR IGNORE INTO opik_cursor
                    SELECT 1, COALESCE(MAX(id),0) FROM workflow_events''')
        except Exception:
            self.enabled = False
            LOG.warning('Opik disabled: invalid configuration or unavailable local queue')

    def job(self, repo, role, model, prompt, started, duration, outcome, result, task=None, version='unknown'):
        if not self.enabled:
            return
        try:
            metadata = {'repository': repo, 'role': role, 'model': model,
                        'controller_version': version,
                        'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                        'prompt_bytes': len(prompt.encode()), 'duration_seconds': round(duration, 3)}
            issue = None
            if task:
                issue, pr, key, limit = task
                metadata.update(issue=issue, pr=pr, attempt_limit=limit)
                row = self.db.execute('SELECT count FROM attempts WHERE key=?', (key,)).fetchone()
                metadata['attempt'] = row[0] if row else 0
                row = self.db.execute('SELECT context FROM workflow WHERE issue=?', (issue,)).fetchone()
                if row:
                    self._revision(metadata, row[0])
            output = {'execution': outcome}
            scores = {'execution_success': int(outcome == 'success')}
            if isinstance(result, dict):
                metadata['criteria_passed'] = sum(c.get('result') == 'pass' for c in result.get('criteria', []))
                metadata['open_blockers'] = sum(f.get('severity') == 'blocking' and f.get('status') == 'open'
                                                for f in result.get('findings', []))
                metadata['disputes'] = sum(r.get('status') == 'disputed' for r in result.get('finding_responses', []))
                # Only controller-validated enum/boolean values, never summaries or findings.
                if result.get('decision') in ('APPROVE', 'REQUEST_CHANGES'):
                    output['decision'] = result['decision']
                    scores['review_approved'] = int(result['decision'] == 'APPROVE')
                if result.get('outcome') in ('completed', 'needs_clarification', 'needs_human_decision'):
                    output['outcome'] = result['outcome']
                    scores['task_completed'] = int(result['outcome'] == 'completed')
                if type(result.get('needed')) is bool:
                    output['proposal_needed'] = result['needed']
            begin = datetime.fromtimestamp(started, timezone.utc)
            trace = self._trace(repo, issue, f'codex.{role}', begin,
                                begin + timedelta(seconds=duration), metadata, output)
            with self.db:
                self._enqueue(trace, scores)
        except Exception:
            LOG.warning('Opik job recording deferred; workflow continues')

    @staticmethod
    def _revision(metadata, context):
        import re
        if re.fullmatch(r'[a-f0-9]{40}:[a-f0-9]{40}', context or ''):
            metadata['head_sha'], metadata['base_sha'] = context.split(':')

    def _trace(self, repo, issue, name, start, end, metadata, output):
        trace = {'id': uuid7(), 'project_name': self.project, 'name': name,
                 'start_time': start.isoformat(), 'end_time': end.isoformat(),
                 'metadata': metadata, 'output': output, 'tags': ['codex-loops']}
        if issue is not None:
            trace['thread_id'] = f'{repo}:issue-{issue}'
        return trace

    def _enqueue(self, trace, scores):
        if self.db.execute('SELECT COUNT(*) FROM opik_outbox').fetchone()[0] >= self.capacity:
            LOG.warning('Opik queue full; new telemetry is dropped, workflow continues')
            return
        self.db.execute('INSERT INTO opik_outbox VALUES (?,?,?)',
                        (trace['id'], json.dumps(trace), json.dumps(scores)))

    def _workflow_events(self, repo):
        cursor = self.db.execute('SELECT event_id FROM opik_cursor WHERE singleton=1').fetchone()[0]
        rows = self.db.execute('''SELECT id,issue,pr,state,context,at FROM workflow_events
            WHERE id>? ORDER BY id LIMIT 50''', (cursor,)).fetchall()
        with self.db:
            for event, issue, pr, state, context, at in rows:
                metadata = {'repository': repo, 'issue': issue, 'pr': pr, 'event_id': event}
                self._revision(metadata, context)
                when = datetime.fromisoformat(at)
                scores = {}
                if state in ('ci_failed', 'ready_to_merge'):
                    scores['ci_green'] = int(state != 'ci_failed')
                if state in ('merged', 'closed'):
                    scores['merged'] = int(state == 'merged')
                trace = self._trace(repo, issue, 'workflow.' + state, when, when, metadata, {'state': state})
                self._enqueue(trace, scores)
                self.db.execute('UPDATE opik_cursor SET event_id=? WHERE singleton=1', (event,))

    def _post(self, path, method, body):
        request = urllib.request.Request(self.url + path, method=method,
            headers=self.headers, data=json.dumps(body).encode())
        with self.opener.open(request, timeout=self.timeout) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError('Opik did not accept telemetry')

    def flush(self, repo):
        if not self.enabled:
            return
        try:
            self._workflow_events(repo)
            rows = self.db.execute('SELECT id,payload,scores FROM opik_outbox ORDER BY rowid LIMIT 50').fetchall()
            if not rows:
                return
            self._post('/v1/private/traces/batch', 'POST', {'traces': [json.loads(row[1]) for row in rows]})
            scores = [{'id': trace_id, 'name': name, 'value': value, 'source': 'sdk',
                       'project_name': json.loads(payload)['project_name']}
                      for trace_id, payload, values in rows for name, value in json.loads(values).items()]
            if scores:
                self._post('/v1/private/traces/feedback-scores', 'PUT', {'scores': scores})
            with self.db:
                self.db.executemany('DELETE FROM opik_outbox WHERE id=?', [(row[0],) for row in rows])
        except Exception as exc:
            # Never log URLs, credentials, response bodies or source-derived exceptions.
            LOG.warning('Opik export deferred (%s); workflow continues', type(exc).__name__)
