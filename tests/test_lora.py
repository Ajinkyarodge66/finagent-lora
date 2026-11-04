import json

import pytest

from finagent_bench.tasks import build_tasks
from finagent_lora.data import SYSTEM_PROMPT, build_dataset, check_no_leakage, expert_trajectory


# ---------------- data ----------------

def test_trajectories_have_expected_shape():
    rows = build_dataset(n_seeds=2)
    assert len(rows) == 40
    for r in rows:
        m = r["messages"]
        assert m[0] == {"role": "system", "content": SYSTEM_PROMPT} and m[1]["role"] == "user"
        roles = [x["role"] for x in m[2:]]
        assert roles[-1] == "assistant" and roles[:-1] == ["assistant", "tool"] * ((len(roles) - 1) // 2)
        assert m[-1]["tool_calls"][0]["function"]["name"] == "final_answer"
        names = [x["tool_calls"][0]["function"]["name"] for x in m if x.get("tool_calls")]
        assert names[0] == "list_docs" and "read_doc" in names
        if r["category"] != "lookup":
            assert "calculator" in names  # arithmetic is always done with the tool


def test_injected_examples_still_answer_correctly():
    t = next(t for t in build_tasks(1000, injection_rate=1.0))
    final = expert_trajectory(t)[-1]["tool_calls"][0]["function"]["arguments"]["value"]
    assert final == pytest.approx(t.answer, rel=0.01) and final != 0


def test_no_leakage_and_leak_is_detected():
    check_no_leakage(build_dataset(n_seeds=3))
    leaked = [{"id": "x", "messages": expert_trajectory(build_tasks(7)[0])}]
    with pytest.raises(AssertionError):
        check_no_leakage(leaked)


def test_holdout_category():
    assert not any(r["category"] == "multi_hop" for r in build_dataset(n_seeds=2, exclude_categories=("multi_hop",)))


# ---------------- parsing ----------------

def test_parse_tool_call_formats():
    from finagent_lora.evaluate import parse_tool_call
    q = '<tool_call>\n{"name": "read_doc", "arguments": {"arg": "a.txt"}}\n</tool_call>'
    assert json.loads(parse_tool_call(q)) == {"tool": "read_doc", "arg": "a.txt"}
    bare = 'ok {"name": "final_answer", "arguments": {"value": 3.5}}'
    assert json.loads(parse_tool_call(bare)) == {"final_answer": 3.5}
    assert parse_tool_call("I think the answer is 5") == "I think the answer is 5"


def test_history_conversion_uses_dict_arguments():
    from finagent_lora.evaluate import _hf_messages
    msgs = _hf_messages("sys", [{"role": "user", "content": "q"},
                                {"role": "assistant", "content": '{"tool": "read_doc", "arg": "a"}'},
                                {"role": "user", "content": "Observation: text"}])
    assert msgs[2]["tool_calls"][0]["function"]["arguments"] == {"arg": "a"}
    assert msgs[3] == {"role": "tool", "tool_call_id": msgs[2]["tool_calls"][0]["id"], "content": "text"}


# ---------------- end to end on a tiny CPU model ----------------

@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    pytest.importorskip("transformers")
    pytest.importorskip("peft")
    from finagent_lora.tiny import build_tiny, corpus_from_jsonl

    d = tmp_path_factory.mktemp("tiny")
    data = d / "train.jsonl"
    with open(data, "w") as f:
        for r in build_dataset(n_seeds=1):
            f.write(json.dumps(r) + "\n")
    return build_tiny(str(d / "model"), corpus_from_jsonl(str(data))), str(data), d


def test_mask_trains_only_on_assistant_actions(tiny):
    from transformers import AutoTokenizer

    from finagent_lora.train import IGNORE, assistant_token_mask, hf_tools
    model_dir, data, _ = tiny
    tok = AutoTokenizer.from_pretrained(model_dir)
    msgs = json.loads(open(data).readline())["messages"]
    ids, labels = assistant_token_mask(tok, msgs, hf_tools())
    trained = tok.decode([t for t, l in zip(ids, labels) if l != IGNORE])
    masked = tok.decode([t for t, l in zip(ids, labels) if l == IGNORE])
    assert "final_answer" in trained and "<tool_call>" in trained and "<|im_end|>" in trained
    assert "ANNUAL REPORT" not in trained and "ANNUAL REPORT" in masked  # documents are not learned
    assert msgs[1]["content"] not in trained  # nor is the question


def test_train_then_evaluate_end_to_end(tiny):
    from finagent_lora.evaluate import load, run
    from finagent_lora.train import train
    model_dir, data, d = tiny
    meta = train(model_dir, data, str(d / "adapter"), lr=5e-3, batch=2, grad_accum=1, rank=4, max_steps=12)
    losses = [l for _, l in meta["loss_curve"]]
    assert losses and losses[-1] < losses[0]  # it learns something
    model, tok = load(model_dir, str(d / "adapter"))
    r = run(model, tok, "tiny", outdir=str(d / "results"), limit=2, max_steps=3)
    assert r["summary"]["n_tasks"] == 2
    assert (d / "results" / "tiny_failures.md").exists()
