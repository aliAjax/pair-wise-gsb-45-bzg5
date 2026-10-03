"""审计时间线：计划事件与系统事件（封航/基准变更）均来自台账事件流。"""
from typing import Any, Dict, List


class AuditRecorder:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def timeline(self, ref: str) -> Dict[str, List[Dict[str, Any]]]:
        return self.repository.plan_timeline(ref)

    def note(self, ref: str, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        # 事件溯源模型下不允许追加游离审计事件，保留接口以兼容旧装配
        raise NotImplementedError("台账系统仅通过事件流留痕")
