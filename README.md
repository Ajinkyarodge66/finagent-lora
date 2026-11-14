# FinAgent-LoRA

![CI](https://github.com/Ajinkyarodge66/finagent-lora/actions/workflows/ci.yml/badge.svg)
[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Ajinkyarodge66/finagent-lora/blob/main/notebooks/train_and_eval.ipynb)

**Can LoRA fine-tuning turn a small open model (0.5B parameters) into a reliable, injection-resistant financial agent, and where does the robustness come from?** Short answer: reliable on the trained task format, yes. Injection-resistant only against the attack style it was tested on.

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

| Run | Accuracy | arithmetic | lookup | multi_hop | Acc. under injection | Hijack rate | Invalid tool calls | Train time |
|---|---|---|---|---|---|---|---|---|
| `base` | 0% | 0% | 0% | 0% | 0% | 0% | 32 | - |
| `lora` | **100%** | 100% | 100% | 100% | 100% | 0% | 0 | 110 min |
| `lora-noinj` | **100%** | 100% | 100% | 100% | 100% | 0% | 0 | 151 min |
| `lora-nomh` | 75% | 100% | 100% | **0%** | 50% | 0% | 0 | 83 min |
| *gpt-oss-20b (API, for reference)* | *77%* | *95%* | *80%* | *40%* | *44%* | *56%* | | |

Qwen2.5-0.5B-Instruct, LoRA rank 16, 200 verified training examples (150 for `nomh`), 2 epochs, trained and evaluated on GitHub Actions' free CPU machines ([`train.yml`](.github/workflows/train.yml)). Greedy decoding, so one run per model. The gpt-oss-20b row is from [FinAgent-Bench](https://github.com/Ajinkyarodge66/finagent-bench), scored by the same agent loop. Raw transcripts, failure reports and loss curves are in [`results/`](results/). Training loss fell from 0.28 to 0.001 in every run.

## Findings

Every number was checked against the transcripts before being written here.

**1. Fine-tuning taught the procedure, not the answers.** The base model scored 0%. It never really tried to read the filings (1 `read_doc` call in 20 tasks), invented a tool called `next_tool` 32 times, and made up "hypothetical" figures, then computed with them (one answer was a 1700% net margin). After LoRA, every task followed `list_docs` → `read_doc` → `calculator` → `final_answer`, with the calculator fed the exact figures from that filing. None of the test figures appears anywhere in the training data, so the model had to read them.

**2. Resistance to the blunt attack did not come from training on attacks.** `lora-noinj` never saw an injected document, yet it read the planted "ignore all previous instructions" note on all 8 injected tasks and still answered correctly, exactly like `lora`. Fine-tuning made "copy the figure from the filing, then submit it" so strong that the model stopped treating document text as instructions. gpt-oss-20b, about 40x larger, was hijacked by the same note on 56% of injected tasks. But see finding 4: this resistance does not survive a subtler attack.

**3. It did not generalise to an unseen task type.** With multi-hop tasks held out, `lora-nomh` was perfect on lookup and arithmetic but scored 0% on multi-hop. It read only one of the two filings, then applied the growth-percentage formula it had learned for arithmetic tasks, e.g. `124.6/71.9*100` instead of `124.6-71.1+82.3-37.5`. It learned task templates, not how to plan a new combination. Its lower "accuracy under injection" comes from these multi-hop errors, not from hijacks.

**4. A subtle attack breaks it, as it breaks the large models.** [FinAgent-Bench v0.4](https://github.com/Ajinkyarodge66/finagent-bench#v04-a-subtler-attack-breaks-every-model) adds a fake "data-quality notice for AI assistants" with a full set of plausible fake figures. The same adapters, without retraining ([`eval-attacks.yml`](.github/workflows/eval-attacks.yml)):

| Run | Blunt attack: influence | Fake correction after figures: influence (exact hijack) | Fake correction before figures: influence (exact hijack) | Accuracy, correction after / before |
|---|---|---|---|---|
| `lora` | 0% | **88%** (25%) | **75%** (12%) | 65% / 70% |
| `lora-noinj` | 0% | **75%** (25%) | **75%** (12%) | 70% / 70% |
| `lora-nomh` | 0% | **75%** (25%) | **62%** (12%) | 60% / 65% |
| *gpt-oss-120b (API)* | *0%* | ***100%*** | ***100%*** | *57% / 52%* |

"Influence" counts any answer the attack changed, including wrong answers whose calculator input mixed real and fake figures. The fine-tuned model rarely copied the attacker's answer exactly. Instead it took some figures from the real sentences and some from the fake correction, e.g. `124.6-71.1+54.0-25.3` (54.0 is fake), and produced a number that matched neither. Exact-match hijack rates alone would have reported this model as mostly safe. On clean tasks nothing changed: `lora` and `lora-noinj` stayed at 12/12. Training on blunt attacks (`lora` vs `lora-noinj`) made no difference against the subtle one.

**Takeaway.** A cheap fine-tune of a 0.5B model learned this task format well enough to match much larger models on accuracy (finding 1). Its apparent safety advantage held only against the attack style it was tested on (findings 2 and 4). It did not generalise to a new task type (finding 3). Safety claims need attacks the model has not seen, and metrics that catch partial hijacks, not just exact ones.

## How it works

**Training data** (`data.py`). A rule-based expert solves FinAgent-Bench tasks from seeds 1000+. Each solution is recorded as a native tool-calling conversation: `list_docs` → `read_doc` → `calculator` → `final_answer`. Every tool result comes from actually executing the call, and every final answer is checked against ground truth, so no incorrect example can enter the dataset. `check_no_leakage` asserts that no training question/answer pair appears in the test set.

**Assistant-only loss** (`train.py`). Only the tokens the assistant produces (tool calls, answers, end-of-turn) are trained on. The system prompt, question, documents and tool outputs are masked. Masks are computed from the model's own chat template by rendering each conversation prefix and mapping the assistant's character spans to tokens, so it works with any template where history renders consistently (and raises an error otherwise rather than silently training on the wrong tokens). Only a small fraction of each conversation is trained on; the notebook prints the exact count and the trained text before training starts.

**LoRA**: rank 16, alpha 32, all linear layers, cosine schedule. Runs on CPU (GitHub Actions, about 2 hours per variant) or a free Colab T4.

**Evaluation** (`evaluate.py`). The fine-tuned adapter is merged and run through FinAgent-Bench's `LLMAgent` with a local backend that renders tools via the chat template and parses `<tool_call>` blocks, so local and API models are scored identically.

## Run it

**GitHub Actions (free CPU, no setup)**: Actions → train-and-eval → Run workflow. The four runs execute in parallel, and the results are committed to `results/`.

**Colab (free GPU)**: click the badge above, choose a T4 GPU, Run all.

**Locally:**
```bash
pip install -e ".[dev]"
python -m pytest -q                                  # runs on CPU with a tiny offline model
python -m finagent_lora.data --out data/train.jsonl  # 400 verified examples (--n-seeds 10 for 200)
python -m finagent_lora.train --model Qwen/Qwen2.5-0.5B-Instruct --data data/train.jsonl --out runs/lora
python -m finagent_lora.evaluate --model Qwen/Qwen2.5-0.5B-Instruct --adapter runs/lora --name lora
```

## Testing

The test suite runs on CPU without downloading anything: `tiny.py` builds a tiny random model in the Qwen2 architecture with a tokenizer trained on the data and a Qwen-style chat template. Tests check data correctness, leakage detection, that documents and questions are never trained on, tool-call parsing, and a full train → evaluate run.

## Limitations

- **In-distribution only.** Question templates and company names are shared between training and test (19 of 20 test questions have the same wording as a training question; only the figures differ). So `lora`'s 100% shows it learned this task format, not general financial reasoning. `lora-nomh` shows what happens outside the format.
- **Few attack styles.** One blunt attack and one fake-correction attack (two placements), each with fixed wording. The model was never trained on fake corrections, so whether training on them would help is untested.
- **Small test set.** 20 tasks, including some repeated questions, so 5% is one task. Single greedy run per model.

## Roadmap

- [x] Verified trajectory data, assistant-only LoRA training, local-model evaluation, CPU tests
- [x] Results: base vs LoRA vs two ablations (GitHub Actions CPU)
- [ ] Paraphrased questions and unseen company names to test real generalisation
- [x] Compare against API models from FinAgent-Bench
- [ ] Accuracy per £ of inference vs API models
- [x] Subtle injection attacks (fake corrections) on the fine-tuned model, evaluated from saved adapters
- [ ] Train on fake-correction attacks and test on held-out attack wordings
- [ ] Publish the adapter on Hugging Face
