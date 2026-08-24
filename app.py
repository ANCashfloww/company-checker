"""
Company Checker - bulk edition
Checks companies against Companies House and verifies email domains.
Built for large files: results are written to disk as they are produced,
so a run can be paused, resumed, or partially downloaded at any time.

API key lives in the CH_API_KEY environment variable - never in the browser.
"""

import csv
import hashlib
import io
import json
import os
import re
import threading
import time

import requests
from flask import Flask, jsonify, render_template, request, Response, send_file

app = Flask(__name__)

CH_BASE = "https://api.company-information.service.gov.uk"
CH_API_KEY = os.environ.get("CH_API_KEY", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
DATA_DIR = os.environ.get("DATA_DIR", "/tmp/company_checker")
MAX_ROWS = 50000

# Companies House allows 600 requests per 5 minutes. 0.52s spacing = 576/5min.
MIN_INTERVAL = float(os.environ.get("CH_MIN_INTERVAL", "0.52"))

EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
NUMBER_COLS = {"company_number", "companynumber", "number", "crn", "reg_number", "registration_number"}
NAME_COLS = {"company_name", "companyname", "company", "name", "organisation", "organization"}
EMAIL_COLS = {"email", "email_address", "e-mail", "contact_email", "emailaddress"}

EXTRA_COLS = ["ch_status", "ch_matched_name", "ch_company_number",
              "ch_dissolved_date", "ch_note", "email_status", "email_note"]

JOBS = {}          # job_id -> live state dict
JOBS_LOCK = threading.Lock()

os.makedirs(DATA_DIR, exist_ok=True)


def job_dir(job_id):
    return os.path.join(DATA_DIR, job_id)


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
    """Rate-limited client with an in-run cache so repeats cost nothing."""

    def __init__(self, api_key):
        self.session = requests.Session()
        self.session.auth = (api_key, "")
        self.last = 0.0
        self.calls = 0
        self.cache = {}

    def _get(self, path, params=None):
        wait = MIN_INTERVAL - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        self.calls += 1
        for attempt in range(3):
            try:
                r = self.session.get(CH_BASE + path, params=params, timeout=20)
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 429:          # rate limited - wait it out
                time.sleep(60)
                self.last = time.time()
                continue
            if r.status_code >= 500:          # their end wobbling
                time.sleep(10)
                continue
            return r
        return r

    def by_number(self, number):
        key = "n:" + number
        if key in self.cache:
            return dict(self.cache[key])
        r = self._get(f"/company/{number}")
        if r.status_code == 404:
            out = {"ch_status": "not_found", "ch_note": f"No company with number {number}"}
        elif r.status_code == 401:
            raise PermissionError("Companies House rejected the API key")
        else:
            r.raise_for_status()
            d = r.json()
            out = {
                "ch_status": d.get("company_status", "unknown"),
                "ch_matched_name": d.get("company_name", ""),
                "ch_company_number": d.get("company_number", number),
                "ch_dissolved_date": d.get("date_of_cessation", ""),
                "ch_note": "matched by company number",
            }
        self.cache[key] = dict(out)
        return out

    def by_name(self, name):
        key = "s:" + name.lower()
        if key in self.cache:
            return dict(self.cache[key])
        r = self._get("/search/companies", params={"q": name, "items_per_page": 5})
        if r.status_code == 401:
            raise PermissionError("Companies House rejected the API key")
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            out = {"ch_status": "not_found", "ch_note": "No search results for this name"}
        else:
            top = items[0]
            exact = top.get("title", "").strip().lower() == name.strip().lower()
            out = self.by_number(top.get("company_number", ""))
            out["ch_note"] = ("exact name match" if exact
                              else f"closest match for '{name}' - worth double-checking")
        self.cache[key] = dict(out)
        return out


def check_email(email, mx_cache):
    """Syntax check plus MX lookup over DNS-over-HTTPS (works on any host)."""
    email = (email or "").strip()
    if not email:
        return {"email_status": "missing", "email_note": ""}
    if not EMAIL_RE.match(email):
        return {"email_status": "invalid_syntax", "email_note": "not a valid email format"}

    domain = email.rsplit("@", 1)[1].lower()
    if domain not in mx_cache:
        result = None
        for attempt in range(2):
            try:
                r = requests.get("https://dns.google/resolve",
                                 params={"name": domain, "type": "MX"}, timeout=10)
                data = r.json()
                result = bool(data.get("Answer")) and data.get("Status") == 0
                break
            except Exception:
                time.sleep(2)
        mx_cache[domain] = result

    ok = mx_cache[domain]
    if ok is True:
        return {"email_status": "domain_ok", "email_note": "domain accepts mail"}
    if ok is False:
        return {"email_status": "dead_domain", "email_note": "domain has no mail server"}
    return {"email_status": "unknown", "email_note": "could not check domain"}


def save_progress(job):
    """Write a small progress file so a run survives a restart."""
    try:
        with open(os.path.join(job_dir(job["id"]), "progress.json"), "w") as f:
            json.dump({"done": job["done"], "total": job["total"],
                       "summary": job["summary"], "state": job["state"],
                       "fields": job["fields"]}, f)
    except OSError:
        pass


def run_job(job_id):
    job = JOBS[job_id]
    ch = CompaniesHouse(CH_API_KEY)
    mx_cache = {}
    d = job_dir(job_id)
    results_path = os.path.join(d, "results.csv")
    fieldnames = job["fields"] + [c for c in EXTRA_COLS if c not in job["fields"]]

    # Append mode: if we're resuming, earlier rows are already written.
    new_file = not os.path.exists(results_path)
    f = open(results_path, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
    if new_file:
        writer.writeheader()

    try:
        for idx in range(job["done"], job["total"]):
            if job.get("cancel"):
                job["state"] = "cancelled"
                break
            row = job["rows"][idx]
            rec = dict(row)
            try:
                number = normalise_number(row.get(job["num_col"], "")) if job["num_col"] else ""
                if number:
                    rec.update(ch.by_number(number))
                elif job["name_col"] and (row.get(job["name_col"]) or "").strip():
                    rec.update(ch.by_name(row[job["name_col"]].strip()))
                else:
                    rec.update({"ch_status": "skipped", "ch_note": "row has no company number or name"})
            except PermissionError as e:
                job["error"] = str(e) + " - check CH_API_KEY in your hosting settings"
                job["state"] = "error"
                break
            except requests.RequestException as e:
                rec.update({"ch_status": "error", "ch_note": type(e).__name__})

            if job["email_col"]:
                rec.update(check_email(row.get(job["email_col"], ""), mx_cache))

            # running totals
            s = job["summary"]
            st = rec.get("ch_status", "")
            if st == "active":
                s["active"] += 1
            elif st in ("dissolved", "liquidation", "administration", "receivership",
                        "insolvency-proceedings", "closed", "converted-closed"):
                s["inactive"] += 1
            elif st:
                s["other"] += 1
            es = rec.get("email_status")
            if es == "domain_ok":
                s["email_ok"] += 1
            elif es in ("dead_domain", "invalid_syntax"):
                s["email_bad"] += 1

            writer.writerow(rec)
            job["done"] += 1

            if job["done"] % 25 == 0:
                f.flush()
                save_progress(job)

        else:
            job["state"] = "finished"
    except Exception as e:
        job["error"] = f"Unexpected problem: {type(e).__name__}"
        job["state"] = "error"
    finally:
        f.close()
        save_progress(job)


@app.route("/")
def index():
    return render_template("index.html",
                           needs_password=bool(APP_PASSWORD),
                           key_missing=not CH_API_KEY,
                           max_rows=MAX_ROWS)


@app.route("/upload", methods=["POST"])
def upload():
    if APP_PASSWORD and request.form.get("password", "") != APP_PASSWORD:
        return jsonify({"error": "Wrong access password."}), 403
    if not CH_API_KEY:
        return jsonify({"error": "The server has no Companies House key set. "
                                 "Add CH_API_KEY in your hosting settings."}), 500

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file received - choose a CSV file first."}), 400

    raw = f.read()
    try:
        text = raw.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        fields = reader.fieldnames or []
    except Exception:
        return jsonify({"error": "Couldn't read that file. Save it as CSV and try again."}), 400

    if not rows:
        return jsonify({"error": "The file appears to be empty."}), 400
    if len(rows) > MAX_ROWS:
        return jsonify({"error": f"That's {len(rows):,} rows - the limit is {MAX_ROWS:,}."}), 400

    num_col = find_col(fields, NUMBER_COLS)
    name_col = find_col(fields, NAME_COLS)
    email_col = find_col(fields, EMAIL_COLS)
    if not num_col and not name_col:
        return jsonify({"error": "Couldn't find a company name or company number column. "
                                 f"Your columns are: {', '.join(fields)}"}), 400

    # Same file uploaded again = same job id, so an interrupted run resumes.
    job_id = hashlib.sha256(raw).hexdigest()[:16]
    d = job_dir(job_id)
    os.makedirs(d, exist_ok=True)

    with JOBS_LOCK:
        existing = JOBS.get(job_id)
        if existing and existing["state"] == "running":
            return jsonify({"job_id": job_id, "total": existing["total"],
                            "resumed": True, "done": existing["done"],
                            "already_running": True})

        done = 0
        summary = {"active": 0, "inactive": 0, "other": 0, "email_ok": 0, "email_bad": 0}
        pfile = os.path.join(d, "progress.json")
        if os.path.exists(pfile) and os.path.exists(os.path.join(d, "results.csv")):
            try:
                with open(pfile) as pf:
                    prev = json.load(pf)
                if prev.get("total") == len(rows):
                    done = int(prev.get("done", 0))
                    summary = prev.get("summary", summary)
            except (OSError, ValueError):
                done = 0

        # rows needing 2 API calls (name lookup) take twice as long
        per_row = 2 if not num_col else 1
        eta_seconds = int((len(rows) - done) * per_row * MIN_INTERVAL)

        JOBS[job_id] = {"id": job_id, "state": "running", "done": done,
                        "total": len(rows), "rows": rows, "fields": fields,
                        "num_col": num_col, "name_col": name_col, "email_col": email_col,
                        "error": "", "summary": summary, "cancel": False,
                        "started": time.time(), "eta": eta_seconds}

    threading.Thread(target=run_job, args=(job_id,), daemon=True).start()

    return jsonify({"job_id": job_id, "total": len(rows), "done": done,
                    "resumed": done > 0, "eta_seconds": eta_seconds,
                    "matched_by": "company number" if num_col else "company name",
                    "columns": {"number": num_col or "", "name": name_col or "",
                                "email": email_col or ""}})


@app.route("/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        # server may have restarted - fall back to what's on disk
        pfile = os.path.join(job_dir(job_id), "progress.json")
        if os.path.exists(pfile):
            try:
                with open(pfile) as f:
                    prev = json.load(f)
                return jsonify({"state": "interrupted", "done": prev.get("done", 0),
                                "total": prev.get("total", 0), "error": "",
                                "summary": prev.get("summary", {}),
                                "partial": True})
            except (OSError, ValueError):
                pass
        return jsonify({"error": "Job not found - upload the file again."}), 404

    elapsed = time.time() - job["started"]
    rate = job["done"] / elapsed if elapsed > 5 and job["done"] else 0
    remaining = int((job["total"] - job["done"]) / rate) if rate else job.get("eta", 0)

    return jsonify({"state": job["state"], "done": job["done"], "total": job["total"],
                    "error": job["error"], "summary": job["summary"],
                    "remaining_seconds": remaining})


@app.route("/cancel/<job_id>", methods=["POST"])
def cancel(job_id):
    job = JOBS.get(job_id)
    if job:
        job["cancel"] = True
    return jsonify({"ok": True})


@app.route("/download/<job_id>")
def download(job_id):
    """Works mid-run too - you get everything finished so far."""
    path = os.path.join(job_dir(job_id), "results.csv")
    if not os.path.exists(path):
        return "No results yet.", 404
    return send_file(path, mimetype="text/csv", as_attachment=True,
                     download_name="verified_results.csv")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
