"""One command to see everything:

    python run.py            # train if needed, then open the dashboard on http://localhost:8000
    python run.py --port 9000
"""

import argparse
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Optional: GEMINI_API_KEY=... in a .env file next to this script.
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))

from rto_guard import MODEL_PATH  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if not MODEL_PATH.exists():
        print("First run: training the risk model on April–August orders…")
        subprocess.run([sys.executable, str(ROOT / "scripts" / "train.py")], check=True)

    from rto_guard.server import serve

    print("Scoring September orders…")
    httpd = serve(args.port)
    url = f"http://localhost:{args.port}"
    print(f"\n  RTO Guard is running →  {url}\n  (Ctrl+C to stop)\n")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
