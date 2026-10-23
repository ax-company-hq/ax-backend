"""문의와 기획개발 변경 요청의 연결 저장소."""

import os

import pytest

from support_agent import persistence
from support_agent.change_requests import ChangeRequestConflict, ChangeRequestStore
from support_agent.persistence import TABLES, PersistentChangeRequestStore, PgRepo

CTX = "ctx_req_0001"


# ---------------------------------------------------------------- 연결
def test_link_stores_the_request_for_an_inquiry() -> None:
    store = ChangeRequestStore()
    link = store.link(CTX, 7, request_ref="REQ-12")
    assert (link.context_id, link.inquiry_id, link.status) == (CTX, 7, "requested")
    assert link.request_ref == "REQ-12" and link.result is None and link.notice_id is None
    assert store.get(CTX) == link
    assert store.get("ctx_req_9999") is None


def test_only_change_request_contexts_can_be_linked() -> None:
    store = ChangeRequestStore()
    for wrong in ("ctx_inc_0001", "ctx_ntc_0001", "ctx_0001", ""):
        with pytest.raises(ValueError, match="ctx_req_"):
            store.link(wrong, 1)
    assert store.pending() == []


def test_linking_the_same_pair_again_is_idempotent() -> None:
    store = ChangeRequestStore()
    first = store.link(CTX, 7)
    again = store.link(CTX, 7)
    assert again == first
    assert len(store.for_inquiry(7)) == 1


def test_a_request_context_belongs_to_one_inquiry() -> None:
    store = ChangeRequestStore()
    store.link(CTX, 7)
    with pytest.raises(ChangeRequestConflict, match="문의 7"):
        store.link(CTX, 8)
    assert store.for_inquiry(8) == []


def test_an_inquiry_can_have_several_requests_in_order() -> None:
    store = ChangeRequestStore()
    store.link("ctx_req_0002", 7)
    store.link("ctx_req_0001", 7)
    store.link("ctx_req_0003", 9)
    assert [link.context_id for link in store.for_inquiry(7)] == ["ctx_req_0002", "ctx_req_0001"]
    assert [link.seq for link in store.for_inquiry(7)] == [1, 2]
    assert [link.context_id for link in store.for_inquiry(9)] == ["ctx_req_0003"]
    assert store.for_inquiry(1) == []


# ---------------------------------------------------------------- 결과
def test_result_closes_the_request_and_leaves_the_pending_list() -> None:
    store = ChangeRequestStore()
    store.link(CTX, 7)
    store.link("ctx_req_0002", 8)
    assert [link.context_id for link in store.pending()] == [CTX, "ctx_req_0002"]
    done = store.record_result(CTX, "completed", result={"pr": "pr_52", "merged": True})
    assert done.status == "completed" and done.result == {"pr": "pr_52", "merged": True}
    assert done.updated_at >= done.created_at
    assert [link.context_id for link in store.pending()] == ["ctx_req_0002"]


def test_the_same_result_twice_is_a_resend_not_a_conflict() -> None:
    store = ChangeRequestStore()
    store.link(CTX, 7)
    first = store.record_result(CTX, "completed", result={"pr": "pr_52"})
    assert store.record_result(CTX, "completed", result={"pr": "pr_52"}) == first


def test_a_different_result_after_closing_is_a_conflict() -> None:
    store = ChangeRequestStore()
    store.link(CTX, 7)
    store.record_result(CTX, "completed", result={"pr": "pr_52"})
    with pytest.raises(ChangeRequestConflict):
        store.record_result(CTX, "declined")
    with pytest.raises(ChangeRequestConflict):
        store.record_result(CTX, "completed", result={"pr": "pr_99"})
    assert store.get(CTX).status == "completed"  # type: ignore[union-attr]


def test_result_needs_a_known_request_and_a_final_status() -> None:
    store = ChangeRequestStore()
    with pytest.raises(KeyError):
        store.record_result(CTX, "completed")
    store.link(CTX, 7)
    with pytest.raises(ValueError, match="결과 상태"):
        store.record_result(CTX, "requested")
    with pytest.raises(ValueError, match="결과 상태"):
        store.record_result(CTX, "merged")  # 계약이 정해지기 전에는 모르는 상태를 받지 않는다
    assert store.get(CTX).status == "requested"  # type: ignore[union-attr]


