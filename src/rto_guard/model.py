"""RTO risk score: a small logistic regression written in numpy.

Why logistic regression and not XGBoost / an LLM:
  * ~2,000 labelled orders is small; a 10-feature linear model doesn't overfit
    and is well calibrated (a "40%" score really means about 4 in 10 come back),
    which the ₹ maths depends on.
  * Every feature's push on the score can be read off and turned into the
    plain-English reason an ops person sees.
  * No heavy dependencies: it trains in under a second.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import MODEL_PATH
from .data import phone_is_valid
from .text_checks import check

FEATURES = [
    "first_order",
    "past_rto_1",
    "past_rto_2plus",
    "area_risk",
    "addr_landmark",
    "addr_vague",
    "addr_unusable",
    "fake_name",
    "bad_phone",
    "high_value",
]

# The words an ops person sees when a feature raises (or lowers) the risk.
REASON_TEXT = {
    "first_order": "first order from this customer",
    "past_rto_1": "refused 1 earlier COD order",
    "past_rto_2plus": "refused {n} earlier COD orders",
    "area_risk": "{area} has a high return rate ({rate:.0%})",
    "area_risk_low": "{area} usually accepts deliveries ({rate:.0%} returns)",
    "addr_landmark": "address has no house number (landmark only)",
    "addr_vague": "address is too vague",
    "addr_unusable": "address is unusable",
    "fake_name": "name looks fake",
    "bad_phone": "phone number is not a valid 10-digit mobile",
    "high_value": "high order value (₹{value:,.0f})",
    "repeat_good": "repeat customer with {d} delivered orders",
}

SMOOTHING = 30  # an area needs ~30 orders before its own history outweighs its parent's


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


@dataclass
class AreaRates:
    """Return rate by pincode -> city -> state, each shrunk toward its parent.

    Pincodes in this data have at most 8 orders each, so one pincode's raw
    rate is mostly noise. Shrinkage lets a pincode earn its own rate only
    once it has enough history.
    """

    global_rate: float = 0.28
    state: dict = field(default_factory=dict)
    city: dict = field(default_factory=dict)
    pincode: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)

    @classmethod
    def fit(cls, df: pd.DataFrame, k: float = SMOOTHING) -> "AreaRates":
        g = df["is_rto"].mean()
        a = cls(global_rate=g)
        for st, grp in df.groupby("state"):
            a.state[st] = (grp.is_rto.sum() + k * g) / (len(grp) + k)
        for (st, c), grp in df.groupby(["state", "city"]):
            parent = a.state.get(st, g)
            a.city[f"{st}|{c}"] = (grp.is_rto.sum() + k * parent) / (len(grp) + k)
            a.counts[f"{st}|{c}"] = len(grp)
        for (st, c, pin), grp in df.dropna(subset=["pincode"]).groupby(["state", "city", "pincode"]):
            parent = a.city.get(f"{st}|{c}", a.state.get(st, g))
            a.pincode[str(pin)] = (grp.is_rto.sum() + k * parent) / (len(grp) + k)
            a.counts[str(pin)] = len(grp)
        return a

    def rate(self, state: str, city: str, pincode) -> float:
        if pincode and str(pincode) in self.pincode:
            return self.pincode[str(pincode)]
        return self.city.get(f"{state}|{city}", self.state.get(state, self.global_rate))

    def area_name(self, state: str, city: str) -> str:
        return city or state


def build_features(df: pd.DataFrame, areas: AreaRates, use_llm: bool = False) -> pd.DataFrame:
    rows = []
    for r in df.itertuples(index=False):
        tv = check(r.customer_name, r.address, r.city, use_llm=use_llm)
        rate = areas.rate(r.state, r.city, r.pincode)
        rows.append(
            {
                "first_order": int(r.customer_prior_orders == 0),
                "past_rto_1": int(r.customer_prior_rto == 1),
                "past_rto_2plus": int(r.customer_prior_rto >= 2),
                "area_risk": _logit(rate) - _logit(areas.global_rate),
                "addr_landmark": int(tv.address_quality == "landmark"),
                "addr_vague": int(tv.address_quality == "vague"),
                "addr_unusable": int(tv.address_quality == "unusable"),
                "fake_name": int(tv.name_looks_fake),
                "bad_phone": int(not phone_is_valid(r.phone)),
                "high_value": int(r.order_value > 1500),
                # carried along for reasons / decisions, not used by the model
                "_area_rate": rate,
                "_address_quality": tv.address_quality,
                "_address_note": tv.address_note,
                "_name_note": tv.name_note,
                "_hinglish": tv.hinglish,
                "_text_source": tv.source,
            }
        )
    return pd.DataFrame(rows, index=df.index)


@dataclass
class RiskModel:
    coef: dict
    intercept: float
    areas: AreaRates

    # -------------------------------------------------------------- train --
    @classmethod
    def fit(cls, df: pd.DataFrame, l2: float = 1.0, iters: int = 50, folds: int = 5) -> "RiskModel":
        areas = AreaRates.fit(df)
        # Area rates for training rows come from the *other* folds. Using an
        # order's own outcome to describe its area leaked the answer: the
        # first version did that and over-weighted area risk by ~70%.
        idx = np.random.default_rng(0).permutation(len(df))
        parts = []
        for fold in np.array_split(idx, folds):
            held = df.iloc[fold]
            parts.append(build_features(held, AreaRates.fit(df.drop(held.index))))
        X = pd.concat(parts).loc[df.index][FEATURES].to_numpy(float)
        y = df["is_rto"].to_numpy(float)
        Xb = np.hstack([np.ones((len(X), 1)), X])
        w = np.zeros(Xb.shape[1])
        reg = np.eye(len(w)) * l2
        reg[0, 0] = 0  # don't shrink the intercept
        for _ in range(iters):  # Newton-Raphson / IRLS
            p = 1 / (1 + np.exp(-Xb @ w))
            grad = Xb.T @ (p - y) + reg @ w
            hess = (Xb * (p * (1 - p))[:, None]).T @ Xb + reg
            step = np.linalg.solve(hess, grad)
            w -= step
            if np.abs(step).max() < 1e-8:
                break
        return cls({f: round(float(c), 4) for f, c in zip(FEATURES, w[1:])}, round(float(w[0]), 4), areas)

    # ------------------------------------------------------------ predict --
    def predict(self, feats: pd.DataFrame) -> np.ndarray:
        z = self.intercept + sum(feats[f] * c for f, c in self.coef.items())
        return 1 / (1 + np.exp(-z.to_numpy(float)))

    def contributions(self, feat_row: pd.Series) -> dict:
        """How many log-odds each feature added for this order (0 = no effect)."""
        return {f: float(feat_row[f] * c) for f, c in self.coef.items() if feat_row[f]}

    # -------------------------------------------------------- save / load --
    def save(self, path=MODEL_PATH) -> None:
        path.parent.mkdir(exist_ok=True)
        a = self.areas
        path.write_text(
            json.dumps(
                {
                    "intercept": self.intercept,
                    "coef": self.coef,
                    "areas": {
                        "global_rate": a.global_rate, "state": a.state, "city": a.city,
                        "pincode": a.pincode, "counts": a.counts,
                    },
                },
                indent=1,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path=MODEL_PATH) -> "RiskModel":
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(d["coef"], d["intercept"], AreaRates(**d["areas"]))


def auc(y: np.ndarray, p: np.ndarray) -> float:
    """Area under the ROC curve (rank-based, no sklearn needed)."""
    order = np.argsort(p)
    ranks = np.empty(len(p))
    ranks[order] = np.arange(1, len(p) + 1)
    pos = y == 1
    n_pos, n_neg = pos.sum(), (~pos).sum()
    return float((ranks[pos].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))
