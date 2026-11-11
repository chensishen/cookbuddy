import hashlib
import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import project
from project_doctor import check_project,project_id
from serve_recipe_app import RecipeServer
import run_recipe_job


class ProjectDoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        files={'scripts/serve_recipe_app.py':'','app/index.html':'<html></html>',
               'data/processed/media_pilot/asset-index.json':json.dumps({'assets':[]}),
               'data/processed/active-dataset.json':json.dumps({'dataset':'all_recipes'}),
               'requirements-engineering.txt':'langchain==1.4.4\n',
               'data/HowToCook/dishes/egg.md':'# 菜谱\n盐\n'}
        row={'chunk_id':'egg','title':'菜谱','source':'data/HowToCook/dishes/egg.md',
             'source_start_line':1,'source_end_line':2,'content':'# 菜谱\n盐'}
        raw=json.dumps(row,ensure_ascii=False)+'\n'
        files['data/processed/all_recipes/chunks.jsonl']=raw
        files['data/processed/all_recipes/embedding-metadata.json']=json.dumps({'api_base_url':'https://example.invalid/v1',
            'requested_model':'test-model','dimensions':1024,'chunk_count':1,
            'source_sha256':hashlib.sha256(raw.encode()).hexdigest()})
        for path,text in files.items():
            target=self.root/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(text)
        self.python=patch('project_doctor.sys.prefix','/tmp/py3.12.11');self.python.start();self.addCleanup(self.python.stop)
        self.version=patch('project_doctor.sys.version_info',(3,12,11));self.version.start();self.addCleanup(self.version.stop)
    def check(self,**kwargs):
        return check_project(self.root,environ={},version_lookup=lambda _: '1.4.4',**kwargs)
    def configure(self):
        secret='never-emit-synthetic-key'
        (self.root/'.env').write_text('EMBEDDING_API_KEY='+secret+'\nEMBEDDING_BASE_URL=https://example.invalid/v1\nEMBEDDING_MODEL=test-model\nEMBEDDING_DIMENSIONS=1024\nLLM_API_KEY='+secret+'\nLLM_MODEL=deepseek-flash\n')
        return secret
    def test_first_run_without_key_keeps_local_tools_available_and_makes_no_calls(self):
        with patch('project_doctor.local_json') as network:
            report=self.check()
        self.assertTrue(report['local_ready']);self.assertFalse(report['full_prerequisites'])
        self.assertEqual(report['remote_model_access'],'not_checked');self.assertEqual(network.call_count,0)
    def test_empty_workspace_returns_actionable_blockers_not_import_failure(self):
        with TemporaryDirectory() as empty:
            report=check_project(Path(empty),environ={},version_lookup=lambda _:'1.4.4')
        self.assertFalse(report['local_ready'])
        self.assertTrue(any(c['name']=='dataset' and c['action'] for c in report['checks']))
    def test_modified_original_is_blocked_even_when_chunk_hash_is_unchanged(self):
        (self.root/'data/HowToCook/dishes/egg.md').write_text('# 菜谱\n白糖\n')
        self.assertFalse(self.check()['local_ready'])
    def test_config_is_redacted_and_model_access_never_claimed_verified(self):
        secret=self.configure();report=self.check();text=json.dumps(report)
        self.assertNotIn(secret,text);self.assertNotIn('example.invalid',text)
        self.assertEqual(next(c['status'] for c in report['checks'] if c['name']=='model_config'),'configured')
        self.assertEqual(report['remote_model_access'],'not_checked')
    def test_legacy_key_alone_is_not_online_rag_configuration(self):
        self.configure();path=self.root/'.env'
        path.write_text(path.read_text().replace('EMBEDDING_API_KEY=','GITEE_AI_API_KEY='))
        report=self.check()
        self.assertEqual(next(c['status'] for c in report['checks'] if c['name']=='model_config'),'warning')
    def test_environment_dimension_mismatch_disables_full_attempt(self):
        self.configure()
        report=check_project(self.root,environ={'EMBEDDING_DIMENSIONS':'2048'},version_lookup=lambda _:'1.4.4')
        self.assertEqual(next(c['status'] for c in report['checks'] if c['name']=='model_config'),'warning')
    def test_bounded_dataset_requires_its_local_tokenizer(self):
        from importlib import metadata
        path=self.root/'data/processed/all_recipes/embedding-metadata.json';values=json.loads(path.read_text())
        values['splitter_version']='bounded-v2';path.write_text(json.dumps(values))
        def lookup(name):
            if name=='tiktoken':raise metadata.PackageNotFoundError(name)
            return '1.4.4'
        report=check_project(self.root,environ={},version_lookup=lookup)
        self.assertFalse(report['local_ready'])
        self.assertEqual(next(c['status'] for c in report['checks'] if c['name']=='local_tokenizer'),'error')
    def test_dependency_extras_are_resolved_as_distribution_names(self):
        (self.root/'requirements-engineering.txt').write_text('psycopg[binary]==3.3.6\n')
        calls=[]
        def lookup(name):
            calls.append(name)
            return '3.3.6'
        report=check_project(self.root,environ={},version_lookup=lookup)
        self.assertIn('psycopg',calls)
        self.assertNotIn('psycopg[binary]',calls)
        self.assertEqual(next(c['status'] for c in report['checks'] if c['name']=='dependencies'),'ok')
    def test_service_outage_does_not_block_local_mode_or_expose_exception_text(self):
        self.configure()
        def failed(*args,**kwargs):raise RuntimeError('secret-provider-response')
        with patch('project_doctor.shutil.which',return_value=None):report=self.check(live=True,probe=failed)
        self.assertTrue(report['local_ready']);self.assertFalse(report['full_prerequisites'])
        self.assertNotIn('secret-provider-response',json.dumps(report))
    def test_live_probes_only_read_known_collection_and_local_app(self):
        self.configure();calls=[]
        def probe(url,payload=None,token=''):
            calls.append((url,payload))
            if url.endswith('/describe'):return {'code':0,'data':{'load':'LoadStateLoaded','fields':[{'name':'vector','params':[{'key':'dim','value':'1024'}]}]}}
            if url.endswith('/get_stats'):return {'code':0,'data':{'rowCount':1}}
            return {'project_id':project_id(self.root)}
        with patch('project_doctor.shutil.which',return_value=None):report=self.check(live=True,probe=probe)
        self.assertTrue(report['full_prerequisites']);self.assertEqual(report['app_state'],'ready')
        self.assertEqual(len(calls),4)
        self.assertTrue(all(url.startswith('http://127.0.0.1:') for url,_ in calls))
        self.assertTrue(all(payload['collectionName']=='recipe_all_v1' for _,payload in calls if payload))
    def test_unknown_service_does_not_receive_access_token(self):
        self.configure();seen=[]
        def probe(url,payload=None,token=''):
            seen.append((url,token));return {'project_id':'another-project'}
        with patch('project_doctor.shutil.which',return_value=None):
            report=check_project(self.root,live=True,environ={'RECIPE_ACCESS_TOKEN':'synthetic-access-token'},
                                 probe=probe,version_lookup=lambda _:'1.4.4')
        self.assertFalse(report['full_prerequisites'])
        self.assertTrue(all(not token for _,token in seen))


