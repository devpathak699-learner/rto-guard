"""The decision engine: one COD order in, one action + plain-English reason out.

    1. text checks  (LLM, or rules as fallback)  -> address quality, fake name
    2. risk model   (maths)                      -> % chance of RTO
    3. risk bands   (config/rules.yaml)          -> ship / nudge / confirm / hold
    4. safety rules (config/rules.yaml)          -> overrides that protect
                                                    loyal and first-time buyers
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .config import load_config
from .economics import ACTION_LABELS, ACTIONS, Impact, impact
from .model import REASON_TEXT, RiskModel, build_features

SEVERITY = {a: i for i, a in enumerate(ACTIONS)}  # ship < nudge < confirm < hold


@dataclass
class Decision:
    order_id: str
    action: str
    risk: float
    reason: str
    drivers: list = field(default_factory=list)
    rule: str = ""
    address_quality: str = ""
    hinglish: bool = False
    text_source: str = "rules"
    impact: Impact = field(default_factory=Impact)

    @property
    def label(self) -> str:
        return ACTION_LABELS[self.action]


def _band(cfg: dict, risk: float) -> str:
    b = cfg["risk_bands"]
    pct = risk * 100
    if pct < b["ship_below"]:
        return "ship"
    if pct < b["nudge_below"]:
        return "nudge"
    if pct < b["confirm_below"]:
        return "confirm"
    return "hold"


def _drivers(model: RiskModel, order, feats: pd.Series) -> list[str]:
    """Top reasons in ops language, biggest push first."""
    contrib = model.contributions(feats)
    contrib.pop("area_risk", None)
    area_push = feats["area_risk"] * model.coef["area_risk"]
    out = []
    area = order.city or order.state
    if area_push > 0.25:
        contrib["area_risk"] = area_push
    for f, v in sorted(contrib.items(), key=lambda kv: -kv[1]):
        if v <= 0.1:
            continue
        out.append(
            REASON_TEXT[f].format(
                n=order.customer_prior_rto, area=area, rate=feats["_area_rate"], value=order.order_value
            )
        )
    delivered = order.customer_prior_orders - order.customer_prior_rto
    if order.customer_prior_orders > 0 and order.customer_prior_rto == 0:
        out.insert(0, REASON_TEXT["repeat_good"].format(d=delivered))
    if area_push < -0.25:
        out.append(REASON_TEXT["area_risk_low"].format(area=area, rate=feats["_area_rate"]))
    return out


def _apply_safety_rules(cfg, order, feats, band) -> tuple[str, str]:
    s = cfg["safety_rules"]
    action, rule = band, ""

    def at_least(a, why):
        nonlocal action, rule
        if SEVERITY[a] > SEVERITY[action]:
            action, rule = a, why

    first = order.customer_prior_orders == 0
    loyal = (
        order.customer_prior_rto == 0
        and order.customer_prior_orders >= s["loyal_min_delivered_orders"]
    )
    evidence = bool(feats["addr_unusable"] or feats["fake_name"] or feats["bad_phone"])

    if order.customer_prior_rto >= s["hold_if_past_rtos_at_least"]:
        return "hold", f"Refused {order.customer_prior_rto} past COD orders — prepaid only."
    if s["always_ship_loyal_customers"] and loyal and not feats["addr_unusable"]:
        return "ship", "Loyal customer — never refused a parcel."

    if s["protect_first_time_customers"] and first and action == "hold" and not evidence:
        action, rule = "confirm", "First-time buyer — confirming instead of holding (protects ad spend)."
    if s["confirm_if_address_unusable"] and feats["addr_unusable"]:
        at_least("confirm", "Address unusable — WhatsApp asks for the full address before we ship.")
    if s["confirm_if_name_looks_fake"] and feats["fake_name"]:
        at_least("confirm", "Name looks fake — needs a WhatsApp confirmation.")
    return action, rule


class DecisionEngine:
    def __init__(self, model: RiskModel | None = None, cfg: dict | None = None, use_llm: bool = True):
        self.model = model or RiskModel.load()
        self.cfg = cfg or load_config()
        self.use_llm = use_llm

    def score_frame(self, orders: pd.DataFrame, use_llm: bool | None = None):
        """The slow part (text checks + model), done once per batch of orders."""
        use_llm = self.use_llm if use_llm is None else use_llm
        orders = orders.reset_index(drop=True)
        feats = build_features(orders, self.model.areas, use_llm=use_llm)
        return orders, feats, self.model.predict(feats)

    def decide_frame(self, orders: pd.DataFrame, use_llm: bool | None = None, scored=None) -> pd.DataFrame:
        """Decide many orders at once (backtests, dashboard). Pass `scored` from
        score_frame() to re-apply changed thresholds without re-scoring."""
        orders, feats, risks = scored or self.score_frame(orders, use_llm)
        rows = [
            self._decide_one(order, feats.iloc[i], float(risks[i]))
            for i, order in enumerate(orders.itertuples(index=False))
        ]
        return pd.DataFrame(
            {
                "order_id": [d.order_id for d in rows],
                "action": [d.action for d in rows],
                "decision": [d.label for d in rows],
                "risk": [d.risk for d in rows],
                "reason": [d.reason for d in rows],
                "address_quality": [d.address_quality for d in rows],
                "hinglish": [d.hinglish for d in rows],
                "saved": [d.impact.saved for d in rows],
                "lost_genuine": [d.impact.lost_genuine for d in rows],
                "discount": [d.impact.discount for d in rows],
                "messaging": [d.impact.messaging for d in rows],
                "rtos_avoided": [d.impact.rtos_avoided for d in rows],
                "genuine_lost": [d.impact.genuine_lost for d in rows],
                "net": [d.impact.net for d in rows],
            }
        )

    def decide(self, order: dict) -> Decision:
        """Score a single order given as a dict with the CSV's column names."""
        defaults = {"customer_prior_orders": 0, "customer_prior_rto": 0, "pincode": None, "city": "", "phone": ""}
        df = pd.DataFrame([{**defaults, **order}])
        feats = build_features(df, self.model.areas, use_llm=self.use_llm)
        risk = float(self.model.predict(feats)[0])
        return self._decide_one(next(df.itertuples(index=False)), feats.iloc[0], risk)

    def _decide_one(self, order, feats: pd.Series, risk: float) -> Decision:
        band = _band(self.cfg, risk)
        action, rule = _apply_safety_rules(self.cfg, order, feats, band)
        drivers = _drivers(self.model, order, feats)
        reason = f"{risk:.0%} return risk"
        if drivers:
            reason += ": " + ", ".join(drivers[:3])
        reason += "."
        if rule:
            reason += f" {rule}"
        return Decision(
            order_id=str(getattr(order, "order_id", "NEW")),
            action=action,
            risk=risk,
            reason=reason[0].upper() + reason[1:],
            drivers=drivers,
            rule=rule,
            address_quality=feats["_address_quality"],
            hinglish=bool(feats["_hinglish"]),
            text_source=feats["_text_source"],
            impact=impact(self.cfg, action, risk, order.order_value, order.customer_prior_rto),
        )
