"""Run with:  python -m unittest discover tests   (or: pytest)"""

import copy
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rto_guard import CONFIG_PATH  # noqa: E402
from rto_guard.config import ConfigError, load_config, rto_cost, save_config, validate  # noqa: E402
from rto_guard.data import load_orders, normalise_phone, phone_is_valid  # noqa: E402
from rto_guard.economics import break_even_discount, cancel_break_even_risk, impact  # noqa: E402
from rto_guard.engine import DecisionEngine  # noqa: E402
from rto_guard.model import RiskModel  # noqa: E402
from rto_guard.text_checks import rule_check  # noqa: E402
from rto_guard.whatsapp import classify_reply, start_conversation  # noqa: E402

CFG = load_config()
BASE = dict(
    order_id="T1", customer_name="Neha Mishra", phone="7581731174", order_value=798,
    address="B-597, Sunrise Society, Gandhi Marg", city="Mumbai", state="Maharashtra",
    pincode="400033", customer_prior_orders=0, customer_prior_rto=0, items="Satin Bonnet",
    order_datetime="2026-09-30 15:00",
)


def _engine():
    df = load_orders()
    model = RiskModel.fit(df[df.order_datetime < "2026-09-01"])
    return DecisionEngine(model=model, cfg=copy.deepcopy(CFG), use_llm=False)


ENGINE = _engine()


class DataCleaning(unittest.TestCase):
    def test_phone_formats(self):
        self.assertEqual(normalise_phone("+91 98451 18261"), "9845118261")
        self.assertTrue(phone_is_valid("9845118261"))
        self.assertFalse(phone_is_valid("984511826"))  # 9 digits

    def test_statuses_and_duplicates_cleaned(self):
        df = load_orders()
        self.assertEqual(set(df.final_status), {"DELIVERED", "RTO"})
        self.assertFalse(df.duplicated(subset=["order_id", "order_datetime"]).any())


class TextChecks(unittest.TestCase):
    def test_address_levels(self):
        self.assertEqual(rule_check("A B", "xyz").address_quality, "unusable")
        self.assertEqual(rule_check("A B", "Patna", "Patna").address_quality, "unusable")
        self.assertEqual(rule_check("A B", "near station").address_quality, "vague")
        self.assertEqual(rule_check("A B", "Hanuman Mandir ke peeche wala ghar").address_quality, "landmark")
        self.assertEqual(rule_check("A B", "Flat 408, Royal Apartments, Mall Road").address_quality, "complete")

    def test_hinglish_is_not_junk(self):
        v = rule_check("Riya Mehta", "gali no 22 ke andar, Petrol Pump ke paas")
        self.assertIn(v.address_quality, ("landmark", "complete"))
        self.assertTrue(v.hinglish)

    def test_fake_names(self):
        for n in ("asdf", "qwerty", "test", "Test Order", "xyz xyz", "aaa", "na"):
            self.assertTrue(rule_check(n, "x").name_looks_fake, n)
        for n in ("Kavya Iyer", "Harpreet Kaur", "Aisha Khan"):
            self.assertFalse(rule_check(n, "x").name_looks_fake, n)


class Economics(unittest.TestCase):
    def test_rto_cost_matches_brief(self):
        self.assertAlmostEqual(rto_cost(CFG), 65 + 65 + 25 + 0.08 * 180)

    def test_shipping_is_free_and_cancel_rarely_pays(self):
        self.assertEqual(impact(CFG, "ship", 0.5, 649).net, 0)
        # Cancelling only beats shipping when most such orders come back.
        self.assertGreater(cancel_break_even_risk(CFG, 649), 0.7)

    def test_discount_break_even_rises_with_risk(self):
        self.assertLess(break_even_discount(CFG, 0.2), 50)
        self.assertGreater(break_even_discount(CFG, 0.4), 50)


