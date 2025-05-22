from datetime import datetime, timezone
import logging
import os
import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import openai

from qdrant_client import QdrantClient
from qdrant_client.http.models import VectorParams, Distance, PointStruct

from essential_methods import calculate_cost_sek, generate_uuid

# Batch Constants
BATCH_SIZE = 1000
SLEEP_TIME = 2
EMBEDDING_MODEL = "text-embedding-3-large"

# Setup Openai
load_dotenv(dotenv_path="API_KEYS.env")
openai_api_key = os.getenv("OPENAI_API_KEY")
openai.api_key = openai_api_key


# Main, Process Item and upload to Qqdrant
def process_item(item, qdrant_client, COLLECTION_NAME="IntranetFalkenbergHemsida"):
    logging.info("Dividing to chunks")
    chunks = get_item_chunks(item)
    logging.info("Embedding chunks")
    embeddings, chunk_cost_SEK = create_embeddings(chunks)
    logging.info("Uploading Embeddings")
    upsert_to_qdrant(chunks, embeddings, qdrant_client, COLLECTION_NAME)
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
            "chunk_info": f"Chunk {index + 1} of {num_chunks}",
        }
        if "source_url" in item:
            chunk_data["source_url"] = item["source_url"]
        if "version" in item:
            chunk_data["version"] = item["version"]

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


# 2 Create Embeddings
def create_embeddings(chunks):
    texts = [chunk["chunk"] for chunk in chunks]
    embeddings = []
    total_cost_sek = 0
    for batch_start in range(0, len(texts), BATCH_SIZE):
        batch_texts = texts[batch_start : batch_start + BATCH_SIZE]
        response = openai.Embedding.create(model=EMBEDDING_MODEL, input=batch_texts)

        batch_cost_sek = calculate_cost_sek(batch_texts)
        total_cost_sek += batch_cost_sek

        batch_embeddings = [e["embedding"] for e in response["data"]]
        embeddings.extend(batch_embeddings)
        time.sleep(SLEEP_TIME)
    return embeddings, total_cost_sek


# 3 Upsert Embeddings to Qdrant
def upsert_to_qdrant(chunks, embeddings, qdrant_client, COLLECTION_NAME):
    points = []
    for i, chunk in enumerate(chunks):
        doc_uuid = generate_uuid(chunk["chunk"])
        utc_time = datetime.now(timezone.utc).replace(microsecond=0)
        update_time = utc_time.astimezone(ZoneInfo("Europe/Stockholm"))
        update_time_str = update_time.strftime("%Y-%m-%dT%H:%M:%S")
        payload = {
            "url": chunk["url"],
            "title": chunk["title"],
            "chunk": chunk["chunk"],
            "chunk_info": chunk["chunk_info"],
            "update_date": update_time_str,
        }
        if "source_url" in chunk:
            payload["source_url"] = chunk["source_url"]

        if "version" in chunk:
            payload["version"] = chunk["version"]

        point = PointStruct(id=doc_uuid, vector=embeddings[i], payload=payload)

        logging.info(f"Chunk uppladdas: {doc_uuid}, URL: {chunk['url']}")
        points.append(point)
    try:
        qdrant_client.upsert(collection_name=COLLECTION_NAME, points=points)
    except Exception as e:
        logging.error(f"Upsert failed: {e}")
