"""Print a newly issued management credential once to stdout."""

import argparse
from datetime import datetime

from db.connection import connect
from identity.security import new_key


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expires-at", type=datetime.fromisoformat)
    args = parser.parse_args()
    if args.expires_at and args.expires_at.tzinfo is None:
        parser.error("--expires-at must include a timezone")
    key_id, raw, digest = new_key()
    with connect("IDENTITY_ADMIN_DATABASE_URL") as conn:
        conn.execute("INSERT INTO identity.credentials(key_id, kind, digest, expires_at) VALUES (%s, 'admin', %s, %s)", (key_id, digest, args.expires_at))
    print(raw)


if __name__ == "__main__":
    main()
