"""Run isolated, short-lived agent and test Jobs without exposing controller state."""
import io
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid


STAGER = r'''import os, time
while not os.path.exists('/stage/out/.release'):
    time.sleep(1)
'''

# The controller writes the input and prompt into separate emptyDir volumes. The
# runner exits after the child; the staging container stays up to collect results.
RUNNER = r'''import json, os, subprocess, sys, time
while not os.path.exists('/out/.input-ready'):
    time.sleep(1)
args = json.loads(os.environ['LOOP_COMMAND'])
with open('/out/events.log', 'w') as log:
    if os.environ.get('LOOP_PROMPT') == 'true':
        with open('/out/prompt.txt', 'rb') as prompt:
            result = subprocess.run(args, stdin=prompt, stdout=log, stderr=log, cwd='/repo')
    else:
        result = subprocess.run(args, stdout=log, stderr=log, cwd='/repo')
sys.exit(result.returncode)
'''

# Fail closed when Python's safe tar extraction filter is not available.
UNPACK = r'''import sys, tarfile
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as archive:
    archive.extractall('/stage', filter=tarfile.data_filter)
open('/stage/out/.input-ready', 'w').close()
'''

PACK = r'''import os, subprocess, sys, tarfile
class Bounded:
    def __init__(self, output, limit): self.output, self.limit, self.total = output, limit, 0
    def write(self, data):
        self.total += len(data)
        if self.total > self.limit: raise ValueError('archive limit exceeded')
        return self.output.write(data)
    def flush(self): self.output.flush()
out = Bounded(sys.stdout.buffer, int(os.environ.get('LOOP_ARCHIVE_LIMIT', '536870912')))
with tarfile.open(fileobj=out, mode='w|') as archive:
    if sys.argv[1] == 'source':
        repo, git = '/stage/repo', '/stage/meta/git'
        if os.path.lexists(repo + '/.git'): raise ValueError('unexpected .git metadata')
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
                   GIT_TERMINAL_PROMPT='0')
        subprocess.run(['git', 'init', '--bare', '--quiet', git], env=env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open('/stage/meta/tracked', 'rb') as f: tracked = f.read().split(b'\0')
        lines = [b'100644 e69de29bb2d1d6434b8b29ae775ad8c2e48c5391 0\t' + p + b'\0'
                 for p in tracked if p]
        base = ['git', '--git-dir', git, '--work-tree', repo]
        subprocess.run(base + ['update-index', '-z', '--index-info'], input=b''.join(lines),
                       env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        files = subprocess.run(base + ['ls-files', '--cached', '--others', '--exclude-standard', '-z'],
                               env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.split(b'\0')
        archive.add(repo, arcname='repo', recursive=False)
        for raw in files:
            if not raw: continue
            path = os.fsdecode(raw)
            if os.path.lexists(os.path.join(repo, path)):
                archive.add(os.path.join(repo, path), arcname='repo/' + path, recursive=False)
    archive.add('/stage/out', arcname='out',
                filter=lambda m: None if m.name in ('out/prompt.txt', 'out/.input-ready', 'out/.release') else m)
'''


class LimitedWriter:
    def __init__(self, target, limit):
        self.target, self.limit, self.total = target, limit, 0

    def write(self, data):
        self.total += len(data)
        if self.total > self.limit:
            raise ValueError('Input archive exceeds limit')
        return self.target.write(data)

    def flush(self):
        self.target.flush()


