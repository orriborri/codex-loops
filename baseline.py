"""Conservative JUnit comparison in credential-free test containers."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid
import xml.etree.ElementTree as ET
from reviewing import display


def parse_junit(output, exit_code):
    unknown = {'status': 'unknown', 'tests': [], 'failures': []}
    path = output / 'tests.xml'
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 5_000_000: return unknown
    try:
        root = ET.parse(path).getroot()
        if root.tag not in ('testsuite', 'testsuites'): return unknown
        # Validate declared counts at every suite and aggregate level before matching IDs.
        for node in root.iter():
            if node.tag not in ('testsuite', 'testsuites'): continue
            cases = list(node.iter('testcase'))
            counts = {'tests': len(cases), 'skipped': sum(c.find('skipped') is not None for c in cases),
                      'failures': sum(c.find('failure') is not None for c in cases),
                      'errors': sum(c.find('error') is not None for c in cases)}
            for key, actual in counts.items():
                if key in node.attrib and int(node.get(key)) != actual: return unknown
        tests, failures = [], []
        # Nested testsuites are common in JUnit; each testcase is read once from its parent.
        suites = list(root.iter('testsuite'))
        for suite in suites:
            cases = suite.findall('testcase')
            for case in cases:
                if case.find('skipped') is not None: continue
                if not case.get('name'): return unknown
                identifier = '::'.join((suite.get('name', ''), case.get('classname', ''), case.get('name', '')))
                tests.append(identifier)
                if case.find('failure') is not None or case.find('error') is not None: failures.append(identifier)
            # Suite-level setup/collection failures cannot be matched to individual tests.
            if suite.find('error') is not None or suite.find('failure') is not None: return unknown
        if not tests or len(set(tests)) != len(tests) or (exit_code == 0) != (not failures): return unknown
        return {'status': 'failed' if failures else 'passed', 'tests': sorted(tests), 'failures': sorted(failures)}
    except (ET.ParseError, OSError, ValueError):
        return unknown


def compare_tests(head, base):
    result = {'classification': 'unknown', 'regressions': [], 'baseline_failures': [],
              'head_status': head['status'], 'base_status': base['status']}
    if 'unknown' in (head['status'], base['status']): return result
    result['baseline_failures'] = base['failures']
    result['regressions'] = sorted(set(head['failures']) - set(base['failures']))
    if head['status'] == 'passed': result['classification'] = 'not_reproduced'
    elif result['regressions']: result['classification'] = 'regression'
    elif base['failures'] and set(base['tests']) <= set(head['tests']): result['classification'] = 'baseline_only'
    return result


class BaselineChecks:
    def __init__(self, db):
        self.db = db
        db.executescript('''
            CREATE TABLE IF NOT EXISTS baseline_checks (
                head TEXT, base TEXT, command_hash TEXT, result TEXT,
                PRIMARY KEY(head,base,command_hash));
            CREATE TABLE IF NOT EXISTS baseline_issues (
                base TEXT, command_hash TEXT, marker TEXT UNIQUE, issue INTEGER,
                PRIMARY KEY(base,command_hash));
        ''')

    def compare(self, loops, pr, head_source, head_output):
        command = os.getenv('BASELINE_TEST_COMMAND', '').strip()
        if not command: return None
        image = os.getenv('AGENT_IMAGE', 'codex-loops-agent:local')
        command_hash = hashlib.sha256(json.dumps([command, image]).encode()).hexdigest()
        head, base = pr['head']['sha'], pr['base']['sha']
        row = self.db.execute('SELECT result FROM baseline_checks WHERE head=? AND base=? AND command_hash=?',
                              (head, base, command_hash)).fetchone()
        if row: return json.loads(row[0])
        _, _, base_source, base_output = loops.workspace(base)
        first = self.run(head_source, head_output, command, image)
        second = self.run(base_source, base_output, command, image)
        result = compare_tests(first, second)
        result.update(head=head, base=base, command_hash=command_hash)
        # Unknown/timeouts are cached too: no unbounded repro runs during polling.
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO baseline_checks VALUES (?,?,?,?)',
                            (head, base, command_hash, json.dumps(result)))
        return result

    @staticmethod
    def run(source, output, command, image):
        # An independent output directory prevents existing/model-written XML reuse.
        results = output / ('baseline-' + uuid.uuid4().hex)
        results.mkdir()
        os.chown(results, 1000, 1000)
        name = 'codex-test-' + uuid.uuid4().hex
        args = ['docker', 'run', '--rm', '--init', '--name', name, '--user', '1000:1000',
                '--cap-drop=ALL', '--security-opt=no-new-privileges', '--read-only',
                '--pids-limit=256', '--memory', os.getenv('JOB_MEMORY', '4g'), '--cpus', os.getenv('JOB_CPUS', '2'),
                '--tmpfs', '/tmp:rw,exec,size=1g,mode=1777', '--env', 'HOME=/tmp/home',
                '--mount', f'type=bind,src={source},dst=/repo',
                '--mount', f'type=bind,src={results},dst=/out', '--workdir', '/repo', image,
                'bash', '-lc', command]
        try:
            with (results / 'test.log').open('w') as log:
                completed = subprocess.run(args, text=True, stdout=log, stderr=log,
                    timeout=int(os.getenv('BASELINE_TIMEOUT_SECONDS', '600')))
            return parse_junit(results, completed.returncode)
        except (subprocess.TimeoutExpired, OSError):
            return {'status': 'unknown', 'tests': [], 'failures': []}
        finally:
            subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)

    def issue(self, gh, result):
        if not result['baseline_failures']: return None
        base, key = result['base'], result['command_hash']
        row = self.db.execute('SELECT marker,issue FROM baseline_issues WHERE base=? AND command_hash=?', (base, key)).fetchone()
        if row is None:
            row = ('<!-- codex-baseline:' + uuid.uuid4().hex + ' -->', None)
            with self.db: self.db.execute('INSERT INTO baseline_issues VALUES (?,?,?,NULL)', (base, key, row[0]))
        marker, number = row
        if number: return number
        # Recover a successful create with a lost HTTP response.
        matches = [i for i in gh.pages('issues?state=all&labels=agent%3Aproposal') if marker in (i.get('body') or '')]
        if matches:
            number = min(matches, key=lambda i: i['number'])['number']
        else:
            failures = '\n'.join('- ' + display(t) for t in result['baseline_failures'])[:4000]
            body = (f'## Outcome\nRestore the configured tests on base revision `{base}`.\n\n'
                    '## Acceptance criteria\n- The identified tests pass on the updated base branch.\n'
                    '- The relevant required CI checks pass without weakening tests.\n\n'
                    f'## Evidence\nBase revision: `{base}`\nCommand fingerprint: `{key}`\n\n{failures}\n\n'
                    'This is separate baseline work. Inspect the cause before adding agent:ready; '
                    'the comparison does not prove which commit introduced the failure.\n\n' + marker)
            number = gh.request('issues', 'POST', {'title': 'Repair baseline test failures at ' + base[:12],
                                'body': body, 'labels': ['agent:proposal']})['number']
        with self.db: self.db.execute('UPDATE baseline_issues SET issue=? WHERE base=? AND command_hash=?', (number, base, key))
        return number
