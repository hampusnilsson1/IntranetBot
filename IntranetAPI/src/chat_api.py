import requests
import os
import re
import json
import logging
import tiktoken
from dotenv import load_dotenv
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import openai
from qdrant_client import QdrantClient
from qdrant_client import models

from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from flask_limiter import Limiter
from asgiref.wsgi import WsgiToAsgi

from essential_methods import swedish_time

# Setup Logging
log_file = "../data/update_logg.txt"

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
from essential_methods import calculate_cost

api_keys_path = "../data/API_KEYS.env"
cookie_path = "../data/COOKIE.env"  # Added path for cookies


def load_api_key(key_variable):
    if not os.path.exists(api_keys_path):
        raise FileNotFoundError(f"{api_keys_path} file not found.")
    load_dotenv(dotenv_path=api_keys_path)
    api_key = os.getenv(key_variable)
    if not api_key is None:
        return api_key

    raise ValueError(
        "API key was not found!, Make sure the environment variable is set."
    )


# Load Cookie Info
if os.path.exists(cookie_path):
    load_dotenv(dotenv_path=cookie_path)
    COOKIE_NAME = os.getenv("COOKIE_NAME")
    COOKIE_VALUE = os.getenv("COOKIE_VALUE")
else:
    logger.warning("COOKIE.env not found. Cookie validation will be skipped.")
    COOKIE_NAME = None
    COOKIE_VALUE = None


def validate_cookie_startup():
    if not COOKIE_NAME or not COOKIE_VALUE:
        logger.warning("Skipping cookie validation (Credentials missing).")
        return

    url = "https://intranet.falkenberg.se/start2"
    logger.info(f"Validating Cookie against {url}...")

    try:
        # Use GET to see the body content
        response = requests.get(
            url, cookies={COOKIE_NAME: COOKIE_VALUE}, allow_redirects=True, timeout=10
        )

        # 1. Check URL Redirect (3xx -> Login URL)
        if "idp.falkenberg.se" in response.url:
            logger.error("CRITICAL: Cookie is INVALID! API redirected to Login Page.")
            return False

        # 2. Check Content Body for SAML/Login Form (The robust check)
        content = response.text
        if "idp.falkenberg.se" in content and "SAMLRequest" in content:
            logger.error(
                "CRITICAL: Cookie is INVALID! Detected SAML Login Form in response."
            )
            return False

        if response.status_code == 200:
            logger.info("Cookie is VALID. API started successfully.")
            return True
        else:
            logger.warning(
                f"Cookie validation returned unexpected status: {response.status_code}"
            )
            return False

    except Exception as e:
        logger.error(f"Failed to validate cookie during startup: {e}")
        return False


validate_cookie_startup()


# Qdrant
COLLECTION_NAME = "IntranetFalkenbergHemsida_RAG"
QDRANT_API_KEY = load_api_key("QDRANT_API_KEY")
QDRANT_URL = "https://qdrant.utvecklingfalkenberg.se"
QDRANT_CLIENT = QdrantClient(
    url=QDRANT_URL, port=443, https=True, api_key=QDRANT_API_KEY
)

# OpenAI
openai.api_key = load_api_key("OPENAI_API_KEY")
GPT_MODEL = "gpt-4o"

# Directus Chat Database
chat_api_url = "https://nav.utvecklingfalkenberg.se/items/falkenberg_intranet_chat"

# Directus Message Database
message_api_url = (
    "https://nav.utvecklingfalkenberg.se/items/falkenberg_intranet_messages"
)

headers = {"Content-Type": "application/json"}
params = {"access_token": load_api_key("DIRECTUS_KEY")}


def generate_embeddings(text):  # Generate embedding of the text
    response = openai.Embedding.create(input=text, model="text-embedding-3-large")
    return response["data"][0]["embedding"]


def search_collection(
    qdrant_client: QdrantClient,
    collection_name,
    user_query_embedding,
    keyword_filter=None,
):
    if keyword_filter is None:
        response = qdrant_client.query_points(
                collection_name=collection_name,
                query=user_query_embedding,
                limit=5,
                with_payload=True,
            )
        return response.points if hasattr(response, 'points') else []

    # Get results from vector search and filtered scroll
    vector_result_obj = qdrant_client.query_points(
        collection_name=collection_name,
        query=user_query_embedding,
        limit=5,
        with_payload=True,
    )
    vector_results = vector_result_obj.points if hasattr(vector_result_obj, 'points') else []

    filtered_results, _ = qdrant_client.scroll(
        collection_name=collection_name, scroll_filter=keyword_filter, limit=3
    )

    filtered_ids = set(point.id for point in filtered_results)
    combined_results = list(filtered_results)

    for r in vector_results:
        if r.id not in filtered_ids:
            combined_results.append(r)
        if len(combined_results) >= 5:
            break

    return combined_results[:5]


