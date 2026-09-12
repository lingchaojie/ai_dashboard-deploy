#!/usr/bin/env python3
"""Manage one AI Gateway Compose installation; no third-party Python packages."""
import argparse
import datetime
import fcntl
import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time


def default_config():
    return dict(GATEWAY_IMAGE='ghcr.io/lingchaojie/ai_dashboard:latest',
                BIND_HOST='127.0.0.1', SERVER_PORT='8080', COMPOSE_PROJECT_NAME='ai-gateway',
                GATEWAY_UID=str(os.getuid() or 10001), GATEWAY_GID=str(os.getgid() or 10001),
                TZ='Asia/Shanghai', DOMAIN='')


def validate_config(config):
    if set(config) != set(default_config()):
        raise ValueError('Unknown or missing .env keys; compare with .env.example')
    for key, value in config.items():
        if any(c in value for c in '\n\r\x00$`"\'\\ '):
            raise ValueError(f'Invalid {key}: use a literal value without shell expressions')
    if not re.fullmatch(r'[a-z0-9][a-z0-9._/:@-]*', config['GATEWAY_IMAGE']):
        raise ValueError('Invalid GATEWAY_IMAGE')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', config['COMPOSE_PROJECT_NAME']):
        raise ValueError('Invalid COMPOSE_PROJECT_NAME')
    ipaddress.IPv4Address(config['BIND_HOST'])
    for key, maximum, minimum in [('SERVER_PORT', 65535, 1), ('GATEWAY_UID', 2147483647, 1), ('GATEWAY_GID', 2147483647, 1)]:
        if not config[key].isdigit() or not minimum <= int(config[key]) <= maximum:
            raise ValueError(f'Invalid {key}')
    if not re.fullmatch(r'[A-Za-z0-9_+/-]+', config['TZ']):
        raise ValueError('Invalid TZ')
    domain = config['DOMAIN']
    if domain and (len(domain) > 253 or not re.fullmatch(r'(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}', domain)):
        raise ValueError('DOMAIN must be a DNS hostname without scheme, port, or path')


def load_config(path):
    config = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or key in config:
            raise ValueError('Invalid or duplicate .env setting')
        config[key] = value
    validate_config(config)
    return config


def save_config(path, config):
    validate_config(config)
    fd, temporary = tempfile.mkstemp(prefix='.env-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(''.join(f'{key}={value}\n' for key, value in config.items()))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')


def unpack_backup(archive, destination):
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()
        names = set()
        for item in members:
            path = PurePosixPath(item.name)
            if path.is_absolute() or '..' in path.parts or not path.parts or (path.parts[0] != 'data' and item.name not in ('deployment.env', 'manifest.json')):
                raise ValueError('Unsafe backup member')
            if not (item.isfile() or item.isdir()) or item.name in names:
                raise ValueError('Backup must contain unique regular files and directories')
            names.add(item.name)
        if not {'data/dashboard.db', 'data/encryption.key', 'deployment.env', 'manifest.json'} <= names:
            raise ValueError('Backup is missing database, encryption key or deployment metadata')
        destination.mkdir(mode=0o700)
        for item in members:
            target = destination / item.name
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tar.extractfile(item) as source, target.open('wb') as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o600)
        config = load_config(destination / 'deployment.env')
        manifest = json.loads((destination / 'manifest.json').read_text())
        if manifest.get('format') != 1 or not isinstance(manifest.get('image'), str):
            raise ValueError('Unsupported backup manifest')
        config['GATEWAY_IMAGE'] = manifest['image']
        validate_config(config)
        return config


