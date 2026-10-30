"""Replay last month (September, never seen in training) through the rules.

Compares three policies:
  * Marketing  — ship every COD order (today's behaviour)
  * Operations — cancel every COD order the model scores as risky
  * RTO Guard  — the recommended rules in config/rules.yaml

Writes artifacts/decisions.csv, which the dashboard reads.

    python scripts/backtest.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from rto_guard import ARTIFACTS  # noqa: E402
from rto_guard.backtest import realised, summarise  # noqa: E402
from rto_guard.config import load_config, rto_cost  # noqa: E402
from rto_guard.data import load_orders  # noqa: E402
from rto_guard.economics import lost_customer_value  # noqa: E402
from rto_guard.engine import DecisionEngine  # noqa: E402

CUTOFF = "2026-09-01"
OPS_CANCEL_ABOVE = 0.40  # "risky" for the Ops policy: model risk >= 40%


def rupees(x: float) -> str:
    return f"-₹{abs(x):,.0f}" if x < 0 else f"₹{x:,.0f}"


def ops_policy(cfg, orders, risk) -> dict:
    """Cancel outright when risk >= threshold. Uses true outcomes."""
    cancel = risk >= OPS_CANCEL_ABOVE
    C = rto_cost(cfg)
    saved = (orders.is_rto[cancel] * C).sum()
    lost = sum(
        lost_customer_value(cfg, v, r)
        for v, r, rto in zip(orders.order_value[cancel], orders.customer_prior_rto[cancel], orders.is_rto[cancel])
        if not rto
    )
    return {"cancelled": int(cancel.sum()), "genuine_cancelled": int((cancel & (orders.is_rto == 0)).sum()),
            "saved": saved, "lost": lost, "net": saved - lost}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = load_config()
    df = load_orders()
    month = df[df.order_datetime >= CUTOFF].reset_index(drop=True)
    engine = DecisionEngine(cfg=cfg, use_llm=False)

    decisions = engine.decide_frame(month)
    d = realised(cfg, decisions, month)
    out = pd.concat([month.drop(columns=["is_rto"]), d.drop(columns=["order_id", "order_value"])], axis=1)
    ARTIFACTS.mkdir(exist_ok=True)
    out.to_csv(ARTIFACTS / "decisions.csv", index=False)

    C = rto_cost(cfg)
    base_rtos = month.is_rto.sum()
    print(f"September: {len(month)} COD orders, {base_rtos} RTOs ({base_rtos/len(month):.1%}), "
          f"RTO loss today {rupees(base_rtos * C)} (₹{C:.1f} each)\n")

    s = summarise(d)
    show = s[["orders", "avg_risk", "actual_rto_rate", "saved", "lost_genuine", "discount", "messaging", "net", "act_net"]]
    print("RTO Guard, per bucket (expected ₹ from risk score; act_net = ₹ using what really happened):")
    print(show.to_string(formatters={
        "avg_risk": "{:.0%}".format, "actual_rto_rate": "{:.0%}".format,
        **{c: rupees for c in ["saved", "lost_genuine", "discount", "messaging", "net", "act_net"]},
    }))

    ops = ops_policy(cfg, month, d.risk.to_numpy())
    print("\nPolicy comparison (true outcomes, September):")
    print(f"  Marketing  ship everything       net {rupees(0)}")
    print(f"  Operations cancel risk >= {OPS_CANCEL_ABOVE:.0%}     cancels {ops['cancelled']} orders "
          f"({ops['genuine_cancelled']} genuine): saves {rupees(ops['saved'])}, "
          f"loses {rupees(ops['lost'])}, net {rupees(ops['net'])}")
    print(f"  RTO Guard  recommended rules     net {rupees(d.act_net.sum())} "
          f"(expected {rupees(d.net.sum())}); RTOs avoided ≈ {d.rtos_avoided.sum():.0f}")
    print(f"\nSaved -> artifacts/decisions.csv")


if __name__ == "__main__":
    main()
