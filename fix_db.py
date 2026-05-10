import sqlite3
import os

db_path = '/data/subscriptions.db'
if not os.path.exists(db_path):
    print(f"Error: {db_path} not found")
    exit(1)

try:
    conn = sqlite3.connect(db_path)
    print("Adding column is_valid to profiles table...")
    conn.execute("ALTER TABLE profiles ADD COLUMN is_valid INTEGER NOT NULL DEFAULT 1;")
    conn.commit()
    print("Success!")
except sqlite3.OperationalError as e:
    if "duplicate column name" in str(e):
        print("Column already exists.")
    else:
        print(f"OperationalError: {e}")
except Exception as e:
    print(f"Error: {e}")
finally:
    conn.close()
