# Company Checker

A small web platform that checks a CSV of companies against the official
Companies House register and tests whether email domains still accept mail.

## How it works
- Upload a CSV with a company name or company number column (email column optional)
- Each company is looked up on Companies House (status, dissolution date)
- Email domains are checked for a working mail server
- Download the results as a new CSV with extra columns added

## Deploying (Render, free)
1. Put these files in a GitHub repository
2. On https://render.com choose New -> Blueprint and pick the repository
3. When asked, paste your Companies House API key as CH_API_KEY
4. APP_PASSWORD is optional - set it to require a password to use the site

Note: on Render's free plan the site goes to sleep when idle; the first
visit after a quiet spell takes ~1 minute to wake up.

## Limits
- Up to 2,000 rows per run (Companies House rate limits apply)
- "Domain working" means the email domain accepts mail; no free method can
  guarantee a specific inbox is live.
