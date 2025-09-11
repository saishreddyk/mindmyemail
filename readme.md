# MindMyEmail: Email Job Application Classifier

This Python application automatically classifies Gmail messages related to job applications by analyzing email content using GPT and applying appropriate labels for better organization.

## Features

- Authenticates with Gmail API using OAuth 2.0
- Fetches and analyzes new emails since last execution
- Uses GPT to identify job-related emails and their status
- Automatically creates and applies Gmail labels based on job application status
- Maintains execution history to avoid reprocessing emails
- Comprehensive logging system

## Impact
It highlighted 2 missed interview calls, and ~5 missed online assessment invitations in the bulk of application emails.

So, perhaps, the most life-changing project I ever setup.


## Setup

1. **Google Cloud Setup**:
   - For now for this, get the `credentials.json` file from @saishreddyk.

2. **Environment Variables**:
   Create a `.env` file with:
   ```
   OPENAI_API_KEY=your_openai_api_key
   ```

3. **First-time Authentication**:
   - Run the script for the first time
   - Complete OAuth flow in browser
   - Token will be saved as `token.json`

## Job Status Labels

The application categorizes emails into the following labels:
- Applied
- Holding
- Assessment
- Interview
- Offer
- Rejected
- Other

## Usage

To install dependencies:
```bash
uv venv
uv install
```

Run the script (single account or all configured accounts):
```bash
uv run python read_email.py
```

The script will:
1. Authenticate with Gmail
2. Fetch emails from a safe lookback window
3. Analyze each email using GPT
4. Create and apply appropriate labels
5. Update a robust processing state (watermark + dedupe)

## File Structure

- `read_email.py`: Main script
- `credentials.json`: Google OAuth credentials
- `token.json`: Generated OAuth token
- `state.json`: Processing state (watermark + dedupe)
- `last_executed_date.txt`: Legacy timestamp file (auto-bootstrapped if present)
- `.env`: Environment variables

## Multiple Accounts

- Add an account (runs OAuth and saves a token under `accounts/<email>/token.json`):
  ```bash
  uv run python read_email.py --add-account
  ```
- Process all configured accounts (default behavior when `accounts/` exists):
  ```bash
  uv run python read_email.py
  ```
- Process a specific account:
  ```bash
  uv run python read_email.py --account you@example.com --account other@example.com
  ```

Per-account state is stored at `accounts/<email>/state.json`.

## Logging

The application uses a configured logger that:
- Records all major operations
- Includes timestamps and log levels
- Helps in debugging and monitoring

## Notes

- The script uses GPT-4-mini for email analysis
- Email content is truncated to ~22000 characters for API limits
- Labels are created hierarchically under "Jobs/" in Gmail
- The script maintains state between runs using `state.json` (watermark + dedupe). It bootstraps from `last_executed_date.txt` if present.

## State & Backfill

- The script stores a high-watermark and a dedupe map in `state.json`:
  - `last_internal_ts`: last seen Gmail `internalDate` (epoch seconds)
  - `seen_ids`: recent message IDs with their timestamps (pruned to a lookback window)
  - `last_run_at`: last run time (epoch seconds)
- Lookback cushion and first-run backfill are configurable via `.env`:
  - `LOOKBACK_SECONDS` (default: 172800 → 48 hours)
  - `BACKFILL_DAYS` (default: 14)
- First run without state uses a backfill from `now - BACKFILL_DAYS` and then filters precisely by `internalDate`.

## Security

- OAuth credentials and tokens should be kept secure
- The `.env` file containing API keys should not be committed to version control
