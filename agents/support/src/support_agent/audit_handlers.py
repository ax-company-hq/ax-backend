"""`get_privacy_audit`(읽기): 문의의 개인정보 처리 기록을 보여 준다. 아무것도 바꾸지 않는다.

기록은 기존 쓰기 skill의 부수 효과로 쌓이고(`privacy_audit.py`), 여기서는 읽기만 한다.
원문, 마스킹한 글, 이름·연락처는 출력 스키마에 필드가 없다.
"""

from __future__ import annotations

from ax_contracts.schemas import AgentRequest, AgentResult
from support_agent import handlers as h
from support_agent import privacy_audit
from support_agent.audit_schemas import GetPrivacyAuditInput


async def get_privacy_audit(request: AgentRequest) -> AgentResult:
    parsed = h._parse(request, GetPrivacyAuditInput)
    if isinstance(parsed, AgentResult):
        return parsed
    if h.STORE.get(parsed.inquiry_id) is None:
        return h._not_found(request, parsed.inquiry_id)
    audit = privacy_audit.build_audit(h.STORE, parsed.inquiry_id, parsed.kinds)
    return h._done(request, audit.model_dump(mode="json"))


AUDIT_HANDLERS = {"get_privacy_audit": get_privacy_audit}