def job_manifest(name, namespace, instance, image, command, timeout, review=False,
                 auth_mode=None, auth_pvc=None, model_secret=None, limits=None, archive_limit=536870912):
    """Build only restricted Pods; the worker service account is never mounted."""
    if not re.fullmatch(r'[a-z0-9](?:[-a-z0-9]*[a-z0-9])?', instance) or len(instance) > 40:
        raise ValueError('KUBE_RUNNER_ID must be a DNS label of at most 40 characters')
    if not re.fullmatch(r'[a-z0-9](?:[-a-z0-9]*[a-z0-9])?', namespace) or len(namespace) > 63:
        raise ValueError('KUBE_JOB_NAMESPACE must be a DNS label')
    if not image or not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise ValueError('Agent image and command are required')
    if auth_mode not in (None, 'api', 'codex'):
        raise ValueError('AUTH_MODE must be api or codex')
    limits = limits or {'cpu': '2', 'memory': '4Gi', 'ephemeral-storage': '4Gi'}
    security = {'runAsNonRoot': True, 'runAsUser': 1000, 'runAsGroup': 1000,
                'readOnlyRootFilesystem': True, 'allowPrivilegeEscalation': False,
                'capabilities': {'drop': ['ALL']}}
    resources = {'requests': {'cpu': '100m', 'memory': '128Mi', 'ephemeral-storage': '256Mi'},
                 'limits': limits}
    stage_resources = {'requests': {'cpu': '50m', 'memory': '64Mi', 'ephemeral-storage': '128Mi'},
                       'limits': {'cpu': '500m', 'memory': '512Mi', 'ephemeral-storage': '2Gi'}}
    volumes = [{'name': n, 'emptyDir': {'sizeLimit': '2Gi'}} for n in ('repo', 'out', 'base')]
    volumes.append({'name': 'meta', 'emptyDir': {'sizeLimit': '64Mi'}})
    volumes.append({'name': 'tmp', 'emptyDir': {'medium': 'Memory', 'sizeLimit': '1Gi'}})
    worker_mounts = [{'name': 'repo', 'mountPath': '/repo', 'readOnly': review},
                     {'name': 'out', 'mountPath': '/out'},
                     {'name': 'base', 'mountPath': '/base', 'readOnly': True},
                     {'name': 'tmp', 'mountPath': '/tmp'}]
    env = [{'name': 'HOME', 'value': '/tmp/home'},
           {'name': 'LOOP_COMMAND', 'value': json.dumps(command)},
           {'name': 'LOOP_PROMPT', 'value': 'true' if auth_mode else 'false'}]
    if auth_mode == 'api':
        if not model_secret: raise ValueError('KUBE_MODEL_SECRET is required in api mode')
        env += [{'name': 'CODEX_HOME', 'value': '/tmp/codex'},
                {'name': 'CODEX_API_KEY', 'valueFrom': {'secretKeyRef': {'name': model_secret, 'key': 'CODEX_API_KEY'}}}]
    elif auth_mode == 'codex':
        if not auth_pvc: raise ValueError('KUBE_CODEX_AUTH_PVC is required in codex mode')
        volumes.append({'name': 'auth', 'persistentVolumeClaim': {'claimName': auth_pvc}})
        worker_mounts.append({'name': 'auth', 'mountPath': '/home/node/.codex'})
        env.append({'name': 'CODEX_HOME', 'value': '/home/node/.codex'})
    stager_mounts = [{'name': n, 'mountPath': '/stage/' + n} for n in ('repo', 'out', 'base', 'meta')]
    return {'apiVersion': 'batch/v1', 'kind': 'Job',
            'metadata': {'name': name, 'namespace': namespace, 'labels': {'codex-loops/runner': instance}},
            'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': timeout + 300, 'ttlSecondsAfterFinished': 3600,
              'template': {'metadata': {'labels': {'codex-loops/runner': instance}},
                'spec': {'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                  'enableServiceLinks': False, 'securityContext': {'seccompProfile': {'type': 'RuntimeDefault'}, 'fsGroup': 1000},
                  'volumes': volumes,
                  'containers': [
                    {'name': 'stage', 'image': image, 'workingDir': '/', 'command': ['python3', '-I', '-u', '-c', STAGER],
                     'securityContext': security, 'resources': stage_resources, 'volumeMounts': stager_mounts,
                     'env': [{'name': 'LOOP_ARCHIVE_LIMIT', 'value': str(archive_limit)}]},
                    {'name': 'runner', 'image': image, 'workingDir': '/', 'command': ['python3', '-I', '-u', '-c', RUNNER],
                     'securityContext': security, 'resources': resources, 'volumeMounts': worker_mounts, 'env': env}
                  ]}}}}


