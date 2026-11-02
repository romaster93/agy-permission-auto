---
name: motivation-not-novelty
description: >-
  Behavioral guidelines to push research from motivation to novelty ("Motivation is not Novelty").
  Use when drafting papers, framing contributions, designing ablations/experiments, or reviewing
  methods to surface root causes, derive methods from analysis, and prove component necessity.
---

# Research Novelty Guidelines (Motivation is not Novelty)

Behavioral guidelines to elevate research from motivation to novelty, derived from [Giseop Kim's essay "Motivation is not Novelty"](https://gisbi-kim.github.io/motivation-is-not-novelty/).

**Tradeoff:** These guidelines bias toward depth over throughput. For internal reports or workshop submissions, use judgment.

## 1. Diagnose, Don't Just Observe

**"X fails" is motivation. "X fails because of Y" is the start of novelty.**

Before claiming a contribution:
- State the naive baseline in one line, then explain *why it fails in principle* — not just that it does.
- If you can only describe failure cases, stop. You are still at motivation.
- Causal reasoning beats experimental volume. More benchmarks do not convert observation into insight.
- Distinguish problem (motivation) from root cause (challenge). A paper without the second has no method to justify.

The test: if your one-line failure explanation reduces to "the score is low," go back.

## 2. Derive, Don't Attach

**The method's form must be forced by the analysis. Attaching a module is not a method.**

When designing the solution:
- Every component must answer "why this form?" with a sentence tied to the root cause.
- No "we added attention/transformer/diffusion because it usually helps."
- If a component is swappable for an arbitrary alternative without changing the story, it is not load-bearing — remove or justify it.
- Reparameterizations and reformulations (ResNet's `H(x)=F(x)+x`, Transformer's removal of recurrence, NeRF's frequency lifting) are derivations, not attachments.

Ask: "If I removed this component, which specific failure mode returns?" If you cannot name it, the component is decoration.

## 3. Prove Necessity, Not Just Performance

**Ablations exist to prove each design choice is required, not to pad the experiments section.**

For every component you introduce:
- Ablate it individually; each removal must reveal a *distinct* failure mode.
- Compare against 3 simpler alternatives and explain in one sentence why each fails.
- Define new comparison axes when scalar benchmarks hide the contribution (path length, parallelism, memory, identifiability).
- Verify on 2+ tasks or domains when claiming generality. A single dataset is motivation-stage evidence.
- State *when and why* your method fails. Deferring failure analysis to "future work" signals incomplete novelty.

Full-system vs baseline alone is a motivation-stage experiment. Component-level ablations are a novelty-stage experiment.

## 4. Situate, Don't Isolate

**Related work is where novelty is defended, not where it is buffered.**

When writing related work:
- Cite 20+ works, not 5. Coverage signals you understand the landscape, not just the gap.
- Criticize the 3 closest papers on *different* axes each. Uniform "low accuracy / small dataset" critiques signal shallow reading.
- "Couldn't find similar research" almost always means you didn't look. Look again.
- The reviewer's "why not the simpler approach X?" must be answerable in one sentence per X.

If your related work section reads as a list rather than an argument, rewrite it as an argument.

## 5. Self-Audit Before Submission

**Run the diagnostic. If you fail it, the method section needs a rewrite, not a polish.**

Self-check, top-tier vs incremental — if 4+ rows match the right column, you are still at motivation:

| | Top-tier | Incremental |
|---|---|---|
| Motivation | "X fails" + principled diagnosis | "X fails," no root cause |
| Problem | New comparison axes defined | Benchmark score only |
| Method | Form forced by analysis | Module attached; swappable |
| Ablation | Each choice's necessity proven | Full system vs baseline only |
| Generality | 2+ tasks/domains verified | Single dataset, single task |
| Failure | When/why failures occur, stated | No failure discussion |
| Reviewer Q | "Why simpler?" answered in one sentence | "That's future work" |

One-line contribution test — remove "we propose [module]" from your contribution sentence. If the insight no longer stands on its own, the contribution was the module, not the insight. Rewrite until the insight remains.

The 30-second test — a reviewer asks "what's new?" Your answer must cover root diagnosis *and* the principle that forced your solution, in 30 seconds. If you need the full method section to answer, the novelty is not yet legible.

---

**These guidelines are working if:** reviewers ask deeper questions instead of basic ones, your method section shrinks while your analysis grows, and you can defend every component with one sentence tied to a failure mode.
