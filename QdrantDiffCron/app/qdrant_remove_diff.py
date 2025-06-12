import os
import requests
import xml.etree.ElementTree as ET
from dotenv import load_dotenv

from qdrant_client import QdrantClient
from qdrant_client.http import models

# Load environment variables
load_dotenv(dotenv_path="/app/data/API_KEYS.env")
qdrant_api_key = os.getenv("QDRANT_API_KEY")
ping_key = os.getenv("HEALTHCHECKS_KEY")

load_dotenv(dotenv_path="/app/data/COOKIE.env")
COOKIE_NAME = os.getenv("COOKIE_NAME")
COOKIE_VALUE = os.getenv("COOKIE_VALUE")

QDRANT_URL = "https://qdrant.utvecklingfalkenberg.se/"
QDRANT_PORT = 443
COLLECTION_NAME = "IntranetFalkenbergHemsida"

qdrant_client = QdrantClient(
    url=QDRANT_URL, port=QDRANT_PORT, https=True, api_key=qdrant_api_key
)


# Hämta alla punkter från Qdrant
def get_web_qdrant_urls():
    url = f"{QDRANT_URL}collections/{COLLECTION_NAME}/points/scroll"
    headers = {"api-key": qdrant_api_key, "Content-Type": "application/json"}
    payload = {"with_payload": True, "limit": 50000}

    results = []
    scroll_id = None

    while True:
        if scroll_id:
            payload["offset"] = scroll_id

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            response.raise_for_status()
        except requests.exceptions.Timeout:
            print("Timeout inträffade. Försöker igen.")
            continue
        except requests.exceptions.RequestException as e:
            print(f"Ett förfrågningsfel inträffade: {e}")
            break

        if not response.text.strip():
            print("Tomt svar från servern. Avslutar.")
            break

        try:
            data = response.json()
        except ValueError as e:
            print("JSON-avkodningsfel:", e)
            break

        points = data.get("result", {}).get("points", [])
        if not points:
            print("Inga fler punkter att scrolla.")
            break

        for point in points:
            try:
                payload = point.get("payload", {})
                url = payload.get("url", None)
                source_url = payload.get("source_url", None)
                # Filter out empty URLs and PDF files
                if not url:
                    continue
                # Filter out Linked Documents
                if source_url:
                    continue

                results.append(url)
            except Exception as e:
                print(f"Fel vid bearbetning av punkt: {e}. Hoppar över.")
                continue

        scroll_id = data.get("result", {}).get("next_page_offset")
        if not scroll_id:
            print("Scroll-id saknas, ingen mer data att hämta.")
            break

    return set(results)


# Get URLs from the web sitemap
def get_web_sitemap_urls(sitemap_url):
    response = requests.get(sitemap_url, cookies={COOKIE_NAME: COOKIE_VALUE})
    if response.status_code == 200:
        sitemap = ET.fromstring(response.content)
        urls = set(
            url_elem.find("{http://www.sitemaps.org/schemas/sitemap/0.9}loc").text
            for url_elem in sitemap.findall(
                ".//{http://www.sitemaps.org/schemas/sitemap/0.9}url"
            )
        )
        return urls
    else:
        raise Exception(f"Failed to fetch sitemap from {sitemap_url}")


def remove_web_sitemap_url_diff(force=False):
    sitemap_url = "https://intranet.falkenberg.se/index.php?option=com_jmap&view=sitemap&format=xml"

    try:
        qdrant_urls = get_web_qdrant_urls()
        sitemap_urls = get_web_sitemap_urls(sitemap_url)
        print(f"Totalt {len(qdrant_urls)} URL:er i Qdrant.")
        print(f"Totalt {len(sitemap_urls)} URL:er i sitemap.")

        # Find urls that are in Qdrant but not in the sitemap
        missing_urls = qdrant_urls - sitemap_urls
        print(f"Totalt {len(missing_urls)} URL:er saknas i sitemap.")
        if missing_urls:
            if len(missing_urls) > 50 and not force:
                print(
                    "För många URL:er skiljer sig från sitemap, vänligen kontrollera."
                )
                requests.post(
                    f"https://healthchecks.utvecklingfalkenberg.se/ping/{ping_key}/intern-qdrant-diff-remove/fail",
                    data=f"Evolution URL Difference Amount= {len(missing_urls)}, Handle manually!",
                    timeout=10,
                )
                return
            print("URL:er som finns i Qdrant men inte i sitemap:")
            for url in missing_urls:
                print(url)
            print("Tas bort från Qdrant...")
            remove_qdrant_urls(missing_urls)
            requests.get(
                f"https://healthchecks.utvecklingfalkenberg.se/ping/{ping_key}/intern-qdrant-diff-remove",
                timeout=10,
            )
        else:
            print("Alla Webb URL:er i Qdrant finns också i sitemap.")

    except Exception as e:
        print(f"Ett fel inträffade: {e}")


def remove_qdrant_urls(urls):
    qdrant_filter = models.Filter(
        should=[
            models.FieldCondition(key="url", match=models.MatchAny(any=urls)),
            models.FieldCondition(key="source_url", match=models.MatchAny(any=urls)),
        ]
    )

    points_selector = models.FilterSelector(filter=qdrant_filter)

    qdrant_client.delete(
        collection_name=COLLECTION_NAME, points_selector=points_selector
    )


# Main function
def main():
    print("Tar bort gamla Webbsitemap URL:er...")
    remove_web_sitemap_url_diff(force=False)  # True to force removal


if __name__ == "__main__":
    main()
