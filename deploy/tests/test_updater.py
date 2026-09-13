import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def module(self):
        self.assertTrue((ROOT / 'updater.py').exists(), 'independent updater is not implemented')
        spec = importlib.util.spec_from_file_location('updater', ROOT / 'updater.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def worker(self, module):
        with patch.object(module.Worker, 'refresh_current'):
            return module.Worker(self.directory)

    def test_registry_rejects_digest_and_platform_mismatch(self):
        module = self.module()
        with self.assertRaises(ValueError):
            module.validate_digest('sha256:bad; touch /tmp/executed')
        with self.assertRaises(ValueError):
            module.select_manifest({'manifests': [{'platform': {'os':'linux','architecture':'arm64'},'digest':'sha256:'+'a'*64}]}, 'amd64')

    def test_latest_detection_supports_classic_and_containerd_image_ids(self):
        module = self.module()
        worker = self.worker(module)
        worker.latest = {'digest':'sha256:'+'a'*64, 'platform_digest':'sha256:'+'b'*64,
                         'image_id':'sha256:'+'c'*64, 'commit':'new', 'created':'now'}
        worker.checked_at = time.monotonic()
        for field in ('digest', 'platform_digest', 'image_id'):
            worker.image_id = worker.latest[field]
            with self.subTest(store_id=field):
                self.assertFalse(worker.snapshot()['has_update'])
                with self.assertRaisesRegex(ValueError, '最新版本'):
                    worker.submit('update', {'request_id':'00000000-0000-4000-8000-000000000001',
                                             'digest':worker.latest['digest']})
        worker.image_id = 'sha256:'+'d'*64
        self.assertTrue(worker.snapshot()['has_update'])

    def test_target_requires_fresh_check_and_cannot_select_arbitrary_image(self):
        module = self.module()
        worker = self.worker(module)
        with self.assertRaises(ValueError):
            worker.submit('update', {'request_id':'00000000-0000-4000-8000-000000000001','digest':'sha256:'+'a'*64})
        worker.latest = {'digest':'sha256:'+'b'*64,'image_id':'sha256:'+'c'*64,'commit':'new','created':'now'}
        worker.checked_at = time.monotonic()
        with self.assertRaises(ValueError):
            worker.submit('update', {'request_id':'00000000-0000-4000-8000-000000000001','digest':'other/image:latest'})
        with self.assertRaises(ValueError):
            worker.submit('update', {'request_id':'00000000-0000-4000-8000-000000000001','digest':worker.latest['digest'],'command':'sh'})
        self.assertIsNone(worker.snapshot()['job'])

    def test_duplicate_request_runs_once_and_competing_operation_is_busy(self):
        module = self.module()
        worker = self.worker(module)
        worker.latest = {'digest':'sha256:'+'b'*64,'image_id':'sha256:'+'c'*64,'commit':'new','created':'now'}
        worker.checked_at = time.monotonic()
        started, release = threading.Event(), threading.Event()
        calls = []
        def execute(job):
            calls.append(job['id'])
            started.set()
            release.wait(5)
        request = {'request_id':'00000000-0000-4000-8000-000000000001','digest':worker.latest['digest']}
        with patch.object(worker, 'execute', side_effect=execute), patch.object(worker, 'refresh_current'):
            first = worker.submit('update', request)
            self.assertTrue(started.wait(3))
            self.assertEqual(worker.submit('update', request)['id'], first['id'])
            with self.assertRaises(module.Busy):
                worker.submit('update', dict(request, request_id='00000000-0000-4000-8000-000000000002'))
            release.set()
            worker.thread.join(5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(worker.snapshot()['job']['status'], 'succeeded')
        self.assertEqual(worker.submit('update', request)['id'], first['id'])
        with self.assertRaises(ValueError):
            worker.submit('rollback', {'request_id':request['request_id']})

    def test_state_survives_restart_and_interrupted_job_is_not_replayed(self):
        module = self.module()
        worker = self.worker(module)
        worker.job = {'id':'00000000-0000-4000-8000-000000000001','action':'update','status':'running','stage':'starting','message':'启动','started_at':'now','finished_at':None}
        worker.persist()
        recovered = self.worker(module)
        self.assertEqual(recovered.snapshot()['job']['status'], 'interrupted')
        self.assertEqual(json.loads((self.directory / '.updates/state.json').read_text())['status'], 'interrupted')
        self.assertEqual((self.directory / '.updates/state.json').stat().st_mode & 0o777, 0o600)

    def test_cli_operation_lock_is_respected_before_accepting_job(self):
        module = self.module()
        worker = self.worker(module)
        worker.latest = {'digest':'sha256:'+'b'*64,'image_id':'sha256:'+'c'*64,'commit':'new','created':'now'}
        worker.checked_at = time.monotonic()
        import fcntl
        with (self.directory / '.gateway.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(module.Busy):
                worker.submit('update', {'request_id':'00000000-0000-4000-8000-000000000001','digest':worker.latest['digest']})
        self.assertIsNone(worker.snapshot()['job'])
