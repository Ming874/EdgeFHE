"""LOCAL ONLY: public summary of the model zoo for the frontend (no private data; metrics from held-out public test)."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent; REC = HERE.parent
WEB = REC / 'frontend_showcase_20261024/web/src/model-zoo.json'
# ZOO19 v2 (boot 5a1163a9): all 19 models on real PL + two-machine mTLS, frozen v2 reviewer PASS
BOARD = REC / 'board/zoo19v2-fcc9daa9d1954332b10b7b7658d1490f/reviewed/REVIEW.json'
TITLES = {'ai4i-failure-logreg-v1': '工業設備故障預測', 'diabetes-progression-ridge-v1': '糖尿病一年後進展', 'diabetes-symptom-logreg-v1': '糖尿病早期症狀風險'}


def main():
    cards = json.loads((HERE / 'model-cards.zoo.json').read_text(encoding='utf-8'))['models']
    train = {m['model_id']: m for m in json.loads((HERE / 'models_v1/ZOO_TRAINING.json').read_text(encoding='utf-8'))['models']}
    native = json.loads((HERE / 'native_qa_r2/ACCEPTANCE.json').read_text(encoding='utf-8'))
    admission = json.loads((HERE / 'MODEL_CARD_ADMISSION_ZOO.json').read_text(encoding='utf-8'))
    board = json.loads(BOARD.read_text(encoding='utf-8'))
    assert native['status'].startswith('ZOO_NATIVE_CPP_CKKS_CPU_SPEC_PASS') and admission['status'] == 'MODEL_CARD_ADMISSION_PASS'
    assert board['status'] == 'ZOO19_ORIGINALS_INDEPENDENT_REVIEW_PASS' and all(board['gates'].values())
    on_board = board['notes']['per_model']
    err = {}
    for c in native['cases']: err.setdefault(c['model'], {})[c['backend']] = c['max_observed_error']
    rows = []
    for c in cards:
        t = train.get(c['id']); facts = admission['models'][c['id']]['facts']
        metric = None
        if t:
            m = t['test_metrics']; metric = dict(name='R²', value=m['R2']) if 'R2' in m else dict(name='AUROC', value=m['AUROC'])
        rows.append(dict(id=c['id'], domain=c['domain'], category=c['category'], title=c.get('title') or TITLES[c['id']],
                         task='regression' if c['thresholdSpace'] == 'affine-output' or 'ridge' in c['id'] else 'classification',
                         features=len(c['fields']), featureNames=[f['name'] for f in c['fields']], testMetric=metric,
                         maxErrorCpu=err[c['id']]['cpu'], maxErrorSpec=err[c['id']]['spec'], gateOverSigma=facts['gate_over_sigma'],
                         crtHeadroomBits=facts['crt_headroom_bits'], dataset=c['dataset'], sourceUrl=c['sourceUrl'], precisionGate=facts['precision_gate'],
                         boardMaxErrorCpu=on_board[c['id']]['cpu']['max_error'] if c['id'] in on_board else None,
                         boardMaxErrorPl=on_board[c['id']]['pl']['max_error'] if c['id'] in on_board else None,
                         boardPlRecordsPerS=round(on_board[c['id']]['pl']['records_per_s']) if c['id'] in on_board else None,
                         status='BOARD_ACCEPTED' if c['id'] in on_board else 'LOCAL_CPP_CKKS_VERIFIED_NOT_ON_BOARD'))
    out = dict(schema='edgefhe.model-zoo.v1', generated='2026-10-28', models=rows,
               board=dict(stage='ZOO19_MODELS_5A11_V2', boot='5a1163a9', review=board['status'], connections=board['notes']['connections'],
                          actual_DMA=board['notes']['actual_DMA'], link=board['notes']['PC_link']),
               scope='公開資料研究模型；本機 C++ CKKS（CPU 與 PL 逐位元規格）驗證，並在 KV260 真實 PL 與兩機 mTLS 各跑一個 10 秒功能窗（非效能宣稱）。非臨床、金融或工業部署核准。')
    WEB.write_text(json.dumps(out, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print('ZOO_SUMMARY', len(rows), 'domains', len({r['domain'] for r in rows}))


if __name__ == '__main__':
    main()
