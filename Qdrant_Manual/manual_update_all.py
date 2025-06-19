from datetime import datetime
import os
from dotenv import load_dotenv
import requests
import xml.etree.ElementTree as ET

from qdrant_client import QdrantClient
from qdrant_client.http import models
from qdrant_client.http.models import (
    Filter,
    FieldCondition,
    MatchValue,
    VectorParams,
    Distance,
)

from individual_update_url import update_url
import time

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
COLLECTION_NAME = "IntranetFalkenbergHemsida"

qdrant_client = QdrantClient(
    url=QDRANT_URL, port=QDRANT_PORT, https=True, api_key=QDRANT_API_KEY, timeout=15
)
try:
    qdrant_client.get_collection(COLLECTION_NAME)
except Exception:
    vectors_config = VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)
    qdrant_client.recreate_collection(
        collection_name=COLLECTION_NAME, vectors_config=vectors_config
    )


# Sitemap Processing
url = "https://intranet.falkenberg.se/index.php?option=com_jmap&view=sitemap&format=xml"

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
                print(f"Wrong dateformat of lastmod {lastmod}: {e}")
        else:
            urls_nolastmod.append(loc)
            continue
        # Check if lastmod is updated after last update
        filter_condition = Filter(
            must=[FieldCondition(key="url", match=MatchValue(value=loc))]
        )
        result = qdrant_client.scroll(
            collection_name=COLLECTION_NAME,
            limit=1,
            scroll_filter=filter_condition,
        )
        # If data point exists
        if result[0]:
            datapoint = result[0][0]
            update_date_str = datapoint.payload.get("update_date")
            if update_date_str is not None:
                try:
                    update_date = datetime.strptime(
                        update_date_str, "%Y-%m-%dT%H:%M:%S"
                    )
                except ValueError as e:
                    print(f"Error parsing date {update_date_str}: {e}")

            # Check if the date is after the last update
            if update_date is not None and lastmod_datetime > update_date:
                urls_update.append(loc)

        else:
            print(f"Datapoint not found for {loc}")
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

    print(f"{len(urls_added)} URLS to update or add to Qdrant")
    start_update = input("Start Update? (y/n) ")
    if start_update.lower() == "y":
        point_count = 0
        for url in urls_added:
            point_count += 1
            print(f"Updating {url}")
            print(f"{point_count} / {len(urls_added)}")
            update_url(url)
