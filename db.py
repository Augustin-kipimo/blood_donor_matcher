"""MongoDB connection and index setup (works with local MongoDB or Atlas)."""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()          # reads a .env file in the project folder, if present
except ImportError:
    pass

from pymongo import MongoClient

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "bloodMatchDB")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
db = client[DB_NAME]


def init_indexes():
    """Create the indexes used by the app (safe to run many times)."""
    db.donors.create_index([("location", "2dsphere")])
    db.hospitals.create_index([("location", "2dsphere")])
    db.donors.create_index([("bloodType", 1), ("lastDonationDate", 1)])
    db.requests.create_index([("status", 1), ("urgency", 1)])
