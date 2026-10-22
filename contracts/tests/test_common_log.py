import pytest
from pydantic import ValidationError

from ax_contracts.data_access.common_log import CommonLogQuery, CommonLogResult, LogRecord


def _record(**over):
    base = {
        "log_id": "log-1",
        "occurred_at": "2026-01-01T00:00:00Z",
        "source": "data",
        "kind": "task_status",
    }
    return {**base, **over}


def test_from_alias():
    q = CommonLogQuery.model_validate({"from": "2026-01-01T00:00:00Z"})
    assert q.from_ is not None
    assert q.limit == 1000
    assert q.tenant_id == "shop-demo"


def test_unknown_kind_rejected():
    with pytest.raises(ValidationError):
        LogRecord.model_validate(_record(kind="nope"))
    with pytest.raises(ValidationError):
        CommonLogQuery.model_validate({"kinds": ["nope"]})


@pytest.mark.parametrize("limit", [0, 5001])
def test_limit_out_of_range_rejected(limit):
    with pytest.raises(ValidationError):
        CommonLogQuery.model_validate({"limit": limit})


def test_naive_datetime_rejected():
    with pytest.raises(ValidationError):
        LogRecord.model_validate(_record(occurred_at="2026-01-01T00:00:00"))
    with pytest.raises(ValidationError):
        CommonLogQuery.model_validate({"to": "2026-01-01T00:00:00"})


def test_context_id_none_allowed():
    r = LogRecord.model_validate(_record(context_id=None))
    assert r.context_id is None
    assert CommonLogResult(records=[r]).truncated is False
