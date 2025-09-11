import base64
import json
import os
import os.path
import time
from datetime import datetime

import openai
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from logger_config import setup_logger
import re
import html as htmllib

# Setup logger
logger = setup_logger(__name__)

load_dotenv()

# --- Configurations ---
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
openai.api_key = os.environ["OPENAI_API_KEY"]  # Replace with your OpenAI API Key

# --- State management (Option A: watermark + dedupe) ---
STATE_PATH = "state.json"


def _get_env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def load_state(backfill_days: int) -> dict:
    """Load processing state. Bootstrap from last_executed_date.txt if present.

    Returns a dict with keys:
      - last_internal_ts: float|None
      - seen_ids: dict[str, float]
      - last_run_at: float|None
    """
    # Existing state.json
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r") as f:
                data = json.load(f)
            return {
                "last_internal_ts": data.get("last_internal_ts"),
                "seen_ids": data.get("seen_ids", {}),
                "last_run_at": data.get("last_run_at"),
            }
        except Exception:
            logger.warning("Failed to read state.json; starting with fresh state")

    # Bootstrap from legacy file if available
    legacy_path = "last_executed_date.txt"
    if os.path.exists(legacy_path):
        try:
            with open(legacy_path, "r") as f:
                ts = float(f.read().strip())
            return {"last_internal_ts": ts, "seen_ids": {}, "last_run_at": None}
        except Exception:
            logger.warning("Invalid legacy last_executed_date.txt; ignoring")

    # Fresh state
    return {"last_internal_ts": None, "seen_ids": {}, "last_run_at": None}


def save_state_atomic(state: dict):
    tmp_path = STATE_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f)
    os.replace(tmp_path, STATE_PATH)


def prune_seen_ids(state: dict, lookback_seconds: int):
    last_ts = state.get("last_internal_ts")
    if not last_ts:
        return
    cutoff = last_ts - lookback_seconds
    seen = state.get("seen_ids", {})
    pruned = {mid: ts for mid, ts in seen.items() if ts >= cutoff}
    removed = len(seen) - len(pruned)
    if removed:
        logger.debug(f"Pruned {removed} seen_ids older than cutoff {format_timestamp(cutoff)}")
    state["seen_ids"] = pruned


# --- Authenticate and Build Gmail Service ---
def authenticate_gmail():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
                if os.path.exists("token.json"):
                    os.remove("token.json")

        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)

        # Save the credentials for the next run
        with open("token.json", "w") as token:
            token.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# --- Fetch Unlabeled Emails ---
def get_emails(service, start_timestamp):
    start_date = datetime.fromtimestamp(start_timestamp).strftime("%Y/%m/%d")
    start_time = datetime.fromtimestamp(start_timestamp).strftime("%H:%M:%S")
    logger.info(f"Fetching emails after date: {start_date} {start_time}")

    messages = []
    page_token = None

    while True:
        results = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=f"after:{start_date}",  # Dynamic date-time query
                pageToken=page_token,
            )
            .execute()
        )
        if "messages" in results:
            messages.extend(results["messages"])
        page_token = results.get("nextPageToken")
        if not page_token:
            break

    if not messages:
        logger.info("No messages found in initial query")
        return []

    logger.info(
        f"Found {len(messages)} messages in initial query, filtering by timestamp {start_timestamp}"
    )

    filtered_messages = []
    for msg in messages:
        msg_details = (
            service.users().messages().get(userId="me", id=msg["id"]).execute()
        )
        internal_timestamp = (
            int(msg_details["internalDate"]) / 1000
        )  # Convert from milliseconds to seconds
        if internal_timestamp >= start_timestamp:
            filtered_messages.append(msg_details)
            logger.debug(
                f"Including message from {datetime.fromtimestamp(internal_timestamp)}"
            )
        else:
            logger.debug(
                f"Excluding message from {datetime.fromtimestamp(internal_timestamp)}"
            )

    logger.info(f"After timestamp filtering: {len(filtered_messages)} messages remain")
    return filtered_messages


