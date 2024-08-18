from __future__ import print_function
import base64
import html2text
import logging
import os
import re
import requests
import time
import tldextract
import traceback
from bs4 import BeautifulSoup, NavigableString
from datetime import datetime, timedelta
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from urllib.parse import urlparse

# Load environment variables
load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# If modifying these scopes, delete the file token.json.
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

# Set constants
DEFAULT_DAYS_TO_LOOK_BACK  = 7
BASE_OUTPUT_DIR  = os.getenv('BASE_OUTPUT_DIR')
logging.debug(f"BASE_OUTPUT_DIR: {BASE_OUTPUT_DIR}")
# List of link texts to exclude (case-insensitive)
EXCLUDED_LINK_TEXTS = [text.strip() for text in os.getenv('EXCLUDED_LINK_TEXTS', '').split(',')]
logging.debug(f"EXCLUDED_LINK_TEXTS: {EXCLUDED_LINK_TEXTS}")
# List of domains to avoid
BLOCKED_DOMAINS = [text.strip() for text in os.getenv('BLOCKED_DOMAINS', '').split(',')]
logging.debug(f"BLOCKED_DOMAINS: {BLOCKED_DOMAINS}")
SLEEP_TIME = 5 # Seconds to wait before retrying after rate limit

def get_credentials():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return creds

def get_most_recent_date_folder(base_dir):
    date_folders = []
    for item in os.listdir(base_dir):
        item_path = os.path.join(base_dir, item)
        if os.path.isdir(item_path):
            try:
                folder_date = datetime.strptime(item, "%Y-%m-%d")
                date_folders.append(folder_date)
            except ValueError:
                # If the folder name is not a valid date, skip it
                continue
    
    return max(date_folders) if date_folders else None

def calculate_days_to_look_back(base_dir):
    most_recent_date = get_most_recent_date_folder(base_dir)
    if most_recent_date:
        days_to_look_back = (datetime.now() - most_recent_date).days
        return max(1, days_to_look_back)  # Ensure we look back at least 1 day
    return DEFAULT_DAYS_TO_LOOK_BACK

def get_emails(service, sender, days_to_look_back):
    query = f"from:{sender} after:{(datetime.now() - timedelta(days=days_to_look_back)).strftime('%Y/%m/%d')}"
    try:
        results = service.users().messages().list(userId='me', q=query).execute()
        messages = results.get('messages', [])
        return messages
    except HttpError as error:
        if error.resp.status == 429:
            logging.warning("Rate limit reached. Waiting before retrying...")
            time.sleep(SLEEP_TIME)  
            return get_emails(service, sender, days_to_look_back)  # Retry the request
        else:
            logging.error(f"An error occurred: {error}")
            return []

