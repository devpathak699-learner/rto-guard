# Decision log

A running list of the choices that shaped RTO Guard, in roughly the order I made them.
Each entry: **what I chose**, the alternatives, and why.

---

### Data

- **Cleaned the data before doing anything with it.** The CSV has 30 exact duplicate rows, `final_status` in 6 spellings (`DELIVERED`, `delivered`, `RTO`, `"RTO "`, `Rto`, `IN_TRANSIT`), phones as 10 digits, 9 digits and `+91 xxxxx xxxxx`, 12 missing pincodes, 4 missing cities and 3 orders worth ₹0.
  - *Alternatives:* drop every messy row, or ignore the mess.
  - *Why:* dropping rows would lose real signal (a truncated phone is itself a risk signal), and ignoring it would double-count duplicates. I normalised what could be normalised, dropped exact duplicates, and dropped the single `IN_TRANSIT` order because it has no outcome yet.

- **Kept September out of training and used it as "last month".** The model learns from April–August (1,658 orders); every ₹ figure in the memo comes from September (329 orders) with its real outcomes.
  - *Alternatives:* random 80/20 split, or cross-validation over all months.
  - *Why:* the founder's real question is "what would this have done last month?" A time-based split answers it directly, and it's harder on the model than a random split because it can't learn from the same weeks it's tested on.

### Where the LLM is used, and where it isn't

- **The LLM (Gemini) only labels text: is the address usable, and does the name look fake?** Everything else is code.
  - *Alternatives:* (a) send the whole order to an LLM and let it decide; (b) no LLM at all.
  - *Why not (a):* an LLM can't do the ₹ maths reliably, can't be calibrated (we need "40% risk" to *mean* 40%), costs money per order, can change its answer between runs, and can't explain itself in a way an auditor can check. Thresholds, history counts, pincode rates and ₹ impact are arithmetic, and code does them better.
  - *Why not (b):* addresses like "Station chowk se left, Hanuman Mandir ke saamne" or "ghar ka naam Om niwas" are a language problem. My rules happen to work on this dataset because its address patterns repeat, but real customer text is far messier.
- **The LLM's output is a feature, not a decision.** The model learns how much "landmark-only address" is worth (+0.31 log-odds) vs. "unusable" (+1.68). If Gemini is down or there's no API key, the same rule-based checks fill in, and an order never waits on the LLM.
- **Rules overrule the LLM on obvious junk.** If the rules already say the address is "xyz" or ".", the LLM can't upgrade it. This prevents a confident-sounding hallucination from shipping a parcel to nowhere.
- **In WhatsApp, code reads buttons and common words; the LLM reads only free text.** "1", "yes", "haan ji", "nahi" and 👍 are matched by patterns, which is instant, free and never wrong. Only replies like "kal bhej dena" or "kab tak aayega?" go to Gemini, and its answer must be one of a fixed set of intents.

### The risk model

- **Logistic regression with 10 readable features, written in numpy.**
  - *Alternatives:* gradient-boosted trees (XGBoost/LightGBM), a neural net, or hand-set point scores.
  - *Why:* 1,658 training rows is small. A simple model gets a held-out AUC of 0.74 and is **well calibrated**: orders scored 32–42% returned 37% of the time. The ₹ maths multiplies by that probability, so calibration matters more than squeezing out a little more AUC. Each feature's weight also turns directly into the plain-English reason ("first order, Patna has a high return rate (46%), address has no house number"). No scikit-learn needed, which keeps setup under 10 minutes.
- **Area risk = pincode → city → state, each pulled toward its parent ("shrinkage").**
  - *Alternatives:* raw pincode RTO rate (what Ops asked for), or ignore geography.
  - *Why:* 1,066 pincodes for 1,987 orders, median 1 order each. A raw pincode rate is mostly noise. With shrinkage, a pincode only earns its own rate after ~30 orders; until then it inherits its city's. I tested this: pincodes that looked bad in Apr–Aug averaged exactly the overall 26% in September.