class Engine(unittest.TestCase):
    def decide(self, **kw):
        return ENGINE.decide({**BASE, **kw})

    def test_every_decision_has_reason(self):
        d = self.decide()
        self.assertIn(d.action, ("ship", "nudge", "confirm", "hold"))
        self.assertIn("return risk", d.reason)

    def test_clean_metro_order_ships(self):
        self.assertEqual(self.decide().action, "ship")

    def test_loyal_customer_always_ships(self):
        d = self.decide(customer_prior_orders=3, city="Darbhanga", state="Bihar", pincode="846002")
        self.assertEqual(d.action, "ship")

    def test_serial_refuser_held(self):
        self.assertEqual(self.decide(customer_prior_orders=4, customer_prior_rto=2).action, "hold")

    def test_junk_address_needs_confirmation_not_cancel(self):
        self.assertEqual(self.decide(address="xyz").action, "confirm")

    def test_first_time_buyer_not_held_without_evidence(self):
        d = self.decide(city="Darbhanga", state="Bihar", pincode=None, order_value=2499,
                        address="near Big Bazaar, ghar ka naam Om niwas", phone="984511826")
        self.assertNotEqual(d.action, "ship")
        e = ENGINE.cfg["safety_rules"]
        e["protect_first_time_customers"] = True
        d2 = self.decide(city="Darbhanga", state="Bihar", address="near Big Bazaar, ghar ka naam Om niwas")
        self.assertNotEqual(d2.action, "hold")

    def test_thresholds_change_decisions_without_code(self):
        cfg = copy.deepcopy(CFG)
        cfg["risk_bands"].update(ship_below=1, nudge_below=2, confirm_below=3)
        cfg["safety_rules"].update(protect_first_time_customers=False)
        e = DecisionEngine(model=ENGINE.model, cfg=cfg, use_llm=False)
        self.assertEqual(e.decide({**BASE}).action, "hold")

    def test_model_beats_coin_flip_on_september(self):
        from rto_guard.model import auc, build_features
        df = load_orders()
        sep = df[df.order_datetime >= "2026-09-01"]
        p = ENGINE.model.predict(build_features(sep, ENGINE.model.areas))
        self.assertGreater(auc(sep.is_rto.values, p), 0.68)


class Config(unittest.TestCase):
    def test_rejects_bands_out_of_order(self):
        bad = copy.deepcopy(CFG)
        bad["risk_bands"].update(ship_below=50, nudge_below=40)
        with self.assertRaises(ConfigError):
            validate(bad)

    def test_save_keeps_comments(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "rules.yaml"
            p.write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            cfg = load_config(p)
            cfg["risk_bands"]["ship_below"] = 25
            save_config(cfg, p)
            text = p.read_text(encoding="utf-8")
            self.assertIn("ship_below: 25", text)
            self.assertIn("# Below this risk we just ship it.", text)
            self.assertEqual(load_config(p)["risk_bands"]["ship_below"], 25)


class WhatsApp(unittest.TestCase):
    def test_reply_understanding(self):
        for t in ("1", "Yes", "haan ji", "ok", "👍"):
            self.assertEqual(classify_reply(t, use_llm=False)[0], "confirm", t)
        for t in ("2", "no", "nahi chahiye", "cancel karo"):
            self.assertEqual(classify_reply(t, use_llm=False)[0], "cancel", t)
        self.assertEqual(classify_reply("address change karna hai", use_llm=False)[0], "change_address")
        self.assertEqual(classify_reply("asdkj lol", use_llm=False)[0], "unclear")

    def test_language(self):
        self.assertEqual(start_conversation({**BASE, "state": "Bihar"}, "confirm", CFG).lang, "hi")
        self.assertEqual(start_conversation(BASE, "confirm", CFG).lang, "en")

    def test_yes_ships_no_cancels(self):
        c = start_conversation(BASE, "confirm", CFG)
        c.reply("yes", use_llm=False)
        self.assertEqual(c.outcome, "ship_cod")
        c = start_conversation(BASE, "confirm", CFG)
        c.reply("no", use_llm=False)
        self.assertEqual(c.outcome, "cancelled")

    def test_two_unclear_replies_go_to_human(self):
        c = start_conversation(BASE, "confirm", CFG)
        c.reply("??", use_llm=False)
        c.reply("hmm", use_llm=False)
        self.assertEqual(c.state, "needs_human")

    def test_no_reply_rules(self):
        c = start_conversation(BASE, "confirm", CFG)
        c.advance_to(c.sent_at + timedelta(hours=25))
        self.assertEqual(c.outcome, "ship_cod")
        self.assertEqual(c.reminders_sent, 2)
        h = start_conversation(BASE, "hold", CFG)
        h.advance_to(h.sent_at + timedelta(hours=25))
        self.assertEqual(h.outcome, "cancelled")

    def test_quiet_hours(self):
        c = start_conversation({**BASE, "order_datetime": "2026-09-29 23:30"}, "confirm", CFG)
        self.assertEqual(c.sent_at.hour, 9)
        c.advance_to(c.sent_at + timedelta(hours=25))
        brand = [m for m in c.messages if m.sender == "brand"]
        self.assertTrue(all(9 <= m.at.hour < 21 for m in brand))

    def test_nudge_paid(self):
        c = start_conversation(BASE, "nudge", CFG)
        self.assertIn("₹50 OFF", c.messages[-1].text)
        c.mark_paid()
        self.assertEqual(c.outcome, "ship_prepaid")

    def test_junk_address_collected_before_shipping(self):
        c = start_conversation({**BASE, "address": "xyz"}, "confirm", CFG)
        self.assertEqual(c.state, "awaiting_address")
        c.reply("home", use_llm=False)
        self.assertEqual(c.state, "awaiting_address")
        c.reply("Flat 12, Shanti Apartments, MG Road, near SBI Bank", use_llm=False)
        self.assertEqual(c.outcome, "ship_cod")


if __name__ == "__main__":
    unittest.main()
