import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.request import Request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from naive_baseline import fixed_windows,dense_search,gold_windows
from acceptance_closeout import exact_evidence,citation_metadata
from service_runtime import request_json,RuntimeStore
from blind_review import kappa,export_packet,import_reviews
from eval_dataset import seal,consume,make_holdout
from evaluate_prd import baseline,rerank_policy_effect,naive_query
from accept_prd import acceptance_matrix
from usage_ledger import record_usage,summarize
from eval_judge import judge_settings
from review_prd_eval import rubric_scores,review
from evaluate_ragas import score_metric_jobs,resume_metric_item


class EvaluationCloseout(unittest.TestCase):
    def test_equivalent_complete_cache_fills_missing_metrics_without_overwriting_own_scores(self):
        complete={'case':'q','variant':'fixed_workflow','audit_sha256':'same','faithfulness':1.,'factual_correctness':.8,
            'diagnostics':{'statements':['other']}}
        own={'case':'q','variant':'full_agent','audit_sha256':'same','faithfulness':.75,
            'diagnostics':{'statements':['own']},'metric_failures':{'factual_correctness':'TimeoutError'}}
        different={'case':'q','variant':'naive_rag','audit_sha256':'different','context_precision':1.}
        resumed=resume_metric_item([complete,own,different],{'case':'q','variant':'full_agent','audit_sha256':'same'})
        self.assertEqual(resumed['faithfulness'],.75);self.assertEqual(resumed['factual_correctness'],.8)
        self.assertEqual(resumed['diagnostics']['statements'],['own'])
        self.assertNotIn('context_precision',resumed);self.assertNotIn('factual_correctness',resumed['metric_failures'])
        self.assertEqual(own['metric_failures'],{'factual_correctness':'TimeoutError'})

    def test_existing_variant_keeps_its_own_observed_score_before_equivalent_cache(self):
        first={'case':'q','variant':'fixed_workflow','audit_sha256':'same','faithfulness':1.}
        own={'case':'q','variant':'full_agent','audit_sha256':'same','faithfulness':.75}
        resumed=resume_metric_item([first,own],{'case':'q','variant':'full_agent','audit_sha256':'same'})
        self.assertEqual(resumed['faithfulness'],.75)

    def test_naive_baseline_owns_embedding_request_and_actual_contexts(self):
        manifest={'spec':{'model':'embedding','dimensions':2,'base_url':'https://embedding.invalid/v1'},'spec_sha256':'version'}
        rows=[{'chunk_id':'a','vector':[1.,0.],'content':'first'},
            {'chunk_id':'b','vector':[0.,1.],'content':'actual'}]
        with patch('evaluate_prd.request_json',return_value={'data':[{'embedding':[0.,1.]}]}) as call:
            hits=naive_query('问题',manifest,rows,{'EMBEDDING_API_KEY':'private'})
        self.assertEqual(hits[0]['chunk_id'],'b')
        self.assertEqual(json.loads(call.call_args.args[0].data)['input'],'问题')
        self.assertEqual(call.call_args.kwargs['phase'],'eval_naive_embedding')
        self.assertEqual(call.call_args.kwargs['cache_namespace'],'naive512:version')

    def test_generation_only_baseline_cannot_pass_end_to_end_latency_gate(self):
        report={'configuration':{'baseline_latency_scope':'generation_only_shared_retrieval'},
            'improvements':[{'from':'naive_rag','to':'full_agent','p95_delta_seconds':0.1}]}
        checks={r['criterion']:r['status'] for r in acceptance_matrix(report)['checks']}
        self.assertEqual(checks['样本P95延迟增量≤1.5秒'],'pending')

    def test_rerank_policy_keeps_fallback_queries_and_requires_complete_pairs(self):
        cases=[];rows=[]
        for i in range(12):
            cases.append({'id':str(i),'gold':['chunk'],'sources':['source'+str(i)]})
            rows.extend([{'case':str(i),'variant':'hybrid_no_rerank','metrics':{'mrr':.25}},
                {'case':str(i),'variant':'hybrid_rerank','metrics':{'mrr':1. if i<9 else .25},'rerank_applied':i<9}])
        effect=rerank_policy_effect(rows,cases)
        self.assertEqual(effect['coverage']['model_applied'],9)
        self.assertEqual(effect['coverage']['paired'],12)
        self.assertTrue(effect['test']['significant']);self.assertTrue(effect['complete'])
        checks={r['criterion']:r['status'] for r in acceptance_matrix({'rerank_policy_effect':effect})['checks']}
        self.assertEqual(checks['重排策略MRR显著提升且查询覆盖完整'],'passed')
        incomplete=rerank_policy_effect(rows[:-1],cases)
        self.assertFalse(incomplete['complete'])
        checks={r['criterion']:r['status'] for r in acceptance_matrix({'rerank_policy_effect':incomplete})['checks']}
        self.assertEqual(checks['重排策略MRR显著提升且查询覆盖完整'],'failed')

    def test_identical_answer_metric_cache_preserves_new_case_and_variant(self):
        old={'case':'first','variant':'full_agent','audit_sha256':'same',
            'faithfulness':1.,'diagnostics':{'statements':['fact']},'metric_failures':{'context_recall':'TimeoutError'}}
        current={'case':'second','variant':'fixed_workflow','audit_sha256':'same',
            'context_policy':'actual contexts','not_applicable':{'context_recall':'N/A'}}
        resumed=resume_metric_item([old],current)
        self.assertEqual((resumed['case'],resumed['variant']),('second','fixed_workflow'))
        self.assertEqual(resumed['faithfulness'],1.)
        self.assertEqual(resumed['context_policy'],'actual contexts')
        self.assertNotIn('context_recall',resumed['metric_failures'])
        resumed['metric_failures'].clear();resumed['diagnostics']['statements'].clear()
        self.assertTrue(old['metric_failures']);self.assertEqual(old['diagnostics']['statements'],['fact'])

    def test_metric_jobs_limit_parallelism_preserve_cached_scores_and_isolate_failure(self):
        import asyncio
        from types import SimpleNamespace
        active=0;maximum=0;calls=[]
        class Scorer:
            async def ascore(self,name,value):
                nonlocal active,maximum
                calls.append(name);active+=1;maximum=max(maximum,active)
                await asyncio.sleep(.01);active-=1
                return SimpleNamespace(value=value)
        jobs={name:(Scorer(),{'name':name,'value':value}) for name,value in
            [('first',.5),('second',.75),('bad',float('nan')),('cached',0.)]}
        item={'cached':1.,'metric_failures':{'first':'old failure'}}
        asyncio.run(score_metric_jobs(jobs,item))
        self.assertEqual(maximum,2);self.assertNotIn('cached',calls)
        self.assertEqual(item['cached'],1.);self.assertEqual(item['second'],.75)
        self.assertEqual(item['metric_failures'],{'bad':'ValueError'})

    def test_malformed_judge_response_keeps_attempt_usage_and_raw_content(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);folder=root/'data/evaluation';folder.mkdir(parents=True)
            (folder/'prices-20261014.json').write_text(json.dumps({'model':'other'}))
            fixture=root/'fixture.json';fixture.write_text(json.dumps({'cases':[
                {'id':'q','question':'q','sources':[],'gold':[]}]}))
            import hashlib
            path=root/'report.json';path.with_suffix('.md').write_text('# Report\n')
            path.write_text(json.dumps({'fixture_path':str(fixture),'fixture_sha256':hashlib.sha256(fixture.read_bytes()).hexdigest(),
                'configuration':{'llm_model':'production','llm_base_url':'https://a.invalid/v1'},
                'answers':[{'case':'q','variant':'closed_book','seconds':1,
                    'result':{'status':'baseline_unreviewed','short_answer':'无法回答','evidence':[],'usage':[]}}],
                'answer_aggregates':{},'acceptance':{}}))
            response={'model':'judge','choices':[{'message':{'content':'invalid JSON'},'finish_reason':'stop'}],
                'usage':{'total_tokens':10}}
            with patch('review_prd_eval.ROOT',root),patch('review_prd_eval.judge_settings',return_value={
                'model':'judge','base_url':'https://a.invalid/v1','api_key':'private-key'}),\
                patch('review_prd_eval.request_json',return_value=response):
                result=review(path,judge=True)
            row=result['answers'][0];attempt=row['judge_attempts'][0]
            self.assertEqual(row['judge_failure'],'JSONDecodeError')
            self.assertEqual(attempt['content'],'invalid JSON')
            self.assertEqual(attempt['usage']['total_tokens'],10)
            self.assertNotIn('private-key',path.read_text())

    def test_judge_rubric_rejects_missing_points_and_preserves_conservative_conflicts(self):
        raw={'correct':True,'complete':True,'citation_supported':True,'reason':'理由',
            'key_points':[{'index':0,'covered':False}],'contradictions':['原文矛盾']}
        scores,conflicts=rubric_scores(raw,1)
        self.assertFalse(scores['correct']);self.assertFalse(scores['complete'])
        self.assertEqual(conflicts,['correct','complete']);self.assertTrue(raw['correct'])
        for points in ([],[{'index':True,'covered':False}],[{'index':1,'covered':False}]):
            with self.assertRaises(ValueError):rubric_scores(raw|{'key_points':points},1)
        scores,_=rubric_scores(raw|{'key_points':{'0':True}},1)
        self.assertTrue(scores['complete'])
        for points in ({'1':True},{'0':1},{'0':True,'1':True}):
            with self.assertRaises(ValueError):rubric_scores(raw|{'key_points':points},1)

    def test_windows_reconstruct_source_without_overlap_or_unicode_damage(self):
        text='# 食谱\n'+('番茄🍅鸡蛋，12.5g；\n'*1000)
        rows=list(fixed_windows(text))
        self.assertGreater(len(rows),2)
        self.assertEqual(''.join(r['content'] for r in rows),text)
        self.assertTrue(all(0<r['token_count']<=512 for r in rows))
        self.assertEqual(rows[0]['start_char'],0)
        self.assertEqual(rows[-1]['end_char'],len(text))
        self.assertTrue(all(a['end_char']==b['start_char'] for a,b in zip(rows,rows[1:])))

    def test_dense_uses_cosine_and_preserves_evidence(self):
        rows=[{'chunk_id':'a','vector':[1.,0.],'content':'a'},{'chunk_id':'b','vector':[0.,2.],'content':'b'}]
        result=dense_search([0.,1.],rows,k=1)
        self.assertEqual(result[0]['chunk_id'],'b');self.assertNotIn('vector',result[0])
        with self.assertRaises(ValueError):dense_search([0.,0.],rows)
        with self.assertRaises(ValueError):dense_search([1.],rows)

    def test_partial_line_citation_requires_exact_char_and_line_offsets(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);source='data/HowToCook/dishes/x.md';path=root/source
            path.parent.mkdir(parents=True);path.write_text('第一行\n番茄鸡蛋\n第三行')
            item={'source':source,'quote':'茄鸡','start_char':5,'end_char':7,'start_line':2,'end_line':2}
            self.assertTrue(exact_evidence({'evidence':[item]},root)['all_exact'])
            for changes in ({'end_char':99},{'start_line':True},{'start_line':1},{'quote':'错误'},{'end_char':None}):
                self.assertFalse(exact_evidence({'evidence':[item|changes]},root)['all_exact'])
            self.assertFalse(exact_evidence({'evidence':[{k:v for k,v in item.items() if k not in ('start_char','end_char')}]},root)['all_exact'])

    def test_metadata_rejects_wrong_title_or_section_even_if_quote_exact(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);source='data/HowToCook/dishes/x.md';path=root/source
            path.parent.mkdir(parents=True);path.write_text('鸡蛋')
            chunk={'source':source,'title':'炒蛋','section':'原料','source_start_line':1,'source_end_line':1}
            item={'source':source,'title':'炒蛋','section':'原料','start_line':1,'end_line':1,'quote':'鸡蛋'}
            self.assertEqual(citation_metadata({'evidence':[item]},[chunk],root)['rate'],1)
            for change in ({'title':'蒸蛋'},{'section':'操作'}):
                self.assertEqual(citation_metadata({'evidence':[item|change]},[chunk],root)['rate'],0)

    def test_baseline_cannot_quote_outside_fixed_window(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);path=root/'x.md';path.write_text('前文番茄鸡蛋后文')
            context={'chunk_id':'w','source':'x.md','content':'番茄鸡蛋','start_char':2}
            def response(quote):return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(
                {'answer':'回答','abstain':False,'evidence':[{'chunk_id':'w','source':'x.md','quote':quote}]})}}]}
            with patch('evaluate_prd.ROOT',root),patch('evaluate_prd.require_llm',return_value={'base_url':'https://example.invalid','model':'test','api_key':'test'}):
                with patch('evaluate_prd.request_json',return_value=response('茄鸡')):
                    evidence=baseline({'question':'q'},[context])['evidence'][0]
                    self.assertEqual((evidence['start_char'],evidence['end_char']),(3,5))
                with patch('evaluate_prd.request_json',return_value=response('前文番茄')):
                    with self.assertRaises(ValueError):baseline({'question':'q'},[context])

    def test_batch_embeddings_reject_duplicate_indices_and_corruption(self):
        with TemporaryDirectory() as directory:
            store=RuntimeStore(Path(directory)/'runtime.sqlite3')
            request=Request('https://example.invalid/embeddings',data=json.dumps({'model':'x','input':['a','b'],'dimensions':2}).encode())
            good={'data':[{'index':1,'embedding':[0,1]},{'index':0,'embedding':[1,0]}]}
            cases=[good,{'data':[{'index':0,'embedding':[1,0]}]*2},
                {'data':[{'index':True,'embedding':[1,0]},{'index':0,'embedding':[1,0]}]},
                {'data':[{'index':0,'embedding':[1,0]},{'index':1,'embedding':[0,0]}]}]
            with patch('service_runtime.RuntimeStore',return_value=store):
                for index,value in enumerate(cases):
                    with patch('service_runtime.urlopen',return_value=io.StringIO(json.dumps(value))):
                        if index==0:self.assertEqual(request_json(request,attempts=1),good)
                        else:
                            with self.assertRaises(ValueError):request_json(request,attempts=1,cache_namespace=str(index))
                    if index:self.assertFalse(any(r.get('outcome')=='cache_hit' for r in store.events_since()))

    def test_sealed_test_rejects_reuse_data_edits_and_code_edits(self):
        with TemporaryDirectory() as directory:
            folder=Path(directory);fixture=folder/'test.json'
            fixture.write_text(json.dumps({'source_hashes':{'new':'hash'},'dev_sources':['old']}))
            with patch('evaluate_prd.code_fingerprint',return_value='code'):
                seal(fixture);record=consume(fixture,folder)
                self.assertEqual(record['state'],'consumed')
                with self.assertRaises(ValueError):consume(fixture,folder)
                with self.assertRaises(ValueError):seal(fixture)
            fixture2=folder/'test2.json';fixture2.write_bytes(fixture.read_bytes())
            with patch('evaluate_prd.code_fingerprint',return_value='code'):seal(fixture2)
            with patch('evaluate_prd.code_fingerprint',return_value='changed'):
                with self.assertRaises(ValueError):consume(fixture2,folder)
            fixture2.write_text('{}')
            with patch('evaluate_prd.code_fingerprint',return_value='code'):
                with self.assertRaises(ValueError):consume(fixture2,folder)

    def test_source_overlap_is_rejected(self):
        with TemporaryDirectory() as directory:
            fixture=Path(directory)/'test.json';fixture.write_text(json.dumps({'source_hashes':{'same':'h'},'dev_sources':['same']}))
            with self.assertRaises(ValueError):seal(fixture)
        with self.assertRaises(ValueError):make_holdout(Path('/unused'),0)

    def test_usage_ledger_never_treats_missing_usage_as_zero_or_stores_prompt(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'ledger.jsonl'
            with patch.dict(os.environ,{'RECIPE_USAGE_LEDGER':str(path)}):
                record_usage('https://example.invalid/v1/embeddings?key=secret','model','embedding',
                    {'data':[{'embedding':[1,2]}],'usage':{'total_tokens':5},'choices':[{'content':'private'}]})
                record_usage('https://example.invalid/v1/rerank','rerank','rerank',error=TimeoutError('private'))
            report=summarize(path);raw=path.read_text()
            self.assertEqual(report['unknown_usage_calls'],1);self.assertIsNone(report['cost'])
            for value in ('private','secret','embedding\": [1'):self.assertNotIn(value,raw)

    def test_judge_cross_provider_does_not_reuse_production_key(self):
        values={'LLM_BASE_URL':'https://provider-a.invalid/v1','LLM_MODEL':'prod','LLM_API_KEY':'production-key'}
        with patch('eval_judge.config',return_value=values),patch.dict(os.environ,{
            'EVAL_JUDGE_BASE_URL':'https://provider-b.invalid/v1','EVAL_JUDGE_MODEL':'judge','EVAL_JUDGE_API_KEY':''}):
            with self.assertRaises(ValueError):judge_settings()
        with patch('eval_judge.config',return_value=values),patch.dict(os.environ,{
            'EVAL_JUDGE_BASE_URL':values['LLM_BASE_URL'],'EVAL_JUDGE_MODEL':'judge','EVAL_JUDGE_API_KEY':''}):
            self.assertEqual(judge_settings()['model'],'judge')

    def test_kappa_does_not_turn_constant_ratings_into_perfect_calibration(self):
        self.assertIsNone(kappa([True]*5,[True]*5))
        self.assertEqual(kappa([True,False],[True,False]),1)
        self.assertEqual(kappa([True,False],[False,True]),-1)

    def test_blind_packet_hides_system_and_blank_reviews_are_not_accepted(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);fixture=root/'fixture.json';fixture.write_text(json.dumps({'cases':[
                {'id':'q','question':'问题','sources':[],'gold':[]}]}))
            import hashlib
            report=root/'report.json';report.write_text(json.dumps({'fixture_path':str(fixture),
                'fixture_sha256':hashlib.sha256(fixture.read_bytes()).hexdigest(),
                'answers':[{'case':'q','type':'unknown','variant':'full_agent','result':{'short_answer':'无法回答','evidence':[]}}]}))
            folder=root/'blind';export_packet([report],folder,limit=1)
            packet=(folder/'packet.json').read_text()
            self.assertNotIn('full_agent',packet);self.assertNotIn('answer_index',packet)
            with self.assertRaises(ValueError):import_reviews(folder)
            with self.assertRaises(ValueError):export_packet([report],folder,limit=1)


if __name__=='__main__':unittest.main()
