import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def module(self):
        self.assertTrue((ROOT / 'gateway.py').exists(), 'deployment manager must exist')
        spec = importlib.util.spec_from_file_location('gateway', ROOT / 'gateway.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_configuration_roundtrip_never_executes_shell(self):
        module = self.module()
        config = module.default_config()
        module.save_config(self.directory / '.env', config)
        self.assertEqual(module.load_config(self.directory / '.env'), config)
        self.assertEqual((self.directory / '.env').stat().st_mode & 0o777, 0o600)
        marker = self.directory / 'executed'
        with (self.directory / '.env').open('a') as stream:
            stream.write(f'\nSERVER_PORT=$(touch {marker})\n')
        with self.assertRaises(ValueError):
            module.load_config(self.directory / '.env')
        self.assertFalse(marker.exists())

    def test_rejects_invalid_ports_and_image_injection(self):
        module = self.module()
        for key, value in [('SERVER_PORT','0'),('SERVER_PORT','65536'),('GATEWAY_IMAGE','image\nBAD=true'),('COMPOSE_PROJECT_NAME','../other'),('GATEWAY_UID','-1'),('DOMAIN','example.org { malicious }')]:
            with self.subTest(key=key, value=value):
                config = module.default_config()
                config[key] = value
                with self.assertRaises(ValueError):
                    module.validate_config(config)

    def test_archive_rejects_traversal_and_links_before_extraction(self):
        module = self.module()
        for name, symlink in [('../escape',False),('/tmp/escape',False),('data/link',True)]:
            archive = self.directory / 'bad.tar.gz'
            with tarfile.open(archive,'w:gz') as tar:
                item = tarfile.TarInfo(name)
                if symlink:
                    item.type = tarfile.SYMTYPE
                    item.linkname = '/tmp'
                tar.addfile(item, io.BytesIO())
            with self.assertRaises(ValueError):
                module.unpack_backup(archive, self.directory / 'extracted')
            self.assertFalse((self.directory / 'extracted').exists())

    def test_failed_backup_restarts_existing_container_without_recreating(self):
        module = self.module()
        app = module.Deployment.__new__(module.Deployment)
        app.directory = self.directory
        app.env_path = self.directory / '.env'
        app.config = module.default_config()
        module.save_config(app.env_path, app.config)
        calls = []
        app.compose = lambda *args, **kwargs: calls.append(args)
        app.container = lambda: {'State': {'Running': True, 'Health': {'Status': 'healthy'}}, 'Image': 'sha256:old'}
        with patch.object(module.subprocess, 'check_output', return_value='[{"RepoDigests":[]}]'):
            with self.assertRaises(ValueError):
                app.backup(restart=False)
        self.assertEqual(calls[0], ('stop', 'dashboard'))
        self.assertEqual(calls[1][0], 'start', 'backup failure must start the original container, never recreate from a moved tag')

    def test_worker_start_only_recreates_dashboard(self):
        module = self.module()
        app = module.Deployment.__new__(module.Deployment)
        app.dashboard_only = True
        calls = []
        app.compose = lambda *args, **kwargs: calls.append(args)
        app.start('never')
        self.assertEqual(calls[0][-2:], ('--no-deps', 'dashboard'))
        self.assertNotIn('--remove-orphans', calls[0])

    def test_unknown_command_has_no_filesystem_side_effect(self):
        result = subprocess.run(['bash',str(ROOT / 'gateway.sh'),'erase-everything'],cwd=self.directory,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(list(self.directory.iterdir()),[])

    def test_installer_uses_one_public_archive_without_github_api(self):
        binaries = self.directory / 'bin'
        binaries.mkdir()
        archive = self.directory / 'snapshot.tar.gz'
        revision = 'a' * 40
        names = ('compose.yaml', 'compose.https.yaml', 'compose.updates.yaml', 'Caddyfile', '.env.example', 'gateway.sh', 'gateway.py', 'README.md')
        with tarfile.open(archive, 'w:gz', format=tarfile.PAX_FORMAT, pax_headers={'comment': revision}) as tar:
            for name in names:
                content = b'# deployment fixture\n'
                if name == 'gateway.sh':
                    content = b'#!/usr/bin/env bash\nprintf installed > "$INSTALL_DIR/installed"\n'
                item = tarfile.TarInfo('public-snapshot/deploy/' + name)
                item.size = len(content)
                tar.addfile(item, io.BytesIO(content))
        curl = binaries / 'curl'
        curl.write_text('#!/usr/bin/env bash\ncase "$*" in *api.github.com*) exit 42 ;; esac\noutput=\'\'\nwhile [ "$#" -gt 0 ]; do\n  if [ "$1" = \'-o\' ]; then shift; output="$1"; fi\n  shift\ndone\ncp "$FIXTURE_ARCHIVE" "$output"\n')
        curl.chmod(0o755)
        docker = binaries / 'docker'
        docker.write_text('#!/bin/sh\nexit 0\n')
        docker.chmod(0o755)
        target = self.directory / 'installed'
        environment = dict(os.environ, PATH=str(binaries)+os.pathsep+os.environ['PATH'], FIXTURE_ARCHIVE=str(archive), INSTALL_DIR=str(target))
        environment.pop('GITHUB_TOKEN', None)
        result = subprocess.run(['bash', str(ROOT / 'install.sh')], env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((target / '.deployment-revision').read_text().strip(), revision)
        self.assertEqual((target / 'installed').read_text(), 'installed')

    def test_installer_download_failure_keeps_existing_installation(self):
        self.assertTrue((ROOT / 'install.sh').exists(), 'installer must exist')
        binaries = self.directory / 'bin'
        binaries.mkdir()
        curl = binaries / 'curl'
        curl.write_text('#!/bin/sh\nexit 22\n')
        curl.chmod(0o755)
        target = self.directory / 'installation'
        target.mkdir()
        (target / '.env').write_text('keep existing secrets\n')
        (target / 'gateway.py').write_text('existing manager\n')
        environment = dict(os.environ,PATH=str(binaries)+os.pathsep+os.environ['PATH'],INSTALL_DIR=str(target),GITHUB_TOKEN='test_private_token')
        result = subprocess.run(['bash',str(ROOT / 'install.sh')],env=environment,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual((target / '.env').read_text(),'keep existing secrets\n')
        self.assertEqual((target / 'gateway.py').read_text(),'existing manager\n')
        self.assertNotIn('test_private_token', result.stdout+result.stderr)

if __name__ == '__main__':
    unittest.main()
