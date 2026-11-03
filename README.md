# OpsFleet data analysis agent

A chat assistant that lets non-technical executives ask questions about the
`bigquery-public-data.thelook_ecommerce` dataset in plain English, and get answers and
reports with action items. Built with Google ADK, Gemini and BigQuery.

The design (architecture, how each requirement is handled, prototype vs production) is in
[`docs/design.md`](docs/design.md).

## Setup

You need Python 3.12+, [uv](https://docs.astral.sh/uv/) and the
[gcloud CLI](https://cloud.google.com/sdk/docs/install), plus a Google Cloud project.
The BigQuery free tier is more than enough. No credit card is needed for the public dataset.

```bash
git clone https://github.com/peejay2/opsfleet-agent.git && cd opsfleet-agent
uv sync                                          # installs everything into .venv
gcloud auth application-default login            # lets the code run BigQuery queries as you
gcloud services enable bigquery.googleapis.com --project <your project ID>   # if not already on
```

Create a `.env` file in the project root:

```
GOOGLE_API_KEY=<your Gemini key from https://aistudio.google.com/apikey>
GOOGLE_CLOUD_PROJECT=<your Google Cloud project ID>
```

Optional settings, also in `.env`:

| Setting | Default | What it does |
|---|---|---|
| `MODEL` | `gemini-3.5-flash-lite` | Gemini model for the agent |
| `MAX_BYTES_BILLED` | `1000000000` (1 GB) | Queries that would scan more are refused |
| `MAX_ROWS` | `50` | Rows of each result passed to the model |
| `MAX_FAILED_QUERIES` | `3` | Failed or empty queries in a row before the agent must stop and explain |
| `MAX_MODEL_CALLS` | `15` | Gemini calls per question |
| `GOLDEN_EXAMPLES` | `3` | Example analyses added per question |
| `REPORTS_DB` | `reports.db` | Where saved reports are kept |
| `LOG_FILE` | `logs/agent.jsonl` | Where the activity log is written |

## Run

```bash
uv run python -m opsfleet_agent --user anna     # --quiet hides the queries
```

Each user may only analyse certain products. The demo users are in `opsfleet_agent/access.json`:

| User | Sees products where |
|---|---|
| `anna` | brand is Calvin Klein or Levi's |
| `ben` | category is Jeans or Sweaters |
| `carla` | distribution center is Memphis TN or Chicago IL |
| `dave` | category is Jeans **and** distribution center is Houston TX or New Orleans LA |

A user can have several filters; a product must pass all of them.

Questions to try:

- What data do we have, and what can we do with it?
- What was our monthly revenue over the last 12 months?
- Compare the top 5 product categories by revenue this year.
- Why did our churn rate spike last month?
- Which states underspend compared with California, and why?
- Create a report for Q1 including insights and action items for Q2.
- Write a short report on Calvin Klein sales in 2025 and save it.

## Example run

A real session as `anna` (with `--quiet`), shortened in two places:

```
Retail analyst (gemini-3.5-flash-lite). Signed in as anna: products where brand in Calvin Klein, Levi's.
Type 'quit' or press Ctrl+D to leave.

you> What data do we have, and what can we do with it?

agent> We have rich retail data covering orders, products, customers, and distribution centers
specifically for our brands **Calvin Klein** and **Levi's**.
[... the five tables and three kinds of analysis ...]
What would you like to explore first?

you> Write a short report on Calvin Klein sales in 2025 and save it.

agent> Here is the short report on Calvin Klein sales for 2025, which has now been successfully
saved to your Saved Reports library.

### Summary
In full-year 2025, Calvin Klein generated **$34,956.08** in net revenue across **531 orders**
and **530 unique customers**. Performance remained relatively steady throughout the year,
peaking during the winter holiday season in December.
[... key figures and findings ...]

### Action Items
1. **Capitalize on Outerwear & Suiting Demand:** Increase marketing and inventory depth for
   *Suits & Sport Coats* and *Outerwear & Coats* ahead of peak cold-weather selling periods.
2. **Leverage Underwear for Customer Acquisition:** Utilize high-volume categories like
   *Underwear* as entry points for new customers, with targeted cross-selling into
   higher-margin apparel categories.
3. **Investigate Mid-Year Dips:** Analyze customer engagement and promotional calendars
   around July to address the mid-summer revenue dip.

you> What are the emails of our top 3 Calvin Klein customers?

agent> I cannot provide customer email addresses or personal details. However, I can share
their customer IDs and spending totals for Calvin Klein products:

1. **Customer ID 44921**: $314.30 total spend (1 order)
2. **Customer ID 62432**: $259.99 total spend (1 order)
3. **Customer ID 84817**: $259.99 total spend (1 order)

you> delete all reports mentioning Calvin Klein

Delete this report?
  1. Calvin Klein Sales Report - 2025  (2026-11-02)
Type yes to delete, anything else to cancel.
confirm> yes

agent> I have moved the "Calvin Klein Sales Report - 2025" report to the trash. You can say
'undo' at any time within the next 30 days to restore it.
```

The "Delete this report?" prompt comes from the program, not from Gemini. The agent can't
see your answer or answer on your behalf.

## Saved reports

Ask the agent to save a report, list your reports, or delete them, e.g. "delete all
reports mentioning Calvin Klein" or "delete all the reports we made in this conversation".
Before anything is deleted, the program itself (not the AI) shows the list and asks you
to type `yes`; for more than 10 reports you type the number instead. Deleted reports go
to the trash for 30 days, and "undo" brings back the last delete. Reports are stored in
`reports.db` in the project root.

## Activity log

Every question is recorded in `logs/agent.jsonl`, one line per step: the question,
which example analyses it was given, each Gemini call (with token counts), each query
with how long it took and how many rows came back or the error, Gemini retries and
failures, the answer, and a summary line. Query results themselves are not logged.
To read the last few questions in plain form:

```bash
uv run python -m opsfleet_agent.activity_log            # --last 10 for more
```

## Tests

```bash
uv run pytest
```

The 75 tests run without the network or any keys.

## Code map

| File | What it does |
|---|---|
| `opsfleet_agent/cli.py` | The terminal chat, including the confirmation prompt for deletes |
| `opsfleet_agent/agent.py` | The agent and its instructions |
| `opsfleet_agent/tools.py` | `describe_tables` and `run_sql`, with the failed-query limit |
| `opsfleet_agent/safety.py` | Checks and rewrites every query to the user's products; removes emails from answers |
| `opsfleet_agent/bq.py` | Runs queries on BigQuery, after a dry run that checks they're read-only and within the cost limit |
| `opsfleet_agent/access.py`, `access.json` | Which products each user may see |
| `opsfleet_agent/golden.py`, `trios.json` | The golden bucket: example analyses, and the lookup that picks the closest ones |
| `opsfleet_agent/reports.py`, `report_tools.py` | Saved reports: storage, and the find / save / delete / undo tools |
| `opsfleet_agent/recovery.py` | Gemini retries and plain-English error messages |
| `opsfleet_agent/activity_log.py` | The activity log, and the command that reads it |
| `opsfleet_agent/config.py` | Settings |