def directus_get_cost(chat_id):
    cost_params = {
        "access_token": load_api_key("DIRECTUS_KEY"),
        "filter[chat_id][_eq]": chat_id,
        "fields": "cost_usd",
    }
    response = requests.get(chat_api_url, headers=headers, params=cost_params)

    if response.status_code == 200:
        data = response.json().get("data")
        if data:
            cost_usd = data[0]["cost_usd"]
            if cost_usd is None:
                cost_usd = 0.0

            return cost_usd
        else:
            logger.warning("No data found for the given chat_id")
            return None
    else:
        logger.error(f"Directus Cost Error: {response.status_code} - {response.text}")
        return None


# Remove emojis from answer right before saving in database
def remove_emojis(text):
    emoji_pattern = re.compile(
        "[\U0001f600-\U0001f64f"  # Smiley
        "\U0001f300-\U0001f5ff"  # Symbols & Pictographs
        "\U0001f680-\U0001f6ff"  # Transport & Map
        "\U0001f700-\U0001f77f"  # Alchemical Symbols
        "\U0001f900-\U0001f9ff"  # Supplemental Symbols and Pictographs
        "\U00002600-\U000027bf"  # Miscellaneous Symbols
        "\U0001f1e0-\U0001f1ff"  # Flags (iOS)
        "]+",
        flags=re.UNICODE,
    )
    return emoji_pattern.sub(r"", text)


