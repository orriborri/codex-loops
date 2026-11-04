import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from kube_runner import KubernetesRunner, extract_result, job_manifest, UNPACK, PACK, RUNNER
from loop import Loops
from baseline import BaselineChecks
from support import verdict


class ManifestTests(unittest.TestCase):
    def build(self, **kw):
        return job_manifest('codex-job-123', 'codex-loops-jobs', 'runner', 'example/agent@sha256:aaa',
                            ['codex', 'exec', '-'], 60, **kw)

    def test_review_job_is_restricted_and_has_no_controller_credentials(self):
        obj = self.build(review=True, auth_mode='codex', auth_pvc='codex-auth')
        spec = obj['spec']['template']['spec']
        self.assertEqual(obj['spec']['backoffLimit'], 0)
        self.assertEqual(obj['spec']['activeDeadlineSeconds'], 360)
        self.assertFalse(spec['automountServiceAccountToken'])
        stage, worker = spec['containers']
        for c in (stage, worker):
            self.assertEqual(c['workingDir'], '/')
            self.assertIn('-I', c['command'])
            self.assertTrue(c['securityContext']['readOnlyRootFilesystem'])
            self.assertTrue(c['securityContext']['runAsNonRoot'])
            self.assertFalse(c['securityContext']['allowPrivilegeEscalation'])
            self.assertEqual(c['securityContext']['capabilities']['drop'], ['ALL'])
        self.assertTrue(next(m for m in worker['volumeMounts'] if m['name'] == 'repo')['readOnly'])
        self.assertTrue(next(m for m in worker['volumeMounts'] if m['name'] == 'base')['readOnly'])
        self.assertNotIn('auth', [m['name'] for m in stage['volumeMounts']])
        self.assertIn('meta', [m['name'] for m in stage['volumeMounts']])
        self.assertNotIn('meta', [m['name'] for m in worker['volumeMounts']])
        self.assertNotIn('GH_TOKEN', json.dumps(obj))
        self.assertNotIn('OPENAI_API_KEY', json.dumps(obj))

    def test_api_key_is_only_secret_reference_and_test_jobs_have_no_auth(self):
        api = self.build(auth_mode='api', model_secret='model-auth')
        env = api['spec']['template']['spec']['containers'][1]['env']
        key = next(e for e in env if e['name'] == 'CODEX_API_KEY')
        self.assertEqual(key['valueFrom']['secretKeyRef'], {'name': 'model-auth', 'key': 'CODEX_API_KEY'})
        test = self.build()
        self.assertNotIn('model-auth', json.dumps(test))
        self.assertNotIn('CODEX_API_KEY', json.dumps(test))
        self.assertNotIn('auth', [v['name'] for v in test['spec']['template']['spec']['volumes']])

    def test_invalid_instance_and_missing_auth_fail_before_job_creation(self):
        for invalid in ['bad/name', 'UPPER', '', 'x' * 41]:
            with self.assertRaises(ValueError):
                job_manifest('job', 'worker', invalid, 'image', ['echo'], 1)
        with self.assertRaises(ValueError): self.build(auth_mode='codex')
        with self.assertRaises(ValueError): self.build(auth_mode='api')


class ArchiveTests(unittest.TestCase):
    def archive(self, name, data=b'hello', kind=None, link=''):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w') as tar:
            item = tarfile.TarInfo(name)
            if kind:
                item.type = kind; item.linkname = link
                tar.addfile(item)
            else:
                item.size = len(data); tar.addfile(item, io.BytesIO(data))
        buf.seek(0)
        return buf

    def test_rejects_traversal_symlink_escape_devices_and_oversize(self):
        cases = [self.archive('../outside'), self.archive('/absolute'),
                 self.archive('repo/link', kind=tarfile.SYMTYPE, link='../../outside'),
                 self.archive('repo/link', kind=tarfile.SYMTYPE, link='../control/secret'),
                 self.archive('out/link', kind=tarfile.SYMTYPE, link='../control/secret'),
                 self.archive('repo/device', kind=tarfile.CHRTYPE), self.archive('repo/huge', b'12345')]
        with tempfile.TemporaryDirectory() as root:
            for archive in cases:
                with self.assertRaises((ValueError, tarfile.FilterError)):
                    extract_result(archive, Path(root), 4)

    def test_normal_source_and_output_are_extracted(self):
        with tempfile.TemporaryDirectory() as root:
            extract_result(self.archive('repo/hi.txt'), Path(root), 1024)
            self.assertEqual((Path(root) / 'repo/hi.txt').read_text(), 'hello')

    def test_symlink_inside_the_same_tree_is_allowed(self):
        with tempfile.TemporaryDirectory() as root:
            extract_result(self.archive('repo/link', kind=tarfile.SYMTYPE, link='target'), Path(root), 1024)
            self.assertEqual(os.readlink(Path(root) / 'repo/link'), 'target')


