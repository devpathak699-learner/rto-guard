# WhatsApp flow: "Ship after confirmation", "Nudge to prepaid" and "Hold"

Try it live: `python run.py` → **WhatsApp simulator** tab. Code: `src/rto_guard/whatsapp.py`.

## When the message goes out

| Event | Timing |
|---|---|
| Order placed 9 am – 9 pm | First message **immediately** (confirmed orders make the same-day pickup) |
| Order placed 9 pm – 9 am (~1 in 3 orders) | Queued, sent at **9:00 am** |
| Reminder 1 | +4 h |
| Reminder 2 | +20 h; if that falls in quiet hours, pulled back to **8 pm** |
| Decision | +24 h after the first message |

All timings are in `config/rules.yaml` → `whatsapp`.

## Which language

- **Hinglish** (Hindi in Roman letters, how most people type on WhatsApp) for Hindi-belt states (UP, Bihar, MP, Rajasthan, Delhi, Haryana, Jharkhand, Chhattisgarh, Uttarakhand, HP), or if the customer wrote their address in Hinglish.
- **English** everywhere else.
- The customer can reply **"Hindi"** or **"English"** at any point to switch.

## The three messages (English shown; every message has a Hinglish version)

**Nudge to prepaid** (33–60% risk)
> Hi Riya! 🌿 Your KT Haircare order #KT100529 is packed: Rice Water Shampoo Bar, Scrunchies 6pk, Hair Oil 100ml — ₹1,197
> 💳 Pay online now and get **₹50 OFF**, so you pay only ₹1,147. 👉 *link*
> Prefer cash? Reply 1️⃣ to confirm Cash on Delivery. 2️⃣ Cancel order

**Ship after confirmation** (60–80% risk, junk address, or fake name)
> Hi Riya! 🌿 Thanks for your order #… Payment: Cash on Delivery. Please confirm so we can ship it today.
> 1️⃣ Confirm order 2️⃣ Cancel order 3️⃣ Change address

If the address is junk ("xyz", ".", only a city), the first message instead **asks for the full address** and we don't ship until we have one.

**Hold** (80%+ with evidence, or refused 2+ COD orders)
> Your order is reserved for you. To ship it, please pay online: ₹50 off, so you pay only ₹… If we don't hear from you in 24 hours the order will be cancelled. Nothing will be charged.

## What happens to each reply

| Customer does… | Nudge | Confirm | Hold |
|---|---|---|---|
| **Pays online** (gateway webhook) | ✅ Ship prepaid | ✅ Ship prepaid | ✅ Ship prepaid |
| **"Yes"**: `1`, yes, ok, haan ji, 👍 … | ✅ Ship COD | ✅ Ship COD | Re-send the payment link (prepaid only) |
| **"No"**: `2`, no, nahi chahiye, cancel … | ❌ Cancel before shipping | ❌ Cancel | ❌ Cancel |
| **`3` / "address change karna hai"** | Ask for the address → check it → ship | same | same, then payment link |
| **Something random** ("kab tak aayega?") | Gemini works out the intent; a question gets an answer, then "reply 1 or 2" | same | same |
| **Still unclear the second time** | Send to an ops agent (no bot loop) | same | same |
| **No reply in 24 h** | 🚚 Ship COD | 🚚 **Ship COD** | ❌ **Cancel** |

**Why does a "confirm" order ship when there's no reply?** At our margins an order with 60–80% risk is still worth shipping unless we're sure it's bad (shipping pays up to ~67–79% risk; see memo). Silence isn't proof. Holds are already at the level where cancelling pays. Both are a setting (`confirm_no_reply_action`, `hold_no_reply_action`).

**How replies are read:** buttons and common words are matched by code (instant, free, never wrong). Only free text that doesn't match goes to Gemini, and its answer must be one of: confirm, cancel, pay, change_address, question, unclear. No key → treated as unclear → buttons again → human.

## How much discount, and why ₹50

When a COD customer with risk *p* switches to prepaid, their return chance drops to 4%. Counting **only the RTO cost avoided** (₹169.40) and no extra margin (switchers were probably going to accept anyway), the most we can afford to give a switcher is:

```
break-even discount = (p − 4%) × ₹169.40 ÷ 96%
```

| Risk p | 20% | 30% | **33%** | 40% | 50% | 60% |
|---|---|---|---|---|---|---|
| Break-even | ₹28 | ₹46 | **₹51** | ₹64 | ₹81 | ₹99 |

So:
- **₹50 pays for itself from about 33% risk up**, which is exactly where the nudge band starts. The grid search over ₹ outcomes landed on 33% independently.
- Below 33% we'd be paying ₹50 to people who were going to accept the parcel anyway, so low-risk orders get no offer.
- Above 60% the discount still pays, but those customers need a firm confirmation, not just an offer.
- **Capped at 10% of order value**: a ₹299 order gets ₹30, not ₹50 (17% off would hurt margin and train people to pick COD for a discount).
- Flat ₹ rather than %: "₹50 off" is easier to understand on WhatsApp than "5% off ₹798".

The take-up rate (20%) is an assumption. The dashboard's Rules page shows how the ₹ result changes if it's 10% or 40%, and the memo proposes measuring it with a control group.
