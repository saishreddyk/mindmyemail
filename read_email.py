import base64
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


def last_executed_timestamp():
    try:
        with open("last_executed_date.txt", "r") as f:
            return float(f.read().strip())
    except (FileNotFoundError, ValueError):
        # Return current timestamp if file doesn't exist or has invalid format
        logger.warning(
            "File 'last_executed_date.txt' not found or invalid. Using current timestamp."
        )
        return datetime.now().timestamp()


def format_timestamp(timestamp):
    """Convert timestamp to human readable format"""
    return datetime.fromtimestamp(timestamp).strftime("%Y/%m/%d %H:%M:%S")


# --- Main Function ---
def main():
    service = authenticate_gmail()
    last_timestamp = last_executed_timestamp()
    logger.info(
        f"Starting email fetch. Last execution timestamp: {format_timestamp(last_timestamp)}"
    )

    emails = get_emails(service, last_timestamp)
    logger.info(f"Found {len(emails)} new emails after timestamp filtering")

    if not emails:
        logger.info("No new unlabeled emails found.")
        # Save current timestamp even if no emails found to prevent re-checking old emails
        current_timestamp = datetime.now().timestamp()
        with open("last_executed_date.txt", "w") as f:
            f.write(str(current_timestamp))
        logger.info(
            f"Updated last executed timestamp to: {format_timestamp(current_timestamp)}"
        )
        return

    for msg in emails:
        msg_id = msg["id"]
        subject, content = get_email_content(service, msg_id)

        if not content:
            continue

        about_job, label = analyze_email_with_llm(content)
        logger.info(
            f"Email subject '{subject}' is about a job: {about_job}"
            + (f", Label: {label}" if about_job else "")
        )
        if about_job:
            apply_label(service, msg_id, f"Jobs/{label}")

    current_timestamp = datetime.now().timestamp()
    with open("last_executed_date.txt", "w") as f:
        f.write(str(current_timestamp))
    logger.info(
        "Updating last executed date to: " + format_timestamp(current_timestamp)
    )
    logger.info("Program executed successfully.")


if __name__ == "__main__":
    main()
