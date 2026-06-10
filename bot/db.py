"""Capa de base de datos (PostgreSQL local via psycopg2).

Reusa el patron del intento previo que SI funcionaba, pero:
  - NO ejecuta TRUNCATE bot_config (eso borraba toda la config).
  - Solo lee config + controla status (sin news filter ni PAUSED_AI_OPUS).
"""

import psycopg2
from psycopg2.extras import RealDictCursor

from . import config


class Database:
    def __init__(self):
        self.conn = None

    def connect(self):
        self.conn = psycopg2.connect(
            host=config.DB_HOST,
            database=config.DB_NAME,
            user=config.DB_USER,
            password=config.DB_PASS,
            port=config.DB_PORT,
        )
        self.conn.autocommit = True

    def load_config(self):
        """Devuelve (config_dict, status). config_dict vacio si no hay fila activa."""
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM bot_config
                WHERE user_id = %s AND symbol = %s AND is_active = true
                LIMIT 1;
                """,
                (config.USER_ID, config.SYMBOL),
            )
            res = cur.fetchone()
            if res:
                return dict(res), res["status"]
            return {}, "INACTIVE"

    def update_status(self, new_status):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE bot_config SET status = %s, updated_at = NOW()
                WHERE user_id = %s AND symbol = %s;
                """,
                (new_status, config.USER_ID, config.SYMBOL),
            )

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None
