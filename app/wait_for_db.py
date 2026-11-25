"""Block until MySQL accepts connections (used in Docker / Codespaces)."""

import sys
import time

from app.db import ping


def main() -> None:
    for attempt in range(60):
        try:
            ping()
            return
        except Exception:  # noqa: BLE001
            time.sleep(2)
    print("Database did not become ready in time.", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()