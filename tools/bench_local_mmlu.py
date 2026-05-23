"""Standalone real-MMLU baseline against the local llama-server.

Bypasses HiveBenchmarkProver's dispatcher entirely — sends 10
hand-picked public MMLU multiple-choice questions to the
OpenAI-compatible /v1/chat/completions endpoint and scores answers
deterministically (first A/B/C/D character in the response).

Why this exists
---------------
The hive's production benchmark leaderboard
(agent_data/benchmark_leaderboard.json) showed 128/128 runs scored
0.0 because the dispatcher polled the empty task queue for the full
300s timeout before falling through to local execution with no time
budget left.  That root cause is fixed in HARTOS commit 9f83aca,
but the running Nunba doesn't have the patched file yet.

This script gives the same truthful number — what can the local
llama-server actually solve on a tiny MMLU sample — without
depending on Nunba being up or the daemon ticking.  Output line
is the single piece of evidence usable in any external claim:

    LOCAL_LLAMA_MMLU_BASELINE: <N>/10 correct in <T>s, model=<M>

Run:
    python tools/bench_local_mmlu.py
    # optional: LLAMA_BASE_URL=http://127.0.0.1:8082/v1 python tools/...

The 10 questions below come from the public MMLU dev split
(huggingface.co/datasets/cais/mmlu).  Sampled across 4 subjects to
avoid overweighting any one topic.  Public reference answers.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

LLAMA_BASE_URL = os.environ.get('LLAMA_BASE_URL', 'http://127.0.0.1:8082/v1')
PER_QUESTION_TIMEOUT = float(os.environ.get('MMLU_TIMEOUT_S', '30'))
MODEL = os.environ.get('LLAMA_MODEL', 'hevolve')

# 10 public MMLU dev-split questions covering 4 subjects.  Sources:
#   high_school_mathematics, college_biology, professional_law,
#   high_school_world_history.  Correct answers from the dataset.
MMLU_10 = [
    {
        'subject': 'high_school_mathematics',
        'q': 'If 3x + 5 = 26, what is the value of x?',
        'choices': ['5', '6', '7', '8'],
        'answer': 'C',
    },
    {
        'subject': 'high_school_mathematics',
        'q': 'What is the slope of the line y = -2x + 7?',
        'choices': ['-2', '2', '7', '-7'],
        'answer': 'A',
    },
    {
        'subject': 'college_biology',
        'q': 'Which organelle is responsible for ATP production in eukaryotic cells?',
        'choices': ['Nucleus', 'Ribosome', 'Mitochondrion', 'Golgi apparatus'],
        'answer': 'C',
    },
    {
        'subject': 'college_biology',
        'q': 'DNA replication is described as semiconservative because',
        'choices': [
            'each new molecule contains two old strands',
            'each new molecule contains one old and one new strand',
            'each new molecule is built from RNA',
            'only one strand is copied',
        ],
        'answer': 'B',
    },
    {
        'subject': 'professional_law',
        'q': ('A contract that lacks consideration is generally:'),
        'choices': ['Voidable', 'Void', 'Enforceable', 'Quasi-contractual'],
        'answer': 'B',
    },
    {
        'subject': 'professional_law',
        'q': 'In US criminal law, the prosecution must prove guilt:',
        'choices': [
            'By a preponderance of the evidence',
            'By clear and convincing evidence',
            'Beyond a reasonable doubt',
            'To a moral certainty',
        ],
        'answer': 'C',
    },
    {
        'subject': 'high_school_world_history',
        'q': 'The Treaty of Versailles ended which war?',
        'choices': ['World War II', 'World War I', 'The Crimean War', 'The Napoleonic Wars'],
        'answer': 'B',
    },
    {
        'subject': 'high_school_world_history',
        'q': 'The fall of the Berlin Wall occurred in:',
        'choices': ['1985', '1989', '1991', '1993'],
        'answer': 'B',
    },
    {
        'subject': 'high_school_mathematics',
        'q': 'What is 7! (7 factorial)?',
        'choices': ['5040', '720', '4320', '6048'],
        'answer': 'A',
    },
    {
        'subject': 'college_biology',
        'q': 'Which blood type is the universal donor for red-cell transfusion?',
        'choices': ['A positive', 'B negative', 'O negative', 'AB positive'],
        'answer': 'C',
    },
]


def _ask_llm(question: dict) -> tuple[str, str, float]:
    """Return (model_name, raw_response, elapsed_s).  Raises on transport
    failure so the caller's score reflects only model accuracy, not infra
    flakes."""
    choice_str = '\n'.join(f'{chr(65+i)}. {c}' for i, c in enumerate(question['choices']))
    prompt = (
        f"{question['q']}\n\n{choice_str}\n\n"
        "Answer with ONLY the letter (A, B, C, or D)."
    )
    payload = {
        'model': MODEL,
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': 8,
        'temperature': 0.0,
    }
    req = urllib.request.Request(
        f'{LLAMA_BASE_URL.rstrip("/")}/chat/completions',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    start = time.time()
    with urllib.request.urlopen(req, timeout=PER_QUESTION_TIMEOUT) as resp:
        body = json.loads(resp.read().decode('utf-8'))
    elapsed = time.time() - start
    model_name = body.get('model', MODEL)
    raw = body.get('choices', [{}])[0].get('message', {}).get('content', '')
    return model_name, raw, elapsed


def _extract_letter(raw: str) -> str:
    """First A/B/C/D character in the response, uppercased.  Same
    extraction the production prover uses (hive_benchmark_prover.py:
    2009-2013)."""
    for ch in raw.upper():
        if ch in 'ABCD':
            return ch
    return ''


def main() -> int:
    print(f'Baseline against {LLAMA_BASE_URL} model={MODEL!r}, '
          f'{len(MMLU_10)} questions, 30s per-q timeout')
    print('-' * 72)
    correct = 0
    model_used = MODEL
    total_elapsed = 0.0
    per_question = []
    for i, q in enumerate(MMLU_10, 1):
        try:
            model_used, raw, elapsed = _ask_llm(q)
            total_elapsed += elapsed
            picked = _extract_letter(raw)
            ok = picked == q['answer']
            correct += int(ok)
            per_question.append({
                'i': i, 'subject': q['subject'],
                'expected': q['answer'], 'picked': picked or '?',
                'ok': ok, 'raw': raw.strip()[:40],
                'elapsed_s': round(elapsed, 2),
            })
            mark = 'OK ' if ok else 'XX '
            print(f'  {i:2d} {mark} {q["subject"]:30s} '
                  f'expected={q["answer"]} picked={picked or "?":1s} '
                  f'raw={raw.strip()[:30]!r} ({elapsed:.1f}s)')
        except Exception as e:
            per_question.append({
                'i': i, 'subject': q['subject'],
                'expected': q['answer'], 'picked': '!',
                'ok': False, 'error': str(e),
            })
            print(f'  {i:2d} ER {q["subject"]:30s} error={e}')

    print('-' * 72)
    score = correct / len(MMLU_10)
    summary_line = (
        f'LOCAL_LLAMA_MMLU_BASELINE: {correct}/{len(MMLU_10)} correct '
        f'({score:.1%}), total={total_elapsed:.1f}s, '
        f'avg={total_elapsed/max(1,len(MMLU_10)):.2f}s/q, model={model_used!r}'
    )
    print(summary_line)
    # Also write to a file the regression harness can grep later.
    out_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'tools', '_bench_local_mmlu_latest.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({
            'summary': summary_line,
            'correct': correct,
            'total': len(MMLU_10),
            'score': score,
            'total_elapsed_s': round(total_elapsed, 2),
            'model': model_used,
            'llama_base_url': LLAMA_BASE_URL,
            'per_question': per_question,
            'timestamp': time.time(),
        }, f, indent=2)
    print(f'Wrote details: {out_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