- **Fixed a leak in my first model.** Version 1 computed each order's area rate *including that order's own outcome*. Training AUC was 0.81 vs. 0.73 on September, and the area weight was inflated by ~70%. I switched to out-of-fold rates (each order's area rate comes from the other 4/5 of the data). Held-out AUC rose to 0.737 and log-loss improved. Also tried: smoothing strength 10/30/60 and dropping the pincode level; 30 with pincodes was best, by a small margin.

### The money

- **Every decision is compared with "ship everything", in ₹.** RTO cost = ₹65 + ₹65 + ₹25 + 8% × ₹180 = **₹169.40**.
- **Delivered margin = 62% × order value − ₹65 forward shipping.**
  - *Alternative:* 62% × value, as the brief literally says.
  - *Why:* gross margin normally excludes shipping, and we pay the courier on delivered orders too. This makes a lost order slightly *cheaper*, which makes the system slightly *more* willing to intervene. It's the conservative choice for Marketing's side, and the memo says so.
- **A lost genuine customer also costs ₹300** (Marketing's Meta CAC, as the cost of replacing them). **Not counted** for a customer who has already refused one of our parcels: we wouldn't pay ₹300 to win back a serial refuser.
- **Prepaid conversions get credit only for the avoided RTO cost, not for extra margin.** People who switch to prepaid were probably going to accept the parcel anyway. This undersells the nudge on purpose.
- **Confirmation, nudge and hold effects are assumptions, and they're in the config:** confirmation stops 35% of would-be RTOs and loses 3% of genuine buyers; 20% of nudged buyers switch; hold stops 90% and loses 40% of genuine buyers. The memo shows the result if they're worse (₹7k/month) or better (₹14k), and proposes a 10% control group to measure them.

### The rules

- **Thresholds were chosen by maximising ₹ on April–August, then checked on September.** I grid-searched ship/nudge/confirm cut-offs. Best: 33% / 60% / 80%.
  - The 33% matched the discount maths independently: a ₹50 discount only pays for itself (on RTO savings alone) when risk is above (50 × 0.96 / 169.4) + 4% ≈ 32%.
- **Reversed two of my own rules after the numbers came in.** My first draft held every order with a junk address and forced confirmation on every order above ₹1,500. Backtested on April–August:
  - *Hold junk addresses:* **−₹4,412**. 36% of those customers were genuine and did accept delivery; losing 40% of them on a hold cost more than the RTOs saved. **Now:** WhatsApp asks for the full address and we ship once we have it (+₹946 on the same orders).
  - *Confirm every order above ₹1,500:* **−₹1,774**. The model already prices order value in. **Removed.**
  - With those fixes, the training-period result went from **−₹2,533 to +₹9,588**.
- **Never hold a first-time buyer on risk score alone.** They need hard evidence (fake name, unusable address, invalid phone) to be held. This is Marketing's guardrail; ₹-wise, a genuine first-timer lost costs ₹637 vs. ₹169 per RTO.
- **Customers with 2+ past RTOs → prepaid only.** Their return rate is 76%. In ₹ it's roughly break-even with "confirm", but it's the clearest case Ops raised, and it doesn't touch the customers Marketing paid to acquire.
- **Loyal customers (delivered before, never refused) always ship.** 14% RTO. Friction here costs more than it saves.
- **Rejected Ops' "stop COD in bad pincodes".** Backtested on September: −₹16,883 (157 "bad" pincodes, 26% RTO, the same as average).
- **Rejected Ops' "cancel any risky order".** At ≥40% risk, September: 61 cancelled, 27 genuine, **−₹16,345**.

### WhatsApp flow

- **Language from the customer's state** (Hindi belt → Hinglish in Roman script) **or a Hinglish address**; the customer can reply "English"/"Hindi" to switch.
  - *Alternatives:* always English; Devanagari Hindi; ask first.
  - *Why:* the customers who wrote their address in Hinglish are telling us how they read. Roman-script Hinglish is how most people actually type on WhatsApp. Asking first adds a round-trip before the message that matters.
- **No messages between 9 pm and 9 am.** About a third of orders arrive at night; a 2 am "confirm your order" feels like spam. A reminder that would land in quiet hours is pulled back to 8 pm (this was a bug in my first version: the reminder went out at 9 am alongside the cancellation, saying "cancels in 0 hours").
- **No reply in 24h: "confirm" orders ship, "hold" orders cancel.**
  - *Alternative:* cancel all non-replies (Ops' instinct).
  - *Why:* a 60–80% risk order is still profit-positive to ship at our margins unless we're sure it's bad. Not replying isn't proof. Holds are different: they're already at the "cancel pays" level. Both are a config setting.
- **Two unclear replies → a human.** Not an endless bot loop.
- **Discount: flat ₹50, capped at 10% of order value.** A percentage looks small on cheap orders; a flat ₹50 is easy to understand. The cap keeps a ₹299 order at ₹30.

### Engineering

- **Dropped Streamlit for Python's built-in web server + one HTML page.** Started with Streamlit; `pip install` kept failing on this machine with "connection reset" (Streamlit pulls in pyarrow and ~40 packages). A reviewer on slow Wi-Fi could hit the same wall and blow the "run in under 10 minutes" requirement. Now the only dependencies are pandas, numpy and PyYAML.
- **Thresholds live in `config/rules.yaml` with plain-English comments, *and* on a Rules page** with sliders that show the September ₹ impact live before saving. Saving rewrites only the numbers, so the comments survive. Bad values (bands out of order) are rejected with a readable message.
- **Tests use the standard library's `unittest`** (pytest also works), so nothing extra is needed to run them.