# --- Get Email Content ---
def get_email_content(service, msg_id):
    message = (
        service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    )
    payload = message["payload"]

    # Extract subject
    subject = ""
    for header in payload.get("headers", []):
        if header.get("name", "").lower() == "subject":
            subject = header.get("value", "")
            break

    def decode_part_data(data: str) -> str:
        try:
            # Gmail provides base64url; ensure bytes then decode
            raw = base64.urlsafe_b64decode(data.encode("utf-8"))
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return ""

    def html_to_text(html: str) -> str:
        if not html:
            return ""
        # Remove scripts/styles
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\\1>", "", html)
        # Line breaks for common block elements
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</p>", "\n\n", text)
        text = re.sub(r"(?i)</div>", "\n", text)
        text = re.sub(r"(?i)</li>", "\n", text)
        # Strip remaining tags
        text = re.sub(r"<[^>]+>", "", text)
        # Unescape HTML entities
        text = htmllib.unescape(text)
        # Normalize whitespace
        text = re.sub(r"\r\n|\r", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def collect_texts(part, acc):
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")

        # Recurse into subparts if present
        if "parts" in part:
            for p in part["parts"]:
                collect_texts(p, acc)

        # Some top-level payloads can have data directly
        if data and mime:
            if mime.startswith("text/plain"):
                acc["plain"].append(decode_part_data(data))
            elif mime.startswith("text/html"):
                acc["html"].append(decode_part_data(data))

    texts = {"plain": [], "html": []}
    # Walk payload recursively
    collect_texts(payload, texts)

    # Fallbacks: prefer plain, else converted HTML, else try top-level body
    if texts["plain"]:
        return subject, "\n\n".join([t for t in texts["plain"] if t])
    if texts["html"]:
        html_joined = "\n\n".join([t for t in texts["html"] if t])
        return subject, html_to_text(html_joined)

    # Last resort: top-level body without parts
    body = payload.get("body", {})
    data = body.get("data")
    if data:
        mime = payload.get("mimeType", "")
        raw = decode_part_data(data)
        if mime.startswith("text/html"):
            return subject, html_to_text(raw)
        return subject, raw

    return subject, ""


def autolabel_openai(content):
    prompt = f"Read the following email and determine if it's about a job I might have applied. If yes, just say Yes, else No.\n\nEmail Content:\n{content}\n\nAnswer:"
    response = openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=150,
        temperature=0.2,
    )
    about_job = response.choices[0].message.content.strip().lower() == "yes"

    label = None
    if about_job:
        prompt = f"Read the email and label it among ['Applied', 'Holding', 'Assessment', 'Interview', 'Offer', 'Rejected', 'Other']. These indicate the status of the job application, if you think this is not a job application status mail label is other. \n\nEmail Content:\n{content}\n\nLabel (just select label, nothing else):"
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.2,
        )
        label = response.choices[0].message.content.strip()

    return [about_job, label]


def analyze_email_with_llm(content):
    # Truncate content to approximately 12000 characters (roughly 3000-4000 tokens)
    # This leaves room for the prompt and other message content
    truncated_content = content[:22000] + "..."

    try:
        about_job, label = autolabel_openai(truncated_content)
    except Exception as e:
        logger.error(f"OpenAI API Error: {e}")
        try:
            logger.info("Waiting for a minute before retrying...")
            time.sleep(60)
            about_job, label = autolabel_openai(truncated_content)
        except Exception as e:
            logger.error(f"OpenAI API Error: {e}")
            logger.error("Something went wrong with OpenAI API. Exiting...")
            exit(1)

    return [about_job, label]


