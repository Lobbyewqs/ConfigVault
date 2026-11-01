import base64
import json
import os
import subprocess
import urllib.error
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import configvault as cv

class FakeApi:
    def __init__(self, content=b'cloud\r\n', head=2):
        self.data, self.head = content, head
        self.requests = []
    def repo(self, state): return {'headVersionId': self.head}
    def content(self, state, version): return self.data
    def push(self, state, pending):
        self.requests.append(dict(pending))
        return {'id': 2, 'sha256': cv.sha(base64.b64decode(pending['content'])), 'noChange': False}

class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2])
        self.dir=Path(self.temp.name); self.file=self.dir/'sample.cfg'; self.file.write_bytes(b'base\r\n')
        self.sp=self.dir/'state.json'; self.state={'server':'http://127.0.0.1:8080','repoId':1,'file':str(self.file),'baseVersionId':1,'baseContentHash':cv.sha(self.file.read_bytes())}
        cv.atomic_json(self.sp,self.state); self.api=FakeApi()
    def tearDown(self): self.temp.cleanup()
    def test_apply_preserves_bytes_and_backup(self):
        data=b'\xef\xbb\xbf// \xe6\xb3\xa8\xe9\x87\x8a\r\nbind "x" "a;b"\r\nbind "x" z\n'
        self.api.data=data; cv.preview(self.sp,self.state,self.api); backup=cv.apply(self.sp,self.state,self.api)
        self.assertEqual(self.file.read_bytes(),data); self.assertEqual(Path(backup).read_bytes(),b'base\r\n')
        self.assertEqual(cv.read_state(self.sp)['baseContentHash'],cv.sha(data))
    def test_local_edit_after_preview_stops(self):
        cv.preview(self.sp,self.state,self.api); self.file.write_bytes(b'user edits')
        with self.assertRaises(cv.VaultError): cv.apply(self.sp,self.state,self.api)
        self.assertEqual(self.file.read_bytes(),b'user edits')
    def test_head_change_after_preview_stops(self):
        cv.preview(self.sp,self.state,self.api); self.api.head=3
        with self.assertRaises(cv.VaultError): cv.apply(self.sp,self.state,self.api)
        self.assertEqual(self.file.read_bytes(),b'base\r\n')
    def test_conflict_requires_explicit_accept(self):
        self.file.write_bytes(b'mine'); cv.preview(self.sp,self.state,self.api)
        with self.assertRaises(cv.VaultError): cv.apply(self.sp,self.state,self.api)
        self.assertEqual(self.file.read_bytes(),b'mine'); cv.apply(self.sp,self.state,self.api,True)
        self.assertEqual(self.file.read_bytes(),self.api.data)
    def test_backup_failure_does_not_replace(self):
        cv.preview(self.sp,self.state,self.api)
        with mock.patch.object(Path,'mkdir',side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError): cv.apply(self.sp,self.state,self.api)
        self.assertEqual(self.file.read_bytes(),b'base\r\n')
    def test_recover_after_replace_before_state_write(self):
        cv.preview(self.sp,self.state,self.api)
        real=cv.atomic_json; calls=[]
        def crash_final(path,obj):
            calls.append(1)
            if len(calls)==2: raise OSError('simulated interruption')
            real(path,obj)
        with mock.patch.object(cv,'atomic_json',side_effect=crash_final):
            with self.assertRaises(OSError): cv.apply(self.sp,self.state,self.api)
        saved=cv.read_state(self.sp); self.assertIn('applyJournal',saved)
        recovered=cv.recover(self.sp,saved)
        self.assertEqual(recovered['baseVersionId'],2);self.assertEqual(recovered['baseContentHash'],cv.sha(self.api.data))
    def test_push_retry_reuses_bytes_and_key(self):
        class Flaky(FakeApi):
            def push(self,state,pending):
                self.requests.append(dict(pending))
                if len(self.requests)==1: raise cv.VaultError('connection lost')
                return {'id':2,'sha256':cv.sha(base64.b64decode(pending['content']))}
        api=Flaky();
        with self.assertRaises(cv.VaultError):cv.do_push(self.sp,self.state,api,'first','utf-8')
        self.file.write_bytes(b'new local edit')
        cv.do_push(self.sp,cv.read_state(self.sp),api,'different','gb18030')
        self.assertEqual(api.requests[0],api.requests[1]);self.assertEqual(self.file.read_bytes(),b'new local edit')
        self.assertEqual(cv.read_state(self.sp)['baseContentHash'],cv.sha(b'base\r\n'))

    @unittest.skipUnless(os.name=='nt','Windows-only sharing semantics')
    def test_windows_locked_file_stops_apply(self):
        import ctypes
        from ctypes import wintypes
        cv.preview(self.sp,self.state,self.api)
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        kernel.CreateFileW.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.CreateFileW(str(self.file),0x80000000,0,None,3,0,None)
        if handle==wintypes.HANDLE(-1).value: raise ctypes.WinError(ctypes.get_last_error())
        try:
            with self.assertRaises(PermissionError):cv.apply(self.sp,self.state,self.api)
        finally: kernel.CloseHandle(handle)
        self.assertEqual(self.file.read_bytes(),b'base\r\n')

    def test_process_exit_after_replacement_recovers(self):
        cv.preview(self.sp,self.state,self.api)
        script="""
import os,sys
sys.path.insert(0,sys.argv[1])
import configvault as cv
sp=sys.argv[2]; state=cv.read_state(sp)
class A:
    def repo(self, state): return {'headVersionId': 2}
    def content(self, state, version): return b'cloud\\r\\n'
real=cv.atomic_json
calls=0
def die(path,obj):
    global calls
    calls+=1
    if calls==2: os._exit(73)
    real(path,obj)
cv.atomic_json=die
cv.apply(sp,state,A())
"""
        run=subprocess.run([sys.executable,'-c',script,str(Path(cv.__file__).parent),str(self.sp)],capture_output=True)
        self.assertEqual(run.returncode,73,run.stderr.decode(errors='replace'))
        self.assertEqual(self.file.read_bytes(),self.api.data)
        recovered=cv.recover(self.sp,cv.read_state(self.sp))
        self.assertEqual(recovered['baseVersionId'],2)
        self.assertEqual(recovered['baseContentHash'],cv.sha(self.api.data))

    def test_definite_rejection_allows_a_new_push(self):
        api=cv.Api('http://localhost:8080','test-token')
        error=urllib.error.HTTPError('http://localhost:8080/',413,'too large',{},None)
        with mock.patch.object(api.opener,'open',side_effect=error):
            with self.assertRaises(cv.VaultError): cv.do_push(self.sp,self.state,api,'test','utf-8')
        self.assertNotIn('pendingPush',cv.read_state(self.sp))

    def test_uncertain_http_failure_retains_original_request(self):
        for status in (408, 429, 500):
            with self.subTest(status=status):
                state=dict(self.state)
                state.pop('pendingPush',None)
                api=FakeApi()
                with mock.patch.object(api,'push',side_effect=cv.HttpError(status,'temporary failure')):
                    with self.assertRaises(cv.HttpError):cv.do_push(self.sp,state,api,'original','utf-8')
                pending=cv.read_state(self.sp)['pendingPush']
                self.assertEqual(base64.b64decode(pending['content']),b'base\r\n')
                self.assertEqual(pending['message'],'original')

    def test_pending_push_blocks_pull_until_resolved(self):
        self.state['pendingPush']={'baseVersionId':1,'content':base64.b64encode(b'old upload').decode(),
                                   'message':'','encodingLabel':'unknown','key':'pending_request_12345'}
        cv.atomic_json(self.sp,self.state)
        cv.preview(self.sp,self.state,self.api)
        self.assertEqual(self.file.read_bytes(),b'base\r\n')
        with self.assertRaises(cv.VaultError): cv.apply(self.sp,self.state,self.api)
        self.assertEqual(self.file.read_bytes(),b'base\r\n')

    def test_retry_does_not_rewind_a_newer_local_baseline(self):
        self.state['baseVersionId']=3
        self.state['baseContentHash']=cv.sha(b'newer baseline')
        self.state['pendingPush']={'baseVersionId':1,'content':base64.b64encode(b'old upload').decode(),
                                   'message':'','encodingLabel':'unknown','key':'pending_request_12345'}
        cv.atomic_json(self.sp,self.state)
        result=cv.do_push(self.sp,self.state,self.api,'','unknown')
        self.assertEqual(result['id'],2)
        saved=cv.read_state(self.sp)
        self.assertEqual(saved['baseVersionId'],3)
        self.assertEqual(saved['baseContentHash'],cv.sha(b'newer baseline'))
        self.assertNotIn('pendingPush',saved)

    def test_network_url_policy(self):
        self.assertEqual(cv.check_url('http://localhost:8080'),'http://localhost:8080')
        for value in ['http://example.com','https://user:pass@example.com','file:///tmp/x']:
            with self.assertRaises(cv.VaultError):cv.check_url(value)
    def test_invalid_download_header_rejected(self):
        class Response:
            headers={'X-Content-SHA256':cv.sha(b'right'),'X-Content-Size':'5'}
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self):return b'wrong'
        api=cv.Api('http://localhost:8080','x')
        with mock.patch.object(api.opener, 'open', return_value=Response()):
            with self.assertRaises(cv.VaultError):api.content(self.state,1)
        self.assertEqual(self.file.read_bytes(),b'base\r\n')

if __name__=='__main__': unittest.main()
