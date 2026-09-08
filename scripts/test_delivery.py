"""Real packaged filler and MCP stdio delivery; all identities and secrets are synthetic."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

import collection
import package_skill
import test_collection as fixtures


class DeliveryTests(unittest.TestCase):
    def test_deterministic_package_and_independent_filler(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one, two = root / 'one.zip', root / 'two.zip'
            package_skill.build_package(one)
            package_skill.build_package(two)
            self.assertEqual(one.read_bytes(), two.read_bytes())
            extracted = root / 'unpacked'
            with zipfile.ZipFile(one) as archive:
                names = archive.namelist()
                self.assertTrue(all(name.startswith('yintian-skill/') for name in names))
                self.assertFalse(any('__pycache__' in name or '/test_' in name or
                                     name.endswith(('.yintian-vault', '.yintian-credential', '.yintian', '.sqlite3'))
                                     for name in names))
                archive.extractall(extracted)
            filler = root / 'independent'
            shutil.move(extracted / 'yintian-skill/yintian-fill', filler)
            shutil.rmtree(extracted)
            env = os.environ.copy()
            env.pop('PYTHONPATH', None)
            env['PYTHONNOUSERSITE'] = '1'
            env['PYTHONDONTWRITEBYTECODE'] = '1'
            ns = fixtures.make_group_scenario(root)
            inspected = subprocess.run([sys.executable, str(filler / 'scripts/fill.py'), 'inspect',
                                        str(ns.task_dir / 'FORM.yintian-form'), '--json'],
                                       cwd=filler, env=env, capture_output=True, text=True, check=True, timeout=20)
            self.assertEqual(json.loads(inspected.stdout)['task_id'], ns.task_id)
            script = '''
import json, sys
sys.path.insert(0, sys.argv[1])
import fill, vault
data = json.load(sys.stdin)
form = fill.bind_credential(fill.load_form(data['form']), data['credential'])
profile = {'version': 1, 'types': {f['id']: f['type'] for f in form['fields']},
           'values': data['values'], 'attachments': {}}
vault.save_vault(data['vault'], 'Synthetic-package-passphrase', profile, create=True)
values, attachments, missing, matches = vault.select_fields(form, vault.load_vault(data['vault'], 'Synthetic-package-passphrase'))
assert not missing
fill.seal_data(form, values, attachments, data['out'], confirmed=True)
'''
            data = {'form': str(ns.task_dir / 'FORM.yintian-form'),
                    'credential': str(ns.task_dir / 'credentials/GRP-E001.yintian-credential'),
                    'values': ns.values, 'vault': str(root / 'synthetic.yintian-vault'),
                    'out': str(ns.incoming / 'reply.yintian')}
            subprocess.run([sys.executable, '-c', script, str(filler / 'scripts')], input=json.dumps(data),
                           cwd=filler, env=env, capture_output=True, text=True, check=True, timeout=20)
            self.assertEqual(collection.ingest_task(ns.task_dir, ns.incoming)['accepted'], 1)
            self.assertEqual(collection.review_task(ns.task_dir, fixtures.PASSWORD)['verified'], 1)

    def test_real_mcp_stdio_and_private_interface_boundary(self):
        asyncio.run(self._mcp_roundtrip())

    async def _mcp_roundtrip(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ns = fixtures.make_group_scenario(root)
            fixtures.submit_group(ns, 'E001', 'one.yintian')
            env = os.environ.copy()
            env['YINTIAN_VAULT_DIR'] = str(root)
            env['PYTHONDONTWRITEBYTECODE'] = '1'
            parameters = StdioServerParameters(command=sys.executable,
                                               args=[str(Path(__file__).with_name('mcp_server.py'))], env=env)
            async with asyncio.timeout(30):
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listing = await session.list_tools()
                        self.assertEqual({tool.name for tool in listing.tools}, {
                            'openvino_status', 'ingest_encrypted_submissions', 'collection_status',
                            'generate_redacted_report', 'verify_invites', 'list_pending_reminders', 'cleanup_temp_files'})
                        async def call(name, args):
                            result = await session.call_tool(name, args)
                            self.assertFalse(result.isError, str(result))
                            text = '\n'.join(item.text for item in result.content if item.type == 'text')
                            self.assertNotIn(ns.values['phone'], text)
                            self.assertNotIn(fixtures.PASSWORD, text)
                            self.assertNotIn('token_hash', text)
                            return json.loads(text)
                        capability = await call('openvino_status', {})
                        self.assertFalse(capability['required'])
                        self.assertIsInstance(capability['available'], bool)
                        accepted = await call('ingest_encrypted_submissions', {'task_dir': str(ns.task_dir), 'submissions_dir': str(ns.incoming)})
                        self.assertEqual(accepted['accepted'], 1)
                        state = await call('collection_status', {'task_dir': str(ns.task_dir)})
                        self.assertEqual(state['pending_count'], 1)
                        report = await call('generate_redacted_report', {'task_dir': str(ns.task_dir), 'formats': ['json', 'xlsx']})
                        self.assertTrue((root / report['xlsx']).is_file())
                        self.assertTrue((await call('verify_invites', {'task_dir': str(ns.task_dir)}))['all_valid'])
                        rejected = await session.call_tool('collection_status', {'task_dir': str(root.parent)})
                        self.assertTrue(rejected.isError)


if __name__ == '__main__':
    unittest.main(verbosity=2)
