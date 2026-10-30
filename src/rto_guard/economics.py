"""The ₹ maths. Every number on the dashboard and in the memo comes from here.

All values are the change versus today's policy, which ships every COD order
as-is. "saved" = RTO losses avoided; "lost" = margin and customers given up,
discounts paid and messages sent.

Assumptions written down once:
  * Margin on a delivered order = gross margin % x order value - forward
    shipping (we pay the courier on delivered orders too).
  * A genuine customer we turn away costs their margin on this order plus the
    ₹300 Meta would charge to win an equivalent customer back. The ₹300 is
    not counted for someone who has already refused one of our parcels.
  * Prepaid conversions are credited ONLY with the avoided RTO cost, not with
    extra margin: people who switch to prepaid were probably going to accept
    the parcel anyway. This makes the nudge look worse than it likely is.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import rto_cost

ACTIONS = ("ship", "nudge", "confirm", "hold")
ACTION_LABELS = {
    "ship": "Ship as-is",
    "confirm": "Ship after confirmation",
    "nudge": "Nudge to prepaid",
    "hold": "Hold / cancel",
}


@dataclass
class Impact:
    saved: float = 0.0          # ₹ of RTO loss avoided
    lost_genuine: float = 0.0   # ₹ margin + customer value lost on genuine orders we stopped
    discount: float = 0.0       # ₹ prepaid discounts paid
    messaging: float = 0.0      # ₹ WhatsApp cost
    rtos_avoided: float = 0.0   # expected number of RTOs avoided
    genuine_lost: float = 0.0   # expected number of genuine orders lost

    @property
    def net(self) -> float:
        return self.saved - self.lost_genuine - self.discount - self.messaging

    def __add__(self, o: "Impact") -> "Impact":
        return Impact(*(getattr(self, f) + getattr(o, f) for f in self.__dataclass_fields__))


def pct(cfg: dict, key: str) -> float:
    return cfg["economics"][key] / 100


def margin(cfg: dict, order_value: float) -> float:
    return pct(cfg, "gross_margin_percent") * order_value - cfg["economics"]["forward_shipping"]


def discount_for(cfg: dict, order_value: float) -> float:
    n = cfg["prepaid_nudge"]
    return min(n["discount_rupees"], n["max_discount_percent"] / 100 * order_value)


def lost_customer_value(cfg: dict, order_value: float, past_rtos: int = 0) -> float:
    """₹ we give up when a genuine customer walks away because of us."""
    replace = cfg["economics"]["new_customer_acquisition_cost"] if past_rtos == 0 else 0
    return margin(cfg, order_value) + replace


def impact(cfg: dict, action: str, p: float, order_value: float, past_rtos: int = 0) -> Impact:
    """Expected ₹ effect of taking `action` on one order with RTO risk `p`."""
    e = cfg["economics"]
    C = rto_cost(cfg)
    lose_value = lost_customer_value(cfg, order_value, past_rtos)
    msg = e["whatsapp_cost_per_order"]

    if action == "ship":
        return Impact()

    if action == "confirm":
        caught = p * pct(cfg, "confirmation_catches_rto_percent")
        lost = (1 - p) * pct(cfg, "confirmation_loses_genuine_percent")
        return Impact(caught * C, lost * lose_value, 0, msg, caught, lost)

    if action == "nudge":
        # A take-up share switch to prepaid; everyone else gets the
        # "confirm your COD order" button in the same message.
        t = cfg["prepaid_nudge"]["expected_take_up_percent"] / 100
        p_pre = pct(cfg, "prepaid_rto_percent")
        conv_avoided = t * max(p - p_pre, 0)
        conf = impact(cfg, "confirm", p, order_value, past_rtos)
        disc = t * (1 - p_pre) * discount_for(cfg, order_value)
        return Impact(
            conv_avoided * C + (1 - t) * conf.saved,
            (1 - t) * conf.lost_genuine,
            disc,
            msg,
            conv_avoided + (1 - t) * conf.rtos_avoided,
            (1 - t) * conf.genuine_lost,
        )

    if action == "hold":
        caught = p * pct(cfg, "hold_catches_rto_percent")
        lost = (1 - p) * pct(cfg, "hold_loses_genuine_percent")
        return Impact(caught * C, lost * lose_value, 0, msg, caught, lost)

    raise ValueError(action)


def break_even_discount(cfg: dict, p: float) -> float:
    """Largest prepaid discount that still pays for itself on a customer who
    switches, counting only the RTO cost avoided (no extra margin)."""
    p_pre = pct(cfg, "prepaid_rto_percent")
    return max(p - p_pre, 0) * rto_cost(cfg) / (1 - p_pre)


def cancel_break_even_risk(cfg: dict, order_value: float, count_customer_value: bool = True) -> float:
    """Risk above which cancelling outright beats shipping."""
    m = lost_customer_value(cfg, order_value, past_rtos=0 if count_customer_value else 1)
    return m / (m + rto_cost(cfg))