class LaunchTests(unittest.TestCase):
    def report(self):return {'checks':[],'local_ready':True,'full_prerequisites':True}
    def test_occupied_foreign_port_is_never_replaced(self):
        with patch('project.check_project',return_value=self.report()),patch('project.socket.create_connection'),\
             patch('project.local_json',return_value={'project_id':'other-app'}),patch('project.subprocess.call') as launch,patch('sys.stdout',new=io.StringIO()):
            result=project.start(8080)
        self.assertEqual(result,1);self.assertEqual(launch.call_count,0)
    def test_existing_matching_mode_is_reused_without_second_process(self):
        with patch('project.check_project',return_value=self.report()),patch('project.socket.create_connection'),\
             patch('project.local_json',return_value={'project_id':project_id(project.ROOT),'local_only':True}),\
             patch('project.subprocess.call') as launch,patch('sys.stdout',new=io.StringIO()):
            result=project.start(8080)
        self.assertEqual(result,0);self.assertEqual(launch.call_count,0)
    def test_local_start_uses_separate_queue_and_disables_models(self):
        with patch('project.check_project',return_value=self.report()),patch('project.socket.create_connection',side_effect=ConnectionRefusedError),\
             patch('project.subprocess.call',return_value=0) as launch,patch('sys.stdout',new=io.StringIO()):
            self.assertEqual(project.start(8081),0)
        self.assertEqual(launch.call_args.kwargs['env']['RECIPE_LOCAL_ONLY'],'1')
        self.assertTrue(launch.call_args.kwargs['env']['RECIPE_JOBS_DB'].endswith('local-jobs.sqlite3'))


