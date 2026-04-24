# AutoBot Agentic Critic - Detailed Handover & Specifications

## 1. Role & Inference Loop
You are taking ownership of the **Autobot Critic (Model 5)** in our 5-model Agentic Architecture. 

At inference time within the VSCode extension loop, the Critic acts as the ultimate code-reviewer safety net before a patch is accepted by the orchestrator.
1. The **Planner** writes a plan. The **Patcher** generates a code diff implementing the plan.
2. The **Critic** intercepts the Patcher's raw diff and evaluates it against the Planner's original directive and strict codebase conventions. 
3. **Your Model's Mandate:** Output one of three strict class verdicts: `ACCEPT`, `REVISE`, or `REJECT`.
    - `ACCEPT`: The pipeline automatically merges the patch. 
    - `REVISE`: Must include constructive line-level feedback. The orchestrator intercepts this text and feeds it *back* to the Patcher to execute a retry loop (max 3 times).
    - `REJECT`: Terminates the agentic loop entirely if the patch is fundamentally unresolvable. 

## 2. Phase 1 Approach: SFT vs. DPO
*Correction to prior assumptions:* For our initial adapter rollout, you are **not** performing Direct Preference Optimization (DPO) yet. You are executing **SFT (Supervised Fine-Tuning).**
*   **Why SFT first?** The base model (`Qwen2.5-Coder-7B`) already possesses internal review logic, but it does not output our strict `VERDICT: [CLASS] \n REASONING:` structure. SFT forces the model to align perfectly to our orchestrator's parsing expectations. 
*   **DPO Phase 2:** Once your SFT adapter is deployed, DPO (with `chosen`/`rejected` triplets) will be executed on top of this adapter to push its reasoning tone from toxic -> constructive.

## 3. Dataset: GraphRAG & Equal Class Balancing
You have been provided with `critic_train_graphrag.jsonl`. This contains exactly **750 highly curated SFT examples**. 

To prevent the model from becoming heavily skewed (e.g., lazily outputting `ACCEPT` on every single diff), we executed a strict **Equal Class Balancing Strategy (33%):**
*   **250 REJECTs:** Extracted purely from un-merged PRs that were forcefully closed following explicit `CHANGES_REQUESTED` events.
*   **250 REVISEs:** Extracted from successfully merged PRs that had historical intermediate review friction, proving that iteration was required.
*   **250 ACCEPTs:** Clean, targeted diffs with pure `APPROVED` reviews and zero friction.

### The GraphRAG Injection Matrix
We leveraged our advanced Neo4j Graph Database to pre-inject your inputs with Repository Memory! 
We mapped out `Target File -> [REVIEWED_IN] -> Historic Review Comments` to extract what past Airflow senior engineers debated regarding the exact files in your dataset.

**Your SFT Target Blueprint:**
```json
{
  "input": "--- HISTORICAL REVIEW FRICTION ---\n- Past reviews on `airflow/models/dagrun.py`: Avoid circular imports at module level. | Never implicitly instantiate NEW_SESSION.\n\n--- CURRENT PATCH ---\nPLAN: Add TaskInstance import.\nDIFF:\n+++ b/airflow/models/dagrun.py\n@@ -100,0 +100,1 @@\n+from airflow.models import TaskInstance\n\nTASK: Evaluate Patch. Output VERDICT and REASONING.",

  "output": "VERDICT: REVISE\nREASONING: Line 100: You added `TaskInstance` at the module level. As per repo conventions, this will cause a circular import loop. Please move the import inside the method."
}
```
*(Notice how the model leverages the Historic Review Friction to justify a `REVISE` verdict!)*

## 4. Hyperparameter Matrix (Qwen2.5-Coder 7B Instruct)
You must fine-tune using a LoRA Adapter strategy alongside Unsloth/PEFT. Because the Critic generates structured text and discrete classification boundaries (unlike the Patcher which memorizes complex syntax mappings), the bounds here are significantly tighter and faster to train:

| Parameter | Recommended Value | Reasoning / Decision Driver |
| :--- | :--- | :--- |
| **Max Sequence Length** | 2,048 tokens | Diff chunks have been truncated to purely relevant hunks to prevent sequence bloat. 2k is plenty. |
| **LoRA Rank (r)** | 32 | You do not need extreme generative capacity. You only need enough capacity to classify patterns and generate text. |
| **Epochs** | 3 | Structured review text and classifications converge significantly faster than code generation. Monitor your eval loss carefully to prevent rote memorization of the 750 samples. |
| **Batch Size** | 8 | The smaller sequence length (2048) frees up VRAM on your A100 GPU, letting you push your BS to 8 for faster epoch sweeps. |
| **Learning Rate** | 1e-4 | Standard LR performs well for text SFT logic. The risk of catastrophic python forgetting is negligent here. |

### Execution
Simply mount the provided `critic_train_graphrag.jsonl` to your SFTTrainer instance, run the batch sweeps, and push the Critic adapter to Huggingface! No data engineering is required on your end.
