import sqlite3
import os
import re

def validate_password_strict(pw):
    errors = []
    if len(pw) < 6:
        errors.append("Too short (<6)")
    if len(pw) > 12:
        errors.append("Too long (>12)")
    if not any(c.isdigit() for c in pw):
        errors.append("No digit")
    if not any(c.islower() for c in pw):
        errors.append("No lower")
    if not any(c.isupper() for c in pw):
        errors.append("No upper")
    if not any(c in "!@#$%^&*()_+-=[]{}|;':\",./<>?`~" for c in pw):
        errors.append("No special")
    return errors

db_path = '/data/subscriptions.db'
conn = sqlite3.connect(db_path)
cur = conn.execute("SELECT id, password, user_id FROM profiles")
rows = cur.fetchall()

print("Scanning for potentially problematic passwords...")
for pid, pw, uid in rows:
    errs = validate_password_strict(pw)
    if errs:
        print(f"Profile {pid} (User {uid}): Password '{pw}' -> Errors: {', '.join(errs)}")

conn.close()
