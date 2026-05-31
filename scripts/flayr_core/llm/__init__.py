"""flayr_core.llm 包：LLM 调用与分析结果处理。

子模块（按依赖层次从底到顶）：
  - api        HTTP 调用底层（无业务规则）
  - parse      JSON 解析 + schema 规范化（含 STAGES 常量、is_effective_voiceover 等基础工具）
  - payload    LLM 请求 payload 构造
  - postprocess 业务规则修补 + 一致性校验（含 apply_postprocess_chain 抽取的共享处理链）
  - pipeline   主入口：merge_analysis_result / parse_and_validate_llm_result / run_large_model_analysis

不在 __init__ 中做 re-export，下游必须显式 import 子模块路径，
避免 translation 等只需要 api 的模块被动加载整个包。
"""
