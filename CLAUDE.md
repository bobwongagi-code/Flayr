
Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

# 项目背景
请见开发文档。

# 工作规范
- 所有注释用中文，变量函数用英文。
- 改动前先说明你打算改什么，确认后再动手。
- 新功能先写实现，不主动加测试，除非我明确要求。
- 数据库表名用下划线分隔，比如 user_profile。

# 禁止项
- 不要主动重构我没提到的文件。
- 不要删除任何文件，除非我明确说删掉。
- 不要在没确认前直接执行 npm install 装新依赖。

# 架构不变量
- 可选功能（voice clone / OCR / proposal video 等带成本或带可选依赖的能力）失败时必须优雅降级：把失败原因写进各自的结果 JSON，绝不能抛错拖垮主分析流程。主分析（两阶段事实提取+对比）必须始终能跑完出报告。

# 压缩时保留
长对话被自动压缩时，按优先级保留：
1. 架构决策和它背后的理由
2. 改过哪些文件、改了什么
3. 当前进展状态
4. 还没做完的 TODO

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