class LocalModeTests(unittest.TestCase):
    def test_public_identity_exposes_only_fingerprint_and_mode(self):
        with TemporaryDirectory() as tmp,patch.dict(os.environ,{'RECIPE_JOBS_DB':str(Path(tmp)/'jobs.sqlite'),
                'RECIPE_WORKERS':'0','RECIPE_LOCAL_ONLY':'1','RECIPE_ACCESS_TOKEN':'synthetic-access-token'}):
            server=RecipeServer(('127.0.0.1',0));thread=Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                base=f'http://127.0.0.1:{server.server_port}'
                with urlopen(base+'/api/identity') as response:identity=json.load(response)
                self.assertEqual(set(identity),{'project_id','local_only'})
                with self.assertRaises(HTTPError) as error:urlopen(base+'/api/catalog')
                self.assertEqual(error.exception.code,401);error.exception.close()
            finally:server.shutdown();server.server_close();thread.join()
    def test_backend_rejects_all_model_modes_before_queue(self):
        with TemporaryDirectory() as tmp,patch.dict(os.environ,{'RECIPE_JOBS_DB':str(Path(tmp)/'jobs.sqlite'),
                'RECIPE_WORKERS':'0','RECIPE_LOCAL_ONLY':'1','RECIPE_ACCESS_TOKEN':''}):
            server=RecipeServer(('127.0.0.1',0));thread=Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                base=f'http://127.0.0.1:{server.server_port}'
                with urlopen(base+'/api/status') as response:self.assertTrue(json.load(response)['local_only'])
                for mode in ('agent','multi_agent','rag'):
                    request=Request(base+'/api/ask',data=json.dumps({'question':'蒸水蛋怎么做','mode':mode}).encode(),headers={'Content-Type':'application/json'})
                    with self.assertRaises(HTTPError) as error:urlopen(request)
                    self.assertEqual(error.exception.code,400);error.exception.close()
                self.assertEqual(server.queue.metrics()['states'],{})
            finally:server.shutdown();server.server_close();thread.join()
    def test_worker_cannot_bypass_local_mode_through_queue(self):
        with patch.dict(os.environ,{'RECIPE_JOB_ID':'test-job','RECIPE_LOCAL_ONLY':'1'}),\
             patch('run_recipe_job.JobQueue') as queue_class,patch('run_recipe_job.execute') as execute:
            queue=queue_class.return_value
            queue.connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value=(
                json.dumps({'question':'蒸水蛋怎么做','mode':'agent'}),time.time()+30,time.time(),time.time())
            run_recipe_job.main()
        self.assertEqual(execute.call_count,0);self.assertEqual(queue.finish.call_args.args[1],'failed')


if __name__=='__main__':unittest.main()
