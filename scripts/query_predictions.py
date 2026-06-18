"""Query predictions.db for recent records and this week's performance."""
import sqlite3
from datetime import datetime, timedelta

conn = sqlite3.connect('data/predictions.db')

# Get schema to understand table structure
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print("=== Tables ===")
for t in tables:
    print(f"Table: {t[0]}")

print("\n=== Predictions table schema ===")
cols = conn.execute("PRAGMA table_info(predictions)").fetchall()
for c in cols:
    print(f"  {c}")

print("\n=== Outcomes table schema ===")
cols = conn.execute("PRAGMA table_info(outcomes)").fetchall()
for c in cols:
    print(f"  {c}")

# Last 30 predictions
print("\n=== Last 30 Predictions with Outcomes ===")
rows = conn.execute('''
    SELECT p.date, p.code, p.name, p.score, p.rating, o.t1_return, o.t5_return
    FROM predictions p
    LEFT JOIN outcomes o ON p.id = o.prediction_id
    ORDER BY p.id DESC LIMIT 30
''').fetchall()
for r in rows:
    t1 = f"{r[5]:+.2f}%" if r[5] is not None else "N/A"
    t5 = f"{r[6]:+.2f}%" if r[6] is not None else "N/A"
    print(f'{r[0]} {r[1]} {r[2]} 评分{r[3]:.0f} 评级{r[4]} T+1:{t1} T+5:{t5}')

# Also count
count = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
print(f"\nTotal predictions in DB: {count}")

# This week (Mon-Fri of current week)
today = datetime.now()
# Find Monday of current week
monday = today - timedelta(days=today.weekday())
print(f"\n=== This week predictions (from {monday.date()}) ===")
rows2 = conn.execute('''
    SELECT p.date, p.code, p.name, p.score, p.rating, o.t1_return, o.t5_return
    FROM predictions p
    LEFT JOIN outcomes o ON p.id = o.prediction_id
    WHERE p.date >= ?
    ORDER BY p.date DESC, p.id DESC
''', (monday.strftime('%Y-%m-%d'),)).fetchall()
for r in rows2:
    t1 = f"{r[5]:+.2f}%" if r[5] is not None else "N/A"
    t5 = f"{r[6]:+.2f}%" if r[6] is not None else "N/A"
    print(f'{r[0]} {r[1]} {r[2]} 评分{r[3]:.0f} 评级{r[4]} T+1:{t1} T+5:{t5}')

if not rows2:
    print("  (no predictions this week yet)")

conn.close()
