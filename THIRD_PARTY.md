# Third-party material

Open-source work adapted in this repo, with its licence. Code or text copied or closely adapted is
listed with the source commit. Projects studied for ideas only (AGPL or unclear licences) are listed
at the end; nothing was copied from them.

## Adapted

| Source | Licence | Commit | What we took | Where |
|---|---|---|---|---|
| [video-shotcraft](https://github.com/Vincentwei1021/video-shotcraft) by Vincentwei1021 | Apache-2.0 | 43bd349a1366 | The shot recipe-card format: one-line summary, use, duration, energy, intent, motion core, parameter table with tuning notes, known pitfalls. Our cards and their content are new (fruit shots, Replicate models). | `creative_intelligence/prompts/shots/` |
| [banana-claude](https://github.com/AgriciDaniel/banana-claude) by AgriciDaniel | MIT | 17131327c557 | From `prompt-engineering.md`: "locks and freedom", composition with negative space tied to a layout need, product consistency locks. From `review-and-recovery.md`: failure → fix routing (identity drift → reattach references; product drift → restate locks), used for retake instructions. Paraphrased, not copied. | `prompts/shots/*.yaml` (still sections), `prompts/still-outpaint.v2.txt`, `adstudio/shots.py` (`RECOVERY`) |

## Reviewed and not used

| Source | Licence | Why |
|---|---|---|
| [Seedance2-skill](https://github.com/zhanghaonan777/Seedance2-skill) (4ecc0046eee2) | MIT | Written for the Jimeng platform with Chinese-only prompts. Only the idea that even a 5 s shot needs one A → B change carried over (the cards' `motion.change`); no text copied. |

## Ideas only (reference)

| Source | Licence | What we studied |
|---|---|---|
| [OpenMontage](https://github.com/calesthio/OpenMontage) | AGPL-3.0 | Approval gates per stage, a budget per job, cost estimate before approval. |
| [ClipForge](https://github.com/xixihhhh/clipforge) | AGPL-3.0 | Locking the product image while the background changes (compare: our outpaint keeps the approved photo pixel for pixel). |
