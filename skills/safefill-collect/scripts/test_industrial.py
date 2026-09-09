"""Product invariants: real ciphertext/SQLite/XLSX, synthetic identities only."""
import contextlib
import io
from pathlib import Path
import tempfile
import types
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import collection
import test_collection as fixtures
import secure_io
import distribute
import cleanup
import time


class IndustrialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_cleanup_respects_active_task_lock(self):
        task = self.root / 'task'
        task.mkdir()
        (task / 'task.json').write_text('{}')
        orphan = task / ('.yintian-tmp-' + 'a' * 24)
        orphan.write_bytes(b'partial')
        os.utime(orphan, (time.time() - 172800,) * 2)
        with secure_io.file_lock(task / '.write.lock'):
            result = cleanup.run_cleanup([str(task)], vault_dir='', apply=True)
            self.assertTrue(orphan.exists())
            self.assertTrue(any('TASK_BUSY' in item['reason'] for item in result['skipped_recheck']))
        cleanup.run_cleanup([str(task)], vault_dir='', apply=True)
        self.assertFalse(orphan.exists())
        self.assertTrue((task / '.write.lock').exists())

    def test_cleanup_parent_symlink_swap_cannot_delete_outside_file(self):
        task = self.root / 'task'
        task.mkdir()
        orphan = task / 'tmpab12_cd3'
        orphan.write_bytes(b'partial')
        os.utime(orphan, (time.time() - 172800,) * 2)
        result = cleanup.run_cleanup([str(task)], vault_dir='')
        task.rename(self.root / 'moved')
        outside = self.root / 'outside'
        outside.mkdir()
        target = outside / orphan.name
        target.write_bytes(b'keep')
        task.symlink_to(outside, target_is_directory=True)
        cleanup.apply_deletions(result, 86400)
        self.assertEqual(target.read_bytes(), b'keep')
        self.assertTrue(result['skipped_recheck'])

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX FIFO only')
    def test_nonregular_input_does_not_block(self):
        path = self.root / 'input'
        os.mkfifo(path)
        with self.assertRaisesRegex(ValueError, 'FILE_LIMIT'):
            secure_io.read_bytes(path)

    def test_distribution_path_and_csv_formula_safety(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.root / 'link').symlink_to(outside, target_is_directory=True)
        messages = [{'employee_id': '=1+1', 'name': '+cmd', 'invite_id': 'INV-AAAAAAAAAA',
                     'invite_path': 'INV-AAAAAAAAAA.yintian-form', 'message': 'test'}]
        with self.assertRaisesRegex(RuntimeError, 'PATH_UNSAFE'):
            distribute.export_messages_csv(messages, str(self.root / 'link' / 'out.csv'))
        self.assertFalse((outside / 'out.csv').exists())
        out = self.root / 'safe.csv'
        distribute.export_messages_csv(messages, str(out))
        self.assertIn("'=1+1", out.read_text(encoding='utf-8-sig'))
        ns = fixtures.make_group_scenario(self.root)
        index = ns.task_dir / 'invite-index.csv'
        index.write_text(index.read_text(encoding='utf-8-sig').replace('FORM.yintian-form', '../outside'))
        with self.assertRaisesRegex(ValueError, 'INDEX_INVALID'):
            distribute.generate_messages(ns.task_dir)

    def test_export_excludes_unapproved_and_unconsented(self):
        ns = fixtures.make_group_scenario(self.root)
        fixtures.submit_group(ns, 'E001', 'bad.yintian',
                              values=dict(ns.values, name='合成错误姓名', phone='invalid'),
                              overrides={'consent_confirmed': False})
        collection.ingest_task(ns.task_dir, ns.incoming)
        self.assertEqual(collection.review_task(ns.task_dir, fixtures.PASSWORD)['needs_review'], 1)
        task = collection.load_json(ns.task_dir / 'task.json')
        key = collection.unlock_private_key(ns.task_dir, fixtures.PASSWORD)
        self.assertEqual(collection.collect_clear_rows(ns.task_dir, task, key, ['phone'], {}), [])

    def test_latest_pending_is_counted(self):
        ns = fixtures.make_group_scenario(self.root)
        fixtures.submit_group(ns, 'E001', 'v1.yintian')
        collection.ingest_task(ns.task_dir, ns.incoming)
        collection.review_task(ns.task_dir, fixtures.PASSWORD)
        fixtures.submit_group(ns, 'E001', 'v2.yintian', values=dict(ns.values, phone='13800138001'))
        collection.ingest_task(ns.task_dir, ns.incoming)
        status = collection.status_task(ns.task_dir)
        self.assertEqual(status.get('pending_count'), 1)
        row = collection.report_rows(ns.task_dir)[0]
        self.assertEqual(row.get('latest_version'), 2)
        self.assertEqual(row.get('approved_version'), 1)

    def test_ocr_partial_and_ambiguous_are_not_verified(self):
        fields = collection.default_config()['fields']
        values = {'name': '张三', 'id_number': fixtures.VALID_ID, 'address': '北京市朝阳区'}
        for text in ['姓名 张三\n', '姓名 张三\n姓名 李四\n']:
            with self.subTest(text=text), patch.object(collection, 'ocr_attachment', return_value=(text, [])):
                self.assertTrue(collection.compare_ocr({'values': values}, [{'field_id': 'id_front'}], fields))

    def test_actual_export_path_rejects_symlink(self):
        ns = fixtures.make_group_scenario(self.root)
        task = collection.load_json(ns.task_dir / 'task.json')
        target = self.root / 'private-target.xlsx'
        target.write_bytes(b'keep')
        (self.root / 'export.xlsx').symlink_to(target)
        with self.assertRaises((ValueError, RuntimeError, OSError)):
            collection.write_clear_export(ns.task_dir, task, [], ['phone'], {},
                                          self.root / 'export.json', ['xlsx'], '合成测试', '合成接收方')
        self.assertEqual(target.read_bytes(), b'keep')

    def test_progress_path_rejects_symlink(self):
        ns = fixtures.make_group_scenario(self.root)
        target = self.root / 'private-target.xlsx'
        target.write_bytes(b'keep')
        (ns.task_dir / 'reports/progress.xlsx').symlink_to(target)
        with self.assertRaises((ValueError, RuntimeError, OSError)):
            collection.write_reports(ns.task_dir, ['xlsx'])
        self.assertEqual(target.read_bytes(), b'keep')

    def test_group_missing_or_transplanted_auth_rejected_before_storage(self):
        ns = fixtures.make_group_scenario(self.root)
        envelope = fixtures.make_group_envelope(ns.task_dir, 'E001', ns.values)
        del envelope['auth_tag']
        collection.dump_json(ns.incoming / 'missing.yintian', envelope)
        envelope = fixtures.make_group_envelope(ns.task_dir, 'E001', ns.values)
        envelope['invite_id'] = 'GRP-E002'
        collection.dump_json(ns.incoming / 'transplanted.yintian', envelope)
        result = collection.ingest_task(ns.task_dir, ns.incoming)
        self.assertEqual(result['rejected'], 2)
        with collection.connect_db(ns.task_dir) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM submissions').fetchone()[0], 0)

    def test_old_group_remains_readonly(self):
        ns = fixtures.make_group_scenario(self.root)
        task = collection.load_json(ns.task_dir / 'task.json')
        task['format_version'] = collection.FORMAT_VERSION
        task.pop('submission_auth')
        collection.dump_json(ns.task_dir / 'task.json', task)
        self.assertEqual(collection.status_task(ns.task_dir)['total'], 2)
        with self.assertRaisesRegex(RuntimeError, 'GROUP_AUTH_REQUIRED'):
            collection.ingest_task(ns.task_dir, ns.incoming)

    def test_group_instructions_and_private_credential(self):
        ns = fixtures.make_group_scenario(self.root)
        message = distribute.generate_messages(ns.task_dir)[0]
        self.assertEqual(message['credential_delivery'], 'private')
        self.assertNotIn('Chrome', message['message'])
        self.assertNotIn('invite_token', json.dumps(message))
        self.assertTrue(distribute.verify_invites(ns.task_dir)['all_valid'])

    def test_ingest_sql_failure_rolls_back_file_and_row(self):
        ns = fixtures.make_group_scenario(self.root)
        fixtures.submit_group(ns, 'E001', 'one.yintian')
        with patch.object(collection, 'audit', side_effect=RuntimeError('synthetic transaction failure')):
            self.assertEqual(collection.ingest_task(ns.task_dir, ns.incoming)['rejected'], 1)
        with collection.connect_db(ns.task_dir) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM submissions').fetchone()[0], 0)
        self.assertEqual(list((ns.task_dir / 'submissions').glob('*/*.yintian')), [])
        self.assertEqual(collection.ingest_task(ns.task_dir, ns.incoming)['accepted'], 1)
        self.assertEqual(collection.ingest_task(ns.task_dir, ns.incoming)['duplicates'], 1)

    def test_operating_system_lock_released_after_process_death(self):
        lock = self.root / '.write.lock'
        env = dict(os.environ, PYTHONPATH=str(Path(collection.__file__).parent))
        code = 'import secure_io,sys,time\nwith secure_io.file_lock(sys.argv[1]):\n print("ready",flush=True)\n time.sleep(30)'
        process = subprocess.Popen([sys.executable, '-c', code, str(lock)], env=env, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            with self.assertRaises((RuntimeError, OSError)):
                with secure_io.file_lock(lock):
                    self.fail('second writer acquired lock')
        finally:
            process.kill()
            process.wait(timeout=5)
            process.stdout.close()
        with secure_io.file_lock(lock):
            self.assertTrue(lock.is_file())

    def test_migration_is_explicit_and_failure_rolls_back(self):
        ns = fixtures.make_scenario(self.root)
        with collection.connect_db(ns.task_dir) as db:
            db.execute('DROP TABLE audit')
            db.execute('PRAGMA user_version=0')
        self.assertTrue(collection.status_task(ns.task_dir)['migration_required'])
        with self.assertRaisesRegex(RuntimeError, 'MIGRATION_REQUIRED'):
            collection.ingest_task(ns.task_dir, ns.incoming)
        args = types.SimpleNamespace(task_dir=ns.task_dir)
        with patch.object(collection, 'audit', side_effect=RuntimeError('rollback')):
            with self.assertRaises(RuntimeError):
                collection.cmd_migrate(args)
        with collection.connect_db(ns.task_dir) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='audit'").fetchone())
        self.assertTrue((ns.task_dir / 'migration-backup.sqlite3').is_file())
        self.assertTrue(collection.cmd_migrate(args)['changed'])

    def test_retry_after_io_recovery_and_preserve_approval(self):
        ns = fixtures.make_group_scenario(self.root)
        fixtures.submit_group(ns, 'E001', 'v1.yintian')
        collection.ingest_task(ns.task_dir, ns.incoming)
        collection.review_task(ns.task_dir, fixtures.PASSWORD)
        fixtures.submit_group(ns, 'E001', 'v2.yintian')
        collection.ingest_task(ns.task_dir, ns.incoming)
        with patch.object(collection, 'stored_payload', side_effect=OSError('synthetic')):
            self.assertEqual(collection.review_task(ns.task_dir, fixtures.PASSWORD)['needs_review'], 1)
        row = collection.report_rows(ns.task_dir)[0]
        self.assertTrue(row['retryable'])
        self.assertEqual(row['approved_version'], 1)
        self.assertEqual(collection.review_task(ns.task_dir, fixtures.PASSWORD, True, 'GRP-E001')['verified'], 1)
        self.assertEqual(collection.report_rows(ns.task_dir)[0]['approved_version'], 2)

    def test_export_confirmation_rechecks_state(self):
        ns = fixtures.make_group_scenario(self.root)
        fixtures.submit_group(ns, 'E001', 'v1.yintian')
        collection.ingest_task(ns.task_dir, ns.incoming)
        collection.review_task(ns.task_dir, fixtures.PASSWORD)
        out = self.root / 'result.xlsx'
        def confirm(prompt):
            fixtures.submit_group(ns, 'E001', 'v2.yintian')
            collection.ingest_task(ns.task_dir, ns.incoming)
            return ns.task_id
        with fixtures.fake_tty(input_func=confirm, getpass_func=lambda _: fixtures.PASSWORD):
            with self.assertRaisesRegex(RuntimeError, 'STATE_CHANGED'):
                collection.cmd_export_clear(fixtures.export_clear_args(ns, out))
        self.assertFalse(out.exists())

    def test_group_handoff_includes_forms_credentials_and_consistent_state(self):
        ns = fixtures.make_group_scenario(self.root)
        fixtures.submit_group(ns, 'E001', 'one.yintian')
        collection.ingest_task(ns.task_dir, ns.incoming)
        bundle = self.root / 'handoff.yintian-task'
        collection.export_task(ns.task_dir, bundle, fixtures.HANDOFF_PASSWORD)
        imported = collection.import_task(bundle, self.root / 'new', fixtures.HANDOFF_PASSWORD)
        target = Path(imported['task_dir'])
        self.assertTrue((target / 'FORM.yintian-form').is_file())
        self.assertTrue((target / 'credentials/GRP-E001.yintian-credential').is_file())
        self.assertEqual(collection.status_task(target)['pending_count'], 1)
        self.assertEqual(collection.review_task(target, fixtures.PASSWORD)['verified'], 1)

    def test_internal_submissions_symlink_rejected(self):
        ns = fixtures.make_group_scenario(self.root)
        (ns.task_dir / 'submissions').rmdir()
        external = self.root / 'external'; external.mkdir()
        (ns.task_dir / 'submissions').symlink_to(external, target_is_directory=True)
        fixtures.submit_group(ns, 'E001', 'one.yintian')
        self.assertEqual(collection.ingest_task(ns.task_dir, ns.incoming)['rejected'], 1)
        self.assertEqual(list(external.iterdir()), [])

    def test_manual_confirmation_only_ocr_and_not_overwritten_by_retry(self):
        ns = fixtures.make_scenario(self.root)
        ns.values['effective_date'] = '2024-02-29'
        fixtures.submit_accepted(ns, attachments=fixtures.valid_test_attachments())
        with patch.object(collection, 'ocr_attachment', return_value=('姓名 张三\n', [])):
            self.assertEqual(collection.review_task(ns.task_dir, fixtures.PASSWORD)['needs_review'], 1)
        args = types.SimpleNamespace(task_dir=ns.task_dir, invite_id=ns.invite_id, version=1, action='confirm', operator='hr-test')
        with fixtures.fake_tty(getpass_func=lambda _: fixtures.PASSWORD), patch('review_evidence.confirm_evidence', return_value=True):
            self.assertEqual(collection.cmd_decide(args)['status'], 'verified_manual')
        self.assertEqual(collection.review_task(ns.task_dir, fixtures.PASSWORD, True)['verified'], 0)
        self.assertEqual(collection.report_rows(ns.task_dir)[0]['approval_method'], 'manual')
        with collection.connect_db(ns.task_dir) as db:
            reasons = [row[0] for row in db.execute("SELECT reason FROM audit WHERE action='manual_confirm'")]
        self.assertEqual(set(reasons), {'ocr:missing:id_number', 'ocr:missing:address'})

    def test_manual_cannot_override_consent_or_input_errors(self):
        ns = fixtures.make_group_scenario(self.root)
        fixtures.submit_group(ns, 'E001', 'bad.yintian', overrides={'consent_confirmed': False})
        collection.ingest_task(ns.task_dir, ns.incoming)
        collection.review_task(ns.task_dir, fixtures.PASSWORD)
        args = types.SimpleNamespace(task_dir=ns.task_dir, invite_id='GRP-E001', version=1, action='confirm', operator='hr-test')
        with fixtures.fake_tty(getpass_func=lambda _: fixtures.PASSWORD), patch('review_evidence.confirm_evidence') as viewer:
            with self.assertRaisesRegex(RuntimeError, 'MANUAL_NOT_ALLOWED'):
                collection.cmd_decide(args)
            viewer.assert_not_called()
        args.action = 'return'
        with fixtures.fake_tty(getpass_func=lambda _: fixtures.PASSWORD, input_func=lambda _: ns.task_id):
            self.assertEqual(collection.cmd_decide(args)['status'], 'returned')
        self.assertEqual(collection.review_task(ns.task_dir, fixtures.PASSWORD, True)['needs_review'], 0)

    def test_manual_state_change_cannot_override_new_submission(self):
        ns = fixtures.make_scenario(self.root)
        ns.values['effective_date'] = '2024-02-29'
        fixtures.submit_accepted(ns, attachments=fixtures.valid_test_attachments())
        with patch.object(collection, 'ocr_attachment', return_value=('姓名 张三\n', [])):
            collection.review_task(ns.task_dir, fixtures.PASSWORD)
        def viewed(*args):
            fixtures.submit_accepted(ns, 'two.yintian', attachments=fixtures.valid_test_attachments())
            return True
        args = types.SimpleNamespace(task_dir=ns.task_dir, invite_id=ns.invite_id, version=1, action='confirm', operator='hr-test')
        with fixtures.fake_tty(getpass_func=lambda _: fixtures.PASSWORD), patch('review_evidence.confirm_evidence', side_effect=viewed):
            with self.assertRaisesRegex(RuntimeError, 'STATE_CHANGED'):
                collection.cmd_decide(args)
        self.assertEqual(collection.report_rows(ns.task_dir)[0]['status'], 'submitted')

    def test_group_init_config_is_usable_after_business_fields_filled(self):
        path = self.root / 'config.json'
        collection.cmd_init_config(types.SimpleNamespace(out=path, force=False, mode='group'))
        config = collection.load_json(path)
        config.update(purpose='合成业务测试', contact='合成人事', correction='联系合成人事更正',
                      deadline='2098-01-01', retention_until='2099-01-01')
        self.assertIn('employee_id', [field['id'] for field in collection.validate_config(config, mode='group')['fields']])


if __name__ == '__main__':
    unittest.main(verbosity=2)
