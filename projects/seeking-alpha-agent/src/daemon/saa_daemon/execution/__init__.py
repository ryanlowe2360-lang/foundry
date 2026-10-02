"""M4 — paper execution in the tastytrade sandbox.

    engine events (open / bank / close, gate-fired only)
        → Executor: feed gate (D19) · kill switch · Tier 1 order caps · approval (Telegram, 3-minute timeout = Skip)
            → OrderManager: limit-at-mid retry ladder · fill logging · flatten ladder for the kill switch
                → Broker: FakeBroker (tests, dry runs) | TastytradeBroker (the sandbox account)
        ← reconciliation every 30 s: broker positions + live orders vs the local book
"""
from .approvals import ApprovalGate, Proposal
from .broker import Broker, BrokerError, BrokerOrder, BrokerPosition, FakeBroker
from .executor import ExecutionPolicy, Executor, PaperTrade
from .killswitch import KillSwitch
from .orders import LadderPolicy, OrderManager, Ticket, ladder_prices
from .symbols import occ_to_streamer, streamer_to_occ

__all__ = ["ApprovalGate", "Proposal", "Broker", "BrokerError", "BrokerOrder", "BrokerPosition", "FakeBroker", "ExecutionPolicy", "Executor",
           "PaperTrade", "KillSwitch", "LadderPolicy", "OrderManager", "Ticket", "ladder_prices", "occ_to_streamer", "streamer_to_occ"]
