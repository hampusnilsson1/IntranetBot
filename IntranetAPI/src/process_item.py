from datetime import datetime, timezone
import logging
import os
import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import openai

from qdrant_client import QdrantClient
from qdrant_client.http import models
from qdrant_client.http.models import PointStruct

from essential_methods import calculate_cost, generate_uuid

# Batch Constants
BATCH_SIZE = 1000
SLEEP_TIME = 2
EMBEDDING_MODEL = "text-embedding-3-large"

# Setup Openai
load_dotenv(dotenv_path="../data/API_KEYS.env")
openai_api_key = os.getenv("OPENAI_API_KEY")
openai.api_key = openai_api_key


# Main, Process Item and upload to Qqdrant
def process_item(
    item, qdrant_client: QdrantClient, COLLECTION_NAME="IntranetFalkenbergHemsida"
):
    logging.info("Dividing to chunks")
    chunks = get_item_chunks(item)
    logging.info(f"Getting chunks in need of update, url: {item['url']}")
    db_hashes = get_db_chunk_hashes(chunks, qdrant_client, COLLECTION_NAME)
    new_chunks = get_new_chunks(chunks, db_hashes)
    old_urls = get_old_urls(chunks, db_hashes)
    if new_chunks == None or len(new_chunks) == 0:
        logging.info("No Update needed for this item.")
        return 0
    logging.info("Embedding chunks")
    embeddings, chunk_cost_SEK = create_embeddings(new_chunks)
    logging.info("Removing old chunks")
    remove_old_datapoints(new_chunks, qdrant_client, COLLECTION_NAME, old_urls)
    logging.info("Uploading Embeddings")
    upsert_to_qdrant(new_chunks, embeddings, qdrant_client, COLLECTION_NAME)
    logging.info("Processing Done")
    return chunk_cost_SEK


# 1 Create into Chunks
def get_item_chunks(item):
    all_chunks = []
    text_chunks = chunk_text(item["texts"], 4000, 300)
    num_chunks = len(text_chunks)
    for index, chunk in enumerate(text_chunks):
        chunk_data = {
            "url": item["url"],
            "title": item["title"],
            "chunk": chunk,
            "chunk_hash": generate_uuid(chunk),
            "chunk_info": f"Chunk {index + 1} of {num_chunks}",
        }
        if "source_url" in item:
            chunk_data["source_url"] = item["source_url"]

        all_chunks.append(chunk_data)
    return all_chunks


def chunk_text(text, chunk_size, overlap):
    length = len(text)
    chunks = []
    start = 0
    while start < length:
        end = start + chunk_size
        if end > length:
            end = length
        chunks.append(text[start:end])
        start = end - overlap
        if end == length:
            break
    return chunks


# 2 Get DB Chunk Hashes
def get_db_chunk_hashes(chunks, qdrant_client: QdrantClient, COLLECTION_NAME):
    db_hashes = []
    chunk_source_url = chunks[0]["source_url"] if "source_url" in chunks[0] else None
    url = chunks[0]["url"]

    logging.info(f"{url},{chunks[0]['chunk_hash']}")

    # if Site
    url_filter = models.Filter(
        must=[
            models.IsEmptyCondition(
                is_empty=models.PayloadField(key="metadata.source_url")
            ),
            models.FieldCondition(
                key="metadata.url", match=models.MatchValue(value=url)
            ),
        ]
    )

    # if Linked Document
    link_filter = None
    if chunk_source_url:
        link_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="metadata.source_url",
                    match=models.MatchValue(value=chunk_source_url),
                ),
                models.FieldCondition(
                    key="metadata.url", match=models.MatchValue(value=url)
                ),
            ]
        )

    # Hash filter
    chunk_hashes = [chunk["chunk_hash"] for chunk in chunks]
    hash_filter = models.Filter(
        must=[
            models.HasIdCondition(has_id=chunk_hashes),
        ],
    )

    if link_filter:
        qdrant_filter = models.Filter(should=[url_filter, link_filter, hash_filter])
    else:
        qdrant_filter = models.Filter(should=[url_filter, hash_filter])

    db_points, _ = qdrant_client.scroll(
        collection_name=COLLECTION_NAME, scroll_filter=qdrant_filter, limit=3000
    )

    for point in db_points:
        point_id = point.id
        point_url = point.payload.get("metadata")["url"]
        db_hash = {"id": point_id, "url": point_url}
        if "source_url" in point.payload.get("metadata"):
            db_hash["source_url"] = point.payload.get("metadata")["source_url"]
        db_hashes.append(db_hash)

    logging.info(
        f"Database Hashes found for url: {db_hashes}, {len(db_hashes)} stycken"
    )

    return db_hashes


