"""Safety state that outlives a single call: approvals, the kill switch and pairing.

Import from the submodules directly, or from here for the three public classes:
    approvals.ApprovalBroker   - owner taps on CONFIRM cards
    killswitch.KillSwitch      - stop, panic and local resume
    pairing.Pairing            - the one-time code that pins the owner
"""
from .approvals import ApprovalBroker
from .killswitch import KillSwitch
from .pairing import Pairing

__all__ = ["ApprovalBroker", "KillSwitch", "Pairing"]
