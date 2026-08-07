"""
Company Checker - web platform
Checks companies against Companies House and verifies email domains.
Deployed on Render (or any host). API key lives in the CH_API_KEY
environment variable - never in this code or in the browser.
"""

import csv
import io
import os
import re
import threading
import time
import uuid

import requests
from flask import Flask, jsonify, render_template, request, Response

app = Flask(__name__)

CH_BASE = "https://api.company-information.service.gov.uk"
CH_API_KEY = os.environ.get("CH_API_KEY", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")  # optional gate for the whole site

EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
NUMBER_COLS = {"company_number", "companynumber", "number", "crn", "reg_number", "registration_number"}
NAME_COLS = {"company_name", "companyname", "company", "name", "organisation", "organization"}
EMAIL_COLS = {"email", "email_address", "e-mail", "contact_email", "emailaddress"}

EXTRA_COLS = ["ch_status", "ch_matched_name", "ch_company_number",
              "ch_dissolved_date", "ch_note", "email_status", "email_note"]

JOBS = {}  # job_id -> dict(state, done, total, rows, fields, error, summary)
JOBS_LOCK = threading.Lock()
MAX_ROWS = 2000


def find_col(fieldnames, candidates):
    for f in fieldnames:
        if f.strip().lower().replace(" ", "_") in candidates:
            return f
    return None


def normalise_number(num):
    num = (num or "").strip().upper().replace(" ", "")
    if num.isdigit() and len(num) < 8:
        num = num.zfill(8)
    return num


class CompaniesHouse:
    """Small client that stays inside the 600 requests / 5 min limit."""

    def __init__(self, api_key):
        self.session = requests.Session()
        self.session.auth = (api_key, "")
        self.last = 0.0

    def _get(self, path, params=None):
        wait = 0.55 - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        r = self.session.get(CH_BASE + path, params=params, timeout=15)
        if r.status_code == 429:
            time.sleep(30)
            r = self.session.get(CH_BASE + path, params=params, timeout=15)
        return r

    def by_number(self, number):
        r = self._get(f"/company/{number}")
        if r.status_code == 404:
            return {"ch_status": "not_found", "ch_note": f"No company with number {number}"}
        if r.status_code == 401:
            raise PermissionError("Companies House rejected the API key")
        r.raise_for_status()
        d = r.json()
        return {
            "ch_status": d.get("company_status", "unknown"),
            "ch_matched_name": d.get("company_name", ""),
            "ch_company_number": d.get("company_number", number),
            "ch_dissolved_date": d.get("date_of_cessation", ""),
            "ch_note": "matched by company number",
        }

    def by_name(self, name):
        r = self._get("/search/companies", params={"q": name, "items_per_page": 5})
        if r.status_code == 401:
            raise PermissionError("Companies House rejected the API key")
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            return {"ch_status": "not_found", "ch_note": "No search results for this name"}
        top = items[0]
        exact = top.get("title", "").strip().lower() == name.strip().lower()
        result = self.by_number(top.get("company_number", ""))
        result["ch_note"] = "exact name match" if exact else f"closest match for '{name}' - worth double-checking"
        return result


def check_email(email, _mx_cache):
    """Syntax check + MX lookup via DNS-over-HTTPS (works on any host)."""
    email = (email or "").strip()
    if not email:
        return {"email_status": "missing", "email_note": ""}
    if not EMAIL_RE.match(email):
        return {"email_status": "invalid_syntax", "email_note": "not a valid email format"}

    domain = email.rsplit("@", 1)[1].lower()
    if domain not in _mx_cache:
        try:
            r = requests.get("https://dns.google/resolve",
                             params={"name": domain, "type": "MX"}, timeout=10)
            data = r.json()
            _mx_cache[domain] = bool(data.get("Answer")) and data.get("Status") == 0
        except Exception:
            _mx_cache[domain] = None

    ok = _mx_cache[domain]
    if ok is True:
        return {"email_status": "domain_ok", "email_note": "domain accepts mail"}
    if ok is False:
        return {"email_status": "dead_domain", "email_note": "domain has no mail server"}
    return {"email_status": "unknown", "email_note": "could not check domain"}


def run_job(job_id, rows, fields, num_col, name_col, email_col):
    job = JOBS[job_id]
    ch = CompaniesHouse(CH_API_KEY)
    mx_cache = {}
    try:
        for row in rows:
            rec = dict(row)
            try:
                number = normalise_number(row.get(num_col, "")) if num_col else ""
                if number:
                    rec.update(ch.by_number(number))
                elif name_col and (row.get(name_col) or "").strip():
                    rec.update(ch.by_name(row[name_col].strip()))
                else:
                    rec.update({"ch_status": "skipped", "ch_note": "row has no company number or name"})
            except PermissionError as e:
                job["error"] = str(e) + " - check the CH_API_KEY setting on the server"
                job["state"] = "error"
                return
            except requests.RequestException as e:
                rec.update({"ch_status": "error", "ch_note": type(e).__name__})

            if email_col:
                rec.update(check_email(row.get(email_col, ""), mx_cache))

            job["rows"].append(rec)
            job["done"] += 1

        s = job["summary"]
        for rec in job["rows"]:
            st = rec.get("ch_status", "")
            if st == "active":
                s["active"] += 1
            elif st in ("dissolved", "liquidation", "administration",
                        "receivership", "insolvency-proceedings", "closed"):
                s["inactive"] += 1
            elif st:
                s["other"] += 1
            if rec.get("email_status") == "domain_ok":
                s["email_ok"] += 1
            elif rec.get("email_status") in ("dead_domain", "invalid_syntax"):
                s["email_bad"] += 1
        job["state"] = "finished"
    except Exception as e:
        job["error"] = f"Unexpected problem: {type(e).__name__}"
        job["state"] = "error"


@app.route("/")
def index():
    return render_template("index.html",
                           needs_password=bool(APP_PASSWORD),
                           key_missing=not CH_API_KEY)


@app.route("/upload", methods=["POST"])
def upload():
    if APP_PASSWORD and request.form.get("password", "") != APP_PASSWORD:
        return jsonify({"error": "Wrong access password."}), 403
    if not CH_API_KEY:
        return jsonify({"error": "The server has no Companies House key set. "
                                 "Add CH_API_KEY in your host's environment settings."}), 500

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file received - choose a CSV file first."}), 400

    try:
        text = f.read().decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        fields = reader.fieldnames or []
    except Exception:
        return jsonify({"error": "Couldn't read that file. Save it as CSV and try again."}), 400

    if not rows:
        return jsonify({"error": "The file appears to be empty."}), 400
    if len(rows) > MAX_ROWS:
        return jsonify({"error": f"That's {len(rows)} rows - the limit is {MAX_ROWS} per run. "
                                 "Split the file and run it in batches."}), 400

    num_col = find_col(fields, NUMBER_COLS)
    name_col = find_col(fields, NAME_COLS)
    email_col = find_col(fields, EMAIL_COLS)
    if not num_col and not name_col:
        return jsonify({"error": "Couldn't find a company name or company number column. "
                                 f"Your columns are: {', '.join(fields)}"}), 400

    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        # keep memory tidy: drop old finished jobs
        for k in [k for k, v in JOBS.items() if v["state"] in ("finished", "error")][:-5]:
            JOBS.pop(k, None)
        JOBS[job_id] = {"state": "running", "done": 0, "total": len(rows),
                        "rows": [], "fields": fields, "error": "",
                        "summary": {"active": 0, "inactive": 0, "other": 0,
                                    "email_ok": 0, "email_bad": 0}}

    threading.Thread(target=run_job, daemon=True,
                     args=(job_id, rows, fields, num_col, name_col, email_col)).start()

    detected = {"number": num_col or "", "name": name_col or "", "email": email_col or ""}
    return jsonify({"job_id": job_id, "total": len(rows), "columns": detected})


@app.route("/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found - it may have expired. Upload again."}), 404
    return jsonify({"state": job["state"], "done": job["done"],
                    "total": job["total"], "error": job["error"],
                    "summary": job["summary"]})


@app.route("/download/<job_id>")
def download(job_id):
    job = JOBS.get(job_id)
    if not job or job["state"] != "finished":
        return "Results not ready.", 404
    out = io.StringIO()
    fieldnames = job["fields"] + [c for c in EXTRA_COLS if c not in job["fields"]]
    writer = csv.DictWriter(out, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(job["rows"])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=verified_results.csv"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