# ---------------------------------------------------------------- 공지
def test_notice_attaches_only_after_completion_and_only_once() -> None:
    store = ChangeRequestStore()
    store.link(CTX, 7)
    with pytest.raises(ChangeRequestConflict, match="completed"):
        store.attach_notice(CTX, "ntc_0001")  # 아직 처리 전
    store.record_result(CTX, "completed")
    attached = store.attach_notice(CTX, "ntc_0001")
    assert attached.notice_id == "ntc_0001"
    assert store.attach_notice(CTX, "ntc_0001") == attached  # 같은 공지 재전송
    with pytest.raises(ChangeRequestConflict, match="ntc_0001"):
        store.attach_notice(CTX, "ntc_0002")
    with pytest.raises(KeyError):
        store.attach_notice("ctx_req_9999", "ntc_0001")


def test_declined_requests_take_no_notice() -> None:
    store = ChangeRequestStore()
    store.link(CTX, 7)
    store.record_result(CTX, "declined")
    with pytest.raises(ChangeRequestConflict):
        store.attach_notice(CTX, "ntc_0001")


# ---------------------------------------------------------------- 영속
def test_the_table_follows_the_naming_rules() -> None:
    table = next(t for t in TABLES if t.name == "change_requests")
    assert {"tenant_id", "context_id"} <= {*table.pk, *table.columns}
    assert persistence.BY_NAME["change_requests"] is table


def test_links_results_and_notices_survive_a_restart(memory_repo) -> None:
    repo = memory_repo()
    store = PersistentChangeRequestStore(repo)
    store.link(CTX, 7, request_ref="REQ-12")
    store.link("ctx_req_0002", 8)
    store.record_result(CTX, "completed", result={"pr": "pr_52"})
    store.attach_notice(CTX, "ntc_0001")

    reborn = PersistentChangeRequestStore(repo)  # 재시작
    assert reborn.get(CTX) == store.get(CTX)
    assert reborn.get(CTX).notice_id == "ntc_0001"  # type: ignore[union-attr]
    assert [link.context_id for link in reborn.pending()] == ["ctx_req_0002"]
    assert [link.inquiry_id for link in reborn.for_inquiry(7)] == [7]
    # 이어서 쓸 수 있고, 모순은 재시작 뒤에도 막힌다
    with pytest.raises(ChangeRequestConflict):
        reborn.link(CTX, 99)
    with pytest.raises(ChangeRequestConflict):
        reborn.record_result(CTX, "declined")
    assert reborn.record_result("ctx_req_0002", "declined").status == "declined"
    reborn.link("ctx_req_0000", 7)  # 키 순서로는 맨 앞이지만 연결한 순서로는 맨 뒤다
    assert [link.context_id for link in reborn.for_inquiry(7)] == [CTX, "ctx_req_0000"]


def test_a_failed_write_does_not_change_the_cache(memory_repo) -> None:
    repo = memory_repo()
    store = PersistentChangeRequestStore(repo)
    repo.fail = True
    with pytest.raises(RuntimeError, match="db down"):
        store.link(CTX, 7)
    assert store.get(CTX) is None  # DB에 못 쓴 연결은 메모리에도 남기지 않는다
    repo.fail = False
    assert store.link(CTX, 7).status == "requested"


def test_without_dsn_the_store_is_plain_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(persistence.DSN_ENV, raising=False)
    assert type(persistence.build_change_request_store()) is ChangeRequestStore


# ---------------------------------------------------------------- 실제 PostgreSQL (DSN 있을 때만)
DSN = os.environ.get(persistence.DSN_ENV)


@pytest.mark.skipif(not DSN, reason="SUPPORT_PG_DSN 없음 (DB 테스트 건너뜀)")
def test_pg_links_survive_a_new_connection() -> None:
    assert DSN
    repo = PgRepo(DSN)
    repo._run(lambda conn: conn.execute("TRUNCATE change_requests"))
    store = PersistentChangeRequestStore(repo)
    store.link(CTX, 7, request_ref="REQ-12")
    store.record_result(CTX, "completed", result={"pr": "pr_52", "merged": True})
    repo.close()

    other = PgRepo(DSN)  # 새 연결 = 프로세스 재시작
    reborn = PersistentChangeRequestStore(other)
    link = reborn.get(CTX)
    assert link is not None and link.status == "completed"
    assert link.result == {"pr": "pr_52", "merged": True} and link.request_ref == "REQ-12"
    assert reborn.pending() == [] and [x.context_id for x in reborn.for_inquiry(7)] == [CTX]
    other.close()
