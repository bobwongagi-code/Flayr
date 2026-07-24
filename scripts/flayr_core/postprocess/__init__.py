"""flayr_core.postprocess：分析结果的修补与校验。

子模块（按依赖层次从底到顶）：
  - utils             通用工具：SRT 读取 / evidence_unit 查找 / 时间关系
  - repair            内容修补（修改 result data，正常返回）
  - validate          通用校验（抛 SystemExit 触发 repair 重跑）
  - claims_my         马来西亚市场认证主张专项
  - health_rewrite    健康品类合规重写专项（含 2 个会抛 SystemExit 的 validate_*）
  - chain             共享流水线 apply_postprocess_chain

包级 re-export 暴露主链和 severity 收口入口。其他函数请显式 import 子模块路径，
让调用方一眼看出函数来自哪个职责层。
"""

from .chain import apply_postprocess_chain, finalize_severity_after_repairs

__all__ = ["apply_postprocess_chain", "finalize_severity_after_repairs"]
