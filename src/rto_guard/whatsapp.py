"""WhatsApp confirmation and prepaid-nudge flow, with a mock send API.

One Conversation per order. It is a small state machine:

    queued ──(send window opens)──> awaiting_reply ──yes──> confirmed  -> ship (COD)
                                        │  ├──pay────────> paid       -> ship (prepaid)
                                        │  ├──no─────────> cancelled
                                        │  ├──address────> awaiting_address ─valid─> confirmed
                                        │  └──unclear x2─> needs_human (ops queue)
                                        └──(4h, 20h) reminders ──(24h)──> no-reply rule in rules.yaml

Reading replies: buttons and obvious words ("1", "yes", "haan", "nahi")
are handled by code: instant, free, never wrong. Only free text that the
code can't match ("kal bhej dena", "who is this?") goes to Gemini, and
whatever Gemini says still has to be one of a fixed set of intents.
"""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import load_config
from .economics import discount_for
from .text_checks import call_gemini, gemini_available, rule_check

HINDI_BELT = {
    "Uttar Pradesh", "Bihar", "Madhya Pradesh", "Rajasthan", "Delhi", "Haryana",
    "Jharkhand", "Chhattisgarh", "Uttarakhand", "Himachal Pradesh",
}
INTENTS = ("confirm", "cancel", "pay", "change_address", "language", "question", "unclear")

# ------------------------------------------------------------------ copy ---

