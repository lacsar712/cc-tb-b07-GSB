import os
import time

import psycopg2

from rules import weigh


def connect():
    last = None
    for _ in range(30):
        try:
            return psycopg2.connect(os.environ["DATABASE_URL"])
        except psycopg2.OperationalError as exc:
            last = exc
            time.sleep(1)
    raise last


def main():
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS cuppings (
            id serial PRIMARY KEY,
            lot text NOT NULL,
            aroma double precision NOT NULL,
            taste double precision NOT NULL,
            liquor double precision NOT NULL,
            score double precision NOT NULL,
            verdict text NOT NULL,
            note text NOT NULL,
            created_by text NOT NULL
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS mother_lots (
            id serial PRIMARY KEY,
            name text NOT NULL UNIQUE,
            created_by text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    # 子批的钉入/剔除都落事件：当前成员 = 最新一条事件未被 remove 的批次。
    # 剔除不删事件，履历永久保留。
    cur.execute(
        """CREATE TABLE IF NOT EXISTS mother_lot_events (
            id serial PRIMARY KEY,
            mother_id integer NOT NULL REFERENCES mother_lots(id),
            lot text NOT NULL,
            action text NOT NULL CHECK (action IN ('add', 'remove')),
            acted_by text NOT NULL,
            acted_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    for lot, aroma, taste, liquor in (
        ("春茶-A", 8, 8, 7),
        ("夏茶-C", 5, 4, 6),
        ("春茶甲", 8, 8, 8),
        ("春芽", 7, 7, 7),
    ):
        cur.execute("SELECT 1 FROM cuppings WHERE lot = %s LIMIT 1", (lot,))
        if cur.fetchone() is None:
            verdict, note, score = weigh(aroma, taste, liquor)
            cur.execute(
                """INSERT INTO cuppings (lot, aroma, taste, liquor, score, verdict, note, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (lot, aroma, taste, liquor, score, verdict, note, "taster"),
            )
    conn.commit()
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