# --- Apply Label to Email ---
def apply_label(service, msg_id, label_name):
    # Split nested label path
    label_parts = label_name.split("/")

    # Check if labels exist and create them if necessary
    labels = service.users().labels().list(userId="me").execute()
    current_path = ""
    final_label_id = None

    for i, part in enumerate(label_parts):
        current_path = "/".join(label_parts[: i + 1])
        label_id = None

        # Check if current level label exists
        for label in labels["labels"]:
            if label["name"].lower() == current_path.lower():
                label_id = label["id"]
                if i == len(label_parts) - 1:  # If this is the final part
                    final_label_id = label_id
                break

        # Create label if it doesn't exist
        if not label_id:
            label = {
                "name": current_path,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            }
            created_label = (
                service.users().labels().create(userId="me", body=label).execute()
            )
            label_id = created_label["id"]
            if i == len(label_parts) - 1:  # If this is the final part
                final_label_id = label_id
            # Refresh labels list after creating new label
            labels = service.users().labels().list(userId="me").execute()

    # Apply the final label to the message
    service.users().messages().modify(
        userId="me", id=msg_id, body={"addLabelIds": [final_label_id]}
    ).execute()


def format_timestamp(timestamp):
    """Convert timestamp to human readable format"""
    return datetime.fromtimestamp(timestamp).strftime("%Y/%m/%d %H:%M:%S")


# --- Main Function ---
def main():
    service = authenticate_gmail()

    backfill_days = _get_env_int("BACKFILL_DAYS", 14)
    lookback_seconds = _get_env_int("LOOKBACK_SECONDS", 2 * 24 * 60 * 60)  # 48h

    state = load_state(backfill_days)
    last_ts = state.get("last_internal_ts")

    if last_ts:
        query_start_ts = max(0, last_ts - lookback_seconds)
        logger.info(
            f"Starting fetch with cushion. Last watermark: {format_timestamp(last_ts)}; "
            f"query start: {format_timestamp(query_start_ts)}"
        )
    else:
        query_start_ts = datetime.now().timestamp() - backfill_days * 24 * 60 * 60
        logger.info(
            f"No prior watermark. Backfilling {backfill_days}d from: {format_timestamp(query_start_ts)}"
        )

    emails = get_emails(service, query_start_ts)
    logger.info(f"Found {len(emails)} emails after filtering by query start")

    if not emails:
        # Save state update for last run
        state["last_run_at"] = datetime.now().timestamp()
        save_state_atomic(state)
        logger.info("No emails to process. State saved.")
        return

    seen_ids = state.get("seen_ids", {})
    processed = 0
    for msg in emails:
        msg_id = msg.get("id")
        internal_timestamp = int(msg.get("internalDate", msg.get("internal_date", 0))) / 1000

        # Deduplicate by message id within lookback window
        if msg_id in seen_ids:
            logger.debug(f"Skipping already-seen message {msg_id}")
            # Still move watermark forward if needed
            if not last_ts or internal_timestamp > last_ts:
                state["last_internal_ts"] = internal_timestamp
                last_ts = internal_timestamp
            continue

        subject, content = get_email_content(service, msg_id)
        if not content:
            # Mark seen to avoid repeated fetching
            seen_ids[msg_id] = internal_timestamp
            if not last_ts or internal_timestamp > last_ts:
                state["last_internal_ts"] = internal_timestamp
                last_ts = internal_timestamp
            continue

        about_job, label = analyze_email_with_llm(content)
        logger.info(
            f"Email subject '{subject}' is about a job: {about_job}"
            + (f", Label: {label}" if about_job else "")
        )
        if about_job:
            apply_label(service, msg_id, f"Jobs/{label}")

        # Update watermark and seen ids
        seen_ids[msg_id] = internal_timestamp
        if not last_ts or internal_timestamp > last_ts:
            state["last_internal_ts"] = internal_timestamp
            last_ts = internal_timestamp
        processed += 1

    state["seen_ids"] = seen_ids
    state["last_run_at"] = datetime.now().timestamp()

    prune_seen_ids(state, lookback_seconds)
    save_state_atomic(state)
    logger.info(f"Processed {processed} messages. Watermark at {format_timestamp(last_ts)}. State saved.")


if __name__ == "__main__":
    main()
