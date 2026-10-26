"""Start the local investigator review server.

The process listens on 127.0.0.1 only. The development database is refused.
"""

from __future__ import annotations

import argparse
import sys

from amlog.config import load_settings
from amlog.review.server import ReviewServer
from amlog.review.service import PROTECTED_DATABASES, ReviewError, assert_review_database
from amlog.workload.load import replace_database

DEFAULT_DATABASE = "amlog_workload_integrated"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open the local AMLOG investigator review page.")
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        assert_review_database(args.database)
        settings = load_settings()
    except (ReviewError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.database in PROTECTED_DATABASES:
        print(f"{args.database} is not a review database.", file=sys.stderr)
        return 2
    if settings.database_url is None:
        print("AMLOG_DATABASE_URL is not set. No database was contacted.", file=sys.stderr)
        return 2
    url = replace_database(settings.database_url, args.database)
    server = ReviewServer(("127.0.0.1", args.port), url)
    print(f"Review page: http://127.0.0.1:{args.port}/", flush=True)
    print(f"SynthAML research benchmark: http://127.0.0.1:{args.port}/synthaml", flush=True)
    print(f"Database: {args.database}", flush=True)
    print("The investigator queue records human review. The research view is read-only and does not train a model.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