# Start
def get_result(user_input, user_history, chat_id, MAX_INPUT_CHAR):
    question_cost = 0
    # Loop through user history and combine user inputs
    user_input_combo = ""
    for message in user_history:
        role = message.get("role")
        if role == "user":
            content = message.get("content")
            user_input_combo += "," + str(content)
    user_input_combo = user_input_combo[:MAX_INPUT_CHAR]

    # Generate a relevant question that we can search for information in QDRANT
    query_instruction = f"""Du ska generera en kort, koncis och relevant fråga baserat på användarens senaste fråga och eventuellt tidigare frågor om FBG kommuns intranet.

        Tidigare frågor: "{user_input_combo}" (första frågan i konversationen först).

        Instruktioner:
        1. Formulera en ny fråga som fokuserar på användarens senaste fråga.
        2. Om tidigare frågor är relevanta till senaste frågan, inkludera endast då deras kontext i den nya frågan; annars ignorera dem.
        3. Frågan ska vara optimerad för sökning i en inbäddad databas.
        4. Avsluta alltid frågan med ett kommatecken(,) - detta används som separator i detta CSV-format.
        5. Efter frågan skriv de viktigaste nyckelorden (max 3st), separerade med kommatecken.
        6. Generera endast nyckelord om de förekommer i frågan och innehåller något av följande:
        - Namn på personer
        - Exakta titlar på dokument, policys, riktlinjer eller liknande
        - Namn på platser, byggnader eller organisationer, eller andra liknande sökbara entiteter
        - Datum (exakta eller formella datum/tidsangivelser)
        - Adresser eller vägnamn
        - Specifika begrepp eller termer som är centrala för frågan.

        Format:
        Fråga,Keyword1,Keyword2,Keyword3 osv.

        Exempel:
        "Vem är Hampus Nilsson?",Hampus Nilsson
        "Var ligger Tångaskolan?",Tångaskolan
        "När är Kulturnatta 2025?",Kulturnatta,2025
        "Vem kan jag kontakta angående bygglov?",Bygglov,Kontakt

        Generera endast en enda rad i CSV-format - Ingen yttligare text eller förklaring.
    """
    query_input = [
        {"role": "system", "content": query_instruction},
        {"role": "user", "content": user_input},
    ]

    openai_query = openai.ChatCompletion.create(model=GPT_MODEL, messages=query_input)
    question_cost += calculate_cost(json.dumps(query_input), model="gpt-4o")

    query_text_out = openai_query["choices"][0]["message"]["content"]
    question_cost += calculate_cost(query_text_out, is_input=False, model="gpt-4o")

    # Split the CSV string into question and keywords
    csv_parts = [part.strip() for part in query_text_out.split(",") if part.strip()]
    question = csv_parts[0] if csv_parts else ""
    keywords = csv_parts[1:] if len(csv_parts) > 1 else []
    logger.info(f"Generated Question: {question}")
    logger.info(f"Keywords: {keywords}")

    if len(keywords) > 0:
        keyword_filter = models.Filter(
            should=[
                models.FieldCondition(
                    key="content",
                    match=models.MatchText(text=keyword),
                )
                for keyword in keywords
            ]
        )
    else:
        keyword_filter = None

    user_embedding = generate_embeddings(question)
    question_cost += calculate_cost(question)

    search_results = search_collection(
        QDRANT_CLIENT,
        COLLECTION_NAME,
        user_embedding,
        keyword_filter=keyword_filter,
    )

    found_ids = [res.id for res in search_results]
    logger.info(f"Search found {len(found_ids)} results: {found_ids}")

    similar_texts = []
    for result in search_results:
        similar_texts.append(
            {
                "chunk": result.payload["content"],
                "title": result.payload["metadata"]["title"],
                "url": result.payload["metadata"]["url"],
                "score": getattr(result, "score", "Keyword Match"),
                "id": result.id,
            }
        )

    # Send in current datetime so it knows
    utc_time = datetime.now(timezone.utc).replace(microsecond=0)
    current_date_time = utc_time.astimezone(ZoneInfo("Europe/Stockholm"))
    current_date_time_str = current_date_time.strftime("%Y-%m-%dT%H:%M:%S")

    doc_context = ""
    for i, text in enumerate(similar_texts):
        doc_context += f"Dokument:\n{text['chunk']}\nURL: {text['url']}\nLikhetsscore: {text['score']}\n\n"

    # Prepare the prompt for GPT-4o in Swedish
    instructions_prompt = f"""
    Du är en AI-assistent som är specialiserad på att hjälpa anställda inom Falkenbergs kommun med frågor kring kommunens intranet. 
    Din roll är att ge tydliga, exakta och användarvänliga svar baserade på tillgänglig information. 
    Du ska alltid försöka ge det mest relevanta svaret med hjälp av dokument du får tillgång till, men även vara öppen om det finns osäkerheter i informationen.

    Här är information som skulle kunna vara till hjälp för att hjälpa användaren kring frågan:
    {doc_context}


    Hjälp användaren att få svar på sin fråga.
    Redovisa endast om dokumenten är relevant. 
    Om du använder dokument, hänvisa alltid med länk till källan,
    Efterfrågas tid/datum eller om det behövs i beslut av relevanta dokument så är den just nu {current_date_time_str}.
    Reply in the same language as: {user_input}.
    """

    messages = [{"role": "system", "content": instructions_prompt}]
    for message in user_history:
        role = message.get("role")
        content = message.get("content")
        messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": user_input})
    question_cost += calculate_cost(json.dumps(messages), model="gpt-4o")

    if not chat_id or not user_history:
        logger.info("No chat_id or history found, creating new chat.")
        chat_data = {}
        post_response = requests.post(
            chat_api_url, json=chat_data, headers=headers, params=params
        )
        if post_response.status_code != 200:
            logger.error(f"Error creating chat: {post_response.json()}")
            chat_id = None
        else:
            chat_id = post_response.json().get("data", {}).get("chat_id")
            logger.info(f"New chat created with ID: {chat_id}")

    collected_response = []

    def generate():
        nonlocal question_cost

        yield json.dumps({"chat_id": chat_id}) + "\n<END_OF_JSON>\n"

        # GPT-4o Generation
        completion = openai.ChatCompletion.create(
            model=GPT_MODEL,
            messages=messages,
            stream=True,
        )

        for chunk in completion:
            if chunk.choices[0].delta.get("content"):
                text_chunk = chunk.choices[0].delta["content"]
                collected_response.append(text_chunk)
                yield text_chunk

        # When the stream ends, we can finalize the response
        full_response = "".join(collected_response)
        question_cost += calculate_cost(full_response, "gpt-4o", is_input=False)
        full_response_no_emojis = remove_emojis(full_response)
        user_input_no_emoji = remove_emojis(user_input)
        if chat_id:
            logger.info(f"Saving message for chat_id: {chat_id}")
            message_data = {
                "chat_id": chat_id,
                "prompt": user_input_no_emoji,
                "response": full_response_no_emojis,
            }
            message_response = requests.post(
                message_api_url, json=message_data, headers=headers, params=params
            )

            if message_response.status_code > 299:
                logger.error(
                    f"Error sending msg to API. Full request: {message_api_url}, {message_data}, {headers}, {params}"
                )

            # Get chat cost
            update_chat_api_url = f"{chat_api_url}/{chat_id}"

            total_chat_cost = directus_get_cost(chat_id)
            total_chat_cost += question_cost

            cost_data = {"cost_usd": total_chat_cost}

            chat_cost_params = params
            chat_cost_params["filter[chat_id][_eq]"] = chat_id

            # Update the chat cost in Directus
            try:
                response = requests.patch(
                    update_chat_api_url,
                    json=cost_data,
                    headers=headers,
                    params=chat_cost_params,
                )

                if response.status_code == 200:
                    logger.info(f"Cost updated for ID: {chat_id}")
                else:
                    logger.error(f"Error updating cost: {response.text}")

            except requests.exceptions.RequestException as e:
                logger.error(f"Network error updating cost: {str(e)}")

    return generate


