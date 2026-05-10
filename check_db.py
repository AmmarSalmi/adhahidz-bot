import sqlite3
import os

db_path = '/data/subscriptions.db'
if not os.path.exists(db_path):
    print(f"Error: {db_path} not found")
    exit(1)

conn = sqlite3.connect(db_path)
cur = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='profiles'")
row = cur.fetchone()
if row:
    print(row[0])
else:
    print("Table 'profiles' not found")

print("\n--- Row 319 details ---")
cur = conn.execute("SELECT * FROM profiles WHERE id=319")
row = cur.fetchone()
if row:
    # Print columns too
    cols = [description[0] for description in cur.description]
    for col, val in zip(cols, row):
        print(f"{col}: {val}")
else:
    print("Profile 319 not found")

conn.close()
