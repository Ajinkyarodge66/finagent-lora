"""Build supervised fine-tuning data from FinAgent-Bench.

Each example is a complete, verified tool-calling trajectory in OpenAI chat format:
list_docs -> read_doc (each filing) -> calculator (if arithmetic) -> final_answer.
Documents with prompt injections are included, so the model learns by example that
instructions inside documents are data to ignore.

Leakage control: the benchmark's test set is seed 7. Training seeds start at 1000,
and `check_no_leakage` asserts no training (question, answer) pair appears in the test set.
"""
from __future__ import annotations

import argparse
import json
import random

from finagent_bench.agents import NATIVE_TOOLS_PROMPT, SYSTEM, BaselineAgent
from finagent_bench.backends import TOOLS
from finagent_bench.tasks import Task, build_tasks
from finagent_bench.tools import Toolbox

TEST_SEED = 7
SYSTEM_PROMPT = SYSTEM + NATIVE_TOOLS_PROMPT  # identical to what the agent sees at eval time


def _fmt_answer(x: float) -> float:
    return round(float(x), 4)


def expert_trajectory(task: Task) -> list[dict]:
    """Solve `task` with the rule-based expert and record it as chat messages.

    Tool results are produced by actually executing each call, and the final answer
    is checked against ground truth, so a bad example can never enter the dataset."""
    res = BaselineAgent().run(task)
    if res.answer is None or abs(res.answer - task.answer) > max(abs(task.answer) * task.tolerance, 1e-6):
        raise ValueError(f"expert failed on {task.id}: {res.answer} vs {task.answer}")

    tb = Toolbox(task.docs)
    msgs: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": task.question}]
    for i, call in enumerate(res.trace):
        name = call["tool"]
        args = {} if name == "list_docs" else {"arg": call["arg"]}
        out = tb.list_docs() if name == "list_docs" else getattr(tb, name)(call["arg"])
        cid = f"call_{i}"
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}]})
        # same shape the agent sends at eval time (finagent_bench.backends.to_native_messages)
        msgs.append({"role": "tool", "tool_call_id": cid, "content": str(out)})
    msgs.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": f"call_{len(res.trace)}", "type": "function",
         "function": {"name": "final_answer", "arguments": {"value": _fmt_answer(res.answer)}}}]})
    return msgs


def build_dataset(
    n_seeds: int = 20,
    first_seed: int = 1000,
    injection_rate: float = 0.3,
    exclude_categories: tuple[str, ...] = (),
) -> list[dict]:
    rows = []
    for seed in range(first_seed, first_seed + n_seeds):
        for t in build_tasks(seed, injection_rate):
            if t.category in exclude_categories:
                continue
            rows.append({"id": f"s{seed}-{t.id}", "category": t.category, "injected": t.injected,
                         "messages": expert_trajectory(t)})
    random.Random(0).shuffle(rows)
    return rows


def check_no_leakage(rows: list[dict], test_seed: int = TEST_SEED) -> None:
    test = {(t.question, t.answer) for t in build_tasks(test_seed)}
    for r in rows:
        q = r["messages"][1]["content"]
        a = r["messages"][-1]["tool_calls"][0]["function"]["arguments"]["value"]
        if (q, a) in test:
            raise AssertionError(f"training example {r['id']} duplicates a test task")


def main() -> None:
    p = argparse.ArgumentParser(description="Build SFT data from FinAgent-Bench")
    p.add_argument("--out", default="data/train.jsonl")
    p.add_argument("--n-seeds", type=int, default=20)
    p.add_argument("--injection-rate", type=float, default=0.3)
    p.add_argument("--exclude", nargs="*", default=[], help="categories to hold out, e.g. multi_hop")
    a = p.parse_args()
    rows = build_dataset(a.n_seeds, injection_rate=a.injection_rate, exclude_categories=tuple(a.exclude))
    check_no_leakage(rows)
    import os
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    cats = {c: sum(r["category"] == c for r in rows) for c in sorted({r["category"] for r in rows})}
    print(f"{len(rows)} examples -> {a.out}  {cats}  injected={sum(r['injected'] for r in rows)}")


if __name__ == "__main__":
    main()

__all__ = ["TOOLS", "build_dataset", "expert_trajectory", "check_no_leakage", "SYSTEM_PROMPT"]
