"""StockPilot trading bot — broker-agnostic, simulation-first.

Everything in this package defaults OFF and to the offline SimulatedBroker.
No module here talks to a real broker unless explicitly configured, a recent
passing backtest exists, and the account has been verified NON-REGISTERED
(the bot hard-refuses TFSA/RRSP/etc. — see risk_manager). Research tooling,
not investment advice.
"""