def remove_qdrant(url):
    logger.info(f"Attempting to remove Qdrant points for URL: {url}")
    qdrant_filter = models.Filter(
        should=[
            models.FieldCondition(
                key="metadata.url", match=models.MatchValue(value=url)
            ),
            models.FieldCondition(
                key="metadata.source_url", match=models.MatchValue(value=url)
            ),
        ]
    )

    points_selector = models.FilterSelector(filter=qdrant_filter)

    deleted_points = QDRANT_CLIENT.scroll(
        collection_name=COLLECTION_NAME, scroll_filter=qdrant_filter, limit=1000
    )

    QDRANT_CLIENT.delete(
        collection_name=COLLECTION_NAME, points_selector=points_selector
    )
    logger.info(f"Deleted points count: {len(deleted_points[0])}")
    return deleted_points


app = Flask(__name__)
CORS(app)
# Limit the API request amount
limiter = Limiter(app=app, key_func=lambda: "global", storage_uri="memory://")

asgi_app = WsgiToAsgi(app)


@app.route("/generate", methods=["POST"])
@limiter.limit("100 per hour")
def generate():
    data = request.get_json()
    if not data or "user_input" not in data:
        logger.warning("Generate request missing user_input")
        return jsonify({"error": "Ingen användarinput inmatad"}), 400

    user_input = data["user_input"]

    if "user_history" in data and "chat_id" in data and data["chat_id"] != "":
        history_list = data["user_history"]
        if len(history_list) > 12:
            # Limit the history to only the last 12 messages/6 Questions
            user_history = history_list[-12:]
        else:
            user_history = history_list

        chat_id = data["chat_id"]

        generator = get_result(user_input, user_history, chat_id, 1000)
    else:
        generator = get_result(user_input, [], None, 1000)
    response = Response(stream_with_context(generator()), mimetype="text/plain")
    return response


# Load Update Key
UPDATE_API_KEY = load_api_key("UPDATE_API_KEY")


# Update Qdrant Datapoints (Currently not allowing pdf inputs)
@app.route("/update-qdrant", methods=["POST"])
def update_qdrant():
    try:
        data = request.get_json()
        if "api_key" not in data or data["api_key"] != UPDATE_API_KEY:
            logger.warning("Update Qdrant unauthorized attempt")
            return jsonify({"error": "Invalid or missing API key"}), 401
        if not data or "url" not in data:
            return jsonify({"error": "URL is required"}), 400

        url = data["url"]
        logger.info(f"Starting Qdrant Update for: {url}")

        result = update_url(url)

        return (
            jsonify(
                {
                    "message": "Successfully updated Qdrant",
                    "result": f"Cost: {result} SEK",
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"Update Qdrant Failed: {str(e)}")
        return jsonify({"error": str(e)}), 500


# Remove Qdrant datapoints linked to URL
@app.route("/remove-qdrant", methods=["POST"])
def remove_qdrant_url():
    try:
        data = request.get_json()
        if "api_key" not in data or data["api_key"] != UPDATE_API_KEY:
            return jsonify({"error": "Invalid or missing API key"}), 401
        if not data or "url" not in data:
            return jsonify({"error": "URL is required"}), 400

        url = data["url"]
        response = remove_qdrant(url)

        return (
            jsonify(
                {
                    "message": "Successfully removed URL from Qdrant",
                    "result": str(response),
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"Remove Qdrant Failed: {str(e)}")
        return jsonify({"error": str(e)}), 500
