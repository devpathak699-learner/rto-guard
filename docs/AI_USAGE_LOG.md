# AI usage log

## Tools used

| Tool | Used for |
|---|---|
| **Claude Code** (Anthropic, Claude Opus 5.5) | Read the brief, explored the data, wrote most of the code, the backtests, the dashboard and first drafts of the docs. I worked with it as a pair programmer: it proposed, I checked against the data, we iterated. |
| **Gemini** (Google, `gemini-2.5-flash`, via REST) | *Inside the product*: labels address quality and fake names, and reads free-text WhatsApp replies. Optional; rules take over without a key. |
| **Headless Microsoft Edge** | Screenshots of the dashboard to check the layout, and printing the memo to PDF. |

## Where the AI was wrong or weak, and how it was caught

### 1. The first risk model leaked the answer (caught by comparing train vs. test)
The first model scored each order's area using a return rate that **included that order's own outcome**. It looked great in training (AUC 0.81) but dropped to 0.73 on September, and its "area risk" weight was ~70% too high, so it would have over-blamed specific pincodes.
**Caught by:** the gap between training and September scores, and a log-loss on September that was *worse* than a simpler version without pincodes.
**Fixed by:** computing each order's area rate only from *other* orders (5-fold, out-of-fold). Held-out AUC 0.737, better log-loss, sane weights. Logged in the decision log.

### 2. Rules that "sounded right" lost money (caught by backtesting each rule separately)
The first rule set held every junk-address order and forced confirmation on every order above ₹1,500. Both sound like what an ops person would want. But every threshold combination came out **negative**, the best being −₹2,533 on April–August.
**Caught by:** breaking the result down per rule: "hold junk addresses" alone was −₹4,412, because 36% of those customers were genuine.
**Fixed by:** asking for the address over WhatsApp instead of holding, and dropping the ₹1,500 rule (the model already prices order value in). Result: +₹9,588 on the same months. This became a headline point in the memo.

### 3. WhatsApp edge cases (caught by simulating each path)
- "haan ji" (a common "yes") was read as **unclear**: the pattern had "haan" but not "haan" + " ji".
- A reminder due at 5 am was pushed to 9 am, the same minute as the 24h cancellation, so the customer would have got "your order cancels in **0 hours**" and then the cancellation.
- When we were waiting for an address, tapping "3 (Change address)" or asking a question got "Sorry, we still can't find that address".

**Caught by:** running every branch (yes / no / random / no reply / night order / junk address) and reading the transcript like a customer would.
**Fixed by:** allowing polite suffixes ("ji", "please"), moving a quiet-hours reminder *back* to 8 pm, and re-asking for the address instead of rejecting chat. Each is now a unit test.

### 4. Smaller things
- Memo draft said cancelling a 40%-risk order loses "~₹140" of profit. Recomputed: 60% × ₹337 − 40% × ₹169 = **₹135**. Fixed.
- Memo draft quoted "380 repeat phone numbers" and "32 malformed phones" from the raw file. After cleaning it's **364**, and only **17** phones are actually broken (the other 15 are just written as +91…). Fixed. **Every number in the memo is now printed by `scripts/analyze.py` or `scripts/backtest.py`.**
- A suggested shell command to stop the dev server would have killed *every* Python process on the machine. It was blocked; I stopped only the server's own process instead.

## How I used AI well (and where I didn't trust it)
- Every claim was checked against the data, not taken from the model's reasoning. If a number couldn't be reproduced by a script, it didn't go in the memo.
- The AI suggested the overall structure; the trade-offs (CAC treatment, margin definition, which rules survive) were decided by backtest results.
- I kept the LLM *inside the product* to the jobs where language is the hard part, and kept it away from anything that has to be exact or explainable.
