class StateStoreSchemaMigrationMixin:
    def _ensure_column(self, table_name: str, column_name: str, column_type: str):
        cursor = self.conn.execute(f"PRAGMA table_info({table_name})")
        columns = {row["name"] for row in cursor.fetchall()}
        if column_name not in columns:
            with self.conn:
                self.conn.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                )
