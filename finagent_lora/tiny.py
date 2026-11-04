"""Build a tiny random Qwen2-architecture model + tokenizer offline, for CPU smoke tests.

It has the same chat-template format as Qwen (tools in the system prompt, <tool_call>
blocks, <tool_response> turns), so tests exercise the real masking, training and
tool-call parsing code paths without downloading anything.
"""
from __future__ import annotations

import json
import os

CHAT_TEMPLATE = (
    "{%- if tools %}<|im_start|>system\n"
    "{%- if messages[0].role == 'system' %}{{ messages[0].content }}\n\n{%- endif %}"
    "# Tools\n<tools>{%- for t in tools %}\n{{ t | tojson }}{%- endfor %}\n</tools><|im_end|>\n"
    "{%- elif messages[0].role == 'system' %}<|im_start|>system\n{{ messages[0].content }}<|im_end|>\n"
    "{%- endif %}"
    "{%- for m in messages %}"
    "{%- if m.role == 'user' %}<|im_start|>user\n{{ m.content }}<|im_end|>\n"
    "{%- elif m.role == 'assistant' %}<|im_start|>assistant\n{{ m.content or '' }}"
    "{%- for c in (m.tool_calls or []) %}<tool_call>\n"
    '{"name": "{{ c.function.name }}", "arguments": {{ c.function.arguments | tojson }}}'
    "\n</tool_call>{%- endfor %}<|im_end|>\n"
    "{%- elif m.role == 'tool' %}<|im_start|>user\n<tool_response>\n{{ m.content }}\n</tool_response><|im_end|>\n"
    "{%- endif %}{%- endfor %}"
    "{%- if add_generation_prompt %}<|im_start|>assistant\n{%- endif %}"
)

SPECIALS = ["<|endoftext|>", "<|im_start|>", "<|im_end|>"]


def build_tiny(out_dir: str, corpus: list[str]) -> str:
    from tokenizers import Tokenizer, decoders, models, pre_tokenizers, trainers
    from transformers import PreTrainedTokenizerFast, Qwen2Config, Qwen2ForCausalLM

    raw = Tokenizer(models.BPE())
    raw.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    raw.decoder = decoders.ByteLevel()
    raw.train_from_iterator(corpus, trainers.BpeTrainer(
        vocab_size=600, special_tokens=SPECIALS, initial_alphabet=pre_tokenizers.ByteLevel.alphabet()))
    tok = PreTrainedTokenizerFast(tokenizer_object=raw, eos_token="<|im_end|>", pad_token="<|endoftext|>",
                                  additional_special_tokens=["<|im_start|>"])
    tok.chat_template = CHAT_TEMPLATE

    cfg = Qwen2Config(vocab_size=len(tok), hidden_size=64, intermediate_size=128, num_hidden_layers=2,
                      num_attention_heads=4, num_key_value_heads=2, max_position_embeddings=4096,
                      eos_token_id=tok.eos_token_id, pad_token_id=tok.pad_token_id, tie_word_embeddings=True)
    os.makedirs(out_dir, exist_ok=True)
    Qwen2ForCausalLM(cfg).save_pretrained(out_dir)
    tok.save_pretrained(out_dir)
    return out_dir


def corpus_from_jsonl(path: str, limit: int = 50) -> list[str]:
    texts = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= limit:
                break
            for m in json.loads(line)["messages"]:
                texts.append(m.get("content") or json.dumps(m.get("tool_calls")))
    return texts
