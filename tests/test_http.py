"""Real HTTP tests against a disposable server and database, no extra dependencies."""
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        cls.url='http://127.0.0.1:'+str(port)
        env={**os.environ,'EHR_DB_PATH':str(Path(cls.tmp.name)/'http.db'),'EHR_UPLOAD_DIR':str(Path(cls.tmp.name)/'uploads')}
        cls.process=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(port)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        cls.client=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(100):
            try:
                cls.client.open(cls.url,timeout=1).close();break
            except OSError: time.sleep(.1)
        else:
            cls.process.terminate();cls.process.wait();cls.tmp.cleanup()
            raise RuntimeError('server failed to start')

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate();cls.process.wait(timeout=10);cls.tmp.cleanup()

    def request(self,path,fields=None,file=None):
        headers={};data=None
        if file:
            name,filename,content=file;boundary='EHRTestBoundary'
            data=b''
            for key,value in (fields or {}).items():
                data+=f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
            data+=f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()+content+f'\r\n--{boundary}--\r\n'.encode()
            headers['Content-Type']='multipart/form-data; boundary='+boundary
        elif fields is not None:
            data=urllib.parse.urlencode(fields).encode()
        try: response=self.client.open(urllib.request.Request(self.url+path,data=data,headers=headers),timeout=10)
        except urllib.error.HTTPError as exc: response=exc
        with response: return response.status,response.headers,response.read()

    def test_end_to_end(self):
        for path in ['/','/patients/1','/ingest','/static/style.css']:
            self.assertEqual(self.request(path)[0],200,path)
        self.assertEqual(self.request('/patients',{'patient_no':'P2026001','name':'重复','gender':'男'})[0],400)
        self.assertEqual(self.request('/patients',{'patient_no':'NEW001','name':'虚构患者','gender':'女'})[0],200)
        for source,external in [('hospital','H001'),('examination','E001'),('followup','C001')]:
            self.assertEqual(self.request('/ingest/bind',{'source':source,'external_id':external,'patient_id':1})[0],200)
            status,_,body=self.request('/api/ingest/csv',{'source':source},('file',source+'.csv',Path('examples',source+'.csv').read_bytes()))
            self.assertEqual(status,200);result=json.loads(body);self.assertEqual(result['imported'],1)
            self.assertEqual(self.request('/api/ingest/jobs/'+str(result['job_id']))[0],200)
        timeline=json.loads(self.request('/api/ehr/1/timeline')[2])['records']
        sourced=[r for r in timeline if r['source']!='manual']
        self.assertEqual(len(sourced),3)
        self.assertEqual(self.request('/api/ehr/1/provenance/'+sourced[0]['id'])[0],200)
        page=self.request('/patients/1')[2].decode()
        self.assertIn('统一档案时间线',page);self.assertIn('hospital',page)
        self.assertEqual(len(json.loads(self.request('/api/ehr/1/context')[2])['structured_records']),len(timeline))
        self.assertEqual(self.request('/patients/1/attachments',{},('file','中文报告.txt','测试附件'.encode()))[0],200)
        status,headers,body=self.request('/attachments/1/download')
        self.assertEqual(status,200);self.assertIn('filename*=',headers['Content-Disposition']);self.assertEqual(body,'测试附件'.encode())
        bundle=self.request('/patients/1/export')[2]
        self.assertEqual(self.request('/ehr/import',{},('file','ehr.json',bundle))[0],200)
        self.assertEqual(self.request('/ehr/import',{},('file','bad.json',b'{'))[0],400)
        self.assertEqual(self.request('/api/ehr/99999/timeline')[0],404)
        self.assertEqual(self.request('/patients/1/attachments',{},('file','large.txt',b'x'*(10*1024*1024+1)))[0],413)


    def test_patient_management(self):
        self.assertEqual(self.request('/patients',{'patient_no':'ED001','name':'测试患者','gender':'男','birth_date':'1950-01-01'})[0],200)
        page=self.request('/?q=ED001&status=all')[2].decode()
        match=re.search(r'/patients/(\d+)',page);self.assertIsNotNone(match);pid=match.group(1)
        self.assertIn('测试患者',self.request('/patients/'+pid+'/edit')[2].decode())
        self.assertEqual(self.request('/patients/'+pid+'/edit',{'patient_no':'ED001','name':'测试患者改','gender':'女','birth_date':'1950-01-01'})[0],200)
        self.assertIn('测试患者改',self.request('/patients/'+pid)[2].decode())
        self.assertEqual(self.request('/patients/'+pid+'/edit',{'patient_no':'ED001','name':'测试患者改','gender':'女','birth_date':'2999-01-01'})[0],400)
        self.assertEqual(self.request('/patients/'+pid+'/edit',{'patient_no':'P2026001','name':'测试患者改','gender':'女'})[0],400)
        self.assertEqual(self.request('/patients/'+pid+'/encounters',{'encounter_date':'2026-01-02','encounter_type':'门诊','complaint':'头晕'})[0],200)
        detail=self.request('/patients/'+pid)[2].decode()
        match=re.search(r'/patients/'+pid+r'/encounters/(\d+)/edit',detail);self.assertIsNotNone(match);eid=match.group(1)
        self.assertEqual(self.request('/patients/'+pid+'/encounters/'+eid+'/edit',{'encounter_date':'2026-01-03','encounter_type':'体检','complaint':'复查','diagnosis':'稳定'})[0],200)
        detail=self.request('/patients/'+pid)[2].decode();self.assertIn('复查',detail);self.assertIn('稳定',detail)
        self.assertEqual(self.request('/patients/'+pid+'/encounters/'+eid+'/edit',{'encounter_date':'bad','encounter_type':'体检'})[0],400)
        self.assertEqual(self.request('/patients/'+pid+'/status',{'status':'inactive'})[0],200)
        row_link='/patients/'+pid+'"'
        self.assertNotIn(row_link,self.request('/?q=ED001')[2].decode())
        self.assertIn(row_link,self.request('/?q=ED001&status=inactive')[2].decode())
        self.assertIn('已停用',self.request('/patients/'+pid)[2].decode())
        self.assertEqual(self.request('/patients/'+pid+'/status',{'status':'active'})[0],200)
        self.assertIn(row_link,self.request('/?q=ED001')[2].decode())
        self.assertEqual(self.request('/patients/999999')[0],404)
        self.assertEqual(self.request('/?created_from=2026-05-01&created_to=2026-01-01')[0],400)


if __name__=='__main__': unittest.main()
