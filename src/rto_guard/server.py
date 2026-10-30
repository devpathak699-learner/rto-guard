"""Tiny web server for the dashboard, order checker, WhatsApp mock and rules editor.

Standard library only (http.server), so there is nothing extra to install.
The "live" data is September: the month the model never saw in training.
"""

from __future__ import annotations

import copy
import json
import math
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd

from . import ROOT
from .backtest import realised
from .config import ConfigError, load_config, rto_cost, save_config, validate
from .data import load_orders
from .economics import ACTION_LABELS, ACTIONS, break_even_discount, impact
from .engine import DecisionEngine
from .text_checks import gemini_available
from .whatsapp import MockWhatsAppAPI

LIVE_FROM = "2026-09-01"
INDEX = ROOT / "app" / "index.html"


class State:
    def __init__(self):
        self.cfg = load_config()
        self.engine = DecisionEngine(cfg=self.cfg)
        self.orders = load_orders()
        live = self.orders[self.orders.order_datetime >= LIVE_FROM].reset_index(drop=True)
        # Text checks + model once; changing thresholds only re-applies the rules.
        self.scored = self.engine.score_frame(live, use_llm=False)
        self.wa = MockWhatsAppAPI()
        self.refresh()

    def decisions(self, cfg: dict) -> pd.DataFrame:
        self.engine.cfg = cfg
        live = self.scored[0]
        d = realised(cfg, self.engine.decide_frame(live, scored=self.scored), live)
        self.engine.cfg = self.cfg
        return pd.concat([live.drop(columns=["is_rto", "order_value"]), d.drop(columns=["order_id"])], axis=1)

    def refresh(self) -> None:
        self.live = self.decisions(self.cfg)
        self.live["date"] = self.live.order_datetime.dt.strftime("%Y-%m-%d")


STATE: State | None = None


def summary(d: pd.DataFrame) -> dict:
    buckets = {
        a: {"label": ACTION_LABELS[a], "orders": int((d.action == a).sum())} for a in ACTIONS
    }
    return {
        "orders": int(len(d)),
        "buckets": buckets,
        "rtos_avoided": float(d.rtos_avoided.sum()),
        "genuine_lost": float(d.genuine_lost.sum()),
        "saved": float(d.saved.sum()),
        "lost_genuine": float(d.lost_genuine.sum()),
        "discount": float(d.discount.sum()),
        "messaging": float(d.messaging.sum()),
        "net": float(d.net.sum()),
        "actual": {
            "rto_rate": float(d.is_rto.mean()) if len(d) else 0,
            "net": float(d.act_net.sum()),
            "saved": float(d.act_saved.sum()),
            "lost_genuine": float(d.act_lost_genuine.sum()),
        },
    }


def order_rows(d: pd.DataFrame) -> list[dict]:
    cols = ["order_id", "order_datetime", "customer_name", "city", "state", "order_value", "action",
            "decision", "risk", "reason", "net", "is_rto", "address", "items", "phone", "pincode",
            "customer_prior_orders", "customer_prior_rto", "utm_source", "hinglish"]
    out = d[cols].copy()
    out["order_datetime"] = out.order_datetime.dt.strftime("%Y-%m-%d %H:%M")
    return out.sort_values("risk", ascending=False).to_dict("records")


# ------------------------------------------------------------------ routes --

def api_dashboard(q: dict) -> dict:
    s = STATE
    dates = sorted(s.live.date.unique())
    date = q.get("date", [dates[-1]])[0]
    day = s.live[s.live.date == date]
    month = s.live[s.live.date <= date]
    daily = (
        s.live.groupby("date")
        .agg(net=("net", "sum"), orders=("action", "size"), held=("action", lambda a: (a == "hold").sum()))
        .reset_index()
        .to_dict("records")
    )
    cod_per_month = 3000 * 0.55
    return {
        "date": date,
        "dates": dates,
        "day": summary(day),
        "month_to_date": summary(month),
        "daily": daily,
        "orders": order_rows(day),
        "scale_to_real_volume": cod_per_month / max(len(s.live), 1),
        "rto_cost": rto_cost(s.cfg),
        "llm": gemini_available(),
    }


