# Emergency Blood Donor Matcher (MongoDB + Flask)

A web app for finding the nearest, eligible, blood-compatible donors for an urgent blood request.
Front end: HTML and CSS (Jinja templates). Back end: Python (Flask) with MongoDB through PyMongo.

## 1. What you need
- Python 3.9 or newer
- MongoDB Community Server (running on the default port 27017) and MongoDB Compass (optional, to look at the data)

## 2. Set up and run
Open a terminal in this folder (the one that contains `app.py`).

```
python -m venv venv
venv\Scripts\activate          (Windows)      source venv/bin/activate   (Mac / Linux)
pip install -r requirements.txt
python seed.py                 (loads the sample data, creates the indexes)
python app.py
```
Then open http://127.0.0.1:5000 in your browser.

`seed.py` empties the four collections and reloads the sample data. Run it only when you want a fresh start.
To use a different server, set `MONGO_URI` (for example an Atlas connection string) before running.

## 3. Project structure
```
app.py            Flask routes: all CRUD operations and the donor matching query
db.py             MongoDB connection and index creation
seed.py           Loads data/*.json into MongoDB
data/             Sample documents (donors, hospitals, requests, donations)
templates/        HTML pages (Jinja)
public/style.css  Styling (served from public/ so it also works on Vercel)
```

## 4. CRUD operations (where to find them)
| Operation | Page / button | MongoDB method |
|---|---|---|
| Create | Add donor, Add hospital, New request, Record donation | `insert_one` |
| Read | Dashboard, every list page, donor detail, Match donors | `find`, `find_one`, `aggregate` |
| Update | Edit buttons on every list | `update_one` with `$set` |
| Delete | Delete buttons on every list | `delete_one` |

## 5. Business rules built into the app
- A request stores its compatible donor blood types automatically (for example A+ patients accept A+, A-, O+, O-).
- **Match donors** uses one query: `$in` (blood type), `$or` (never donated, or last donation before the 90-day cutoff) and `$near` (2dsphere index) to return donors nearest first.
- Recording a donation updates the donor's `lastDonationDate`, and the request's `unitsReceived` and `status` (Open or Fulfilled). Editing or deleting a donation keeps them correct.
- A donor, hospital or request that other records depend on cannot be deleted until those records are removed (keeps references valid).
- A donation to a request is rejected if the donor's blood type is not compatible.

## 6. Suggested demo (about 5 minutes)
1. Dashboard: open requests, donors per blood type (aggregation).
2. Requests > R01 > **Match donors**: show the nearest eligible donor; change the radius.
3. **Record donation** from that page: request becomes 1 / 2; the donor is no longer matched.
4. Create a donor, edit it, then delete it (Create, Update, Delete).
5. Open Compass and show the same changes in the collections.

## 7. Troubleshooting
| Problem | Fix |
|---|---|
| "Could not reach the database" page | Start the MongoDB service, then refresh |
| `ModuleNotFoundError: flask` or `pymongo` | Activate the virtual environment and run `pip install -r requirements.txt` |
| Match page shows nothing | Run `python seed.py` again (it also creates the geospatial index) |
| `unable to find index for $geoNear` | Run `python seed.py` or `python app.py` once so the indexes are created |

## 8. Using MongoDB Atlas and deploying to Vercel
1. Copy `.env.example` to `.env` and put your Atlas connection string in `MONGO_URI` (and a random `SECRET_KEY`).
2. Run `python seed.py` once. It loads the sample data into Atlas and creates the indexes.
3. Push the project to GitHub (`.env` is ignored by `.gitignore`).
4. Import the repository on vercel.com, add the environment variables `MONGO_URI`, `DB_NAME` and `SECRET_KEY`, then deploy.
5. In Atlas > Network Access, allow `0.0.0.0/0` (Vercel does not use fixed IP addresses).
