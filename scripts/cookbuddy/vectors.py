import json
from urllib.request import Request
from pooled_http import urlopen
from .models import settings, embedding_spec
from .store import valid_id


class VectorStore:
    def __init__(self):
        cfg=settings();spec,version=embedding_spec()
        self.base=cfg.get('COOKBUDDY_MILVUS_URL','http://127.0.0.1:19532').rstrip('/')+'/v2/vectordb/'
        self.collection='cookbuddy_memory_'+version
        self.dim=int(spec['EMBEDDING_DIMENSIONS'])
    def call(self,operation,payload):
        request=Request(self.base+operation,data=json.dumps(payload,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'})
        with urlopen(request,timeout=5) as response:data=json.load(response)
        if data.get('code')!=0:raise RuntimeError('memory_vector_operation_failed')
        return data.get('data')
    def ensure(self):
        if self.collection not in self.call('collections/list',{}):
            self.call('collections/create',{'collectionName':self.collection,'schema':{'autoId':False,'enabledDynamicField':False,'fields':[
                {'fieldName':'id','dataType':'VarChar','isPrimary':True,'elementTypeParams':{'max_length':64}},
                {'fieldName':'user_id','dataType':'VarChar','elementTypeParams':{'max_length':64}},
                {'fieldName':'version','dataType':'Int64'},
                {'fieldName':'vector','dataType':'FloatVector','elementTypeParams':{'dim':str(self.dim)}}]},
                'indexParams':[{'fieldName':'vector','metricType':'COSINE','params':{'index_type':'AUTOINDEX'}}],
                'params':{'consistencyLevel':'Strong'}})
        self.call('collections/load',{'collectionName':self.collection})
    def upsert(self,row,vector):
        if len(vector)!=self.dim or not valid_id(row['id']) or not valid_id(row['user_id']):raise ValueError('invalid_vector_record')
        self.call('entities/upsert',{'collectionName':self.collection,'data':[{'id':row['id'],'user_id':row['user_id'],'version':row['version'],'vector':vector}]})
    def delete(self,uid,mid):
        if not valid_id(uid) or not valid_id(mid):raise ValueError('invalid_vector_id')
        self.call('entities/delete',{'collectionName':self.collection,'filter':f'user_id == "{uid}" and id == "{mid}"'})
    def search(self,uid,vector,limit=24):
        if not valid_id(uid) or len(vector)!=self.dim:raise ValueError('invalid_vector_query')
        rows=self.call('entities/search',{'collectionName':self.collection,'data':[vector],'annsField':'vector','filter':f'user_id == "{uid}"',
            'limit':limit,'outputFields':['id','user_id','version'],'consistencyLevel':'Strong'})
        return rows
