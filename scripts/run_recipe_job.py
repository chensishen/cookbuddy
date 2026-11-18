"""独立进程执行任务；进程组可取消，所有子阶段继承总 deadline。"""
import json
import os
import sqlite3
import time
from job_queue import JobQueue
from recipe_agent import execute, save_run
from service_runtime import request_budget
from conversation_memory import contextual_question


def export_record(result):
    try:
        path=save_run(result)
        return str(path.relative_to(path.parents[3]))
    except (OSError,ValueError,KeyError,TypeError) as error:
        # Answer persistence has already succeeded. Artifact export is optional
        # and cannot replace that answer with a failed conversation.
        result['record_export_error']=type(error).__name__
        return None


def main():
    started=time.monotonic()
    queue = JobQueue()
    ident = os.environ['RECIPE_JOB_ID']
    with queue.connect() as db:
        row = db.execute('SELECT payload,deadline,created,started FROM jobs WHERE id=?', (ident,)).fetchone()
    try:
        with request_budget(deadline=row[1], cancelled=lambda:queue.get(ident)['state']=='cancelled'):
            payload=json.loads(row[0])
            if os.environ.get('RECIPE_LOCAL_ONLY','0')=='1' and payload.get('mode','agent') not in ('recipe','images'):
                raise ValueError('本地模式禁止执行模型任务')
            if payload.get('mode')=='auto':
                from cookbuddy.pipeline import run
                result=run(payload['user_id'],payload['conversation_id'],ident,payload['question'],payload['memory_epoch'],lambda message:queue.progress(ident,message))
                queue.progress(ident,'正在保存对话与整理菜谱卡片')
                result['queue_seconds']=round(row[3]-row[2],3)
                record=export_record(result)
                queue.finish(ident,'done',result,record)
                snapshot=queue.get(ident)
                result['trace']={**result.get('trace',{}),**{k:v for k,v in snapshot.items() if k!='result'}}
                from cookbuddy.store import Store
                try:Store().update_record(payload['user_id'],ident,result)
                except Exception:pass
                return
            history = payload.pop('history', [])
            memories = payload.pop('memories', [])
            original = payload['question']
            uses_memory = payload.get('mode','agent') not in ('recipe','images') and bool(history or memories)
            if uses_memory:
                queue.progress(ident, '正在结合会话与已保存偏好理解问题')
                payload['question'] = contextual_question(original, history, memories)
            result = execute(**payload, progress=lambda message:queue.progress(ident,message))
            result['question'] = original
            result['context_question'] = payload['question']
            result['memory_used'] = {'recent_messages':len(history) if uses_memory else 0, 'preferences':len(memories) if uses_memory else 0}
            queue.progress(ident, '正在整理菜谱卡片与来源')
        result['queue_seconds'] = round(row[3]-row[2],3)
        record = export_record(result)
        queue.finish(ident, 'done', result, record)
    except Exception as error:
        from pathlib import Path
        from src.capabilities import degradation
        metadata=degradation({},[],ident,tool_degraded=True,tool_failed=True)
        frames=[];frame=error.__traceback__
        while frame:
            frames.append({'module':Path(frame.tb_frame.f_code.co_filename).name,'function':frame.tb_frame.f_code.co_name,'line':frame.tb_lineno})
            frame=frame.tb_next
        failed={'question':payload.get('question','') if 'payload' in locals() else '', 'mode':payload.get('mode','agent') if 'payload' in locals() else 'agent', 'status':'agent_unavailable','outputs':[],'steps':[], 'elapsed_seconds':time.monotonic()-started,'error':'本次查询未完成，请稍后重试。','error_code':type(error).__name__,**metadata,
                'trace':{'request_id':ident,'outcome':'failed','error_code':type(error).__name__,'frames':frames[-4:]}}
        queue.finish(ident,'failed',result=failed,progress=failed['service_notice'])
        if 'payload' in locals() and payload.get('mode')=='auto':
            snapshot=queue.get(ident)
            failed['trace'].update({k:v for k,v in snapshot.items() if k!='result'})
            try:
                from cookbuddy.store import Store
                Store().append(payload['user_id'],payload['conversation_id'],ident,'assistant',failed['error'],failed)
            except Exception:pass


if __name__ == '__main__':
    main()
