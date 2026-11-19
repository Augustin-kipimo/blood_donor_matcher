"""Load the sample data (data/*.json) into MongoDB.

WARNING: this empties the four collections first, then inserts the sample data.
Run:  python seed.py
"""
import os

from bson import json_util

from db import DB_NAME, db, init_indexes

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
COLLECTIONS = ["donors", "hospitals", "requests", "donations"]

for name in COLLECTIONS:
    with open(os.path.join(DATA_DIR, name + ".json"), encoding="utf-8") as fh:
        docs = json_util.loads(fh.read())
    db[name].delete_many({})
    db[name].insert_many(docs)
    print(f"{name}: inserted {len(docs)} documents")

init_indexes()
print(f"Indexes created. Database '{DB_NAME}' is ready.")