class Deployment:
    def __init__(self, directory):
        self.directory = directory
        self.env_path = directory / '.env'
        self.config = load_config(self.env_path)
        self.check_paths()

    def check_paths(self):
        for name in ('data', 'backups', '.env', '.previous-backup'):
            if (self.directory / name).is_symlink():
                raise ValueError(f'Refusing symlink: {name}')
        self.compose('config', '--quiet')

    def compose(self, *args, config=None, capture=False):
        config = config or self.config
        env = dict(os.environ, **config)
        # Host shell secrets and Compose overrides must not alter this installation.
        for key in list(env):
            if key.startswith('COMPOSE_') and key != 'COMPOSE_PROJECT_NAME':
                del env[key]
        command = ['docker', 'compose', '--project-directory', str(self.directory),
                   '--env-file', str(self.env_path), '-p', config['COMPOSE_PROJECT_NAME'],
                   '-f', str(self.directory / 'compose.yaml')]
        if config['DOMAIN']:
            command += ['-f', str(self.directory / 'compose.https.yaml')]
        return subprocess.run(command + list(args), env=env, check=True, text=True,
                              stdout=subprocess.PIPE if capture else None).stdout

    def container(self):
        result = self.compose('ps', '--all', '--quiet', 'dashboard', capture=True).strip()
        if not result:
            return None
        return json.loads(subprocess.check_output(['docker', 'inspect', result], text=True))[0]

    def start(self, pull='missing'):
        self.compose('up', '-d', '--wait', '--wait-timeout', '120', '--pull', pull, '--remove-orphans')

    def resume_existing(self):
        self.compose('start', 'dashboard')
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            container = self.container()
            if container and container['State'].get('Health', {}).get('Status') == 'healthy':
                return
            time.sleep(1)
        raise ValueError('Existing container did not recover readiness within 120 seconds')

    def backup(self, restart=True):
        container = self.container()
        if not container:
            raise ValueError('No installed container to back up')
        running = container['State']['Running']
        image_id = container['Image']
        image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image_id], text=True))[0]
        image_ref = (image.get('RepoDigests') or [image_id])[0]
        data = self.directory / 'data'
        backups = self.directory / 'backups'
        backups.mkdir(mode=0o700, exist_ok=True)
        backups.chmod(0o700)
        archive = backups / f'gateway-{stamp()}.tar.gz'
        try:
            if running:
                self.compose('stop', 'dashboard')
            for name in ('dashboard.db', 'encryption.key'):
                if not (data / name).is_file():
                    raise ValueError(f'Data file missing: {name}')
            with tarfile.open(archive, 'w:gz') as tar:
                tar.add(data, arcname='data')
                tar.add(self.env_path, arcname='deployment.env')
                metadata = json.dumps({'format': 1, 'image': image_ref, 'created': stamp()}).encode()
                info = tarfile.TarInfo('manifest.json')
                info.size = len(metadata)
                info.mode = 0o600
                tar.addfile(info, io.BytesIO(metadata))
            archive.chmod(0o600)
        except BaseException:
            archive.unlink(missing_ok=True)
            # Even an update backup failure must not leave the old service stopped.
            if running:
                self.resume_existing()
            raise
        else:
            if restart and running:
                self.resume_existing()
        print(f'Backup: {archive}', flush=True)
        return archive

    def restore(self, archive, safety_backup=True):
        # Validate everything before stopping the running service.
        with tempfile.TemporaryDirectory(prefix='.restore-', dir=self.directory) as temporary:
            extracted = Path(temporary) / 'contents'
            recovered = unpack_backup(archive, extracted)
            # Keep this installation's ports, ownership, project and TLS settings.
            config = dict(self.config, GATEWAY_IMAGE=recovered['GATEWAY_IMAGE'])
            present = subprocess.run(['docker', 'image', 'inspect', config['GATEWAY_IMAGE']],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            if present.returncode:
                subprocess.run(['docker', 'pull', config['GATEWAY_IMAGE']], check=True)
            # Compose --pull missing permits restoring registry digest backups on a new host.
            if safety_backup and self.container():
                if all((self.directory / 'data' / name).is_file() for name in ('dashboard.db', 'encryption.key')):
                    self.backup(restart=False)
                else:
                    print('Current data is incomplete; retaining it beside data/ before recovery.', flush=True)
            self.compose('stop', 'dashboard')
            data = self.directory / 'data'
            if data.exists():
                data.rename(self.directory / f'data-before-restore-{stamp()}')
            shutil.move(str(extracted / 'data'), data)
            uid, gid = int(config['GATEWAY_UID']), int(config['GATEWAY_GID'])
            if os.geteuid() == 0:
                for root, dirs, files in os.walk(data):
                    os.chown(root, uid, gid)
                    for name in dirs + files:
                        os.chown(Path(root) / name, uid, gid)
            save_config(self.env_path, config)
            self.config = config
            self.start()
        print('Backup restored; pre-restore data retained beside data/.', flush=True)

    def update(self, requested, no_pull=False):
        candidate = dict(self.config, GATEWAY_IMAGE=requested or self.config['GATEWAY_IMAGE'])
        validate_config(candidate)
        # Pull failure cannot stop or change the current installation.
        if no_pull:
            subprocess.run(['docker', 'image', 'inspect', candidate['GATEWAY_IMAGE']],
                           check=True, stdout=subprocess.DEVNULL)
        else:
            self.compose('pull', config=candidate)
        archive = self.backup(restart=False)
        try:
            save_config(self.env_path, candidate)
            self.config = candidate
            self.start('never')
        except (subprocess.CalledProcessError, OSError):
            print('New version failed readiness; restoring pre-upgrade image and data.', file=sys.stderr)
            self.restore(archive, safety_backup=False)
            raise
        (self.directory / '.previous-backup').write_text(archive.name + '\n')
        print('Upgrade healthy. gateway.sh rollback restores the previous image and data.', flush=True)


def initialize(directory):
    directory.mkdir(parents=True, exist_ok=True)
    env_path = directory / '.env'
    if not env_path.exists():
        config = default_config()
        for key in config:
            if key in os.environ:
                config[key] = os.environ[key]
        save_config(env_path, config)
    config = load_config(env_path)
    data = directory / 'data'
    if data.is_symlink():
        raise ValueError('Refusing symlink: data')
    if not data.exists():
        data.mkdir(mode=0o700)
        if os.geteuid() == 0:
            os.chown(data, int(config['GATEWAY_UID']), int(config['GATEWAY_GID']))
    return config


def main():
    parser = argparse.ArgumentParser(description='AI Gateway deployment manager (Docker Compose v2, Python 3.10+)')
    parser.add_argument('--directory', type=Path, default=Path(__file__).resolve().parent)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('install', 'start', 'stop', 'restart', 'status', 'logs', 'backup', 'rollback'):
        commands.add_parser(name)
    update = commands.add_parser('update')
    update.add_argument('image', nargs='?')
    update.add_argument('--no-pull', action='store_true', help='Use an already loaded local image')
    commands.add_parser('restore').add_argument('archive', type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.command != 'install' and not (directory / '.env').exists():
        parser.error('Run gateway.sh install first')
    if args.command in ('logs', 'status'):
        app = Deployment(directory)
        if args.command == 'logs':
            app.compose('logs', '--tail', '100', '-f')
        else:
            app.compose('ps', '--all')
        return
    directory.mkdir(parents=True, exist_ok=True)
    os.umask(0o077)
    with (directory / '.gateway.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('Another deployment operation is running')
        if args.command == 'install':
            initialize(directory)
        app = Deployment(directory)
        if args.command == 'install':
            # Re-running install preserves the running version. Use update to upgrade.
            app.start()
            print(f"AI Gateway ready: http://{app.config['BIND_HOST']}:{app.config['SERVER_PORT']}\nComplete administrator setup on first access. Configuration: {app.env_path}")
        elif args.command == 'start':
            app.start()
        elif args.command == 'stop':
            app.compose('stop')
        elif args.command == 'restart':
            app.compose('stop')
            app.start('never')
        elif args.command == 'backup':
            app.backup()
        elif args.command == 'update':
            app.update(args.image, args.no_pull)
        elif args.command == 'restore':
            app.restore(args.archive.resolve())
        elif args.command == 'rollback':
            previous = (directory / '.previous-backup').read_text().strip()
            if Path(previous).name != previous:
                raise ValueError('Invalid previous backup pointer')
            app.restore(directory / 'backups' / previous)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError, tarfile.TarError) as error:
        print(f'Deployment failed: {error}', file=sys.stderr)
        sys.exit(1)