def api_decide(body: dict) -> dict:
    order = body["order"]
    for k in ("order_value", "customer_prior_orders", "customer_prior_rto"):
        order[k] = int(order.get(k) or 0)
    d = STATE.engine.decide(order)
    options = {
        a: {"label": ACTION_LABELS[a], **impact(STATE.cfg, a, d.risk, order["order_value"], order["customer_prior_rto"]).__dict__}
        for a in ACTIONS
    }
    for o in options.values():
        o["net"] = o["saved"] - o["lost_genuine"] - o["discount"] - o["messaging"]
    return {
        "action": d.action, "label": d.label, "risk": d.risk, "reason": d.reason,
        "drivers": d.drivers, "rule": d.rule, "address_quality": d.address_quality,
        "hinglish": d.hinglish, "text_source": d.text_source, "options": options,
    }


def api_config_preview(body: dict) -> dict:
    cfg = merge(body["config"])
    validate(cfg)
    new, cur = STATE.decisions(cfg), STATE.live
    return {"proposed": summary(new), "current": summary(cur)}


def api_config_save(body: dict) -> dict:
    cfg = merge(body["config"])
    save_config(cfg)
    STATE.cfg = load_config()
    STATE.engine.cfg = STATE.cfg
    STATE.refresh()
    return {"ok": True, "config": STATE.cfg}


def merge(partial: dict) -> dict:
    cfg = copy.deepcopy(STATE.cfg)
    for section, vals in partial.items():
        for k, v in vals.items():
            old = cfg[section][k]
            cfg[section][k] = type(old)(v) if not isinstance(old, bool) else bool(v)
    return cfg


def api_wa_start(body: dict) -> dict:
    s = STATE
    if "order_id" in body:
        row = s.live[s.live.order_id == body["order_id"]].iloc[0]
        order = {k: (v.item() if hasattr(v, "item") else v) for k, v in row.items()}
        order["order_datetime"] = str(row.order_datetime)
        flow = body.get("flow") or row.action
        hinglish = bool(row.hinglish)
    else:
        order = body["order"]
        order["order_value"] = int(order.get("order_value") or 0)
        flow, hinglish = body["flow"], False
    if flow == "ship":
        flow = "confirm"
    keep = ("order_id", "customer_name", "address", "city", "state", "pincode", "items", "order_value", "order_datetime")
    conv = s.wa.start({k: order.get(k) for k in keep}, flow, cfg=s.cfg, hinglish=hinglish)
    return conv.to_dict()


def api_wa_reply(body: dict) -> dict:
    c = STATE.wa.get(int(body["id"]))
    c.reply(body["text"])
    return c.to_dict()


def api_wa_wait(body: dict) -> dict:
    c = STATE.wa.get(int(body["id"]))
    c.advance_to(c.now + timedelta(hours=float(body["hours"])))
    return c.to_dict()


def api_wa_pay(body: dict) -> dict:
    c = STATE.wa.get(int(body["id"]))
    c.mark_paid()
    return c.to_dict()


def api_discount_curve(_q) -> dict:
    cfg = STATE.cfg
    return {
        "points": [{"risk": r / 100, "break_even": break_even_discount(cfg, r / 100)} for r in range(5, 96, 5)],
        "discount": cfg["prepaid_nudge"]["discount_rupees"],
    }


GET = {"/api/dashboard": api_dashboard, "/api/config": lambda q: STATE.cfg, "/api/discount": api_discount_curve}
POST = {
    "/api/decide": api_decide,
    "/api/config/preview": api_config_preview,
    "/api/config/save": api_config_save,
    "/api/wa/start": api_wa_start,
    "/api/wa/reply": api_wa_reply,
    "/api/wa/wait": api_wa_wait,
    "/api/wa/pay": api_wa_pay,
}


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return None if math.isnan(o) else float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, fn, arg) -> None:
        try:
            out = json.dumps(_clean(fn(arg)), ensure_ascii=False).encode()
            self._send(200, out, "application/json; charset=utf-8")
        except (ConfigError, KeyError, ValueError, IndexError) as e:
            self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        if u.path in GET:
            return self._json(GET[u.path], parse_qs(u.query))
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        if u.path not in POST:
            return self._send(404, b"not found", "text/plain")
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        self._json(POST[u.path], body)

    def log_message(self, *args):  # keep the terminal quiet
        pass


def serve(port: int = 8000) -> ThreadingHTTPServer:
    global STATE
    STATE = State()
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
