import pytest
from pydantic import ValidationError

from ax_contracts.data_access.dev_status import DevStatus, FeedItem, StageResult


def test_subset_is_valid_and_defaults_are_empty():
    status = DevStatus.model_validate({"generated_at": "2026-10-23T00:00:00Z"})
    assert status.scenario == "SCN-A"
    assert status.stages == [] and status.forecast is None
    assert status.totals.total == 15 and status.signals.merge_rate.overall is None


def test_stage_roundtrip_with_real_none():
    data = {
        "generated_at": "2026-10-23T00:00:00+09:00",
        "stages": [
            {
                "no": 1,
                "name": "diagnose",
                "dept": "infra",
                "req_ids": ["DEP-INF-01"],
                "unit": {"state": "pass", "since": "2026-10-23T00:00:00Z", "last_pass_commit": "a"},
                "chain": {"state": "broken", "note": "x", "issue": 3},
            }
        ],
    }
    status = DevStatus.model_validate(data)
    stage = status.stages[0]
    assert stage.real is None and stage.unit.state == "pass"
    assert status.generated_at.utcoffset().total_seconds() == 0  # UTC로 바뀐다
    assert DevStatus.model_validate(status.model_dump(mode="json")) == status


def test_unknown_state_and_extra_fields_rejected():
    with pytest.raises(ValidationError):
        StageResult.model_validate({"state": "green"})
    with pytest.raises(ValidationError):
        StageResult.model_validate({"state": "pass", "extra": 1})
    with pytest.raises(ValidationError):
        DevStatus.model_validate({"generated_at": "2026-10-23T00:00:00"})  # 시간대 없음


def test_feed_from_alias_roundtrip():
    item = FeedItem.model_validate(
        {"at": "2026-10-23T00:00:00Z", "kind": "bot", "from": "ax-bot", "text": "머지"}
    )
    assert item.from_ == "ax-bot"
    assert item.model_dump(mode="json", by_alias=True)["from"] == "ax-bot"


def test_history_date_format():
    with pytest.raises(ValidationError):
        DevStatus.model_validate(
            {
                "generated_at": "2026-10-23T00:00:00Z",
                "history": [{"date": "10/23", "chain_pass": 1, "real_pass": 0}],
            }
        )
