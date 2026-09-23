# Starter prompts

MemDream ships **no prompts**. These are a working set to copy and edit — they
are never loaded unless your configuration points at them.

```bash
python -m memdream prompts ./prompts     # copy them into your project
export DREAM_PROMPTS_DIR=./prompts       # or MemDream(prompts="./prompts")
```

| File | Decides |
|---|---|
| `extraction.md` | what is worth remembering in a message. **Start here** — it defines what your product remembers at all |
| `decision.md` | ADD / SUPERSEDE / MERGE / NOOP for one fact against what is already known |
| `supersede.md` | SUPERSEDE or NOOP — only used in staged decision mode |
| `merge.md` | MERGE or NOOP — only used in staged decision mode |
| `synthesis.md` | whether a cluster of memories reveals a pattern |
| `compaction.md` | how to fold a crowd of low-value memories into one summary |

Not every prompt is required. Unified decision mode (the default) needs
`extraction` and `decision`; staged mode swaps `decision` for `supersede` and
`merge`. `synthesis` and `compaction` are needed only when those features are
enabled, so the smallest working set is two files.

## Editing them

**The whole file is the prompt.** There is no front matter and no comment
syntax: anything you write here is sent to the model, including this line if you
paste it in. (This README is not a prompt — only files named after a prompt are
loaded.)

`{domain}` anywhere in a file is replaced with your `DREAM_DOMAIN` sentence, or
with nothing when it is unset.

You do **not** need to describe the JSON to return: MemDream appends the exact
output shape to every prompt. Keep the field names the prompt mentions
(`temporalNature`, `targetMemoryIds`, `sourceMemoryIds`) — those are the
contract.

Full guide, including what each prompt receives and must return:
[`docs/prompts.md`](../docs/prompts.md).
