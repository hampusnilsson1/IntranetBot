from datetime import datetime
import logging
import os
import sys
from dotenv import load_dotenv
import requests
import xml.etree.ElementTree as ET

from qdrant_client import QdrantClient
from qdrant_client import models
from individual_update_url import update_url
from essential_methods import swedish_time

# Setup Logging
log_file = "../data/manual_update_logg.txt"

root_logger = logging.getLogger()
if root_logger.hasHandlers():
    root_logger.handlers.clear()

logging.Formatter.converter = swedish_time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_file),  # Log to file
        logging.StreamHandler(),  # Log to console
    ],
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv("../data/COOKIE.env")
COOKIE_NAME = os.getenv("COOKIE_NAME")
COOKIE_VALUE = os.getenv("COOKIE_VALUE")

load_dotenv(dotenv_path="../data/API_KEYS.env")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")

# Qdrant Configuration
QDRANT_URL = "https://qdrant.utvecklingfalkenberg.se"
QDRANT_PORT = 443
COLLECTION_NAME = "IntranetFalkenbergHemsida_RAG"

qdrant_client = QdrantClient(
    url=QDRANT_URL, port=QDRANT_PORT, https=True, api_key=QDRANT_API_KEY, timeout=30
)

def validate_cookie(url, cookie_name, cookie_value):
    try:
        # Changed to GET to receive the body content
        response = requests.get(
            url, cookies={cookie_name: cookie_value}, allow_redirects=True, timeout=10
        )

        # Check 1: Standard URL Redirect (3xx)
        if "idp.falkenberg.se" in response.url:
            logger.error("CRITICAL: Cookie is invalid! HTTP Redirect to Login Page.")
            return False

        content = response.text
        if "idp.falkenberg.se" in content and "SAMLRequest" in content:
            logger.error("CRITICAL: Cookie is invalid! Detected SAML Redirect HTML.")
            return False

        if "<?xml" not in content[:100] and "<urlset" not in content[:100]:
            logger.error("CRITICAL: Response is not XML. Likely a login page or error.")
            logger.debug(f"Response snippet: {content[:150]}...")
            return False

        if response.status_code == 200:
            logger.info("Cookie is VALID.")
            return True
        else:
            logger.warning(f"Cookie check returned status {response.status_code}")
            return False

    except Exception as e:
        logger.error(f"Failed to validate cookie: {e}")
        return False

def get_all_existing_urls():
    """Fetches all URLs currently stored in Qdrant metadata."""
    logger.info("Fetching existing URLs from Qdrant... (This might take a moment)")
    existing_urls = set()
    
    # We only need the metadata field, not the vectors
    offset = None
    while True:
        results, next_offset = qdrant_client.scroll(
            collection_name=COLLECTION_NAME,
            limit=500,
            with_payload=["metadata"], # Only fetch the metadata to save bandwidth
            with_vectors=False,
            offset=offset
        )
        
        for point in results:
            url = point.payload.get("metadata", {}).get("url")
            if url:
                existing_urls.add(url)
        
        offset = next_offset
        if offset is None:
            break
            
    logger.info(f"Found {len(existing_urls)} existing URLs in Qdrant.")
    return existing_urls

# --- START SCRIPT ---

sitemap_url = "https://intranet.falkenberg.se/index.php?option=com_jmap&view=sitemap&format=xml"

if not validate_cookie(sitemap_url, COOKIE_NAME, COOKIE_VALUE):
    logger.error("Cookie is invalid. Please update COOKIE.env")
    sys.exit(1)

# 1. Get existing data from Qdrant
existing_urls = get_all_existing_urls()

# 2. Get data from Sitemap
logger.info("Fetching Sitemap...")
response = requests.get(sitemap_url, cookies={COOKIE_NAME: COOKIE_VALUE})
if response.status_code != 200:
    logger.error(f"Failed to fetch sitemap. Status: {response.status_code}")
    sys.exit(1)

root = ET.fromstring(response.content)
namespace = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}

urls_to_exclude = [
    "/search", "/min-sida", "/mitt-konto", "/reset", "/logout", 
    "/uppdatera-uppgifter", "https://intranet.falkenberg.se/reg", 
    "/min-profil", "/loggaut", "/uppdatera-min-profil", 
    "/mina-kontakter", "/samarbete", "/sok-efter-anvandare-och-grupper", 
    "/visa-alla-anvandare"
]

missing_urls = []

# 3. Filter and compare
for url_tag in root.findall("ns:url", namespace):
    loc = url_tag.find("ns:loc", namespace).text
    
    # Filter out excluded patterns
    if any(pattern in loc for pattern in urls_to_exclude):
        continue
    
    # Add to list if NOT in Qdrant
    if loc not in existing_urls:
        missing_urls.append(loc)

# 4. User confirmation
if not missing_urls:
    logger.info("Everything is up to date! No missing URLs found.")
    sys.exit(0)

logger.info(f"Found {len(missing_urls)} URLs that are in the sitemap but NOT in Qdrant.")

confirm = input(f"Do you want to add these {len(missing_urls)} missing URLs? (y/n): ")
if confirm.lower() == 'y':
    count = 0
    total = len(missing_urls)
    for url in missing_urls:
        count += 1
        logger.info(f"[{count}/{total}] Processing: {url}")
        try:
            update_url(url)
        except Exception as e:
            logger.error(f"Failed to update {url}: {e}")
    logger.info("Process finished.")
else:
    logger.info("Process aborted by user.")