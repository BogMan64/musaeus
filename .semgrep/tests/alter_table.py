def ensure_columns(conn):
    # ruleid: alter-table-add-column-outside-db
    conn.execute("ALTER TABLE archive ADD COLUMN foo TEXT")

def unrelated(conn):
    # ok: alter-table-add-column-outside-db
    conn.execute("SELECT * FROM archive")
