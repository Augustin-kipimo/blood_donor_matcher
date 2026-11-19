"""Emergency Blood Donor Matcher: Flask front end + MongoDB (PyMongo) back end.

Every page talks to MongoDB directly. CRUD summary:
  Create -> insert_one      Read   -> find / find_one / aggregate
  Update -> update_one      Delete -> delete_one
"""
import math
import os
import re
from datetime import datetime, timedelta, timezone

from flask import Flask, abort, flash, redirect, render_template, request, url_for
from pymongo.errors import PyMongoError

from db import db, init_indexes

# CSS lives in public/ : Vercel serves public/ from its CDN, and Flask serves it locally.
app = Flask(__name__, static_folder="public", static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-for-real-use")

BLOOD_TYPES = ["O-", "O+", "A-", "A+", "B-", "B+", "AB-", "AB+"]
# Which donor blood types can safely give to a patient of each type (red cells).
COMPATIBLE_DONORS = {
    "O-": ["O-"],
    "O+": ["O+", "O-"],
    "A-": ["A-", "O-"],
    "A+": ["A+", "A-", "O+", "O-"],
    "B-": ["B-", "O-"],
    "B+": ["B+", "B-", "O+", "O-"],
    "AB-": ["AB-", "A-", "B-", "O-"],
    "AB+": ["AB+", "AB-", "A+", "A-", "B+", "B-", "O+", "O-"],
}
URGENCIES = ["Critical", "High", "Medium"]
URGENCY_RANK = {u: i for i, u in enumerate(URGENCIES)}
DONATION_TYPES = ["Emergency", "Voluntary"]
ELIGIBILITY_DAYS = 90  # a donor can give again this many days after the last donation


# ------------------------------------------------------------------ helpers
def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def eligibility_cutoff():
    return now_utc() - timedelta(days=ELIGIBILITY_DAYS)


def parse_date(text):
    return datetime.strptime(text, "%Y-%m-%d")


def f(name):
    return request.form.get(name, "").strip()


def point(lat, lon):
    """Build a GeoJSON Point. GeoJSON order is [longitude, latitude]."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        raise ValueError("Latitude and longitude must be numbers.")
    if not -90 <= lat <= 90:
        raise ValueError("Latitude must be between -90 and 90.")
    if not -180 <= lon <= 180:
        raise ValueError("Longitude must be between -180 and 180.")
    return {"type": "Point", "coordinates": [lon, lat]}


def next_id(collection, prefix):
    highest = 0
    for doc in db[collection].find({}, {"_id": 1}):
        m = re.fullmatch(re.escape(prefix) + r"(\d+)", str(doc["_id"]))
        if m:
            highest = max(highest, int(m.group(1)))
    return f"{prefix}{highest + 1:02d}"


def haversine_km(lon1, lat1, lon2, lat2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def hospital_names():
    return {h["_id"]: h["name"] for h in db.hospitals.find()}


def refresh_donor(donor_id):
    """Keep a donor's lastDonationDate equal to their most recent donation."""
    if not donor_id:
        return
    dates = [d["donationDate"] for d in db.donations.find({"donorId": donor_id})]
    db.donors.update_one({"_id": donor_id},
                         {"$set": {"lastDonationDate": max(dates) if dates else None}})


def refresh_request(request_id):
    """Recalculate unitsReceived and status of a request from its donations."""
    if not request_id:
        return
    req = db.requests.find_one({"_id": request_id})
    if not req:
        return
    received = sum(d["units"] for d in db.donations.find({"requestId": request_id}))
    status = req["status"]
    if status != "Cancelled":
        status = "Fulfilled" if received >= req["unitsNeeded"] else "Open"
    db.requests.update_one({"_id": request_id},
                           {"$set": {"unitsReceived": received, "status": status}})


@app.template_filter("date")
def fmt_date(value, empty="-"):
    return value.strftime("%Y-%m-%d") if value else empty


@app.template_filter("datetime")
def fmt_datetime(value):
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


@app.errorhandler(PyMongoError)
def mongo_error(err):
    return render_template("error.html", message=str(err)), 500


# ---------------------------------------------------------------- dashboard
@app.route("/")
def dashboard():
    counts = {n: db[n].count_documents({}) for n in ("donors", "hospitals", "requests", "donations")}
    names = hospital_names()
    open_requests = list(db.requests.find({"status": "Open"}))
    open_requests.sort(key=lambda r: (URGENCY_RANK.get(r["urgency"], 9), r["requestedAt"]))
    by_type = list(db.donors.aggregate([
        {"$group": {"_id": "$bloodType", "total": {"$sum": 1}}},
        {"$sort": {"total": -1, "_id": 1}},
    ]))
    per_hospital = list(db.donations.aggregate([
        {"$group": {"_id": "$hospitalId", "units": {"$sum": "$units"}}},
        {"$sort": {"units": -1}},
    ]))
    for row in per_hospital:
        row["name"] = names.get(row["_id"], row["_id"])
    return render_template("dashboard.html", counts=counts, open_requests=open_requests,
                           names=names, by_type=by_type, per_hospital=per_hospital)


# ------------------------------------------------------------------- donors
def donor_from_form():
    errors = []
    name, gender, phone = f("name"), f("gender"), f("phone")
    blood, area = f("bloodType"), f("area")
    if not name:
        errors.append("Name is required.")
    if gender not in ("M", "F"):
        errors.append("Choose a gender.")
    if not phone:
        errors.append("Phone is required.")
    if blood not in BLOOD_TYPES:
        errors.append("Choose a valid blood type.")
    if not area:
        errors.append("Area is required.")
    try:
        location = point(f("lat"), f("lon"))
    except ValueError as e:
        errors.append(str(e))
        location = None
    last = None
    if f("lastDonationDate"):
        try:
            last = parse_date(f("lastDonationDate"))
        except ValueError:
            errors.append("Last donation date is not a valid date.")
    doc = {"name": name, "gender": gender, "phone": phone, "bloodType": blood, "area": area,
           "location": location, "lastDonationDate": last,
           "available": request.form.get("available") == "on"}
    return doc, errors


def donor_to_form(d):
    lon, lat = d["location"]["coordinates"]
    return {"name": d["name"], "gender": d["gender"], "phone": d["phone"],
            "bloodType": d["bloodType"], "area": d["area"], "lat": lat, "lon": lon,
            "lastDonationDate": fmt_date(d.get("lastDonationDate"), ""),
            "available": "on" if d.get("available") else ""}


@app.route("/donors")
def donors_list():
    blood, q = request.args.get("bloodType", ""), request.args.get("q", "").strip()
    flt = {}
    if blood:
        flt["bloodType"] = blood
    if q:
        rx = {"$regex": re.escape(q), "$options": "i"}
        flt["$or"] = [{"name": rx}, {"area": rx}]
    donors = list(db.donors.find(flt).sort("name", 1))
    return render_template("donors_list.html", donors=donors, blood=blood, q=q,
                           blood_types=BLOOD_TYPES, cutoff=eligibility_cutoff())


@app.route("/donors/<donor_id>")
def donor_detail(donor_id):
    donor = db.donors.find_one({"_id": donor_id}) or abort(404)
    donations = list(db.donations.find({"donorId": donor_id}).sort("donationDate", -1))
    return render_template("donor_detail.html", d=donor, donations=donations,
                           names=hospital_names(), cutoff=eligibility_cutoff())


@app.route("/donors/new", methods=["GET", "POST"])
def donor_new():
    form = {"available": "on"}
    if request.method == "POST":
        doc, errors = donor_from_form()
        if not errors:
            doc["_id"] = next_id("donors", "D")
            db.donors.insert_one(doc)                      # CREATE
            flash(f"Donor {doc['_id']} ({doc['name']}) added.", "ok")
            return redirect(url_for("donors_list"))
        for e in errors:
            flash(e, "error")
        form = request.form
    return render_template("donor_form.html", form=form, mode="new", blood_types=BLOOD_TYPES)


@app.route("/donors/<donor_id>/edit", methods=["GET", "POST"])
def donor_edit(donor_id):
    donor = db.donors.find_one({"_id": donor_id}) or abort(404)
    form = donor_to_form(donor)
    if request.method == "POST":
        doc, errors = donor_from_form()
        if not errors:
            db.donors.update_one({"_id": donor_id}, {"$set": doc})   # UPDATE
            flash(f"Donor {donor_id} updated.", "ok")
            return redirect(url_for("donors_list"))
        for e in errors:
            flash(e, "error")
        form = request.form
    return render_template("donor_form.html", form=form, mode="edit", donor_id=donor_id,
                           blood_types=BLOOD_TYPES)


@app.route("/donors/<donor_id>/delete", methods=["POST"])
def donor_delete(donor_id):
    used = db.donations.count_documents({"donorId": donor_id})
    if used:
        flash(f"Cannot delete {donor_id}: the donor has {used} donation record(s). Delete those first.", "error")
    else:
        db.donors.delete_one({"_id": donor_id})                     # DELETE
        flash(f"Donor {donor_id} deleted.", "ok")
    return redirect(url_for("donors_list"))


# ---------------------------------------------------------------- hospitals
def hospital_from_form():
    errors = []
    name, area, phone = f("name"), f("area"), f("phone")
    if not name:
        errors.append("Hospital name is required.")
    if not area:
        errors.append("Area is required.")
    if not phone:
        errors.append("Phone is required.")
    try:
        location = point(f("lat"), f("lon"))
    except ValueError as e:
        errors.append(str(e))
        location = None
    doc = {"name": name, "area": area, "phone": phone, "location": location,
           "hasBloodBank": request.form.get("hasBloodBank") == "on"}
    return doc, errors


def hospital_to_form(h):
    lon, lat = h["location"]["coordinates"]
    return {"name": h["name"], "area": h["area"], "phone": h["phone"], "lat": lat, "lon": lon,
            "hasBloodBank": "on" if h.get("hasBloodBank") else ""}


@app.route("/hospitals")
def hospitals_list():
    hospitals = list(db.hospitals.find().sort("name", 1))
    return render_template("hospitals_list.html", hospitals=hospitals)


@app.route("/hospitals/new", methods=["GET", "POST"])
def hospital_new():
    form = {}
    if request.method == "POST":
        doc, errors = hospital_from_form()
        if not errors:
            doc["_id"] = next_id("hospitals", "H")
            db.hospitals.insert_one(doc)                    # CREATE
            flash(f"Hospital {doc['_id']} ({doc['name']}) added.", "ok")
            return redirect(url_for("hospitals_list"))
        for e in errors:
            flash(e, "error")
        form = request.form
    return render_template("hospital_form.html", form=form, mode="new")


@app.route("/hospitals/<hospital_id>/edit", methods=["GET", "POST"])
def hospital_edit(hospital_id):
    hospital = db.hospitals.find_one({"_id": hospital_id}) or abort(404)
    form = hospital_to_form(hospital)
    if request.method == "POST":
        doc, errors = hospital_from_form()
        if not errors:
            db.hospitals.update_one({"_id": hospital_id}, {"$set": doc})   # UPDATE
            flash(f"Hospital {hospital_id} updated.", "ok")
            return redirect(url_for("hospitals_list"))
        for e in errors:
            flash(e, "error")
        form = request.form
    return render_template("hospital_form.html", form=form, mode="edit", hospital_id=hospital_id)


@app.route("/hospitals/<hospital_id>/delete", methods=["POST"])
def hospital_delete(hospital_id):
    used = (db.requests.count_documents({"hospitalId": hospital_id})
            + db.donations.count_documents({"hospitalId": hospital_id}))
    if used:
        flash(f"Cannot delete {hospital_id}: {used} request/donation record(s) refer to it.", "error")
    else:
        db.hospitals.delete_one({"_id": hospital_id})                # DELETE
        flash(f"Hospital {hospital_id} deleted.", "ok")
    return redirect(url_for("hospitals_list"))


# ----------------------------------------------------------------- requests
def request_from_form(editing=False):
    errors = []
    hospital_id, blood, urgency, reason = f("hospitalId"), f("bloodTypeNeeded"), f("urgency"), f("reason")
    if not db.hospitals.find_one({"_id": hospital_id}):
        errors.append("Choose a valid hospital.")
    if blood not in BLOOD_TYPES:
        errors.append("Choose a valid blood type.")
    if urgency not in URGENCIES:
        errors.append("Choose an urgency level.")
    if not reason:
        errors.append("Reason is required.")
    units = None
    try:
        units = int(f("unitsNeeded"))
        if units < 1:
            raise ValueError
    except ValueError:
        errors.append("Units needed must be a whole number of at least 1.")
    doc = {"hospitalId": hospital_id, "bloodTypeNeeded": blood,
           "compatibleTypes": COMPATIBLE_DONORS.get(blood, []),
           "unitsNeeded": units, "urgency": urgency, "reason": reason}
    if editing:
        doc["status"] = "Cancelled" if f("status") == "Cancelled" else "Open"
    return doc, errors


def request_to_form(r):
    return {"hospitalId": r["hospitalId"], "bloodTypeNeeded": r["bloodTypeNeeded"],
            "unitsNeeded": r["unitsNeeded"], "urgency": r["urgency"], "reason": r["reason"],
            "status": "Cancelled" if r["status"] == "Cancelled" else "Open"}


@app.route("/requests")
def requests_list():
    status = request.args.get("status", "")
    flt = {"status": status} if status else {}
    rows = list(db.requests.find(flt))
    rows.sort(key=lambda r: (URGENCY_RANK.get(r["urgency"], 9), r["requestedAt"]))
    return render_template("requests_list.html", rows=rows, names=hospital_names(), status=status)


@app.route("/requests/new", methods=["GET", "POST"])
def request_new():
    form = {}
    if request.method == "POST":
        doc, errors = request_from_form()
        if not errors:
            doc.update({"_id": next_id("requests", "R"), "unitsReceived": 0,
                        "status": "Open", "requestedAt": now_utc()})
            db.requests.insert_one(doc)                     # CREATE
            flash(f"Request {doc['_id']} created. Use 'Match donors' to find donors.", "ok")
            return redirect(url_for("requests_list"))
        for e in errors:
            flash(e, "error")
        form = request.form
    return render_template("request_form.html", form=form, mode="new", blood_types=BLOOD_TYPES,
                           urgencies=URGENCIES, hospitals=list(db.hospitals.find().sort("name", 1)))


@app.route("/requests/<request_id>/edit", methods=["GET", "POST"])
def request_edit(request_id):
    req = db.requests.find_one({"_id": request_id}) or abort(404)
    form = request_to_form(req)
    if request.method == "POST":
        doc, errors = request_from_form(editing=True)
        if not errors:
            db.requests.update_one({"_id": request_id}, {"$set": doc})   # UPDATE
            refresh_request(request_id)  # re-check Open/Fulfilled with the new units needed
            flash(f"Request {request_id} updated.", "ok")
            return redirect(url_for("requests_list"))
        for e in errors:
            flash(e, "error")
        form = request.form
    return render_template("request_form.html", form=form, mode="edit", request_id=request_id,
                           blood_types=BLOOD_TYPES, urgencies=URGENCIES,
                           hospitals=list(db.hospitals.find().sort("name", 1)))


@app.route("/requests/<request_id>/delete", methods=["POST"])
def request_delete(request_id):
    used = db.donations.count_documents({"requestId": request_id})
    if used:
        flash(f"Cannot delete {request_id}: {used} donation(s) are linked to it.", "error")
    else:
        db.requests.delete_one({"_id": request_id})                  # DELETE
        flash(f"Request {request_id} deleted.", "ok")
    return redirect(url_for("requests_list"))


@app.route("/requests/<request_id>/match")
def request_match(request_id):
    """The core feature: nearest, eligible, blood-compatible donors for a request."""
    req = db.requests.find_one({"_id": request_id}) or abort(404)
    hosp = db.hospitals.find_one({"_id": req["hospitalId"]})
    if not hosp:
        flash("The hospital of this request no longer exists.", "error")
        return redirect(url_for("requests_list"))
    try:
        radius_km = max(1.0, float(request.args.get("radius", "10")))
    except ValueError:
        radius_km = 10.0
    cutoff = eligibility_cutoff()
    donors = list(db.donors.find({
        "bloodType": {"$in": req["compatibleTypes"]},
        "available": True,
        "$or": [{"lastDonationDate": None}, {"lastDonationDate": {"$lt": cutoff}}],
        "location": {"$near": {"$geometry": hosp["location"], "$maxDistance": radius_km * 1000}},
    }))                                                      # results come nearest first
    hlon, hlat = hosp["location"]["coordinates"]
    for d in donors:
        dlon, dlat = d["location"]["coordinates"]
        d["distance_km"] = round(haversine_km(hlon, hlat, dlon, dlat), 1)
    return render_template("match.html", req=req, hosp=hosp, donors=donors,
                           radius=radius_km, days=ELIGIBILITY_DAYS)


# ---------------------------------------------------------------- donations
def donation_from_form():
    errors = []
    donor = db.donors.find_one({"_id": f("donorId")})
    if not donor:
        errors.append("Choose a valid donor.")
    req = None
    if f("requestId"):
        req = db.requests.find_one({"_id": f("requestId")})
        if not req:
            errors.append("The chosen request does not exist.")
    hospital_id = req["hospitalId"] if req else f("hospitalId")
    if not db.hospitals.find_one({"_id": hospital_id}):
        errors.append("Choose a valid hospital.")
    if donor and req and donor["bloodType"] not in req["compatibleTypes"]:
        errors.append(f"Donor blood type {donor['bloodType']} is not compatible with request "
                      f"{req['_id']} (needs {req['bloodTypeNeeded']}).")
    units = None
    try:
        units = int(f("units"))
        if units < 1:
            raise ValueError
    except ValueError:
        errors.append("Units must be a whole number of at least 1.")
    date = None
    try:
        date = parse_date(f("donationDate"))
    except ValueError:
        errors.append("Donation date is not a valid date.")
    dtype = f("type")
    if dtype not in DONATION_TYPES:
        errors.append("Choose a donation type.")
    doc = {"donorId": donor["_id"] if donor else None, "hospitalId": hospital_id,
           "requestId": req["_id"] if req else None,
           "bloodType": donor["bloodType"] if donor else None,
           "units": units, "donationDate": date, "type": dtype}
    return doc, errors


def donation_to_form(d):
    return {"donorId": d["donorId"], "hospitalId": d["hospitalId"], "requestId": d.get("requestId") or "",
            "units": d["units"], "donationDate": fmt_date(d["donationDate"], ""), "type": d["type"]}


def donation_form_context():
    return {"donors": list(db.donors.find().sort("name", 1)),
            "hospitals": list(db.hospitals.find().sort("name", 1)),
            "open_requests": list(db.requests.find({"status": "Open"})),
            "types": DONATION_TYPES}


@app.route("/donations")
def donations_list():
    rows = list(db.donations.find().sort("donationDate", -1))
    donors = {d["_id"]: d["name"] for d in db.donors.find()}
    return render_template("donations_list.html", rows=rows, donors=donors, names=hospital_names())


@app.route("/donations/new", methods=["GET", "POST"])
def donation_new():
    form = {"donationDate": now_utc().strftime("%Y-%m-%d"), "units": 1,
            "requestId": request.args.get("requestId", ""), "donorId": request.args.get("donorId", "")}
    if request.method == "POST":
        doc, errors = donation_from_form()
        if not errors:
            doc["_id"] = next_id("donations", "DN")
            db.donations.insert_one(doc)                    # CREATE
            refresh_donor(doc["donorId"])
            refresh_request(doc["requestId"])
            flash(f"Donation {doc['_id']} recorded. Donor and request were updated.", "ok")
            return redirect(url_for("donations_list"))
        for e in errors:
            flash(e, "error")
        form = request.form
    return render_template("donation_form.html", form=form, mode="new", **donation_form_context())


@app.route("/donations/<donation_id>/edit", methods=["GET", "POST"])
def donation_edit(donation_id):
    old = db.donations.find_one({"_id": donation_id}) or abort(404)
    form = donation_to_form(old)
    if request.method == "POST":
        doc, errors = donation_from_form()
        if not errors:
            db.donations.update_one({"_id": donation_id}, {"$set": doc})   # UPDATE
            for donor_id in {old["donorId"], doc["donorId"]}:
                refresh_donor(donor_id)
            for rid in {old.get("requestId"), doc["requestId"]}:
                refresh_request(rid)
            flash(f"Donation {donation_id} updated.", "ok")
            return redirect(url_for("donations_list"))
        for e in errors:
            flash(e, "error")
        form = request.form
    ctx = donation_form_context()
    # an edited donation may belong to a request that is no longer Open: keep it selectable
    if old.get("requestId") and not any(r["_id"] == old["requestId"] for r in ctx["open_requests"]):
        current = db.requests.find_one({"_id": old["requestId"]})
        if current:
            ctx["open_requests"].append(current)
    return render_template("donation_form.html", form=form, mode="edit", donation_id=donation_id, **ctx)


@app.route("/donations/<donation_id>/delete", methods=["POST"])
def donation_delete(donation_id):
    old = db.donations.find_one({"_id": donation_id}) or abort(404)
    db.donations.delete_one({"_id": donation_id})                    # DELETE
    refresh_donor(old["donorId"])
    refresh_request(old.get("requestId"))
    flash(f"Donation {donation_id} deleted. Donor and request were updated.", "ok")
    return redirect(url_for("donations_list"))


if __name__ == "__main__":
    try:
        init_indexes()
    except PyMongoError as err:
        print("WARNING: could not reach MongoDB. Is the MongoDB service running?\n", err)
    app.run(debug=True)
