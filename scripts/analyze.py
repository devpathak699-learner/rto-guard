"""Reproduce every data finding quoted in the business memo.

    python scripts/analyze.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from rto_guard import DATA_PATH  # noqa: E402
from rto_guard.config import load_config, rto_cost  # noqa: E402
from rto_guard.data import load_orders  # noqa: E402
from rto_guard.economics import lost_customer_value  # noqa: E402
from rto_guard.text_checks import rule_check  # noqa: E402

METROS = {"Delhi", "Mumbai", "Bengaluru", "Hyderabad", "Pune", "Chennai", "Kolkata"}


def rate(s: pd.Series) -> str:
    return f"{s.mean():.0%} of {len(s):,}"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    raw = pd.read_csv(DATA_PATH, dtype=str)
    df = load_orders()
    cfg = load_config()
    C = rto_cost(cfg)

    print("== Data quality ==")
    print(f"raw rows {len(raw):,}; exact duplicates {raw.duplicated().sum()}; "
          f"status spellings {sorted(raw.final_status.unique())}")
    print(f"usable labelled orders {len(df):,}; overall RTO {df.is_rto.mean():.1%}")
    print(f"average COD order value in data ₹{df.order_value.mean():.0f} (brief says AOV ₹649)")

    v = df.apply(lambda r: rule_check(r.customer_name, r.address, r.city), axis=1)
    df["addr"] = [x.address_quality for x in v]
    df["fake"] = [x.name_looks_fake for x in v]
    df["hinglish"] = [x.hinglish for x in v]

    print("\n== 1. Address quality beats everything else that's cheap to check ==")
    for q in ("complete", "landmark", "vague", "unusable"):
        print(f"  {q:9s} RTO {rate(df.is_rto[df.addr == q])}")
    print(f"  Hinglish-but-real addresses: RTO {rate(df.is_rto[df.hinglish & df.addr.isin(['landmark', 'complete'])])}")
    print(f"  fake-looking names: RTO {rate(df.is_rto[df.fake])}  (so ~1 in 3 is a real buyer)")

    print("\n== 2. Customer history is the strongest signal ==")
    first = df.customer_prior_orders == 0
    clean_repeat = (~first) & (df.customer_prior_rto == 0)
    print(f"  first order:                 RTO {rate(df.is_rto[first])}")
    print(f"  repeat, never refused:       RTO {rate(df.is_rto[clean_repeat])}")
    print(f"  refused 1 before:            RTO {rate(df.is_rto[df.customer_prior_rto == 1])}")
    print(f"  refused 2+ before:           RTO {rate(df.is_rto[df.customer_prior_rto >= 2])}")
    meta = df.utm_source == "meta_ads"
    print(f"  first order from Meta ads:   RTO {rate(df.is_rto[first & meta])}")
    print(f"  first order, other channels: RTO {rate(df.is_rto[first & ~meta])}")

    print("\n== 3. Geography: city tier matters, single pincodes are noise ==")
    tier3 = df.city.isin({"Darbhanga", "Gorakhpur", "Bareilly", "Sagar", "Madhubani", "Kota", "Nagaon"})
    print(f"  metros RTO {rate(df.is_rto[df.city.isin(METROS)])}; small towns RTO {rate(df.is_rto[tier3])}")
    pc = df.groupby("pincode").size()
    print(f"  {len(pc):,} pincodes for {len(df):,} orders; busiest pincode has {pc.max()} orders; "
          f"median {pc.median():.0f}")
    train, sep = df[df.order_datetime < "2026-09-01"], df[df.order_datetime >= "2026-09-01"]
    hist = train.groupby("pincode").is_rto.agg(["size", "mean"])
    bad = hist[(hist["size"] >= 2) & (hist["mean"] >= 0.5)].index
    hit = sep[sep.pincode.isin(bad)]
    lost = sum(lost_customer_value(cfg, o, r) for o, r, y in
               zip(hit.order_value, hit.customer_prior_rto, hit.is_rto) if not y)
    print(f"  'bad' pincodes in Apr–Aug (2+ orders, ≥50% RTO): {len(bad)}. In September they had "
          f"{len(hit)} orders and RTO {hit.is_rto.mean():.0%} (overall {sep.is_rto.mean():.0%}).")
    print(f"  Blocking COD there in Sept: saves ₹{hit.is_rto.sum() * C:,.0f}, loses ₹{lost:,.0f}")
    print(f"  September orders from pincodes never seen before: {(~sep.pincode.isin(train.pincode)).mean():.0%}")

    print("\n== 4. Order value ==")
    for lo, hi in ((0, 999), (1000, 1499), (1500, 99999)):
        s = df.is_rto[(df.order_value >= lo) & (df.order_value <= hi)]
        print(f"  ₹{lo:,}–{hi:,}: RTO {rate(s)}")

    print("\n== 5. Shipping a risky order still makes money ==")
    m = lost_customer_value(cfg, 649, past_rtos=1)
    print(f"  ₹649 order: delivered margin ₹{m:.0f} vs RTO loss ₹{C:.0f} → shipping pays until risk "
          f"{m / (m + C):.0%} (or {(m + 300) / (m + 300 + C):.0%} counting the ₹300 to replace the customer)")


if __name__ == "__main__":
    main()
