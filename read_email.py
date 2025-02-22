import os.path
import os
import base64
import openai
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()

# --- Configurations ---
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
openai.api_key = os.environ["OPENAI_API_KEY"]  # Replace with your OpenAI API Key

# --- Authenticate and Build Gmail Service ---
def authenticate_gmail():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds)

# --- Fetch Unlabeled Emails ---
def get_unlabeled_emails(service):
    results = service.users().messages().list(
        userId='me', 
        q="-has:userlabels"  # Query for messages without any user labels
    ).execute()
    messages = results.get('messages', [])
    return messages

# --- Get Email Content ---
def get_email_content(service, msg_id):
    message = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
    payload = message['payload']
    
    # Check if 'parts' exist
    if 'parts' in payload:
        for part in payload['parts']:
            if part['mimeType'] == 'text/plain':
                data = part['body']['data']
                text = base64.urlsafe_b64decode(data.encode('UTF-8')).decode('utf-8')
                return text
    else:
        # If 'parts' doesn't exist, check if 'body' has 'data'
        if 'body' in payload and 'data' in payload['body']:
            data = payload['body']['data']
            text = base64.urlsafe_b64decode(data.encode('UTF-8')).decode('utf-8')
            return text
    
    return ""

# --- Analyze Email with OpenAI GPT ---
def analyze_email_with_llm(content):
    prompt = f"Read the following email and determine if it's about a job. If yes, say just Yes, else No.\n\nEmail Content:\n{content}\n\nAnswer:"
    response = openai.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=150,
        temperature=0.2
    )
    about_job = response.choices[0].message.content.strip().lower() == "yes"
    print(response)
    label = None
    if about_job:
        prompt = f"Apparently, the following email is about a job. Read it and label it among ['Applied', 'Assessment', 'Interview', 'Offer', 'Rejection', 'Other'].\n\nEmail Content:\n{content}\n\nLabel:"
        response = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.2
        )
        print(response)
        label = response.choices[0].message.content.strip()
    return [about_job, label]

# --- Apply Label to Email ---
def apply_label(service, msg_id, label_name):
    # Split nested label path
    label_parts = label_name.split('/')
    
    # Check if labels exist and create them if necessary
    labels = service.users().labels().list(userId='me').execute()
    current_path = ''
    final_label_id = None
    
    for i, part in enumerate(label_parts):
        current_path = '/'.join(label_parts[:i+1])
        label_id = None
        
        # Check if current level label exists
        for label in labels['labels']:
            if label['name'].lower() == current_path.lower():
                label_id = label['id']
                if i == len(label_parts) - 1:  # If this is the final part
                    final_label_id = label_id
                break
        
        # Create label if it doesn't exist
        if not label_id:
            label = {
                'name': current_path,
                'labelListVisibility': 'labelShow',
                'messageListVisibility': 'show'
            }
            created_label = service.users().labels().create(userId='me', body=label).execute()
            label_id = created_label['id']
            if i == len(label_parts) - 1:  # If this is the final part
                final_label_id = label_id
            # Refresh labels list after creating new label
            labels = service.users().labels().list(userId='me').execute()

    # Apply the final label to the message
    service.users().messages().modify(
        userId='me',
        id=msg_id,
        body={'addLabelIds': [final_label_id]}
    ).execute()

# --- Main Function ---
def main():
    service = authenticate_gmail()
    emails = get_unlabeled_emails(service)
    
    if not emails:
        print("No unlabeled emails found.")
        return

    for msg in emails:
        msg_id = msg['id']
        content = get_email_content(service, msg_id)
        
        if not content:
            continue

        about_job, label = analyze_email_with_llm(content)
        print(f"Email {msg_id} is about a job: {about_job}")
        if about_job:
            apply_label(service, msg_id, f"Jobs/{label}")  # Changed to use nested label format
        break

if __name__ == '__main__':
    main()