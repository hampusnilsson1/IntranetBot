from datetime import datetime
import logging
import os
import sys
from dotenv import load_dotenv
import requests
import xml.etree.ElementTree as ET

from qdrant_client import QdrantClient
from qdrant_client import models

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

from individual_update_url import update_url

# Load environment variables
load_dotenv("../data/COOKIE.env")
COOKIE_NAME = os.getenv("COOKIE_NAME")
COOKIE_VALUE = os.getenv("COOKIE_VALUE")

load_dotenv(dotenv_path="../data/API_KEYS.env")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")

# Setup Qdrant
QDRANT_URL = "https://qdrant.utvecklingfalkenberg.se"
QDRANT_PORT = 443
VECTOR_SIZE = 3072
COLLECTION_NAME = "IntranetFalkenbergHemsida_RAG"

qdrant_client = QdrantClient(
    url=QDRANT_URL, port=QDRANT_PORT, https=True, api_key=QDRANT_API_KEY, timeout=15
)
if not qdrant_client.collection_exists(collection_name=COLLECTION_NAME):
    logger.info(
        f"Collection {COLLECTION_NAME} not found. Creating... (Manual Update Script)"
    )
    vectors_config = models.VectorParams(
        size=VECTOR_SIZE, distance=models.Distance.COSINE
    )
    try:
        qdrant_client.create_collection(
            collection_name=COLLECTION_NAME, vectors_config=vectors_config
        )
    except Exception as e:
        logger.error(f"Error creating collection: {e} (Manual Update Script)")
else:
    logger.info(
        f"Collection {COLLECTION_NAME} exists. Proceeding.(Manual Update Script)"
    )


def validate_cookie_startup(url, cookie_name, cookie_value):
    logger.info("Validating Cookie before starting...")
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


# Sitemap Processing
url = "https://intranet.falkenberg.se/index.php?option=com_jmap&view=sitemap&format=xml"

if not validate_cookie_startup(url, COOKIE_NAME, COOKIE_VALUE):
    logger.error("Exiting due to invalid cookie.")
    sys.exit(1)

response = requests.get(url, cookies={COOKIE_NAME: COOKIE_VALUE})
urls = []
if response.status_code == 200:

    root = ET.fromstring(response.content)
    namespace = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    urls_nolastmod = []
    urls_new = []
    urls_update = []

    urls_added = []
    for url in root.findall("ns:url", namespace):
        loc = url.find("ns:loc", namespace).text
        lastmod_elem = url.find("ns:lastmod", namespace)
        if loc and (
            "/search" in loc
            or "/min-sida" in loc
            or "/mitt-konto" in loc
            or "/reset" in loc
            or "/logout" in loc
            or "/uppdatera-uppgifter" in loc
            or "https://intranet.falkenberg.se/reg" in loc
            or "/min-profil" in loc
            or "/loggaut" in loc
            or "/uppdatera-min-profil" in loc
            or "/mina-kontakter" in loc
            or "/samarbete" in loc
            or "/sok-efter-anvandare-och-grupper" in loc
            or "/visa-alla-anvandare" in loc
        ):
            continue

        if lastmod_elem is not None:
            lastmod = lastmod_elem.text
            try:
                lastmod_datetime = datetime.strptime(lastmod, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError as e:
                logger.error(f"Wrong dateformat of lastmod {lastmod}: {e}")
        else:
            urls_nolastmod.append(loc)
            continue
        # Check if lastmod is updated after last update
        filter_condition = models.Filter(
            must=[
                models.FieldCondition(
                    key="metadata.url", match=models.MatchValue(value=loc)
                )
            ]
        )
        result = qdrant_client.scroll(
            collection_name=COLLECTION_NAME,
            limit=1,
            scroll_filter=filter_condition,
        )
        # If data point exists
        if result[0]:
            datapoint = result[0][0]
            metadata = datapoint.payload.get("metadata", {})
            update_date_str = metadata.get("update_date")
            if update_date_str is not None:
                try:
                    update_date = datetime.strptime(
                        update_date_str, "%Y-%m-%dT%H:%M:%S"
                    )
                except ValueError as e:
                    logger.error(f"Error parsing date {update_date_str}: {e}")

            # Check if the date is after the last update
            if update_date is not None and lastmod_datetime > update_date:
                urls_update.append(loc)

        else:
            logger.info(f"Datapoint not found for {loc}")
            urls_new.append(loc)

    add_update = input(
        f"{len(urls_update)} URLs that are in need of update. Do you want to add these? (y/n) "
    )
    if add_update.lower() == "y":
        urls_added.extend(urls_update)

    add_new = input(
        f"{len(urls_new)} URLs that do not exist in Qdrant. Do you want to add these? (y/n) "
    )
    if add_new.lower() == "y":
        urls_added.extend(urls_new)

    add_nolastmod = input(
        f"{len(urls_nolastmod)} URLs without a date in sitemap, would use like to add these? (y/n) "
    )
    if add_nolastmod.lower() == "y":
        urls_added.extend(urls_nolastmod)

    logger.info(f"{len(urls_added)} URLS to update or add to Qdrant")
    if len(urls_added) == 0:
        logger.info("No URLs to update or add. Exiting.")
        exit()
    start_update = input("Start Update? (y/n) ")
    if start_update.lower() == "y":
        point_count = 0
        for url in urls_added:
            point_count += 1
            logger.info(f"Updating {url}")
            logger.info(f"{point_count} / {len(urls_added)}")
            update_url(url)
