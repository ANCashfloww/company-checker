Company Checker
A small web platform that checks a CSV of companies against the official
Companies House register and tests whether email domains still accept mail.
How it works
Upload a CSV with a company name or company number column (email column optional)
Each company is looked up on Companies House (status, dissolution date)
Email domains are checked for a working mail server
Download the results as a new CSV with extra columns added
Deploying (Render, free)
Put these files in a GitHub repository
On https://render.com choose New -> Blueprint and pick the repository
When asked, paste your Companies House API key as CH_API_KEY
APP_PASSWORD is optional - set it to require a password to use the site
Note: on Render's free plan the site goes to sleep when idle; the first
visit after a quiet spell takes ~1 minute to wake up.
Large files
Companies House allows 600 lookups per 5 minutes, so big runs take time:
~1 second per company if your file has company numbers
~2 seconds per company if it only has names (search + fetch)
So 20,000 companies with numbers is roughly 3 hours; by name, roughly 6.
The run is resumable. Results are written to disk as they are produced, so
you can download partial results at any point, and re-uploading the same
file continues from where it stopped rather than starting over.
Keep the browser tab open during a run. On Render's free plan the site
sleeps after 15 minutes without traffic; the open tab's progress checks
count as traffic and keep it awake. For unattended multi-hour runs, use
Render's Starter plan (about $7/month) which never sleeps.
Limits
Up to 50,000 rows per run
"Domain working" means the email domain accepts mail; no free method can
guarantee a specific inbox is live.
