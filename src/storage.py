"""
SQLite 存储层

设计思路：
- 每次抓取都是一个 snapshot（不覆盖历史）
- 这样可以画价格走势图
- 老旧数据定期清理（默认保留 90 天）
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS flight_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at   TEXT NOT NULL,
    source        TEXT NOT NULL,
    from_city     TEXT NOT NULL,
    to_city       TEXT NOT NULL,
    depart_date   TEXT NOT NULL,
    flight_no     TEXT NOT NULL,
    carrier       TEXT,
    dep_time      TEXT,
    arr_time      TEXT,
    dep_airport   TEXT,
    arr_airport   TEXT,
    duration_min  INTEGER,
    price         INTEGER NOT NULL,
    base_price    INTEGER,
    discount      REAL,
    raw_json      TEXT
);

CREATE INDEX IF NOT EXISTS idx_flight_route_date
    ON flight_snapshots(from_city, to_city, depart_date);

CREATE INDEX IF NOT EXISTS idx_flight_captured
    ON flight_snapshots(captured_at);

CREATE TABLE IF NOT EXISTS train_snapshots (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at           TEXT NOT NULL,
    from_city             TEXT NOT NULL,
    to_city               TEXT NOT NULL,
    depart_date           TEXT NOT NULL,
    train_no              TEXT NOT NULL,
    dep_station           TEXT,
    arr_station           TEXT,
    dep_time              TEXT,
    arr_time              TEXT,
    duration_min          INTEGER,
    second_class_price    INTEGER,
    first_class_price     INTEGER,
    business_price        INTEGER,
    second_class_seats    TEXT,
    first_class_seats     TEXT,
    business_seats        TEXT,
    raw_json              TEXT
);

CREATE INDEX IF NOT EXISTS idx_train_route_date
    ON train_snapshots(from_city, to_city, depart_date);

CREATE TABLE IF NOT EXISTS alerts_fired (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fired_at      TEXT NOT NULL,
    rule_name     TEXT NOT NULL,
    payload       TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, db_path: str | Path = "data/prices.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------------- 写入 ----------------
    def save_flights(self, offers: Iterable[dict]):
        rows = []
        for o in offers:
            rows.append((
                o.get("captured_at") or datetime.now().isoformat(timespec="seconds"),
                o.get("source", ""),
                o.get("from_city", ""),
                o.get("to_city", ""),
                o.get("depart_date", ""),
                o.get("flight_no", ""),
                o.get("carrier", ""),
                o.get("dep_time", ""),
                o.get("arr_time", ""),
                o.get("dep_airport", ""),
                o.get("arr_airport", ""),
                o.get("duration_min", 0),
                o.get("price", 0),
                o.get("base_price"),
                o.get("discount"),
                json.dumps(o, ensure_ascii=False),
            ))
        if not rows:
            return 0
        with self._conn() as conn:
            conn.executemany("""
                INSERT INTO flight_snapshots
                (captured_at, source, from_city, to_city, depart_date,
                 flight_no, carrier, dep_time, arr_time, dep_airport,
                 arr_airport, duration_min, price, base_price, discount, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
        logger.info(f"[Storage] 写入 {len(rows)} 条机票快照")
        return len(rows)

    def save_trains(self, offers: Iterable[dict]):
        rows = []
        for o in offers:
            rows.append((
                o.get("captured_at") or datetime.now().isoformat(timespec="seconds"),
                o.get("from_city", ""),
                o.get("to_city", ""),
                o.get("depart_date", ""),
                o.get("train_no", ""),
                o.get("dep_station", ""),
                o.get("arr_station", ""),
                o.get("dep_time", ""),
                o.get("arr_time", ""),
                o.get("duration_min", 0),
                o.get("second_class_price", 0),
                o.get("first_class_price", 0),
                o.get("business_price", 0),
                o.get("second_class_seats", ""),
                o.get("first_class_seats", ""),
                o.get("business_seats", ""),
                json.dumps(o, ensure_ascii=False),
            ))
        if not rows:
            return 0
        with self._conn() as conn:
            conn.executemany("""
                INSERT INTO train_snapshots
                (captured_at, from_city, to_city, depart_date, train_no,
                 dep_station, arr_station, dep_time, arr_time, duration_min,
                 second_class_price, first_class_price, business_price,
                 second_class_seats, first_class_seats, business_seats, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
        logger.info(f"[Storage] 写入 {len(rows)} 条高铁快照")
        return len(rows)

    def record_alert(self, rule_name: str, payload: dict):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO alerts_fired (fired_at, rule_name, payload)
                VALUES (?, ?, ?)
            """, (
                datetime.now().isoformat(timespec="seconds"),
                rule_name,
                json.dumps(payload, ensure_ascii=False),
            ))

    # ---------------- 查询 ----------------
    def get_latest_flights(
        self,
        from_city: str,
        to_city: str,
        depart_date: str,
    ) -> list[dict]:
        """获取最近一次抓取的某天某航线全部航班"""
        with self._conn() as conn:
            cur = conn.execute("""
                SELECT * FROM flight_snapshots
                WHERE from_city = ? AND to_city = ? AND depart_date = ?
                  AND captured_at = (
                      SELECT MAX(captured_at) FROM flight_snapshots
                      WHERE from_city = ? AND to_city = ? AND depart_date = ?
                  )
                ORDER BY price ASC
            """, (from_city, to_city, depart_date,
                  from_city, to_city, depart_date))
            return [dict(r) for r in cur.fetchall()]

    def get_latest_trains(
        self,
        from_city: str,
        to_city: str,
        depart_date: str,
    ) -> list[dict]:
        with self._conn() as conn:
            cur = conn.execute("""
                SELECT * FROM train_snapshots
                WHERE from_city = ? AND to_city = ? AND depart_date = ?
                  AND captured_at = (
                      SELECT MAX(captured_at) FROM train_snapshots
                      WHERE from_city = ? AND to_city = ? AND depart_date = ?
                  )
                ORDER BY dep_time ASC
            """, (from_city, to_city, depart_date,
                  from_city, to_city, depart_date))
            return [dict(r) for r in cur.fetchall()]

    def get_price_history(
        self,
        from_city: str,
        to_city: str,
        depart_date: str,
        days: int = 30,
    ) -> list[dict]:
        """
        获取某航线某天的最低价历史走势
        返回格式：[{captured_date, min_price}, ...]
        """
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._conn() as conn:
            cur = conn.execute("""
                SELECT DATE(captured_at) AS captured_date,
                       MIN(price) AS min_price
                FROM flight_snapshots
                WHERE from_city = ? AND to_city = ? AND depart_date = ?
                  AND captured_at >= ?
                GROUP BY DATE(captured_at)
                ORDER BY captured_date
            """, (from_city, to_city, depart_date, since))
            return [dict(r) for r in cur.fetchall()]

    def cleanup_old(self, keep_days: int = 90):
        cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat(timespec="seconds")
        with self._conn() as conn:
            r1 = conn.execute("DELETE FROM flight_snapshots WHERE captured_at < ?", (cutoff,))
            r2 = conn.execute("DELETE FROM train_snapshots WHERE captured_at < ?", (cutoff,))
            r3 = conn.execute("DELETE FROM alerts_fired WHERE fired_at < ?", (cutoff,))
            logger.info(
                f"[Storage] 清理 {keep_days} 天前的数据: "
                f"flights={r1.rowcount} trains={r2.rowcount} alerts={r3.rowcount}"
            )
