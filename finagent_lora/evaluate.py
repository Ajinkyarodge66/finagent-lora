"""Evaluate a local Hugging Face model (optionally with a LoRA adapter) on FinAgent-Bench.

Uses the exact agent loop, prompt and scoring as the API-model runs in finagent-bench,
so base vs fine-tuned vs API models are directly comparable.

  python -m finagent_lora.evaluate --model Qwen/Qwen2.5-1.5B-Instruct --name base
  python -m finagent_lora.evaluate --model Qwen/Qwen2.5-1.5B-Instruct --adapter runs/lora --name lora
"""
from __future__ import annotations

import argparse
import json
import os
import re

import torch

from finagent_bench.agents import LLMAgent
from finagent_bench.backends import TOOLS, to_native_messages, tool_call_to_json
from finagent_bench.evaluate import evaluate_repeated, failure_report, to_markdown
from finagent_bench.tasks import build_tasks

_TOOL_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


def parse_tool_call(text: str) -> str:
    """Turn a model's raw output into the agent's JSON protocol.

    Accepts the Qwen/Hermes `<tool_call>{...}</tool_call>` format, or a bare
    {"name": ..., "arguments": ...} object. Anything else is returned unchanged,
    so the agent can tell the model to call a tool."""
    candidates = [m.group(1) for m in _TOOL_CALL.finditer(text)]
    if not candidates:
        candidates = [text[i:] for i, ch in enumerate(text) if ch == "{"][:1]
    for c in candidates:
        try:
            obj, _ = json.JSONDecoder().raw_decode(c.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "name" in obj:
            args = obj.get("arguments", {})
            return tool_call_to_json({"function": {"name": obj["name"],
                                                   "arguments": args if isinstance(args, str) else json.dumps(args)}})
    return text


def _hf_messages(system: str, messages: list[dict]) -> list[dict]:
    """Agent history -> chat-template messages (tool arguments as dicts, empty content as "")."""
    out = [{"role": "system", "content": system}]
    for m in to_native_messages(messages):
        m = dict(m)
        if m.get("tool_calls"):
            m["content"] = ""
            m["tool_calls"] = [{**c, "function": {**c["function"],
                                                  "arguments": json.loads(c["function"]["arguments"])}}
                               for c in m["tool_calls"]]
        out.append(m)
    return out


def hf_backend(model, tokenizer, max_new_tokens: int = 256):
    device = next(model.parameters()).device

    @torch.no_grad()
    def complete(system: str, messages: list[dict]) -> str:
        ids = tokenizer.apply_chat_template(_hf_messages(system, messages), tools=TOOLS,
                                            add_generation_prompt=True, return_tensors="pt")
        if not isinstance(ids, torch.Tensor):  # newer transformers may return a BatchEncoding
            ids = ids["input_ids"]
        ids = ids.to(device)
        out = model.generate(ids, attention_mask=torch.ones_like(ids), max_new_tokens=max_new_tokens,
                             do_sample=False, pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id)
        text = tokenizer.decode(out[0, ids.shape[1]:], skip_special_tokens=False)
        for stop in (tokenizer.eos_token or "", "<|im_end|>", "<|endoftext|>"):
            if stop:
                text = text.split(stop)[0]
        return parse_tool_call(text)

    complete.native_tools = True  # use the native-tools prompt, same as training data
    return complete


def load(model_name: str, adapter: str | None):
    from transformers import AutoModelForCausalLM, AutoTokenizer

    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32
    tok = AutoTokenizer.from_pretrained(adapter or model_name)
    try:
        model = AutoModelForCausalLM.from_pretrained(model_name, dtype=dtype)
    except TypeError:
        model = AutoModelForCausalLM.from_pretrained(model_name, torch_dtype=dtype)
    if adapter:
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, adapter).merge_and_unload()
    return model.to(device).eval(), tok


def run(model, tok, name: str, outdir: str = "results", limit: int | None = None,
        seed: int = 7, max_steps: int = 10) -> dict:
    tasks = build_tasks(seed)[:limit]
    agent = LLMAgent(hf_backend(model, tok), name=name, max_steps=max_steps)
    result = evaluate_repeated(agent, tasks, 1)  # greedy decoding is deterministic
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, f"{name}.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=str)
    with open(os.path.join(outdir, f"{name}_failures.md"), "w", encoding="utf-8") as f:
        f.write(failure_report(result))
    print(to_markdown([result]))
    return result


def main() -> None:
    p = argparse.ArgumentParser(description="Evaluate a local model on FinAgent-Bench")
    p.add_argument("--model", default="Qwen/Qwen2.5-1.5B-Instruct")
    p.add_argument("--adapter", help="path to a LoRA adapter from finagent_lora.train")
    p.add_argument("--name", required=True, help="label for the results files")
    p.add_argument("--limit", type=int)
    p.add_argument("--outdir", default="results")
    a = p.parse_args()
    model, tok = load(a.model, a.adapter)
    run(model, tok, a.name, a.outdir, a.limit)


if __name__ == "__main__":
    main()