T = {
    "en": {
        "confirm": (
            "Hi {first}! 🌿 Thanks for your {brand} order #{oid}:\n{items} — ₹{value:,}\n"
            "Payment: Cash on Delivery\n\nPlease confirm so we can ship it today.\n"
            "1️⃣ Confirm order\n2️⃣ Cancel order\n3️⃣ Change address"
        ),
        "confirm_addr": (
            "Hi {first}! 🌿 Thanks for your {brand} order #{oid} ({items}, ₹{value:,}).\n\n"
            "We couldn't find a complete delivery address (we have: \"{address}\"). "
            "Please reply with your full address — house/flat no., street, area and a landmark — "
            "so the courier can reach you.\n\n2️⃣ Cancel order"
        ),
        "nudge": (
            "Hi {first}! 🌿 Your {brand} order #{oid} is packed:\n{items} — ₹{value:,}\n\n"
            "💳 Pay online now and get ₹{disc} OFF — pay only ₹{net:,}.\n👉 {link}\n\n"
            "Prefer cash? Reply 1️⃣ to confirm Cash on Delivery.\n2️⃣ Cancel order"
        ),
        "hold": (
            "Hi {first}, your {brand} order #{oid} ({items}, ₹{value:,}) is reserved for you. 🌿\n\n"
            "To ship it, please pay online — we'll take ₹{disc} off, so you pay only ₹{net:,}.\n👉 {link}\n\n"
            "If we don't hear from you in 24 hours the order will be cancelled. Nothing will be charged.\n"
            "2️⃣ Cancel order"
        ),
        "reminder": "Just checking, {first} 🙂 Your order #{oid} is waiting. Reply 1️⃣ to confirm{pay_hint} or 2️⃣ to cancel.",
        "reminder_hold": "Your order #{oid} will be cancelled in {hours} hours. Pay ₹{net:,} here to keep it: {link}",
        "confirmed": "Confirmed! ✅ Order #{oid} ships today. Please keep ₹{value:,} ready at delivery.",
        "paid": "Payment received ✅ Order #{oid} ships today. You saved ₹{disc}!",
        "cancelled": "Done — order #{oid} is cancelled and nothing will be charged. We hope to see you again 🌿",
        "ask_address": "Sure! Please type your full address: house/flat no., street, area, landmark and pincode.",
        "address_ok": "Thanks! Updated address: \"{address}\". Your order #{oid} is confirmed and ships today ✅",
        "address_bad": "Sorry, we still can't find that. Please add your house/flat number and area, e.g. \"Flat 12, Shanti Apartments, MG Road, near SBI Bank\".",
        "hold_need_pay": "Thanks {first}! For this order we can only ship after online payment: {link} (₹{disc} off).",
        "pay_link": "Here's your secure payment link 💳 {link}\n₹{disc} off — pay only ₹{net:,}. We ship as soon as it's paid.",
        "unclear": "Sorry, I didn't get that 🙏 Please reply with a number:\n1️⃣ Confirm order\n2️⃣ Cancel order\n3️⃣ Change address",
        "human": "Thanks for your message! A member of our team will reply shortly. 🌿",
        "answer": "{answer}\n\nReply 1️⃣ to confirm or 2️⃣ to cancel your order #{oid}.",
        "lang_switched": "Sure, we'll continue in English.",
        "noreply_ship": "We haven't heard back, so we're shipping order #{oid} as Cash on Delivery. Reply 2️⃣ any time before delivery to cancel.",
        "noreply_cancel": "We didn't hear back, so order #{oid} has been cancelled. Nothing was charged. You can re-order any time 🌿",
    },
    "hi": {
        "confirm": (
            "Namaste {first} ji! 🌿 {brand} se order karne ke liye dhanyavaad. Order #{oid}:\n{items} — ₹{value:,}\n"
            "Payment: Cash on Delivery\n\nKripya confirm karein taaki hum aaj hi bhej sakein.\n"
            "1️⃣ Order confirm karein\n2️⃣ Order cancel karein\n3️⃣ Address badlein"
        ),
        "confirm_addr": (
            "Namaste {first} ji! 🌿 Aapka {brand} order #{oid} ({items}, ₹{value:,}) mila.\n\n"
            "Humein poora address nahi mila (abhi hai: \"{address}\"). Kripya poora address bhejein — "
            "ghar/flat no., gali, mohalla aur paas ka landmark — taaki courier aap tak pahunch sake.\n\n"
            "2️⃣ Order cancel karein"
        ),
        "nudge": (
            "Namaste {first} ji! 🌿 Aapka {brand} order #{oid} pack ho gaya hai:\n{items} — ₹{value:,}\n\n"
            "💳 Abhi online pay karein aur ₹{disc} ki CHHOOT paayein — sirf ₹{net:,} dena hoga.\n👉 {link}\n\n"
            "Cash se dena hai? 1️⃣ reply karke Cash on Delivery confirm karein.\n2️⃣ Order cancel karein"
        ),
        "hold": (
            "Namaste {first} ji, aapka {brand} order #{oid} ({items}, ₹{value:,}) aapke liye rakha hai. 🌿\n\n"
            "Ise bhejne ke liye online payment karein — ₹{disc} ki chhoot ke saath sirf ₹{net:,}.\n👉 {link}\n\n"
            "24 ghante mein jawab na milne par order cancel ho jayega. Koi paisa nahi katega.\n"
            "2️⃣ Order cancel karein"
        ),
        "reminder": "{first} ji, aapka order #{oid} intezaar kar raha hai 🙂 Confirm ke liye 1️⃣{pay_hint}, cancel ke liye 2️⃣ bhejein.",
        "reminder_hold": "Aapka order #{oid} {hours} ghante mein cancel ho jayega. Rakhne ke liye ₹{net:,} yahan pay karein: {link}",
        "confirmed": "Confirm ho gaya! ✅ Order #{oid} aaj nikal jayega. Delivery par ₹{value:,} tayyar rakhein.",
        "paid": "Payment mil gaya ✅ Order #{oid} aaj nikal jayega. Aapne ₹{disc} bachaye!",
        "cancelled": "Ho gaya — order #{oid} cancel kar diya gaya hai, koi paisa nahi katega. Phir milenge 🌿",
        "ask_address": "Zaroor! Kripya poora address likhein: ghar/flat no., gali, mohalla, landmark aur pincode.",
        "address_ok": "Dhanyavaad! Naya address: \"{address}\". Order #{oid} confirm ho gaya, aaj nikal jayega ✅",
        "address_bad": "Maaf kijiye, yeh address abhi bhi adhoora hai. Ghar/flat number aur mohalla zaroor likhein, jaise \"Ghar no. 12, Gali 4, Shanti Nagar, SBI Bank ke paas\".",
        "hold_need_pay": "Dhanyavaad {first} ji! Is order ke liye online payment ke baad hi bhej payenge: {link} (₹{disc} chhoot).",
        "pay_link": "Yeh raha aapka payment link 💳 {link}\n₹{disc} ki chhoot — sirf ₹{net:,} dena hai. Payment hote hi order nikal jayega.",
        "unclear": "Maaf kijiye, samajh nahi aaya 🙏 Kripya number bhejein:\n1️⃣ Confirm\n2️⃣ Cancel\n3️⃣ Address badlein",
        "human": "Aapke message ke liye dhanyavaad! Hamari team jaldi jawab degi. 🌿",
        "answer": "{answer}\n\nOrder #{oid} confirm karne ke liye 1️⃣, cancel ke liye 2️⃣ bhejein.",
        "lang_switched": "Theek hai, ab hum Hindi mein baat karenge.",
        "noreply_ship": "Aapka jawab nahi aaya, isliye order #{oid} Cash on Delivery se bhej rahe hain. Delivery se pehle kabhi bhi 2️⃣ bhejkar cancel kar sakte hain.",
        "noreply_cancel": "Jawab na milne ki wajah se order #{oid} cancel ho gaya. Koi paisa nahi kata. Aap kabhi bhi dobara order kar sakte hain 🌿",
    },
}

