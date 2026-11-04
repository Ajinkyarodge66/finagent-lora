"""LoRA fine-tuning with assistant-only loss.

Only tokens the assistant produces (tool calls and final answers) contribute to the
loss. System prompt, question, documents and tool outputs are masked with -100, so
the model learns *how to act*, not to memorise filings.

  python -m finagent_lora.train --model Qwen/Qwen2.5-1.5B-Instruct --data data/train.jsonl --out runs/lora
"""
from __future__ import annotations

import argparse
import json
import os
import time

import torch

from finagent_bench.backends import TOOLS

IGNORE = -100


def hf_tools() -> list[dict]:
    return TOOLS


def assistant_token_mask(tokenizer, messages: list[dict], tools: list[dict]) -> tuple[list[int], list[int]]:
    """Tokenise a conversation and return (input_ids, labels) with non-assistant tokens masked.

    For each assistant turn k, the text rendered for messages[:k] with a generation prompt is
    a prefix of the text rendered for messages[:k+1]; the characters between the two are what
    the assistant produced. Those character spans are mapped to tokens via offsets."""
    full = tokenizer.apply_chat_template(messages, tools=tools, tokenize=False)
    spans = []
    for k, m in enumerate(messages):
        if m["role"] != "assistant":
            continue
        before = tokenizer.apply_chat_template(messages[:k], tools=tools, tokenize=False, add_generation_prompt=True)
        upto = tokenizer.apply_chat_template(messages[: k + 1], tools=tools, tokenize=False)
        if not (full.startswith(before) and full.startswith(upto)):
            raise ValueError("chat template renders history differently from prefixes; "
                             "assistant-only masking is unreliable for this model")
        spans.append((len(before), len(upto)))
    enc = tokenizer(full, add_special_tokens=False, return_offsets_mapping=True)
    labels = [IGNORE] * len(enc["input_ids"])
    for i, (s, e) in enumerate(enc["offset_mapping"]):
        if any(a <= s < b for a, b in spans):
            labels[i] = enc["input_ids"][i]
    return enc["input_ids"], labels


class SFTDataset(torch.utils.data.Dataset):
    def __init__(self, path: str, tokenizer, max_len: int):
        self.items, skipped = [], 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                ids, labels = assistant_token_mask(tokenizer, json.loads(line)["messages"], hf_tools())
                if len(ids) > max_len:
                    skipped += 1
                    continue
                self.items.append((ids, labels))
        lens = [len(i) for i, _ in self.items]
        print(f"{len(self.items)} examples (skipped {skipped} > {max_len} tokens); "
              f"tokens/example mean {sum(lens) / len(lens):.0f}, max {max(lens)}; "
              f"trained tokens/example {sum(sum(l != IGNORE for l in lb) for _, lb in self.items) / len(self.items):.0f}")

    def __len__(self):
        return len(self.items)

    def __getitem__(self, i):
        return self.items[i]


def collate(batch, pad_id: int):
    n = max(len(ids) for ids, _ in batch)
    ids = torch.full((len(batch), n), pad_id)
    labels = torch.full((len(batch), n), IGNORE)
    attn = torch.zeros((len(batch), n), dtype=torch.long)
    for j, (x, y) in enumerate(batch):
        ids[j, : len(x)] = torch.tensor(x)
        labels[j, : len(y)] = torch.tensor(y)
        attn[j, : len(x)] = 1
    return {"input_ids": ids, "labels": labels, "attention_mask": attn}


def load_model(name: str, device: str):
    from transformers import AutoModelForCausalLM

    dtype = torch.float16 if device == "cuda" else torch.float32  # T4 has no bf16
    try:
        model = AutoModelForCausalLM.from_pretrained(name, dtype=dtype)
    except TypeError:  # older transformers
        model = AutoModelForCausalLM.from_pretrained(name, torch_dtype=dtype)
    return model.to(device)


def train(model_name: str, data: str, out: str, epochs: float = 2, lr: float = 2e-4, batch: int = 4,
          grad_accum: int = 2, rank: int = 16, max_len: int = 2048, seed: int = 0, max_steps: int = -1) -> dict:
    from peft import LoraConfig, get_peft_model
    from transformers import AutoTokenizer, Trainer, TrainingArguments, set_seed

    set_seed(seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    tok = AutoTokenizer.from_pretrained(model_name)
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token
    ds = SFTDataset(data, tok, max_len)

    model = load_model(model_name, device)
    model.gradient_checkpointing_enable()
    model.enable_input_require_grads()
    model = get_peft_model(model, LoraConfig(r=rank, lora_alpha=2 * rank, lora_dropout=0.05,
                                             target_modules="all-linear", task_type="CAUSAL_LM"))
    model.print_trainable_parameters()

    import math
    total = max_steps if max_steps > 0 else math.ceil(len(ds) / (batch * grad_accum)) * math.ceil(epochs)
    warmup = max(1, int(0.05 * total))  # warmup_ratio was removed in transformers 5.x
    args = TrainingArguments(
        output_dir=out, num_train_epochs=epochs, max_steps=max_steps, learning_rate=lr,
        per_device_train_batch_size=batch, gradient_accumulation_steps=grad_accum,
        lr_scheduler_type="cosine", warmup_steps=warmup, logging_steps=5, save_strategy="no",
        fp16=(device == "cuda"), report_to=[], seed=seed, remove_unused_columns=False,
    )
    t0 = time.time()
    trainer = Trainer(model=model, args=args, train_dataset=ds,
                      data_collator=lambda b: collate(b, tok.pad_token_id))
    trainer.train()
    os.makedirs(out, exist_ok=True)
    model.save_pretrained(out)
    tok.save_pretrained(out)
    log = [h for h in trainer.state.log_history if "loss" in h]
    meta = {"base_model": model_name, "data": data, "examples": len(ds), "epochs": epochs, "lr": lr,
            "batch": batch * grad_accum, "lora_rank": rank, "minutes": round((time.time() - t0) / 60, 1),
            "loss_curve": [(h["step"], h["loss"]) for h in log]}
    with open(os.path.join(out, "train_meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    print(f"saved adapter to {out} ({meta['minutes']} min); final loss {log[-1]['loss'] if log else 'n/a'}")
    return meta


def main() -> None:
    p = argparse.ArgumentParser(description="LoRA fine-tune a model on FinAgent-Bench trajectories")
    p.add_argument("--model", default="Qwen/Qwen2.5-1.5B-Instruct")
    p.add_argument("--data", default="data/train.jsonl")
    p.add_argument("--out", default="runs/lora")
    p.add_argument("--epochs", type=float, default=2)
    p.add_argument("--lr", type=float, default=2e-4)
    p.add_argument("--batch", type=int, default=4)
    p.add_argument("--grad-accum", type=int, default=2)
    p.add_argument("--rank", type=int, default=16)
    p.add_argument("--max-steps", type=int, default=-1)
    a = p.parse_args()
    train(a.model, a.data, a.out, a.epochs, a.lr, a.batch, a.grad_accum, a.rank, max_steps=a.max_steps)


if __name__ == "__main__":
    main()
