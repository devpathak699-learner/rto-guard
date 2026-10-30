"""Train the risk model and report how well it ranks and calibrates.

Training uses April–August. September is held out and treated as
"last month" for the backtest in scripts/backtest.py.

    python scripts/train.py            # held-out evaluation + save model trained on Apr–Aug
    python scripts/train.py --all      # retrain on every month for production use
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from rto_guard import MODEL_PATH  # noqa: E402
from rto_guard.data import load_orders  # noqa: E402
from rto_guard.model import RiskModel, auc, build_features  # noqa: E402

CUTOFF = "2026-09-01"


def main(use_all: bool = False) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    df = load_orders()
    train, test = df[df.order_datetime < CUTOFF], df[df.order_datetime >= CUTOFF]
    model = RiskModel.fit(df if use_all else train)

    print(f"Trained on {len(df if use_all else train):,} orders "
          f"({'all months' if use_all else 'Apr–Aug'}).\n")
    print("Weights (log-odds; positive = raises RTO risk):")
    for f, c in sorted(model.coef.items(), key=lambda kv: -kv[1]):
        print(f"  {f:16s} {c:+.2f}")

    if not use_all:
        p = model.predict(build_features(test, model.areas))
        t = test.assign(risk=p)
        print(f"\nSeptember hold-out: {len(t)} orders, actual RTO {t.is_rto.mean():.1%}, "
              f"AUC {auc(t.is_rto.values, p):.3f}")
        bins = pd.cut(t.risk, [0, 0.15, 0.22, 0.32, 0.42, 0.55, 0.7, 1])
        cal = t.groupby(bins, observed=True).agg(
            orders=("is_rto", "size"), predicted=("risk", "mean"), actual=("is_rto", "mean")
        )
        print("\nCalibration (does a 40% score really mean 40%?):")
        print(cal.to_string(float_format=lambda x: f"{x:.0%}"))

    model.save(MODEL_PATH)
    print(f"\nSaved -> {MODEL_PATH.relative_to(MODEL_PATH.parents[1])}")


if __name__ == "__main__":
    main(use_all="--all" in sys.argv)