# ---------------------------------------------------------- understanding ---

YES = r"^(1|1️⃣|yes|y|yess+|yeah|yep|ok|okay|okk+|ha+n?|haanji|ji|ji haan|confirm(ed)?|done|sure|theek hai|thik hai|bhej do|bhejo|👍|✅)( ji| please| sir| mam)?\W*$"
NO = r"^(2|2️⃣|no|n|nope|nahi|nahin|nhi|na|cancel|cancel karo|cancel kar do|mat bhejo|nahi chahiye|❌|👎)( ji| please| thanks)?\W*$"
ADDR = r"^(3|3️⃣)\W*$|\b(address|addres|adress|pata)\b.*\b(change|badal|galat|wrong|update)|\b(change|badal|update)\b.*\b(address|pata)\b"
PAY = r"^(pay|paid|payment done|paid online|pay kar diya|online)\W*$"
LANG = {"hindi": "hi", "हिंदी": "hi", "english": "en", "angrezi": "en"}

INTENT_PROMPT = """A customer of an Indian haircare brand replied on WhatsApp to a
message asking them to confirm their Cash-on-Delivery order. Reply may be in
English, Hindi or Hinglish.

Customer reply: "{text}"

Classify into exactly one intent:
confirm (wants the order), cancel (doesn't want it / didn't order),
change_address, pay (wants to pay online), question (asks something about
the order or product), unclear (anything else).
If it is a question, also write a short helpful answer in the customer's
language (max 25 words). If you don't know the answer, say the team will reply.

JSON only: {{"intent": "...", "answer": "..."}}"""


def classify_reply(text: str, use_llm: bool = True) -> tuple[str, str, str]:
    """Return (intent, answer, source). Code first, LLM only for free text."""
    t = re.sub(r"\s+", " ", (text or "").strip().lower())
    if t in LANG:
        return "language", LANG[t], "rules"
    for intent, pattern in (("confirm", YES), ("cancel", NO), ("change_address", ADDR), ("pay", PAY)):
        if re.search(pattern, t):
            return intent, "", "rules"
    if use_llm and gemini_available():
        try:
            r = call_gemini(INTENT_PROMPT.format(text=text))
            if r.get("intent") in INTENTS:
                return r["intent"], r.get("answer", ""), "gemini"
        except Exception:
            pass
    return "unclear", "", "rules"


# ------------------------------------------------------------ conversation --

_ids = itertools.count(1)


@dataclass
class Message:
    at: datetime
    sender: str          # "brand" | "customer" | "system"
    text: str


