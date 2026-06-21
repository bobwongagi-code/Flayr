"""全量 baseline 大跑：剩余 15 条走完整流程(Step-0→事实→stage2 打分)。
脱离式跑(nohup setsid)，进度写 runs/_fullrun_status.json，断点续跑(已出今日结果则跳过)。"""
import sys, json, time, os; sys.path.insert(0,'scripts')
from types import SimpleNamespace
from pathlib import Path
from flayr_core.llm.pipeline import run_large_model_analysis
from flayr_core.prompt import write_analysis_input

SAMPLES=['are_xie','tashadiyana','bluetoothwanju','skincare','wukoubo-c0','wukoubo-c1',
         'youkoubo-c0','youkoubo-c1','youkoubo-c2','carslan-b1','colorkey-b0','colorkey-b1',
         'paint','simplus','mmx']
if len(sys.argv)>1: SAMPLES=sys.argv[1:]  # 只跑指定几条（前台一条条跑用）
args=SimpleNamespace(llm_model='qwen3.5-omni-plus',
  llm_api_url='https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
  llm_dry_run=False, llm_image_limit=12, llm_include_images=True,
  llm_api_key_env='OPENAI_API_KEY', llm_api_key_keychain_service='VidLingo.Qwen', llm_api_key_keychain_account='API_KEY')
status_path=Path('runs/_fullrun_status.json'); status={}
def save(): status_path.write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf-8')
for s in SAMPLES:
    rd=Path('runs')/f'sample-{s}'; t0=time.time()
    res=rd/'analysis_result.json'  # 断点续跑：48 小时内出的结果算已完成（跨天也不误判）
    if res.is_file() and time.time()-res.stat().st_mtime < 48*3600:
        status[s]={'state':'skip-done'}; save(); continue
    status[s]={'state':'running','start':time.strftime('%H:%M:%S')}; save()
    try:
        a=json.loads((rd/'analysis.json').read_text(encoding='utf-8'))
        aip=write_analysis_input(rd,a)
        run_large_model_analysis(args,a,aip,rd)
        status[s]={'state':'done','mins':round((time.time()-t0)/60,1)}
    except Exception as e:
        status[s]={'state':'failed','err':str(e)[:200]}
    save()
status['_ALL_DONE']=time.strftime('%Y-%m-%d %H:%M:%S'); save()
