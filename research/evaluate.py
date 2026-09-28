"""Report the existing simulation ledger; does not perform backtesting or trade."""

import argparse
from pathlib import Path

from crypto_agent.models import AgentError, dumps
from crypto_agent.storage.database import Database


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--mode", choices=("paper", "offline"), required=True)
    args = parser.parse_args()
    if not args.database.is_file():
        raise AgentError("No existing simulation database at that path")
    database = Database(args.database, args.mode)
    try:
        print(dumps(database.report()))
    finally:
        database.close()


if __name__ == "__main__":
    main()