class FakeRunner(KubernetesRunner):
    def __init__(self, root, code=0, review=False):
        self.namespace, self.instance, self.image = 'worker', 'test', 'example/agent:dev'
        self.max_bytes = 1024 * 1024
        self.start_timeout = 30
        self.root, self.code, self.review, self.calls = root, code, review, []
        self.manifest = None

    def tracked_files(self, source):
        return b'app.py\0'

    def call(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == 'create':
            self.manifest = json.loads(kwargs['input']); return subprocess.CompletedProcess(args, 0, b'')
        if args[:2] == ['get', 'pods']:
            states = [{'name': name, 'state': {'running': {}}} for name in ('stage', 'runner')]
            return subprocess.CompletedProcess(args, 0, json.dumps({'items': [{'metadata': {'name': 'test-pod'},
                'status': {'containerStatuses': states}}]}).encode())
        if args[:2] == ['get', 'pod']:
            states = [{'name': 'runner', 'state': {'terminated': {'exitCode': self.code}}}]
            return subprocess.CompletedProcess(args, 0, json.dumps({'status': {'containerStatuses': states}}).encode())
        if args[0] == 'exec' and '-i' in args:
            with tarfile.open(fileobj=kwargs['stdin'], mode='r:') as archive:
                archive.extractall(self.root / 'remote', filter=tarfile.data_filter)
            (self.root / 'remote' / 'repo' / 'app.py').write_text('updated')
            (self.root / 'remote' / 'out' / 'result.json').write_text('{"outcome":"completed"}')
            (self.root / 'remote' / 'out' / 'events.log').write_text('job log')
            return subprocess.CompletedProcess(args, 0, b'')
        if args[0] == 'exec':
            with tarfile.open(fileobj=kwargs['stdout'], mode='w') as archive:
                archive.add(self.root / 'remote' / 'repo', arcname='repo')
                archive.add(self.root / 'remote' / 'out', arcname='out',
                    filter=lambda m: None if m.name == 'out/prompt.txt' else m)
            return subprocess.CompletedProcess(args, 0, b'')
        return subprocess.CompletedProcess(args, 0, b'')


class ProtocolTests(unittest.TestCase):
    def test_actual_stage_rejects_unsafe_input_without_starting_runner(self):
        with tempfile.TemporaryDirectory() as folder:
            stage = Path(folder) / 'stage'
            for name in ('repo', 'out', 'base', 'meta'):
                (stage / name).mkdir(parents=True, exist_ok=True)
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode='w') as archive:
                member = tarfile.TarInfo('repo/link')
                member.type = tarfile.SYMTYPE; member.linkname = '../../outside'
                archive.addfile(member)
            result = subprocess.run(['python3', '-I', '-c', UNPACK.replace('/stage', str(stage))],
                                    input=buf.getvalue(), capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((stage / 'out' / '.input-ready').exists())

    def test_runner_helper_ignores_untrusted_python_modules_in_repo(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); repo = root / 'repo'; out = root / 'out'
            repo.mkdir(); out.mkdir()
            (repo / 'subprocess.py').write_text('raise RuntimeError("shadowed")')
            (repo / 'json.py').write_text('raise RuntimeError("shadowed")')
            (out / '.input-ready').touch()
            env = dict(os.environ, LOOP_PROMPT='false',
                       LOOP_COMMAND=json.dumps(['python3', '-I', '-c', 'print("ran")']))
            script = RUNNER.replace('/out', str(out)).replace('/repo', str(repo))
            result = subprocess.run(['python3', '-I', '-c', script], cwd='/', env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('ran', (out / 'events.log').read_text())

    def test_actual_stage_scripts_transfer_without_returning_prompt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); stage = root / 'stage'
            for name in ('repo', 'out', 'base', 'meta'):
                (stage / name).mkdir(parents=True, exist_ok=True)
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode='w') as archive:
                for name, data in [('repo/code.py', b'code'), ('out/prompt.txt', b'secret prompt')]:
                    member = tarfile.TarInfo(name); member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
            uploaded = subprocess.run(['python3', '-c', UNPACK.replace('/stage', str(stage))],
                                      input=buf.getvalue(), capture_output=True)
            self.assertEqual(uploaded.returncode, 0, uploaded.stderr)
            self.assertTrue((stage / 'out' / '.input-ready').exists())
            downloaded = subprocess.run(['python3', '-c', PACK.replace('/stage', str(stage)), 'output'], capture_output=True)
            self.assertEqual(downloaded.returncode, 0, downloaded.stderr)
            with tarfile.open(fileobj=io.BytesIO(downloaded.stdout), mode='r:') as archive:
                self.assertNotIn('repo/code.py', archive.getnames())
                self.assertNotIn('out/prompt.txt', archive.getnames())

    def test_source_export_honors_ignore_but_keeps_already_tracked_files(self):
        with tempfile.TemporaryDirectory() as folder:
            stage = Path(folder) / 'stage'
            for name in ('repo', 'out', 'base', 'meta'):
                (stage / name).mkdir(parents=True, exist_ok=True)
            repo = stage / 'repo'
            (repo / '.gitignore').write_text('node_modules/\n*.log\n')
            (repo / 'node_modules').mkdir()
            (repo / 'node_modules' / 'tracked').write_text('kept')
            (repo / 'node_modules' / 'generated').write_text('ignored')
            (repo / 'new.py').write_text('new')
            (repo / 'debug.log').write_text('ignored')
            (stage / 'meta' / 'tracked').write_bytes(b'node_modules/tracked\0')
            result = subprocess.run(['python3', '-I', '-c', PACK.replace('/stage', str(stage)), 'source'],
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            with tarfile.open(fileobj=io.BytesIO(result.stdout), mode='r:') as archive:
                names = archive.getnames()
            self.assertIn('repo/node_modules/tracked', names)
            self.assertIn('repo/new.py', names)
            self.assertNotIn('repo/node_modules/generated', names)
            self.assertNotIn('repo/debug.log', names)

    def test_success_stages_and_returns_work_without_prompt_or_controller_data(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); source = root / 'source'; output = root / 'output'; remote = root / 'remote'
            source.mkdir(); output.mkdir(); remote.mkdir()
            (source / 'app.py').write_text('original')
            runner = FakeRunner(root)
            with patch.dict(os.environ, {'AUTH_MODE': 'codex', 'KUBE_CODEX_AUTH_PVC': 'auth'}):
                code = runner.run(source, output, ['codex', 'exec', '-'], 30, prompt='private prompt')
            self.assertEqual(code, 0)
            self.assertEqual((source / 'app.py').read_text(), 'updated')
            self.assertEqual((output / 'events.log').read_text(), 'job log')
            self.assertFalse((output / 'prompt.txt').exists())
            self.assertEqual(runner.calls[0][0:2], ['delete', 'jobs'])
            self.assertIn('--cascade=foreground', runner.calls[0])
            self.assertEqual(runner.calls[-1][0:2], ['delete', 'job'])

    def test_failed_and_review_jobs_never_replace_source(self):
        for code, review in [(1, False), (0, True)]:
            with self.subTest(code=code, review=review), tempfile.TemporaryDirectory() as root:
                root = Path(root); source = root / 'source'; output = root / 'output'; remote = root / 'remote'
                source.mkdir(); output.mkdir(); remote.mkdir(); (source / 'app.py').write_text('original')
                runner = FakeRunner(root, code, review)
                with patch.dict(os.environ, {'AUTH_MODE': 'codex', 'KUBE_CODEX_AUTH_PVC': 'auth'}):
                    self.assertEqual(runner.run(source, output, ['codex'], 30, prompt='goal', review=review), code)
                self.assertEqual((source / 'app.py').read_text(), 'original')

    def test_failed_orphan_cleanup_prevents_new_job(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'source'; output = root / 'output'
            source.mkdir(); output.mkdir()
            runner = FakeRunner(root)
            runner.call = Mock(side_effect=RuntimeError('cleanup unavailable'))
            with patch.dict(os.environ, {'AUTH_MODE': 'codex', 'KUBE_CODEX_AUTH_PVC': 'auth'}):
                with self.assertRaisesRegex(RuntimeError, 'cleanup unavailable'):
                    runner.run(source, output, ['codex'], 30, prompt='goal')
            runner.call.assert_called_once()

    def test_missing_model_secret_fails_promptly_and_cleans_job(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'source'; output = root / 'output'
            source.mkdir(); output.mkdir()
            runner = FakeRunner(root)
            original = runner.call
            def call(args, **kwargs):
                if args[:2] == ['get', 'pods']:
                    body = {'items': [{'metadata': {'name': 'pod'}, 'status': {'containerStatuses': [
                        {'name': 'runner', 'state': {'waiting': {'reason': 'CreateContainerConfigError',
                                                                  'message': 'secret contents'}}}]}}]}
                    return subprocess.CompletedProcess(args, 0, json.dumps(body).encode())
                return original(args, **kwargs)
            runner.call = call
            with patch.dict(os.environ, {'AUTH_MODE': 'api', 'KUBE_MODEL_SECRET': 'missing'}):
                with self.assertRaisesRegex(RuntimeError, 'CreateContainerConfigError'):
                    runner.run(source, output, ['codex'], 30, prompt='goal')
            self.assertEqual(runner.calls[-1][:2], ['delete', 'job'])


class ControllerIntegrationTests(unittest.TestCase):
    def test_review_and_implementation_call_kubernetes_without_docker_auth(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'source'; output = root / 'out'
            source.mkdir(); output.mkdir()
            loops = object.__new__(Loops)
            def run(_source, dest, command, _timeout, **kwargs):
                self.assertEqual(command[0], 'codex')
                self.assertEqual(kwargs['prompt'], 'Review goal')
                self.assertTrue(kwargs['review'])
                (dest / 'result.json').write_text(json.dumps(verdict()))
                return 0
            with patch.dict(os.environ, {'JOB_BACKEND': 'kubernetes'}), \
                 patch('kube_runner.KubernetesRunner.run', side_effect=run), \
                 patch('loop.agent_auth', side_effect=AssertionError('Docker auth must not be used')):
                self.assertEqual(loops._agent(source, output, 'Review goal', review=True), verdict())

    def test_baseline_kubernetes_job_has_no_model_auth(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'source'; output = root / 'out'
            source.mkdir(); output.mkdir()
            def run(_source, dest, command, _timeout, **kwargs):
                self.assertEqual(command, ['bash', '-lc', 'tests'])
                self.assertNotIn('prompt', kwargs)
                (dest / 'tests.xml').write_text('<testsuite tests="1"><testcase name="ok"/></testsuite>')
                (dest / 'events.log').write_text('test log')
                return 0
            with patch.dict(os.environ, {'JOB_BACKEND': 'kubernetes'}), patch('baseline.os.chown'), \
                 patch('kube_runner.KubernetesRunner.run', side_effect=run):
                result = BaselineChecks.run(source, output, 'tests', 'agent:test')
            self.assertEqual(result['status'], 'passed')
            self.assertEqual(next(output.rglob('test.log')).read_text(), 'test log')


if __name__ == '__main__': unittest.main()
