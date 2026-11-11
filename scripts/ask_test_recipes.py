"""一次输入问题，串联现有检索、重排和回答生成脚本。"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import os
import time
from service_runtime import remaining

from check_embedding import ROOT

from rag_dataset import FOLDER


class StageFailure(RuntimeError):
    pass


def run_stage(script, argument, output_folder, *, fallback_reserve=10):
    if script!='rerank_test_recipes.py':return _run_stage(script,argument,output_folder,fallback_reserve=fallback_reserve)
    import tempfile
    from stage_diagnostics import collect
    started=time.monotonic();state={'budget':remaining(510)}
    with tempfile.TemporaryDirectory(prefix='recipe-stage-') as folder:
        path=Path(folder)/'progress.jsonl'
        try:
            result_path,result=_run_stage(script,argument,output_folder,diagnostic_path=path,diagnostic_state=state)
        except Exception as error:
            error.stage_diagnostics=collect(path,started,state['budget'],error=error)
            raise
        result['_stage_diagnostics']=collect(path,started,state['budget'])
        return result_path,result


def _run_stage(script, argument, output_folder,diagnostic_path=None,diagnostic_state=None,*,fallback_reserve=10):
    # 参数通过列表传入，问题中的引号或命令字符不会作为命令执行。
    wait = remaining(510)
    reserve = 0
    if script == 'generate_test_answer.py':
        from qa_execution import execution_profile
        if type(fallback_reserve) not in (int,float) or not 0<=fallback_reserve<=10:raise ValueError('invalid_fallback_reserve')
        if execution_profile() == 'balanced' and wait > fallback_reserve+5:
            reserve = fallback_reserve
            wait -= reserve
    from qa_execution import execution_profile
    if execution_profile()=='balanced':
        cap={'rerank_test_recipes.py':12,'generate_test_answer.py':40}.get(script)
        if cap:wait=min(wait,cap)
    env = dict(os.environ, RECIPE_DEADLINE_EPOCH=str(time.time()+wait),
               RECIPE_QA_STAGE_RESERVED=str(reserve))
    if diagnostic_path is not None:env['RECIPE_STAGE_DIAGNOSTICS']=str(diagnostic_path)
    if diagnostic_state is not None:diagnostic_state['budget']=wait
    from cookbuddy.ltm.guard import subprocess_env
    env.update(subprocess_env())
    from search_filters import current_filters
    env['RECIPE_SEARCH_FILTERS']=json.dumps(current_filters(),ensure_ascii=False)
    # 生成进程的硬超时也预留降级时间，不能只依赖HTTP读取后才检查deadline。
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / script), argument],
                            cwd=ROOT, capture_output=True, text=True, timeout=wait, env=env)
    if result.returncode:
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        raise StageFailure(f"{script} 未完成，后续步骤已停止。")
    lines = [line for line in result.stdout.splitlines() if line.startswith("完整结果：")]
    if not lines:
        raise ValueError("该步骤没有返回结果文件。")
    preview = Path(lines[-1].removeprefix("完整结果：").strip()).resolve()
    if preview.parent != (FOLDER / output_folder).resolve() or preview.suffix != ".md":
        raise ValueError("步骤返回了非预期的结果路径。")
    data_path = preview.with_suffix(".json")
    if not preview.is_file() or not data_path.is_file():
        raise ValueError("该步骤的结果文件不完整。")
    return data_path, json.loads(data_path.read_text(encoding="utf-8"))


def main():
    question = " ".join(sys.argv[1:]).strip() if len(sys.argv) > 1 else input("请输入菜谱问题：").strip()
    if not question:
        raise ValueError("问题不能为空。")
    print("1/3 正在生成问题向量并检索菜谱……", flush=True)
    search_path, search = run_stage("search_test_recipes.py", question, "search-results")
    if search["question"] != question:
        raise ValueError("检索结果的问题不一致。")
    print("2/3 正在重排检索候选……", flush=True)
    rerank_path, rerank = run_stage("rerank_test_recipes.py", str(search_path), "rerank-results")
    if rerank["question"] != question or rerank["source_search"] != str(search_path.relative_to(ROOT)):
        raise ValueError("重排结果与本次检索不一致。")
    print("3/3 正在生成回答并核对引用与原文依据……", flush=True)
    answer_path, answer = run_stage("generate_test_answer.py", str(rerank_path), "answer-results")
    if answer["question"] != question or answer["source_rerank"] != str(rerank_path.relative_to(ROOT)):
        raise ValueError("回答结果与本次重排不一致。")
    output = FOLDER / "qa-results"
    output.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex
    record = {"question": question, "created_at": datetime.now(timezone.utc).isoformat(),
              "search_result": str(search_path.relative_to(ROOT)),
              "rerank_result": str(rerank_path.relative_to(ROOT)),
              "answer_result": str(answer_path.relative_to(ROOT)),
              "models": {"embedding": search["model"], "rerank": rerank["rerank_model"],
                         "generation": answer["generation_model"]},
              "short_answer": answer["short_answer"], "answer": answer["answer"]}
    record["status"] = answer.get("status", "verified")
    (output / f"{stamp}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    preview = [f"# 菜谱问答全流程测试\n\n问题：{question}\n\n{answer['answer']}\n\n"
               "## 流程记录\n\n",
               f"- [向量检索结果]({search_path.with_suffix('.md')})\n",
               f"- [重排结果]({rerank_path.with_suffix('.md')})\n",
               f"- [回答生成结果]({answer_path.with_suffix('.md')})\n\n",
               "| 原排名 | 重排排名 | 菜名 / 章节 |\n|---|---|---|\n"]
    for row in rerank["results"]:
        preview.append(f"| {row['original_rank']} | {row['rerank_rank']} | {row['title']} / {row['section']} |\n")
    path = output / f"{stamp}.md"
    path.write_text("".join(preview), encoding="utf-8")
    print(f"\n{answer['answer']}\n完整流程记录：{path}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.TimeoutExpired:
        print("当前步骤超时，流程已停止；此前完成的步骤结果仍保留。", file=sys.stderr)
        sys.exit(1)
    except (ValueError, KeyError, TypeError, OSError):
        print("问答流程未完成，请检查上方提示、服务状态和配置；此前完成的步骤结果仍保留。", file=sys.stderr)
        sys.exit(1)
