# Stage Judgment Candidate Source

The original mixed source is preserved in Git commit `cb10bb1d5ca3bfc86b2b5ee478860cba85aa7b84`. Do not apply that commit wholesale: it also contains the approved Phase C cleanup and tests. For a future isolated comparison, apply only these candidate hunks to the candidate arm:

- `scripts/flayr_core/llm/payload.py`, `_stage_group_flag_contract("S2")`: the four sentences appended after the `benchmark_s2` schema in the S2 tuple.
- `scripts/flayr_core/stage_contract_registry.py`, `S3_EXPRESSION_PROMPT`: the overall-expression and comprehension-gain clauses added to the baseline expression instructions. The independent wording correction `双方都是口播时，比较` is not part of this candidate.
- `structure_library_full.md`, `## 六、S3 功能与使用表达`: the clauses stating that equivalent explanations do not establish equivalent overall expression, and that a demonstrated comprehension gain may support a difference.

Keep the approved Phase C cleanup and the separate small-eligibility correction common to both arms. The active baseline must remain candidate-free. This reference records source locations only; it adds no runtime switch or generator.
