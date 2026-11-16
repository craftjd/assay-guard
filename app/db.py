from collections.abc import Generator
from contextlib import contextmanager

import pymysql
from pymysql.connections import Connection

from app.settings import mysql_config


@contextmanager
def get_conn() -> Generator[Connection, None, None]:
    conn = pymysql.connect(**mysql_config())
    try:
        yield conn
    finally:
        conn.close()


def ping() -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DATABASE()")
            row = cur.fetchone()
            return row[0] if row else ""