@dataclass
class Conversation:
    order: dict
    flow: str                       # confirm | nudge | hold
    lang: str
    created: datetime
    cfg: dict
    id: int = field(default_factory=lambda: next(_ids))
    state: str = "queued"
    sent_at: datetime | None = None
    now: datetime | None = None
    unclear_count: int = 0
    reminders_sent: int = 0
    outcome: str = ""               # ship_cod | ship_prepaid | cancelled | human
    messages: list = field(default_factory=list)

    # -------------------------------------------------------- helpers --
    def _vars(self) -> dict:
        o = self.order
        disc = int(round(discount_for(self.cfg, o["order_value"])))
        return {
            "first": (o.get("customer_name") or "there").split()[0].title(),
            "brand": self.cfg["whatsapp"].get("brand_name", "our store"),
            "oid": o.get("order_id", "NEW"),
            "items": o.get("items", "your items").replace("|", ", "),
            "value": int(o["order_value"]),
            "disc": disc,
            "net": int(o["order_value"]) - disc,
            "link": f"https://pay.example.com/{o.get('order_id', 'NEW')}",
            "address": o.get("address", ""),
            "pay_hint": "" if self.flow == "confirm" else (" (ya online pay)" if self.lang == "hi" else " (or pay online)"),
        }

    def _say(self, key: str, **extra) -> None:
        self.messages.append(Message(self.now, "brand", T[self.lang][key].format(**{**self._vars(), **extra})))

    def _log(self, text: str) -> None:
        self.messages.append(Message(self.now, "system", text))

    def _finish(self, outcome: str, key: str) -> None:
        self.state, self.outcome = "done", outcome
        self._say(key)
        self._log(f"Outcome: {outcome.replace('_', ' ')}")

    @property
    def address_unusable(self) -> bool:
        return rule_check(self.order.get("customer_name", ""), self.order.get("address", ""),
                          self.order.get("city", "")).address_quality == "unusable"

    # ----------------------------------------------------------- time --
    def _quiet(self, t: datetime) -> bool:
        w = self.cfg["whatsapp"]
        return t.hour >= w["quiet_hours_start"] or t.hour < w["quiet_hours_end"]

    def send_time(self, t: datetime) -> datetime:
        """First moment we're allowed to message: no texts in quiet hours."""
        w = self.cfg["whatsapp"]
        start, end = w["quiet_hours_start"], w["quiet_hours_end"]
        if t.hour >= start:
            return (t + timedelta(days=1)).replace(hour=end, minute=0)
        if t.hour < end:
            return t.replace(hour=end, minute=0)
        return t

    def _reminder_times(self) -> list[datetime]:
        """Reminder slots. One that would land in quiet hours is pulled back to
        the last allowed hour before them, so it still arrives before the deadline."""
        w = self.cfg["whatsapp"]
        out = []
        for h in (w["first_reminder_after_hours"], w["second_reminder_after_hours"]):
            t = self.sent_at + timedelta(hours=h)
            if self._quiet(t):
                day = t if t.hour >= w["quiet_hours_start"] else t - timedelta(days=1)
                t = day.replace(hour=w["quiet_hours_start"] - 1, minute=0)
            if (not out or t > out[-1]) and t > self.sent_at:
                out.append(t)
        return out

    def start(self) -> "Conversation":
        self.now = self.created
        when = self.send_time(self.created)
        if when > self.created:
            self._log(f"Order placed at {self.created:%H:%M} (quiet hours) — first message queued for {when:%d %b %H:%M}.")
        self.advance_to(when)
        return self

    def advance_to(self, t: datetime) -> None:
        """Move the clock forward, firing whatever is due on the way."""
        w = self.cfg["whatsapp"]
        if self.state == "queued" and t >= self.send_time(self.created):
            self.now = self.send_time(self.created)
            self.sent_at, self.state = self.now, "awaiting_reply"
            key = self.flow
            if self.flow in ("confirm", "nudge") and self.address_unusable:
                key, self.state = "confirm_addr", "awaiting_address"
            self._say(key)
        if self.state in ("awaiting_reply", "awaiting_address") and self.sent_at:
            for i, due in enumerate(self._reminder_times()):
                if self.reminders_sent == i and t >= due and self.state != "done":
                    self.now = due
                    self.reminders_sent += 1
                    left = int(w["give_up_after_hours"] - (self.now - self.sent_at).total_seconds() // 3600)
                    self._say("reminder_hold" if self.flow == "hold" else "reminder", hours=left)
            deadline = self.sent_at + timedelta(hours=w["give_up_after_hours"])
            if t >= deadline and self.state != "done":
                self.now = deadline
                rule = w["hold_no_reply_action"] if self.flow == "hold" else w["confirm_no_reply_action"]
                if self.state == "awaiting_address":
                    rule = "cancel"  # never ship to an address the courier can't find
                self._log(f"No reply in {w['give_up_after_hours']}h → rule says '{rule}'.")
                if rule == "ship":
                    self._finish("ship_cod", "noreply_ship")
                else:
                    self._finish("cancelled", "noreply_cancel")
        self.now = max(self.now or t, t)

    # ---------------------------------------------------------- reply --
    def reply(self, text: str, use_llm: bool = True) -> None:
        if self.state in ("done", "queued", "needs_human"):
            self.messages.append(Message(self.now, "customer", text))
            if self.state == "needs_human":
                self._log("Customer message forwarded to the ops agent.")
            return
        self.messages.append(Message(self.now, "customer", text))

        if self.state == "awaiting_address":
            intent, _, _ = classify_reply(text, use_llm=False)
            if intent == "cancel":
                return self._finish("cancelled", "cancelled")
            if intent in ("confirm", "change_address"):
                return self._say("ask_address")
            v = rule_check(self.order.get("customer_name", ""), text, self.order.get("city", ""))
            if v.address_quality in ("complete", "landmark"):
                self.order["address"] = text.strip()
                self._log(f"New address accepted ({v.address_quality}).")
                if self.flow == "hold":
                    self.state = "awaiting_reply"
                    return self._say("hold_need_pay")
                self.state, self.outcome = "done", "ship_cod"
                self._say("address_ok")
                return self._log("Outcome: ship cod")
            if not re.search(r"\d", text):
                # Looks like chat, not an attempt at an address: answer it, then re-ask.
                intent, answer, _ = classify_reply(text, use_llm=use_llm)
                if answer:
                    self.messages.append(Message(self.now, "brand", answer))
                return self._say("ask_address")
            return self._say("address_bad")

        intent, answer, source = classify_reply(text, use_llm=use_llm)
        self._log(f"Understood as '{intent}' (by {source}).")

        if intent == "language":
            self.lang = answer
            return self._say("lang_switched")
        if intent == "cancel":
            return self._finish("cancelled", "cancelled")
        if intent == "pay":
            return self._say("pay_link")
        if intent == "change_address":
            self.state = "awaiting_address"
            return self._say("ask_address")
        if intent == "confirm":
            if self.flow == "hold":
                return self._say("hold_need_pay")
            return self._finish("ship_cod", "confirmed")
        if intent == "question" and answer:
            return self._say("answer", answer=answer)

        self.unclear_count += 1
        if self.unclear_count >= 2:
            self.state, self.outcome = "needs_human", "human"
            self._say("human")
            return self._log("Sent to ops agent queue after 2 unclear replies.")
        self._say("unclear")

    def mark_paid(self) -> None:
        """Payment-gateway webhook: the customer paid online."""
        if self.state == "done":
            return
        self.messages.append(Message(self.now, "system", "Payment gateway: paid online ✔"))
        self._finish("ship_prepaid", "paid")

    def to_dict(self) -> dict:
        return {
            "id": self.id, "flow": self.flow, "lang": self.lang, "state": self.state,
            "outcome": self.outcome, "now": self.now.isoformat() if self.now else None,
            "order": self.order,
            "messages": [{"at": m.at.strftime("%d %b %H:%M"), "sender": m.sender, "text": m.text} for m in self.messages],
        }


def pick_language(order: dict, hinglish_address: bool = False) -> str:
    return "hi" if order.get("state") in HINDI_BELT or hinglish_address else "en"


def start_conversation(order: dict, flow: str, cfg: dict | None = None, hinglish: bool = False,
                       created: datetime | None = None) -> Conversation:
    if flow not in ("confirm", "nudge", "hold"):
        raise ValueError("Only confirm / nudge / hold orders get a WhatsApp flow.")
    created = created or datetime.fromisoformat(str(order.get("order_datetime") or datetime.now().isoformat()))
    conv = Conversation(dict(order), flow, pick_language(order, hinglish), created, cfg or load_config())
    return conv.start()


class MockWhatsAppAPI:
    """Stands in for the WhatsApp Business API. Keeps conversations in memory."""

    def __init__(self):
        self.conversations: dict[int, Conversation] = {}

    def start(self, order: dict, flow: str, **kw) -> Conversation:
        c = start_conversation(order, flow, **kw)
        self.conversations[c.id] = c
        return c

    def get(self, cid: int) -> Conversation:
        return self.conversations[cid]

    def dump(self, cid: int) -> str:
        return json.dumps(self.get(cid).to_dict(), ensure_ascii=False, indent=1)