# 3 Compare new and old chunk hashes( IF new_chunks contain a new chunk hash/document it will be added to the update)
def get_new_chunks(new_chunks, db_hashes):
    if not new_chunks:
        logging.info("Empty input data - no chunks to update")
        return

    db_hashes_set = {db_point["id"] for db_point in db_hashes}

    urls_needing_update = {
        chunk["url"] for chunk in new_chunks if chunk["chunk_hash"] not in db_hashes_set
    }

    chunks_to_update = [
        chunk for chunk in new_chunks if chunk["url"] in urls_needing_update
    ]

    logging.info(
        f"Found {len(urls_needing_update)} URLs needing update with {len(chunks_to_update)} total chunks"
    )
    logging.info(f"URLs to update: {urls_needing_update}")

    return chunks_to_update


# 3.5 Get pdfs/documents that no longer is linked on page
def get_old_urls(new_chunks, db_hashes):
    db_urls_set = {db_point["url"] for db_point in db_hashes}
    new_urls_set = {chunk["url"] for chunk in new_chunks}

    removed_urls = db_urls_set - new_urls_set

    return removed_urls if removed_urls else None


# 4 Create Embeddings
def create_embeddings(chunks):
    texts = [chunk["chunk"] for chunk in chunks]
    embeddings = []
    total_cost_sek = 0
    for batch_start in range(0, len(texts), BATCH_SIZE):
        batch_texts = texts[batch_start : batch_start + BATCH_SIZE]
        response = openai.Embedding.create(model=EMBEDDING_MODEL, input=batch_texts)

        batch_cost_sek = calculate_cost(batch_texts) * 10  # Ish SEK conversion
        total_cost_sek += batch_cost_sek

        batch_embeddings = [e["embedding"] for e in response["data"]]
        embeddings.extend(batch_embeddings)
        time.sleep(SLEEP_TIME)
    return embeddings, total_cost_sek


# 5 Remove Old Datapoints
def remove_old_datapoints(
    new_chunks, qdrant_client: QdrantClient, COLLECTION_NAME, old_urls=None
):
    # Remove url datapoints
    urls = [chunk["url"] for chunk in new_chunks]
    if old_urls:
        urls.extend(old_urls)

    url_filter = models.Filter(
        must=[
            models.FieldCondition(key="metadata.url", match=models.MatchAny(any=urls))
        ]
    )

    points_selector = models.FilterSelector(filter=url_filter)

    qdrant_client.delete(
        collection_name=COLLECTION_NAME, points_selector=points_selector
    )
    logging.info("Removed OLD datapoints")


# 6 Upsert Embeddings to Qdrant
def upsert_to_qdrant(chunks, embeddings, qdrant_client: QdrantClient, COLLECTION_NAME):
    points = []
    for i, chunk in enumerate(chunks):
        utc_time = datetime.now(timezone.utc).replace(microsecond=0)
        update_time = utc_time.astimezone(ZoneInfo("Europe/Stockholm"))
        update_time_str = update_time.strftime("%Y-%m-%dT%H:%M:%S")
        payload = {
            "content": chunk["chunk"],
            "metadata": {
                "chunk_info": chunk["chunk_info"],
                "title": chunk["title"],
                "update_date": update_time_str,
                "url": chunk["url"],
            },
        }
        if "source_url" in chunk:
            payload["metadata"]["source_url"] = chunk["source_url"]

        point = PointStruct(
            id=chunk["chunk_hash"], vector=embeddings[i], payload=payload
        )

        logging.info(f"Chunk uppladdas: {chunk['chunk_hash']}, URL: {chunk['url']}")
        points.append(point)
    try:
        qdrant_client.upsert(collection_name=COLLECTION_NAME, points=points)
    except Exception as e:
        logging.error(f"Upsert failed: {e}")
