"""데모 화면이 보여 줄 현재 상태(카드·보류 큐·감사 로그 등)를 한 번에 모은다.

기능이 늘 때마다 여기에 항목을 더한다(명부, 인수인계, 온보딩, 발령·퇴직 등).
"""

from __future__ import annotations

from typing import Any

from hr_agent.clients.memory import card_state
from hr_agent.handover import store as hstore
from hr_agent.handover.fields import FIELD_LABEL, FIELDS
from hr_agent.roster import store as roster
from hr_agent.roster.gaps import compute_gaps
from hr_agent.roster.service import roster_entries
from hr_agent.services import Services


async def card_rows(svc: Services) -> list[dict[str, Any]]:
    rows = []
    for card in await svc.memory.list_cards():
        row = card.model_dump(mode="json")
        row["state"] = card_state(card)
        rows.append(row)
    return rows


async def snapshot(svc: Services) -> dict[str, Any]:
    holds = svc.gate.list_holds() if hasattr(svc.gate, "list_holds") else []
    handovers = []
    for row in hstore.all_handovers(svc.store):
        hid = row["handover_id"]
        handovers.append(
            {
                **row,
                "items": [i.model_dump(mode="json") for i in hstore.items(svc.store, hid)],
                "fields": [
                    {"field": f, "label": FIELD_LABEL[f], **hstore.fields(svc.store, hid)[f]}
                    for f, _ in FIELDS
                ],
                "questions": svc.store.all(
                    "SELECT question_id, question, gap_field, status, answer FROM"
                    " handover_questions WHERE handover_id = ? ORDER BY rowid",
                    (hid,),
                ),
            }
        )
    onboardings = []
    for row in svc.store.all("SELECT * FROM onboardings ORDER BY created_at, onboarding_id"):
        steps = svc.store.all(
            "SELECT step_id, step_order, title, card_id, done FROM onboarding_steps"
            " WHERE onboarding_id = ? ORDER BY step_order",
            (row["onboarding_id"],),
        )
        done = sum(1 for s in steps if s["done"])
        onboardings.append(
            {**row, "steps": steps, "progress": round(100 * done / len(steps)) if steps else 0}
        )
    gaps = compute_gaps(svc.store, svc.clock())
    return {
        "clock": svc.clock().isoformat(),
        "handovers": handovers,
        "onboardings": onboardings,
        "roster": [e.model_dump(mode="json") for e in roster_entries(svc)],
        "roster_gaps": {
            "no_owner": gaps.no_owner_areas,
            "shift": [{"area": a, "at": t.isoformat()} for a, t in gaps.shift_gaps],
        },
        "areas": roster.list_areas(svc.store),
        "cards": await card_rows(svc),
        "records": svc.store.all(
            "SELECT record_id, context_id, kind, summary, source_refs FROM records ORDER BY rowid"
        ),
        "holds": holds,
        "audit": svc.audit.list(limit=40),
    }
