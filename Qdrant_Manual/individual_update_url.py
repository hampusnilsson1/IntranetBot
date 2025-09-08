import logging
import os
from dotenv import load_dotenv
import qdrant_client
from qdrant_client.http import models
from qdrant_client.http.models import VectorParams, Distance, PointStruct

from scrap import scrap_site
from process_item import process_item


# Setup Cookies
load_dotenv("../data/COOKIE.env")
COOKIE_NAME = os.getenv("COOKIE_NAME")
COOKIE_VALUE = os.getenv("COOKIE_VALUE")

# Setup Logging
log_file = "../data/update_logg.txt"

logging.basicConfig(
    filename=log_file,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# Also print to console
console = logging.StreamHandler()
console.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
console.setFormatter(formatter)
logging.getLogger("").addHandler(console)


# Load API Keys
load_dotenv(dotenv_path="../data/API_KEYS.env")
qdrant_api_key = os.getenv("QDRANT_API_KEY")

# Qdrant Constants And Setup
QDRANT_URL = "https://qdrant.utvecklingfalkenberg.se"
QDRANT_PORT = 443
VECTOR_SIZE = 3072
COLLECTION_NAME = "IntranetFalkenbergHemsida_RAG"

qdrant_client = qdrant_client.QdrantClient(
    url=QDRANT_URL, port=QDRANT_PORT, https=True, api_key=qdrant_api_key
)

try:
    qdrant_client.get_collection(COLLECTION_NAME)
except Exception:
    vectors_config = VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)
    qdrant_client.recreate_collection(
        collection_name=COLLECTION_NAME, vectors_config=vectors_config
    )


# Main
def update_url(url):
    page_chunks = scrap_site(url, COOKIE_NAME, COOKIE_VALUE)
    point_count = 0
    total_update_cost_SEK = 0
    for chunk in page_chunks:
        point_count += 1
        logging.info(f"{point_count} av {len(page_chunks)}")
        total_update_cost_SEK += process_item(
            chunk, qdrant_client, COLLECTION_NAME=COLLECTION_NAME
        )

    logging.info(f"Total URL Update Cost = {total_update_cost_SEK} SEK")
    return total_update_cost_SEK
