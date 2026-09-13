#!/usr/bin/env python3
"""Local-only web update worker. Docker authority stays outside the dashboard."""
import argparse
import copy
import datetime
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import platform
import re
import socketserver
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid

import gateway

REPOSITORY = 'ghcr.io/lingchaojie/ai_dashboard'
ACTIVE = ('queued', 'running')


class Busy(Exception):
    pass


def validate_digest(value):
    if not isinstance(value, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', value):
        raise ValueError('无效的镜像摘要，请重新检查更新')
    return value


def select_manifest(index, architecture):
    for item in index.get('manifests', []):
        p = item.get('platform', {})
        if p.get('os') == 'linux' and p.get('architecture') == architecture:
            return validate_digest(item['digest'])
    raise ValueError('新版本不支持当前服务器架构')


class RegistryRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith('https://'):
            raise ValueError('镜像仓库返回了不安全的下载地址')
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected:
            redirected.remove_header('Authorization')
        return redirected


def latest_release():
    opener = urllib.request.build_opener(RegistryRedirect())
    def fetch(url, token=None, digest=None):
        headers = {'Accept': 'application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        with opener.open(urllib.request.Request(url, headers=headers), timeout=10) as response:
            data = response.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            raise ValueError('镜像版本信息过大')
        actual = 'sha256:' + hashlib.sha256(data).hexdigest()
        if digest and actual != digest:
            raise ValueError('镜像版本信息校验失败')
        return json.loads(data), actual
    auth, _ = fetch('https://ghcr.io/token?service=ghcr.io&scope=repository:lingchaojie/ai_dashboard:pull')
    token = auth['token']
    base = 'https://ghcr.io/v2/lingchaojie/ai_dashboard/'
    index, digest = fetch(base + 'manifests/latest', token)
    arch = {'x86_64':'amd64', 'aarch64':'arm64'}.get(platform.machine(), platform.machine())
    child = select_manifest(index, arch)
    manifest, _ = fetch(base + 'manifests/' + child, token, child)
    image_id = validate_digest(manifest['config']['digest'])
    config, _ = fetch(base + 'blobs/' + image_id, token, image_id)
    labels = config.get('config', {}).get('Labels') or {}
    return dict(digest=digest, platform_digest=child, image_id=image_id, commit=labels.get('org.opencontainers.image.revision', ''),
                created=labels.get('org.opencontainers.image.created', ''))


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.state-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Worker:
    def __init__(self, directory):
        self.directory = directory.resolve()
        self.state_dir = self.directory / '.updates'
        if self.state_dir.is_symlink():
            raise ValueError('Refusing symlink: .updates')
        self.state_dir.mkdir(mode=0o700, exist_ok=True)
        self.state_dir.chmod(0o700)
        self.jobs_dir = self.state_dir / 'jobs'
        self.jobs_dir.mkdir(mode=0o700, exist_ok=True)
        self.lock = threading.RLock()
        self.check_lock = threading.Lock()
        self.latest = None
        self.checked_at = 0
        self.current = dict(version='unknown', commit='unknown', build_date='unknown')
        self.image_id = None
        self.job = None
        self.thread = None
        state = self.state_dir / 'state.json'
        if state.exists():
            self.job = json.loads(state.read_text())
            if self.job['status'] in ACTIVE:
                self.job.update(status='interrupted', stage='failed', finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                message='更新服务曾重启，任务已中断。请检查运行状态；必要时使用备份恢复。')
                self.persist()
        self.refresh_current()

    def refresh_current(self):
        app = gateway.Deployment(self.directory, dashboard_only=True)
        container = app.container()
        if container:
            labels = container.get('Config', {}).get('Labels') or {}
            with self.lock:
                self.image_id = container['Image']
                self.current = dict(version=labels.get('org.opencontainers.image.version', 'unknown'),
                                    commit=labels.get('org.opencontainers.image.revision', 'unknown'),
                                    build_date=labels.get('org.opencontainers.image.created', 'unknown'))

    def previous_backup(self):
        pointer = self.directory / '.previous-backup'
        if not pointer.exists() or pointer.is_symlink():
            return None
        name = pointer.read_text().strip()
        if not name or Path(name).name != name:
            return None
        archive = self.directory / 'backups' / name
        return archive if archive.is_file() and not archive.is_symlink() else None

    def has_update(self):
        # Classic stores identify images by config digest; containerd stores can
        # identify them by index or platform-manifest digest. All are immutable.
        return bool(self.latest and self.image_id not in {
            digest for digest in (self.latest['digest'], self.latest['image_id'], self.latest.get('platform_digest')) if digest})

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(dict(enabled=True, current=self.current, channel='main', latest=self.latest,
                                      has_update=self.has_update(),
                                      rollback_available=self.previous_backup() is not None, job=self.job))

    def check(self):
        if not self.check_lock.acquire(blocking=False):
            raise Busy('正在检查更新，请稍后重试')
        try:
            with self.lock:
                if self.job and self.job['status'] in ACTIVE:
                    raise Busy('更新任务正在执行')
            release = latest_release()
            self.refresh_current()
            with self.lock:
                self.latest = release
                self.checked_at = time.monotonic()
            return self.snapshot()
        finally:
            self.check_lock.release()

    def persist(self):
        atomic_json(self.jobs_dir / (self.job['id'] + '.json'), self.job)
        atomic_json(self.state_dir / 'state.json', self.job)

    def progress(self, stage, message):
        with self.lock:
            self.job.update(status='running', stage=stage, message=message)
            self.persist()

    def submit(self, action, request):
        allowed = {'request_id', 'digest'} if action == 'update' else {'request_id'}
        if action not in ('update', 'rollback') or not isinstance(request, dict) or set(request) != allowed:
            raise ValueError('无效的更新请求')
        request_id = request['request_id']
        if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
            raise ValueError('无效的请求编号')
        if action == 'update':
            validate_digest(request['digest'])
        with self.lock:
            record = self.jobs_dir / (request_id + '.json')
            if record.exists():
                previous = json.loads(record.read_text())
                if previous['action'] != action or previous.get('digest') != request.get('digest'):
                    raise ValueError('请求编号已用于另一个操作')
                return previous
            if (self.job and self.job['status'] in ACTIVE) or (self.thread and self.thread.is_alive()):
                raise Busy('已有更新任务正在执行')
            if action == 'update':
                if not self.latest or time.monotonic() - self.checked_at > 600 or request['digest'] != self.latest['digest']:
                    raise ValueError('版本信息已过期或变更，请重新检查更新')
                if not self.has_update():
                    raise ValueError('当前已是最新版本')
            elif self.previous_backup() is None:
                raise ValueError('没有可回滚的升级备份')
            operation_lock = (self.directory / '.gateway.lock').open('a')
            try:
                fcntl.flock(operation_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                operation_lock.close()
                raise Busy('服务器正在执行其他部署操作') from error
            try:
                self.job = dict(id=request_id, action=action, status='queued', stage='queued',
                                message='任务已提交', started_at=datetime.datetime.now(datetime.timezone.utc).isoformat(), finished_at=None)
                if action == 'update':
                    self.job['digest'] = request['digest']
                self.persist()
                self.thread = threading.Thread(target=self.run, args=(copy.deepcopy(self.job), operation_lock), daemon=True)
                self.thread.start()
                return copy.deepcopy(self.job)
            except BaseException:
                operation_lock.close()
                raise

    def execute(self, job):
        app = gateway.Deployment(self.directory, dashboard_only=True, progress=self.progress)
        if job['action'] == 'update':
            app.update(REPOSITORY + '@' + validate_digest(job['digest']))
        else:
            archive = self.previous_backup()
            if archive is None:
                raise ValueError('升级备份已不存在')
            app.restore(archive)

    def run(self, job, operation_lock):
        try:
            self.progress('pulling' if job['action'] == 'update' else 'recovering',
                          '正在下载新版本' if job['action'] == 'update' else '正在恢复升级前的版本和数据')
            self.execute(job)
            with self.lock:
                self.job.update(status='succeeded', stage='completed', message='更新完成' if job['action'] == 'update' else '回滚完成')
        except Exception as error:
            # Detailed diagnostics remain in container logs; never expose host configuration.
            print(f'Update job {job["id"]} failed: {error}', flush=True)
            with self.lock:
                self.job.update(status='failed', stage='failed', message='操作失败；若已进入升级阶段，已尝试恢复旧版本。请检查网站状态和更新服务日志。')
        finally:
            try:
                self.refresh_current()
            except Exception as error:
                print(f'Cannot inspect dashboard after operation: {error}', flush=True)
            with self.lock:
                self.job['finished_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                try:
                    self.persist()
                finally:
                    operation_lock.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Jobs are detached from the HTTP connection.

    def do_GET(self):
        if self.path == '/status':
            self.reply(200, self.server.worker.snapshot())
        else:
            self.reply(404, {'error':'接口不存在'})

    def do_POST(self):
        try:
            if self.path not in ('/check', '/update', '/rollback'):
                self.reply(404, {'error':'接口不存在'})
                return
            length = int(self.headers.get('Content-Length', '0'))
            if self.headers.get('Transfer-Encoding') or not 0 < length <= 2048:
                raise ValueError('无效的请求大小')
            self.connection.settimeout(5)
            payload = json.loads(self.rfile.read(length))
            if self.path == '/check':
                if payload != {}:
                    raise ValueError('检查更新不接受额外参数')
                self.reply(200, self.server.worker.check())
            else:
                self.reply(202, self.server.worker.submit(self.path[1:], payload))
        except Busy as error:
            self.reply(409, {'error':str(error)})
        except (ValueError, TypeError, KeyError):
            self.reply(400, {'error':'请求无效或版本信息已过期，请重新检查更新'})
        except Exception as error:
            print(f'Updater request failed: {error}', flush=True)
            self.reply(502, {'error':'无法检查镜像或部署状态，请稍后重试并查看更新服务日志'})


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    directory = args.directory.resolve()
    config = gateway.load_config(directory / '.env')
    run = directory / 'update-run'
    if run.is_symlink():
        raise ValueError('Refusing symlink: update-run')
    run.mkdir(mode=0o755, exist_ok=True)
    run.chmod(0o755)
    # A lifetime lock prevents two workers unlinking each other's socket.
    with (directory / '.updater.lock').open('a') as lifetime:
        fcntl.flock(lifetime, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = Worker(directory)
        socket = run / 'updater.sock'
        socket.unlink(missing_ok=True)
        with Server(str(socket), Handler) as server:
            if os.geteuid() == 0:
                os.chown(socket, int(config['GATEWAY_UID']), int(config['GATEWAY_GID']))
            socket.chmod(0o600)
            server.worker = worker
            print('AI Gateway local update worker ready', flush=True)
            server.serve_forever()


if __name__ == '__main__':
    main()