def html_to_markdown(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Function to convert a tag and its contents to markdown
    def process_tag(tag):
        if isinstance(tag, NavigableString):
            return tag.string
        
        if tag.name == 'a':
            href = tag.get('href')
            # Check if the link contains an image
            img = tag.find('img')
            if img:
                src = img.get('src', '')
                alt = img.get('alt', '')
                # Create a linked image in Markdown
                return f"[![{alt}]({src})]({href})"
            else:
                content = ''.join(process_tag(child) for child in tag.contents)
                return f"[{content}]({href})" if href else content
        elif tag.name == 'img':
            src = tag.get('src', '')
            alt = tag.get('alt', '')
            return f"![{alt}]({src})"
        elif tag.name in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
            level = int(tag.name[1])
            content = ''.join(process_tag(child) for child in tag.contents)
            return f"\n\n{'#' * level} {content.strip()}\n\n"
        elif tag.name == 'p':
            content = ''.join(process_tag(child) for child in tag.contents)
            return f"\n\n{content}\n\n"
        elif tag.name in ['ul', 'ol']:
            items = []
            for i, li in enumerate(tag.find_all('li', recursive=False)):
                marker = '*' if tag.name == 'ul' else f"{i+1}."
                content = ''.join(process_tag(child) for child in li.contents)
                items.append(f"{marker} {content.strip()}")
            return '\n' + '\n'.join(items) + '\n'
        elif tag.name == 'br':
            return '\n'
        else:
            return ''.join(process_tag(child) for child in tag.contents)

    markdown_content = process_tag(soup.body or soup)
    
    # Clean up extra whitespace
    markdown_content = re.sub(r'\n\s*\n', '\n\n', markdown_content)
    markdown_content = markdown_content.strip()

    return markdown_content

def email_to_markdown(service, msg_id):
    try:
        message = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
    except HttpError as error:
        if error.resp.status == 429:
            logging.warning("Rate limit reached. Waiting before retrying...")
            time.sleep(SLEEP_TIME)  
            return email_to_markdown(service, msg_id)  # Retry the request
        else:
            logging.error(f"An error occurred: {error}")
            return None, None
    
    headers = message['payload']['headers']
    subject = next(header['value'] for header in headers if header['name'].lower() == 'subject')
    from_header = next(header['value'] for header in headers if header['name'].lower() == 'from')
    date = next(header['value'] for header in headers if header['name'].lower() == 'date')
    
    parts = [message['payload']]
    body = ""
    while parts:
        part = parts.pop(0)
        if part.get('parts'):
            parts.extend(part['parts'])
        if part.get('mimeType') == 'text/html' and part['body'].get('data'):
            body = base64.urlsafe_b64decode(part['body']['data']).decode()
            break
    
    if not body:
        # Fallback to plain text if no HTML content
        for part in parts:
            if part.get('mimeType') == 'text/plain' and part['body'].get('data'):
                body = base64.urlsafe_b64decode(part['body']['data']).decode()
                break

    markdown_body = html_to_markdown(body)

    markdown_content = f"Subject: {subject}\nFrom: {from_header}\nDate: {date}\n\n{markdown_body}"
    return markdown_content, subject

def save_markdown(content, filename):
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(content)
        
def read_sender_emails(filename):
    with open(filename, 'r') as file:
        content = file.read()
    
    # Regular expression to match mailto links
    mailto_pattern = r'\[([^\]]+)\]\(mailto:([^)]+)\)'
    
    # Find all mailto links in the content
    matches = re.findall(mailto_pattern, content)
    
    # Return a list of tuples (name, email)
    return matches

def is_valid_url(url):
    """Check if the URL is valid and has a scheme."""
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except ValueError:
        logging.debug(f"Invalid URL: {url}")
        return False

def is_web_page_link(url):
    """Check if the URL is likely to be a web page."""
    if not is_valid_url(url):
        return False
    # List of common web page extensions
    web_extensions = ['.html', '.htm', '.php', '.asp', '.aspx', '', '.jsp', '.pdf']
    # List of common non-web page extensions
    non_web_extensions = ['.doc', '.docx', '.xls', '.xlsx', '.mp3', '.mp4', '.avi', '.mov']
    
    parsed_url = urlparse(url)
    path = parsed_url.path.lower()
    
    # Check if the URL has no extension or a web extension
    if any(path.endswith(ext) for ext in web_extensions):
        return True
    # Check if the URL doesn't have a non-web extension
    if not any(path.endswith(ext) for ext in non_web_extensions):
        return True
    logging.debug(f"Not a web page link: {url}")
    return False

def should_exclude_link_text(text):
    """Check if the link text should be excluded."""
    # Convert text to lowercase for case-insensitive matching
    text_lower = text.lower().strip()
    
    for excluded in EXCLUDED_LINK_TEXTS:
        excluded_lower = excluded.lower().strip()
        
        # Check for exact match
        if excluded_lower == text_lower:
            return True
        
        # Check if the excluded text is a substring of the link text
        if excluded_lower in text_lower:
            # Ensure it's a whole word or phrase match
            if re.search(r'\b' + re.escape(excluded_lower) + r'\b', text_lower):
                return True
    
    return False

def is_blocked_domain(url):
    """Check if the domain or any parent domain is in the blocked list."""
    extracted = tldextract.extract(url)
    domain = f"{extracted.domain}.{extracted.suffix}"
    logging.debug(f"Checking domain: {domain}")
    subdomains = extracted.subdomain.split('.')
    
    # Check the main domain
    if domain in BLOCKED_DOMAINS:
        return True
    
    # Check each subdomain level
    for i in range(len(subdomains)):
        full_domain = '.'.join(subdomains[i:] + [domain])
        if full_domain in BLOCKED_DOMAINS:
            return True
    
    return False

def is_redirect_to_blocked_domain(url):
    try:
        response = requests.head(url, allow_redirects=False, timeout=5)
        if response.is_redirect:
            location = response.headers.get('Location')
            if location:
                return is_blocked_domain(location)
    except requests.RequestException as e:
        logging.warning(f"Error checking redirect for {url}: {e}")
    return False

def transform_arxiv_url(url):
    """Transform an arxiv.org URL to directly link to the PDF."""
    parsed_url = urlparse(url)
    if 'arxiv.org' in parsed_url.netloc:
        # Extract the arxiv ID
        match = re.search(r'(arxiv.org/abs/|arxiv.org/pdf/)(\d+\.\d+)', url)
        if match:
            arxiv_id = match.group(2)
            transformed_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
            logging.info(f"Transformed arXiv URL: {url} -> {transformed_url}")
            return transformed_url, arxiv_id
        else:
            logging.warning(f"Could not extract arXiv ID from URL: {url}")
    return url, None

def extract_links(markdown_content):
    # Regular expression to find markdown links, excluding image links
    link_pattern = r'(?<!!)\[([^\]]+)\]\(([^\)]+)\)'
    links = re.findall(link_pattern, markdown_content)
    
    # Filter and deduplicate links
    unique_web_links = []
    for text, url in links:
        # Skip image links
        if text.strip().startswith('!'):
            logging.debug(f"Skipping image link: {text} - {url}")
            continue
        
        if is_valid_url(url) and not should_exclude_link_text(text):
            if not is_blocked_domain(url):
                if not is_redirect_to_blocked_domain(url):
                    if url not in [link[1] for link in unique_web_links]:
                        unique_web_links.append((text, url))
                    else:
                        logging.debug(f"Duplicate link skipped: {url}")
                else:
                    logging.debug(f"Skipping link that redirects to blocked domain: {url}")
            else:
                logging.debug(f"Skipping blocked domain: {url}")
        else:
            logging.debug(f"Excluded link: {text} - {url}")
    
    logging.debug(f"Extracted {len(unique_web_links)} unique web links")
    return unique_web_links

def is_text_based_content(content_type):
    """Check if the content type is text-based."""
    text_based_types = [
        'text/html', 'text/plain', 'text/markdown'
        #'text/xml', 'application/json', 'application/xml', 'application/xhtml+xml'
    ]
    is_text = any(content_type.startswith(text_type) for text_type in text_based_types)
    logging.debug(f"Content type {content_type} is {'text-based' if is_text else 'not text-based'}")
    return is_text

def download_pdf(url, output_path):
    """Download a PDF file."""
    try:
        logging.info(f"Attempting to download PDF from: {url}")
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = requests.get(url, headers=headers, stream=True)
        response.raise_for_status()
        with open(output_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        file_size = os.path.getsize(output_path)
        logging.info(f"Downloaded PDF: {output_path} (Size: {file_size} bytes)")
        return True
    except Exception as e:
        logging.error(f"Error downloading PDF {url}: {e}")
        return False

def fetch_and_convert_to_markdown(url):
    try:
        logging.debug(f"Fetching URL: {url}")
             
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36', 'Referer': 'https://substack.com'}
        response = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        
        if response.status_code == 429:
            logging.warning(f"Received 429 Too Many Requests for {url}. Waiting before retrying...")
            time.sleep(SLEEP_TIME)  
            return fetch_and_convert_to_markdown(url)  # Retry the request
        
        response.raise_for_status()
        
        # Check final URL after redirects
        final_url = response.url
        logging.debug(f"Final URL after redirection: {final_url}")
        
        if is_blocked_domain(final_url):
            logging.warning(f"URL {url} redirected to blocked domain: {final_url}")
            return None, None, None
        
        # Transform arXiv URL if necessary
        transformed_url, arxiv_id = transform_arxiv_url(final_url)
        if transformed_url != final_url:
            logging.info(f"Transformed arXiv URL: {final_url} -> {transformed_url}")
            final_url = transformed_url
        
        content_type = response.headers.get('content-type', '').split(';')[0].lower()
        logging.debug(f"Content type: {content_type}")
        
        if content_type == 'application/pdf' or final_url.endswith('.pdf') or arxiv_id:
            logging.info(f"Detected PDF: {final_url}")
            return None, 'pdf', final_url
        
        if not content_type.startswith('text/'):
            logging.debug(f"Skipping non-text content: {final_url} (Content-Type: {content_type})")
            return None, None, None
        
        html_content = response.text
        
        # Convert HTML to markdown
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = False
        h.ignore_tables = False
        h.body_width = 0
        h.inline_links = True
        h.protect_links = True
        h.use_automatic_links = False
        markdown_content = h.handle(html_content)
        
        logging.debug(f"Successfully converted {final_url} to markdown")
        return markdown_content, 'markdown', final_url
    except requests.HTTPError as e:
        if e.response.status_code == 429:
            logging.debug(f"Received 429 Too Many Requests for {url}. Skipping.")
        else:
            logging.error(f"HTTP error fetching {url}: {e}")
        return None, None, None
    except requests.RequestException as e:
        logging.error(f"Error fetching {url}: {e}")
        return None, None, None
    except Exception as e:
        logging.error(f"Unexpected error processing {url}: {e}")
        logging.debug(traceback.format_exc())
        return None, None, None

def generate_unique_filename(base_path, base_name, extension):
    """Generate a unique filename by appending a number if the file already exists."""
    counter = 1
    file_path = os.path.join(base_path, f"{base_name}{extension}")
    while os.path.exists(file_path):
        file_path = os.path.join(base_path, f"{base_name}_{counter}{extension}")
        counter += 1
    logging.debug(f"Generated unique filename: {file_path}")
    return file_path

def process_markdown_links(content, output_dir, processed_links):
            
    links = extract_links(content)
    logging.info(f"Found {len(links)} links in the content")
    
    for link_text, url  in links:
        if link_text.strip().startswith('!'):
            logging.debug(f"Skipping image link: {link_text}")
            continue
        
        if url not in processed_links:
            logging.debug(f"Processing link: {link_text} - {url}")
            result = fetch_and_convert_to_markdown(url)
            if result is None or result == (None, None, None):
                logging.debug(f"Failed to process: {url}")
                continue
            
            content, content_type, final_url  = result 
            
            if content_type == 'pdf':
                safe_filename = re.sub(r'[^\w\-_\. ]', '_', link_text or final_url)
                safe_filename = safe_filename[:200]  # Limit filename length
                if 'arxiv.org' in final_url:
                    arxiv_id = final_url.split('/')[-1].replace('.pdf', '')
                    safe_filename = f"arxiv_{arxiv_id}_{safe_filename}"
                output_filename = generate_unique_filename(output_dir, safe_filename, '.pdf')
                if download_pdf(final_url, output_filename):
                    logging.info(f"Saved PDF: {output_filename}")
                    processed_links.add(url) # Add the original URL to the processed set
            elif content_type == 'markdown':
                # Create a filename based on the link text or URL
                safe_filename = re.sub(r'[^\w\-_\. ]', '_', link_text or url)
                safe_filename = safe_filename[:200]  # Limit filename length
                # Generate a unique filename
                output_filename = generate_unique_filename(output_dir, safe_filename, '.md')
                
                try:
                    with open(output_filename, 'w', encoding='utf-8') as f:
                        f.write(f"# {link_text}\n\nOriginal URL: {url}\n\n{content}")
                
                    logging.info(f"Saved: {output_filename}")
                    processed_links.add(url)
                except IOError as e:
                    logging.error(f"Error writing file {output_filename}: {e}")
        else:
            logging.debug(f"Skipping already processed link: {url}")

def main():
    creds = get_credentials()
    service = build('gmail', 'v1', credentials=creds)

    sender_emails = read_sender_emails('sender_emails.md')
    days_to_look_back = calculate_days_to_look_back(BASE_OUTPUT_DIR)
    print(f"Looking back {days_to_look_back} days")
    
    # Create a folder with today's date
    today = datetime.now().strftime('%Y-%m-%d')
    output_dir = os.path.join(BASE_OUTPUT_DIR, today)
    output_links_dir = os.path.join(output_dir, "links")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        os.makedirs(output_links_dir)

    # To keep track of processed links
    processed_links = set()  
    
    for sender_name, sender_email in sender_emails:
        print(f"Processing emails from: {sender_name} ({sender_email})")
        messages = get_emails(service, sender_email, days_to_look_back)
        
        # Create a folder for this sender
        sender_dir = os.path.join(output_dir, sender_name)
        if not os.path.exists(sender_dir):
            os.makedirs(sender_dir)
        
        for message in messages:
            content, subject = email_to_markdown(service, message['id'])
            safe_filename = "".join([c for c in subject if c.isalpha() or c.isdigit() or c==' ']).rstrip()
            filename = os.path.join(sender_dir, f"{safe_filename}.md")
            save_markdown(content, filename)
            print(f"Saved: {filename}")
            process_markdown_links(content, output_links_dir, processed_links)

if __name__ == '__main__':
    main()