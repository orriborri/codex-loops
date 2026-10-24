"""Persistent workflow observations and progress comments, owned by the controller."""
from datetime import datetime, timezone
import hashlib
import json
import uuid
import urllib.error


def goal_fingerprint(goal):
    return hashlib.sha256(json.dumps([goal['title'], goal.get('body') or '']).encode()).hexdigest()


def hold_action(state):
    if state == 'needs_clarification':
        return 'Answer the questions by editing the goal title/body, or add agent:resume after clarifying.'
    return 'Record the decision on GitHub, then add agent:resume; existing CI and review gates still apply.'


class Workflow:
    def __init__(self, db):
        self.db = db
        db.executescript('''
            CREATE TABLE IF NOT EXISTS workflow (
                issue INTEGER PRIMARY KEY, pr INTEGER, state TEXT, reason TEXT,
                owner TEXT, next_action TEXT, context TEXT, since TEXT);
            CREATE TABLE IF NOT EXISTS workflow_events (
                id INTEGER PRIMARY KEY, issue INTEGER, pr INTEGER, state TEXT,
                reason TEXT, owner TEXT, next_action TEXT, context TEXT, at TEXT);
            CREATE TABLE IF NOT EXISTS workflow_holds (
                issue INTEGER PRIMARY KEY, state TEXT, reason TEXT, fingerprint TEXT);
            CREATE TABLE IF NOT EXISTS progress_comments (
                target INTEGER PRIMARY KEY, marker TEXT, comment_id INTEGER, body TEXT);
        ''')

    def transition(self, issue, pr, state, reason, owner, next_action, context=''):
        values = (pr, state, reason, owner, next_action, context)
        old = self.db.execute('''SELECT pr,state,reason,owner,next_action,context
            FROM workflow WHERE issue=?''', (issue,)).fetchone()
        if old == values:
            return False
        now = datetime.now(timezone.utc).isoformat(timespec='microseconds')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO workflow VALUES (?,?,?,?,?,?,?,?)',
                            (issue, *values, now))
            self.db.execute('''INSERT INTO workflow_events
                (issue,pr,state,reason,owner,next_action,context,at) VALUES (?,?,?,?,?,?,?,?)''',
                (issue, *values, now))
        return True

    def hold(self, issue, pr, state, reason, goal):
        if state not in ('needs_clarification', 'needs_human_decision'):
            raise ValueError('Invalid workflow hold')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO workflow_holds VALUES (?,?,?,?)',
                            (issue, state, reason, goal_fingerprint(goal)))
        self.transition(issue, pr, state, reason, 'maintainer', hold_action(state))

    def blocked(self, issue, pr, goal):
        hold = self.db.execute('SELECT state,reason,fingerprint FROM workflow_holds WHERE issue=?',
                               (issue,)).fetchone()
        if not hold:
            return False
        if hold[0] == 'needs_clarification' and hold[2] != goal_fingerprint(goal):
            with self.db:
                self.db.execute('DELETE FROM workflow_holds WHERE issue=?', (issue,))
            return False
        self.transition(issue, pr, hold[0], hold[1], 'maintainer', hold_action(hold[0]))
        return True

    def resume(self, issue, pr):
        """Explicit maintainer action starts a new bounded attempt cycle."""
        with self.db:
            self.db.execute('DELETE FROM workflow_holds WHERE issue=?', (issue,))
            self.db.execute('DELETE FROM attempts WHERE key=?', (f'issue:{issue}',))
            if pr is not None:
                self.db.execute('DELETE FROM attempts WHERE key=? OR key LIKE ?',
                                (f'fix:{pr}', f'review:{pr}:%'))
                self.db.execute('DELETE FROM fixes WHERE pr=?', (pr,))
        self.transition(issue, pr, 'queued', 'Explicitly resumed by a maintainer.',
                        'implement', 'Re-evaluate the current goal, reviews and checks.')

    def history(self, issue, limit=100):
        columns = ('state', 'reason', 'owner', 'next_action', 'context', 'at')
        return [dict(zip(columns, row)) for row in self.db.execute('''SELECT
            state,reason,owner,next_action,context,at FROM workflow_events
            WHERE issue=? ORDER BY id DESC LIMIT ?''', (issue, limit))]

    def durations(self, issue):
        """Observed wall time per state, including the current interval."""
        rows = self.db.execute('SELECT state,at FROM workflow_events WHERE issue=? ORDER BY id',
                               (issue,)).fetchall()
        now = datetime.now(timezone.utc)
        totals = {}
        for index, (state, start) in enumerate(rows):
            end = datetime.fromisoformat(rows[index + 1][1]) if index + 1 < len(rows) else now
            # Terminal states measure completion, not continuing delivery time.
            if state not in ('merged', 'closed'):
                totals[state] = totals.get(state, 0) + (end - datetime.fromisoformat(start)).total_seconds()
        return {state: round(seconds, 2) for state, seconds in totals.items()}

    def sync(self, gh):
        """Only the implementation identity calls this; failures are retried next poll."""
        failure = None
        for issue, pr, state, reason, owner, action, context, since in self.db.execute(
                'SELECT * FROM workflow').fetchall():
            progress = ('### Codex loop progress\n\n'
                        f'**State:** `{state}`\n\n**Reason:** {reason}\n\n'
                        f'**Next owner:** {owner}\n\n**Next action:** {action}\n\n'
                        f'Goal: #{issue}' + (f' · PR: #{pr}' if pr else '') +
                        f'\n\nObserved since: {since}' + (f'\n\nRevision: `{context}`' if context else '') +
                        '\n\nMerging does not imply deployment. Workflow history is retained by the runner.')
            for target in dict.fromkeys([issue] + ([pr] if pr else [])):
                try:
                    self._sync_comment(gh, target, progress)
                except Exception as exc:
                    # One inaccessible issue must not starve progress on other work.
                    failure = exc
        if failure is not None:
            raise failure

    def _sync_comment(self, gh, target, progress):
        row = self.db.execute('SELECT marker,comment_id,body FROM progress_comments WHERE target=?',
                              (target,)).fetchone()
        if row is None:
            # Persist an unguessable marker before POST so a lost response can be recovered.
            row = (f'<!-- codex-loop-status:{uuid.uuid4().hex} -->', None, None)
            with self.db:
                self.db.execute('INSERT INTO progress_comments VALUES (?,?,NULL,NULL)', (target, row[0]))
        marker, comment_id, published = row
        body = marker + '\n' + progress
        if body == published:
            return
        if comment_id is None:
            matches = [c for c in gh.pages(f'issues/{target}/comments')
                       if (c.get('body') or '').startswith(marker + '\n')]
            if matches:
                comment_id = min(matches, key=lambda c: c['id'])['id']
        if comment_id is not None:
            try:
                gh.request(f'issues/comments/{comment_id}', 'PATCH', {'body': body})
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
                comment_id = None
        if comment_id is None:
            comment_id = gh.request(f'issues/{target}/comments', 'POST', {'body': body})['id']
        with self.db:
            self.db.execute('UPDATE progress_comments SET comment_id=?,body=? WHERE target=?',
                            (comment_id, body, target))
