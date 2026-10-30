"""Replay past orders through the engine and add up the ₹.

Two ways to count, both shown:
  * expected — uses the model's risk score for each order (what the dashboard
    does for new orders, whose outcome we don't know yet)
  * actual   — uses what really happened to the order (RTO or delivered),
    so it can't flatter the model
"""

from __future__ import annotations

import pandas as pd

from .config import rto_cost
from .economics import ACTION_LABELS, ACTIONS, margin, pct


def realised(cfg: dict, decisions: pd.DataFrame, orders: pd.DataFrame) -> pd.DataFrame:
    """₹ effect per order using the true outcome instead of the risk score."""
    d = decisions.copy()
    d["is_rto"] = orders["is_rto"].to_numpy()
    d["order_value"] = orders["order_value"].to_numpy()
    e, C = cfg["economics"], rto_cost(cfg)
    t = cfg["prepaid_nudge"]["expected_take_up_percent"] / 100
    catch = {
        "ship": 0.0,
        "confirm": pct(cfg, "confirmation_catches_rto_percent"),
        "hold": pct(cfg, "hold_catches_rto_percent"),
    }
    lose = {
        "ship": 0.0,
        "confirm": pct(cfg, "confirmation_loses_genuine_percent"),
        "hold": pct(cfg, "hold_loses_genuine_percent"),
    }
    catch["nudge"] = t * (1 - pct(cfg, "prepaid_rto_percent")) + (1 - t) * catch["confirm"]
    lose["nudge"] = (1 - t) * lose["confirm"]

    a = d["action"]
    first_chance = (orders["customer_prior_rto"].to_numpy() == 0)
    value_lost = margin(cfg, d.order_value) + e["new_customer_acquisition_cost"] * first_chance
    d["act_saved"] = d.is_rto * a.map(catch) * C
    d["act_lost_genuine"] = (1 - d.is_rto) * a.map(lose) * value_lost
    d["act_net"] = d.act_saved - d.act_lost_genuine - d.discount - d.messaging
    return d


def summarise(d: pd.DataFrame) -> pd.DataFrame:
    g = d.groupby("action").agg(
        orders=("action", "size"),
        avg_risk=("risk", "mean"),
        rtos_avoided=("rtos_avoided", "sum"),
        saved=("saved", "sum"),
        lost_genuine=("lost_genuine", "sum"),
        discount=("discount", "sum"),
        messaging=("messaging", "sum"),
        net=("net", "sum"),
        **({"actual_rto_rate": ("is_rto", "mean"), "act_net": ("act_net", "sum")} if "is_rto" in d else {}),
    )
    g = g.reindex(ACTIONS).fillna(0)
    g.index = [ACTION_LABELS[a] for a in g.index]
    return g
