"""Opt-in HTTP integration tests against a disposable running MySQL-backed server."""
import base64
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'client'))
import configvault as cv
URL=os.environ.get('CV_TEST_URL')

class Browser:
    def __init__(self):
        self.opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        self.csrf=self.get('/api/csrf')['token']
    def call(self,path,method='GET',payload=None,headers=None):
        h=dict(headers or {})
        if method!='GET': h['X-XSRF-TOKEN']=self.csrf
        req=urllib.request.Request(URL+path,data=payload,method=method,headers=h)
        with self.opener.open(req,timeout=20) as r:
            data=r.read()
            return json.loads(data) if data else None
    def get(self,p): return self.call(p)
    def json(self,p,obj): return self.call(p,'POST',json.dumps(obj).encode(),{'Content-Type':'application/json'})
    def setup(self):
        user='test_'+secrets.token_hex(7); password=secrets.token_urlsafe(20)
        self.json('/api/register',{'username':user,'password':password})
        body=urllib.parse.urlencode({'username':user,'password':password}).encode()
        self.call('/api/session','POST',body,{'Content-Type':'application/x-www-form-urlencoded'})
        self.csrf=self.get('/api/csrf')['token']
        return self.json('/api/tokens',{'label':'integration'})

@unittest.skipUnless(URL,'set CV_TEST_URL to a disposable running server')
class Integration(unittest.TestCase):
    def test_full_protocol(self):
        browser=Browser(); t=browser.setup(); api=cv.Api(URL,t['token'])
        other=Browser(); token2=other.setup(); api2=cv.Api(URL,token2['token'])
        repo=api.json('POST','/api/config-repositories',{'name':'test'})
        rid=repo['id']; state={'repoId':rid}
        def push(base,body,key,encoding='utf-8'):
            return api.push(state,{'baseVersionId':base,'content':base64.b64encode(body).decode(),'message':'test','encodingLabel':encoding,'key':key})
        original=b'// first\r\nbind "x" "a;b"\r\nbind "x" z\n'
        v1=push(None,original,'first_request_12345'); self.assertEqual(api.content(state,v1['id']),original)
        with self.assertRaises(cv.VaultError): api2.repo(state)
        # 20 concurrent requests with the same key are one logical write.
        payload=b'\xef\xbb\xbf// changed\r\n'
        with ThreadPoolExecutor(max_workers=10) as pool:
            results=list(pool.map(lambda _:push(v1['id'],payload,'retry_request_12345','utf-8-bom'),range(20)))
        self.assertEqual(len({v['id'] for v in results}),1); v2=results[0]
        self.assertEqual(len(api.json('GET',f'/api/config-repositories/{rid}/versions')),2)
        with self.assertRaises(cv.VaultError): push(v1['id'],b'conflict','different_request_12345')
        with self.assertRaises(cv.VaultError): push(v1['id'],b'different','retry_request_12345')
        same=push(v2['id'],payload,'nochange_request_12345','utf-8-bom'); self.assertTrue(same['noChange']); self.assertEqual(same['id'],v2['id'])
        diff=api.json('GET',f'/api/config-repositories/{rid}/diff?from={v1["id"]}&to={v2["id"]}')
        self.assertTrue(any(x['type']=='add' for x in diff['lines']))
        r=api.json('POST',f'/api/config-repositories/{rid}/restore',{'baseVersionId':v2['id'],'sourceVersionId':v1['id'],'message':'restore'},{'Idempotency-Key':'restore_request_12345'})
        self.assertNotEqual(r['id'],v1['id']); self.assertEqual(api.content(state,r['id']),original); self.assertEqual(api.content(state,v2['id']),payload)
        repo2=api.json('POST','/api/config-repositories',{'name':'second'})
        with self.assertRaises(cv.VaultError): api.content({'repoId':repo2['id']},v1['id'])
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as d:
            local=Path(d)/'file.cfg'; local.write_bytes(payload)
            sp=Path(d)/'state.json'; st={'repoId':rid,'file':str(local),'baseVersionId':v2['id'],'baseContentHash':cv.sha(payload)}
            cv.atomic_json(sp,st);cv.preview(sp,st,api);backup=cv.apply(sp,st,api)
            self.assertEqual(local.read_bytes(),original);self.assertEqual(Path(backup).read_bytes(),payload)
        browser.call('/api/tokens/'+str(t['id']),'DELETE')
        with self.assertRaises(cv.VaultError): api.repo(state)


    def test_competing_writes_and_encoding(self):
        browser=Browser(); token=browser.setup(); api=cv.Api(URL,token['token'])
        repo=api.json('POST','/api/config-repositories',{'name':'race'})
        state={'repoId':repo['id']}; rid=repo['id']
        def push(base, data, key, encoding):
            pending={'baseVersionId':base,'content':base64.b64encode(data).decode(),'message':'','encodingLabel':encoding,'key':key}
            try: return api.push(state,pending)
            except cv.VaultError as exc: return str(exc)
        first=push(None,b'base\r\n','race_first_request','utf-8')
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks=[pool.submit(push,first['id'],b'left','race_left_request_1','utf-8'),pool.submit(push,first['id'],b'right','race_right_request','utf-8')]
            results=[x.result() for x in tasks]
        self.assertEqual(sum(isinstance(x,dict) for x in results),1)
        self.assertEqual(sum(isinstance(x,str) and '409' in x for x in results),1)
        winner=next(x for x in results if isinstance(x,dict))
        gb='// 中文注释\r\n'.encode('gb18030')
        third=push(winner['id'],gb,'gb18030_request_01','gb18030')
        self.assertEqual(api.content(state,third['id']),gb)
        comparison=api.json('GET',f'/api/config-repositories/{rid}/diff?from={winner["id"]}&to={third["id"]}')
        self.assertTrue(any('中文注释' in line['text'] for line in comparison['lines']))
        bad=push(third['id'],b'\xff','invalid_enc_request','utf-8')
        self.assertEqual(api.content(state,bad['id']),b'\xff')
        with self.assertRaises(cv.VaultError):api.json('GET',f'/api/config-repositories/{rid}/diff?from={third["id"]}&to={bad["id"]}')
        with self.assertRaises(urllib.error.HTTPError) as err:
            req=urllib.request.Request(URL+'/api/config-repositories',data=b'{"name":"bad csrf"}',method='POST',headers={'Content-Type':'application/json','Authorization':'Bearer invalid'})
            browser.opener.open(req,timeout=20)
        self.assertEqual(err.exception.code,403)
        err.exception.close()

    def test_invalid_restore_input_and_failed_push_recovery(self):
        browser=Browser(); token=browser.setup(); api=cv.Api(URL,token['token'])
        repo=api.json('POST','/api/config-repositories',{'name':'invalid-input'})
        state={'repoId':repo['id']}
        pending={'baseVersionId':None,'content':base64.b64encode(b'base').decode(),
                 'message':'中文备注','encodingLabel':'utf-8','key':'initial_invalid_test'}
        first=api.push(state,pending)
        self.assertEqual(first['message'],'中文备注')
        for invalid in ('invalid', 1.5, -1):
            with self.subTest(invalid=invalid):
                with self.assertRaises(cv.HttpError) as err:
                    api.json('POST',f'/api/config-repositories/{repo["id"]}/restore',
                             {'sourceVersionId':invalid,'baseVersionId':first['id']},
                             {'Idempotency-Key':'invalid_restore_test'})
                self.assertEqual(err.exception.status,400)
        for invalid in ('invalid', 1.5, -1):
            with self.subTest(invalid_base=invalid):
                with self.assertRaises(cv.HttpError) as err:
                    api.json('POST',f'/api/config-repositories/{repo["id"]}/restore',
                             {'sourceVersionId':first['id'],'baseVersionId':invalid},
                             {'Idempotency-Key':'invalid_restore_base'})
                self.assertEqual(err.exception.status,400)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as d:
            path=Path(d)/'sample.cfg'; path.write_bytes(b'new contents')
            state.update({'file':str(path),'baseVersionId':first['id'],'baseContentHash':cv.sha(b'base')})
            sp=Path(d)/'state.json'; cv.atomic_json(sp,state)
            bad=dict(state); bad['baseVersionId']=first['id']+10000
            with self.assertRaises(cv.HttpError) as err: cv.do_push(sp,bad,api,'wrong baseline','utf-8')
            self.assertEqual(err.exception.status,409)
            self.assertNotIn('pendingPush',cv.read_state(sp))
            result=cv.do_push(sp,state,api,'correct baseline','utf-8')
            self.assertEqual(api.content(state,result['id']),b'new contents')

    @unittest.skipUnless(URL == 'http://127.0.0.1:18082' and os.environ.get('CV_AUDIT_SECRET_PATH') and os.environ.get('CV_AUDIT_MYSQL'),
                         'requires an isolated MySQL test instance')
    def test_expired_token_is_rejected(self):
        browser=Browser(); token=browser.setup()['token']; api=cv.Api(URL,token)
        self.assertIsInstance(api.json('GET','/api/config-repositories'),list)
        script=("Import-Module (Join-Path $PSHOME 'Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1'); "
                "$s=(Get-Content -LiteralPath $env:CV_AUDIT_SECRET_PATH -Raw -Encoding UTF8).Trim(); "
                "$p=[Runtime.InteropServices.Marshal]::SecureStringToBSTR((ConvertTo-SecureString $s)); "
                "try {[Console]::Out.Write([Runtime.InteropServices.Marshal]::PtrToStringBSTR($p))} "
                "finally {[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p)}")
        result=subprocess.run(['powershell.exe','-NoProfile','-Command',script],capture_output=True,text=True,check=True)
        env=os.environ.copy(); env['MYSQL_PWD']=result.stdout
        sql=("UPDATE configvault.api_token SET expires_at=DATE_SUB(CURRENT_TIMESTAMP,INTERVAL 1 SECOND) "
             "WHERE token_hash='"+cv.sha(token.encode())+"';")
        update=subprocess.run([os.environ['CV_AUDIT_MYSQL'],'--no-defaults','--protocol=TCP','--host=127.0.0.1',
                               '--port=33089','--user=root'],input=sql,text=True,capture_output=True,env=env,timeout=10)
        self.assertEqual(update.returncode,0,'isolated database update failed')
        with self.assertRaises(cv.HttpError) as error: api.json('GET','/api/config-repositories')
        self.assertEqual(error.exception.status,401)

    def test_cli_two_clients_conflict_and_pull(self):
        browser=Browser(); token=browser.setup()['token']; api=cv.Api(URL,token)
        repo=api.json('POST','/api/config-repositories',{'name':'cli-test'})
        cli=Path(__file__).resolve().parents[1]/'client'/'configvault.py'
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as d:
            root=Path(d); first=root/'first.cfg'; second=root/'second.cfg'
            first.write_bytes(b'// v1\r\n'); second.write_bytes(first.read_bytes())
            state1=root/'one.json'; state2=root/'two.json'
            def run(state,*args,auth=True,expected=0):
                if auth:
                    wrapper="import io,runpy,sys; sys.stdin=io.StringIO(sys.stdin.read()); sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')"
                    command=[sys.executable,'-c',wrapper,str(cli),'--state',str(state),*args]
                else:
                    command=[sys.executable,str(cli),'--state',str(state),*args]
                result=subprocess.run(command,input=(token+'\n') if auth else None,text=True,capture_output=True,timeout=40)
                self.assertEqual(result.returncode,expected,'CLI command failed: '+args[0])
                return result
            for state,path in ((state1,first),(state2,second)):
                run(state,'init','--server',URL,'--repo',str(repo['id']),'--file',str(path),auth=False)
            run(state1,'push','--message','first','--encoding','utf-8')
            run(state2,'pull','--dry-run')
            run(state2,'pull','--apply')
            first.write_bytes(b'// v2\r\n'); run(state1,'push','--message','second','--encoding','utf-8')
            second.write_bytes(b'// local change\r\n'); run(state2,'push','--message','conflict','--encoding','utf-8',expected=1)
            self.assertNotIn('pendingPush',cv.read_state(state2))
            run(state2,'pull','--dry-run')
            run(state2,'pull','--apply','--accept-cloud')
            self.assertEqual(second.read_bytes(),b'// v2\r\n')
            backups=list((root/'.configvault-backups').glob('second.cfg.*.bak'))
            self.assertEqual(len(backups),1)
            self.assertEqual(backups[0].read_bytes(),b'// local change\r\n')

    def test_repository_rename_delete_and_authorization(self):
        browser=Browser(); api=cv.Api(URL,browser.setup()['token'])
        other=Browser(); foreign=cv.Api(URL,other.setup()['token'])
        r=api.json('POST','/api/config-repositories',{'name':'original'})
        sibling=api.json('POST','/api/config-repositories',{'name':'sibling'})
        rid=r['id']; path=f'/api/config-repositories/{rid}'
        def rename(client,name): return client.json('PATCH',path,{'name':name})
        def delete(client,base): return client.request('DELETE',path,json.dumps({'baseVersionId':base}).encode(),{'Content-Type':'application/json'})
        self.assertEqual(rename(api,'renamed')['name'],'renamed')
        for name,status in [('',400),('sibling',409)]:
            with self.assertRaises(cv.HttpError) as error: rename(api,name)
            self.assertEqual(error.exception.status,status)
        with self.assertRaises(cv.HttpError) as error: rename(foreign,'stolen')
        self.assertEqual(error.exception.status,404)
        with self.assertRaises(cv.HttpError) as error: delete(foreign,None)
        self.assertEqual(error.exception.status,404)
        self.assertEqual(api.repo({'repoId':rid})['name'],'renamed')
        state={'repoId':rid}
        def push(base,body,key):
            return api.push(state,{'baseVersionId':base,'content':base64.b64encode(body).decode(),'message':'','encodingLabel':'utf-8','key':key})
        first=push(None,b'v1','delete_first_request')
        second=push(first['id'],b'v2','delete_second_request')
        third=api.json('POST',path+'/restore',{'sourceVersionId':first['id'],'baseVersionId':second['id']},{'Idempotency-Key':'delete_restore_request'})
        with self.assertRaises(cv.HttpError) as error: delete(api,second['id'])
        self.assertEqual(error.exception.status,409)
        self.assertEqual(api.content(state,third['id']),b'v1')
        with self.assertRaises(cv.HttpError) as error: api.request('DELETE',path,b'{}',{'Content-Type':'application/json'})
        self.assertEqual(error.exception.status,400)
        delete(api,third['id'])
        with self.assertRaises(cv.HttpError) as error: api.repo(state)
        self.assertEqual(error.exception.status,404)
        self.assertEqual(api.repo({'repoId':sibling['id']})['name'],'sibling')
        replacement=api.json('POST','/api/config-repositories',{'name':'renamed'})
        state={'repoId':replacement['id']}
        self.assertEqual(push(None,b'v1','delete_first_request')['revision'],1)
        empty=api.json('POST','/api/config-repositories',{'name':'empty'})
        api.request('DELETE',f'/api/config-repositories/{empty["id"]}',b'{"baseVersionId":null}',{'Content-Type':'application/json'})
        with self.assertRaises(cv.HttpError):api.repo({'repoId':empty['id']})

    def test_repository_delete_competes_with_push(self):
        browser=Browser(); api=cv.Api(URL,browser.setup()['token'])
        repo=api.json('POST','/api/config-repositories',{'name':'race-delete'})
        rid=repo['id']; state={'repoId':rid}; path=f'/api/config-repositories/{rid}'
        def push(base,content,key):
            return api.push(state,{'baseVersionId':base,'content':base64.b64encode(content).decode(),'message':'','encodingLabel':'utf-8','key':key})
        first=push(None,b'first','race_delete_first_key')
        def upload():
            try:return push(first['id'],b'next','race_delete_second_key')
            except cv.HttpError as error:return error.status
        def remove():
            try:api.request('DELETE',path,json.dumps({'baseVersionId':first['id']}).encode(),{'Content-Type':'application/json'});return 204
            except cv.HttpError as error:return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            fu=pool.submit(upload); fd=pool.submit(remove)
            uploaded,deleted=fu.result(),fd.result()
        if deleted==204:
            self.assertEqual(uploaded,404)
            with self.assertRaises(cv.HttpError):api.repo(state)
        else:
            self.assertEqual(deleted,409)
            self.assertIsInstance(uploaded,dict)
            self.assertEqual(api.content(state,uploaded['id']),b'next')

    def test_quota_and_size(self):
        browser=Browser(); token=browser.setup(); api=cv.Api(URL,token['token'])
        repo=api.json('POST','/api/config-repositories',{'name':'quota'})
        state={'repoId':repo['id']}; base=None
        def attempt(base, data, n):
            return api.push(state,{'baseVersionId':base,'content':base64.b64encode(data).decode(),'message':'','encodingLabel':'unknown','key':f'quota_request_{n:06d}'})
        with self.assertRaises(cv.VaultError):attempt(None,b'x'*(256*1024+1),99999)
        for i in range(99):
            result=attempt(base,f'version {i}'.encode(),i);base=result['id']
        def compete(n):
            try: return attempt(base,f'concurrent {n}'.encode(),n)
            except cv.HttpError as error: return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(compete,(100,101)))
        self.assertEqual(sum(isinstance(result,dict) for result in results),1)
        self.assertIn(409,results)
        base=next(result['id'] for result in results if isinstance(result,dict))
        with self.assertRaises(cv.VaultError):attempt(base,b'over quota',102)
        history=api.json('GET',f'/api/config-repositories/{repo["id"]}/versions?size=100')
        self.assertEqual(len(history),100)
        for i in range(18):api.json('POST','/api/config-repositories',{'name':f'other {i}'})
        def create(n):
            try: return api.json('POST','/api/config-repositories',{'name':f'concurrent {n}'})
            except cv.HttpError as error: return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            created=list(pool.map(create,(1,2)))
        self.assertEqual(sum(isinstance(result,dict) for result in created),1)
        self.assertIn(409,created)
        self.assertEqual(len(api.json('GET','/api/config-repositories')),20)

if __name__=='__main__':unittest.main(verbosity=2)
