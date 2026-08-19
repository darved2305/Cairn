"""The nine agent actions — docs/project/PROJECT.md §6.4.

Re-exports `db/decisions.py::ACTIONS` as a proper enum for the agent loop
to switch on, rather than defining a second, driftable list of action
names. `db/decisions.py` stays the source of truth (it's what the CHECK
constraint and `ReuseDecision` validate against); this module exists so
`agent/loop.py` gets exhaustiveness-checkable `match` arms instead of bare
strings.
"""

from __future__ import annotations

from enum import StrEnum

from cairn.db.decisions import ACTIONS


class Action(StrEnum):
    REUSE = "REUSE"
    PARTIAL_REUSE = "PARTIAL_REUSE"
    RECOMPUTE = "RECOMPUTE"
    REFUSE_DUPLICATE = "REFUSE_DUPLICATE"
    SUBSCRIBE = "SUBSCRIBE"
    REFUSE_DOOMED = "REFUSE_DOOMED"
    REMEDIATE_AND_REPLAN = "REMEDIATE_AND_REPLAN"
    RESUME = "RESUME"
    ESCALATE = "ESCALATE"


assert {member.value for member in Action} == ACTIONS, (
    "agent.actions.Action must stay exactly in sync with db.decisions.ACTIONS "
    "— that set is what the reuse_decisions CHECK constraint and "
    "ReuseDecision.__post_init__ validate against."
)
