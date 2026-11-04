# FinAgent-LoRA

![CI](https://github.com/Ajinkyarodge66/finagent-lora/actions/workflows/ci.yml/badge.svg)
[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Ajinkyarodge66/finagent-lora/blob/main/notebooks/train_and_eval.ipynb)

**Can LoRA fine-tuning turn a small open model (1.5B parameters) into a reliable, injection-resistant financial agent, and where does the robustness come from?**

Large models can read filings, use tools and resist some prompt injections, but they are expensive to run. This project fine-tunes a small open model with LoRA on verified tool-calling trajectories, then measures it on [FinAgent-Bench](https://github.com/Ajinkyarodge66/finagent-bench) using exactly the same agent loop, prompt and scoring as large API models.

## Experiment design

| Run | Training data | Question it answers |
|---|---|---|
| `base` | none | How good is the small model out of the box? |
| `lora` | all task types, 30% injected documents | Does fine-tuning make it reliable? |
| `lora-noinj` | same, but **no** injected documents | Does injection robustness come from seeing attacks in training, or from learning the task format? |
| `lora-nomh` | multi-hop tasks **held out** | Does it generalise to combining two filings, a task type it never saw? |

Evaluation: FinAgent-Bench test set (seed 7, 20 tasks, 8 with prompt injection), greedy decoding.

## Results

*To be added after the Colab run.*

## How it works

**Training data** (`data.py`). A rule-based expert solves FinAgent-Bench tasks from seeds 1000+. Each solution is recorded as a native tool-calling conversation: `list_docs` → `read_doc` → `calculator` → `final_answer`. Every tool result comes from actually executing the call, and every final answer is checked against ground truth, so no incorrect example can enter the dataset. `check_no_leakage` asserts that no training question/answer pair appears in the test set.

**Assistant-only loss** (`train.py`). Only the tokens the assistant produces (tool calls, answers, end-of-turn) are trained on. The system prompt, question, documents and tool outputs are masked. Masks are computed from the model's own chat template by rendering each conversation prefix and mapping the assistant's character spans to tokens, so it works with any template where history renders consistently (and raises an error otherwise rather than silently training on the wrong tokens). Only a small fraction of each conversation is trained on; the notebook prints the exact count and the trained text before training starts.

**LoRA**: rank 16, alpha 32, all linear layers, cosine schedule, fp16 on a free Colab T4.

**Evaluation** (`evaluate.py`). The fine-tuned adapter is merged and run through FinAgent-Bench's `LLMAgent` with a local backend that renders tools via the chat template and parses `<tool_call>` blocks, so local and API models are scored identically.

## Run it

**Colab (free GPU)**: click the badge above, choose a T4 GPU, Run all.

**Locally:**
```bash
pip install -e ".[dev]"
python -m pytest -q                                  # runs on CPU with a tiny offline model
python -m finagent_lora.data --out data/train.jsonl  # 400 verified examples
python -m finagent_lora.train --model Qwen/Qwen2.5-1.5B-Instruct --data data/train.jsonl --out runs/lora
python -m finagent_lora.evaluate --adapter runs/lora --name lora
```

## Testing

The test suite runs on CPU without downloading anything: `tiny.py` builds a tiny random model in the Qwen2 architecture with a tokenizer trained on the data and a Qwen-style chat template. Tests check data correctness, leakage detection, that documents and questions are never trained on, tool-call parsing, and a full train → evaluate run.

## Limitations

- Task templates and company names are shared between training and test (only the numbers differ), so `lora` measures in-distribution learning. The `nomh` ablation is the generalisation test. Paraphrased questions and real filings are on the roadmap.
- The injection in FinAgent-Bench v0.2 is blunt. Subtler attacks may not be resisted.
- One test set of 20 tasks: differences of 5% are a single task.

## Roadmap

- [x] Verified trajectory data, assistant-only LoRA training, local-model evaluation, CPU tests
- [ ] Results: base vs LoRA vs ablations on Colab
- [ ] Paraphrased questions and unseen company names to test real generalisation
- [ ] Compare against API models from FinAgent-Bench (accuracy per £ of inference)
- [ ] Publish the adapter on Hugging Face
