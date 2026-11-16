import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from repairbot.demo import DemoApplication, DemoError, DemoServer
from repairbot.__main__ import main


class DemoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'demo.sqlite3'
        self.app = DemoApplication(self.path)
        self.server = DemoServer(('127.0.0.1', 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.app.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            client.request(method, path, body, headers or {})
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            client.close()

    def test_local_core_roundtrip_persists_and_separates_actors(self):
        with patch('urllib.request.urlopen', side_effect=AssertionError('No Telegram calls')):
            status, _, raw = self.request('POST', '/api/update', json.dumps({'actor':'client','text':'/new'}), {'Content-Type':'application/json'})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)['mode'], 'local')
        self.assertEqual(self.app.service.db.execute('SELECT step FROM sessions').fetchone()[0], 'device')
        self.assertTrue(self.app.history('client')['messages'])
        self.assertEqual(self.app.history('other')['messages'], [])
        self.assertEqual(self.app.service.pending_outbox(), [])
        self.assertEqual(self.request('GET', '/api/history?actor=other')[0], 200)

    def test_host_origin_content_type_and_static_path_guards(self):
        self.assertEqual(self.request('GET','/api/session',headers={'Host':'evil.example'})[0],403)
        payload = json.dumps({'actor':'client','text':'/new'})
        self.assertEqual(self.request('POST','/api/update',payload,{'Content-Type':'application/json','Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request('POST','/api/update',payload,{'Content-Type':'text/plain'})[0],415)
        self.assertEqual(self.request('GET','/../repairbot/core.py')[0],404)
        self.assertEqual(self.request('GET','/api/not-found')[0],404)
        status, headers, body = self.request('GET','/')
        self.assertEqual(status,200)
        self.assertIn('frame-ancestors',headers['Content-Security-Policy'])
        self.assertIn('локальная',body.decode())

    def test_malformed_payloads_are_400_and_do_not_create_drafts(self):
        for payload in ([], {'actor':[],'text':'/new'}, {'actor':'staff','text':'/new','photo':True}, {'actor':'client','photo':False}, {'actor':'client','text':''}):
            status, _, _ = self.request('POST','/api/update',json.dumps(payload),{'Content-Type':'application/json'})
            self.assertEqual(status,400)
        self.assertEqual(self.request('POST','/api/update','{',{'Content-Type':'application/json'})[0],400)
        self.assertEqual(self.app.service.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],0)

    def test_demo_transcript_survives_restart(self):
        self.app.update({'actor':'client','text':'/new'})
        self.app.update({'actor':'client','text':'Учебное устройство'})
        second = DemoApplication(self.path)
        try:
            self.assertTrue(second.history('client')['messages'])
            self.assertEqual(second.service.db.execute('SELECT step FROM sessions').fetchone()[0],'description')
        finally:
            second.close()


class EntrypointTest(unittest.TestCase):
    def test_default_requires_subcommand_and_network_is_explicit(self):
        for args in ([], ['telegram'], ['demo','--port','0']):
            with self.assertRaises(SystemExit):
                main(args)

    def test_configuration_check_is_offline_and_hides_values(self):
        with patch.dict('os.environ',{'BOT_TOKEN':'12345:'+'x'*30,'STAFF_IDS':'9901'},clear=True), patch('urllib.request.urlopen',side_effect=AssertionError('No network')):
            self.assertEqual(main(['check-config']),0)

    def test_existing_webhook_stops_without_deleting_it(self):
        from repairbot.telegram import TelegramClient
        calls = []
        def request(_client, method, params):
            calls.append(method)
            return {'is_bot':True} if method == 'getMe' else {'url':'https://demo.invalid/webhook'}
        with patch.dict('os.environ',{'BOT_TOKEN':'12345:'+'x'*30,'STAFF_IDS':'9901'},clear=True), patch.object(TelegramClient,'request',request):
            self.assertEqual(main(['telegram','--allow-network']),2)
        self.assertEqual(calls,['getMe','getWebhookInfo'])

    def test_telegram_rejects_demo_database(self):
        from repairbot.telegram import TelegramClient
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/'demo.sqlite3')
            app = DemoApplication(path)
            app.close()
            with patch.dict('os.environ',{'BOT_TOKEN':'12345:'+'x'*30,'STAFF_IDS':'9901','DB_PATH':path},clear=True), patch.object(TelegramClient,'request',side_effect=[{'is_bot':True},{'url':''}]):
                self.assertEqual(main(['telegram','--allow-network']),2)

if __name__ == '__main__': unittest.main()