def extract_result(archive, dest, max_bytes):
    """Treat returned work as hostile; reject traversal, devices and excessive data."""
    total = 0
    with tarfile.open(fileobj=archive, mode='r:') as bundle:
        members = bundle.getmembers()
        if len(members) > 100000: raise ValueError('Too many archive entries')
        for member in members:
            parts = Path(member.name).parts
            if not parts or parts[0] not in ('repo', 'out') or member.name.startswith('/') or '..' in parts:
                raise ValueError('Unsafe archive path')
            if not (member.isdir() or member.isfile() or member.issym()):
                raise ValueError('Unsupported archive entry')
            if member.issym():
                # Each tree is moved independently after extraction: a link that
                # stays inside the temporary root can otherwise point into the
                # controller's sibling control directory after relocation.
                target = posixpath.normpath(posixpath.join(posixpath.dirname(member.name), member.linkname))
                if member.linkname.startswith('/') or target.split('/')[0] != parts[0]:
                    raise ValueError('Archive symlink escapes its own tree')
            total += member.size
            if total > max_bytes: raise ValueError('Archive exceeds limit')
        bundle.extractall(dest, members=members, filter=tarfile.data_filter)


class KubernetesRunner:
    def __init__(self):
        self.namespace = os.getenv('KUBE_JOB_NAMESPACE', 'codex-loops-jobs')
        self.instance = os.getenv('KUBE_RUNNER_ID', 'codex-loops')
        self.image = os.getenv('AGENT_IMAGE', '')
        self.max_bytes = int(os.getenv('KUBE_ARCHIVE_MAX_BYTES', str(512 * 1024 * 1024)))
        self.start_timeout = int(os.getenv('KUBE_START_TIMEOUT_SECONDS', '300'))
        if not 1 <= self.max_bytes <= 2 * 1024 * 1024 * 1024: raise ValueError('Invalid archive limit')
        if not 1 <= self.start_timeout <= 3600: raise ValueError('Invalid Kubernetes start timeout')

    def call(self, args, **kwargs):
        result = subprocess.run(['kubectl', '-n', self.namespace, *args], stderr=subprocess.DEVNULL,
                                timeout=kwargs.pop('timeout', 60), **kwargs)
        if result.returncode: raise RuntimeError('Kubernetes job operation failed')
        return result

    def tracked_files(self, source):
        # Only path names are staged privately; the trusted Git directory stays
        # in the controller. Preserve tracked files even if .gitignore matches.
        control = source.parent / 'control'
        result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                                 '--git-dir', str(control / '.git'), 'ls-files', '-z'],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
        if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
            raise RuntimeError('Cannot obtain bounded tracked file list')
        return result.stdout

    def run(self, source, output, command, timeout, *, prompt=None, review=False, baseline=None):
        if not 1 <= timeout <= 86400: raise ValueError('Invalid Kubernetes job timeout')
        name = 'codex-job-' + uuid.uuid4().hex[:20]
        auth = os.getenv('AUTH_MODE', 'codex') if prompt is not None else None
        manifest = job_manifest(name, self.namespace, self.instance, self.image, command, timeout,
            review=review, auth_mode=auth, auth_pvc=os.getenv('KUBE_CODEX_AUTH_PVC'),
            model_secret=os.getenv('KUBE_MODEL_SECRET'), archive_limit=self.max_bytes,
            limits={'cpu': os.getenv('KUBE_JOB_CPU', '2'), 'memory': os.getenv('KUBE_JOB_MEMORY', '4Gi'),
                    'ephemeral-storage': os.getenv('KUBE_JOB_EPHEMERAL_STORAGE', '4Gi')})
        deadline = time.monotonic() + timeout
        export_source = prompt is not None and not review
        tracked = self.tracked_files(source) if export_source else b''
        # The common controller flock serializes this runner. Clear crash-orphaned
        # Jobs before another job can reuse the writable Codex login volume.
        self.call(['delete', 'jobs', '-l', 'codex-loops/runner=' + self.instance,
                   '--ignore-not-found=true', '--cascade=foreground', '--wait=true', '--timeout=120s'], timeout=130,
                  stdout=subprocess.DEVNULL)
        try:
            self.call(['create', '-f', '-'], input=json.dumps(manifest).encode(), stdout=subprocess.DEVNULL)
            pod = None
            start_deadline = min(deadline, time.monotonic() + self.start_timeout)
            while time.monotonic() < start_deadline:
                data = self.call(['get', 'pods', '-l', 'job-name=' + name, '-o', 'json'], stdout=subprocess.PIPE)
                for item in json.loads(data.stdout)['items']:
                    statuses = item.get('status', {}).get('containerStatuses', [])
                    for status in statuses:
                        reason = status.get('state', {}).get('waiting', {}).get('reason', '')
                        if reason in ('CreateContainerConfigError', 'InvalidImageName', 'ErrImageNeverPull', 'ImagePullBackOff'):
                            raise RuntimeError('Kubernetes job cannot start: ' + reason)
                    if all(any(s['name'] == n and 'running' in s.get('state', {}) for s in statuses)
                           for n in ('stage', 'runner')):
                        pod = item['metadata']['name']; break
                    if item.get('status', {}).get('phase') == 'Failed': raise RuntimeError('Kubernetes job failed to start')
                if pod: break
                time.sleep(1)
            if not pod: raise TimeoutError('Kubernetes job did not start before the configured start deadline')
            with tempfile.TemporaryFile(dir=source.parent) as staged:
                with tarfile.open(fileobj=LimitedWriter(staged, self.max_bytes), mode='w|') as bundle:
                    bundle.add(source, arcname='repo')
                    bundle.add(output, arcname='out')
                    if baseline is not None: bundle.add(baseline, arcname='base')
                    if export_source:
                        info = tarfile.TarInfo('meta/tracked'); info.mode = 0o600; info.size = len(tracked)
                        bundle.addfile(info, io.BytesIO(tracked))
                    if prompt is not None:
                        raw = prompt.encode()
                        info = tarfile.TarInfo('out/prompt.txt'); info.mode = 0o600; info.size = len(raw)
                        bundle.addfile(info, io.BytesIO(raw))
                if staged.tell() > self.max_bytes: raise ValueError('Input archive exceeds limit')
                staged.seek(0)
                self.call(['exec', '-i', pod, '-c', 'stage', '--', 'python3', '-I', '-c', UNPACK],
                          stdin=staged, stdout=subprocess.DEVNULL, timeout=max(1, int(deadline-time.monotonic())))
            exit_code = None
            while time.monotonic() < deadline:
                data = self.call(['get', 'pod', pod, '-o', 'json'], stdout=subprocess.PIPE)
                statuses = json.loads(data.stdout).get('status', {}).get('containerStatuses', [])
                for item in statuses:
                    if item['name'] == 'runner' and 'terminated' in item.get('state', {}):
                        exit_code = item['state']['terminated']['exitCode']
                if exit_code is not None: break
                time.sleep(1)
            if exit_code is None: raise TimeoutError('Kubernetes job exceeded deadline')
            with tempfile.TemporaryFile(dir=source.parent) as returned, tempfile.TemporaryDirectory(dir=source.parent) as folder:
                self.call(['exec', pod, '-c', 'stage', '--', 'python3', '-I', '-c', PACK,
                           'source' if export_source and exit_code == 0 else 'output'],
                          stdout=returned, timeout=max(1, int(deadline-time.monotonic())))
                if returned.tell() > self.max_bytes: raise ValueError('Output archive exceeds limit')
                returned.seek(0); extract_result(returned, Path(folder), self.max_bytes)
                staged_out = Path(folder) / 'out'
                if not staged_out.is_dir() or staged_out.is_symlink(): raise ValueError('Missing output directory')
                # Preserve outputs even on command failure, but never publish failed work.
                shutil.copytree(staged_out, output, dirs_exist_ok=True, symlinks=True)
                staged_repo = Path(folder) / 'repo'
                if prompt is not None and not review and exit_code == 0:
                    if not staged_repo.is_dir() or staged_repo.is_symlink(): raise ValueError('Missing source directory')
                    old = source.parent / ('previous-' + uuid.uuid4().hex)
                    source.rename(old)
                    try: staged_repo.rename(source)
                    except Exception:
                        old.rename(source); raise
                    shutil.rmtree(old)
            return exit_code
        finally:
            # A lost create response may still have created the Job, so delete by name.
            try:
                self.call(['delete', 'job', name, '--ignore-not-found=true', '--cascade=foreground', '--wait=true', '--timeout=120s'],
                          timeout=130, stdout=subprocess.DEVNULL)
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                pass  # Active deadline and TTL still bound a successfully created Job.
