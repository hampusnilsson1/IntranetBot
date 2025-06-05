import requests
import os
import re
import json
import tiktoken
from dotenv import load_dotenv
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import openai
from qdrant_client import QdrantClient

from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from flask_limiter import Limiter
from asgiref.wsgi import WsgiToAsgi

from individual_update_url import update_url
from individual_update_url import remove_qdrant_data

api_keys_path = "../data/API_KEYS.env"


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


# Qdrant
COLLECTION_NAME = "IntranetFalkenbergHemsida"
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
    qdrant_client, collection_name, user_query_embedding
):  # Searches for the 10 most similar documents
    response = qdrant_client.search(
        collection_name=collection_name,
        query_vector=user_query_embedding,
        limit=10,
        with_payload=True,
    )
    return response


# OpenAI Token Counter
def count_tokens(text, model="gpt-4o"):

    encoding = tiktoken.encoding_for_model(model)

    tokens = encoding.encode(text)
    num_tokens = len(tokens)
    return num_tokens


# Token Cost Calculator
def calculate_cost(text, model="gpt-4o", is_input=True):
    # Get the number of tokens in the text
    num_tokens = count_tokens(text, model)

    # Calculation per 1000 tokens USD
    if model == "gpt-4o":
        if is_input:
            cost_per_1000_tokens = 0.0025  # USD
        else:
            cost_per_1000_tokens = 0.0100  # USD
    elif model == "text-embedding-3-large":
        cost_per_1000_tokens = 0.00013  # USD
    else:
        raise ValueError("Unsupported model")

    # Calculate text cost
    cost = (num_tokens / 1000) * cost_per_1000_tokens
    return cost


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
            print("No data found for the given chat_id")
            return None
    else:
        print(f"Error: {response.status_code} - {response.text}")
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
    query_instruction = f"""Du ska generera en kort, koncis och relevant fråga baserat på användarens senaste fråga och eventuellt tidigare frågor om FBG kommun.

        Tidigare frågor: "{user_input_combo}" (första frågan i konversationen först).

        Generera en fråga som:
        1. Fokuserar på användarens senaste fråga, men tar hänsyn till tidigare frågor om de är relevanta.
        2. Om tidigare frågor är relevanta, inkludera endast då deras kontext i den nya frågan; annars fokusera enbart på den senaste frågan.
        3. Frågan ska vara optimerad för att söka information i en inbäddad databas.
        
    """
    query_input = [
        {"role": "system", "content": query_instruction},
        {"role": "user", "content": user_input},
    ]

    openai_query = openai.ChatCompletion.create(model=GPT_MODEL, messages=query_input)
    question_cost += calculate_cost(json.dumps(query_input))

    query_text_out = openai_query["choices"][0]["message"]["content"]
    question_cost += calculate_cost(query_text_out, is_input=False)

    user_embedding = generate_embeddings(query_text_out)
    question_cost += calculate_cost(query_text_out, "text-embedding-3-large")

    search_results = search_collection(QDRANT_CLIENT, COLLECTION_NAME, user_embedding)
    similar_texts = [
        {
            "chunk": result.payload["chunk"],
            "title": result.payload["title"],
            "url": result.payload["url"],
            "score": result.score,
            "id": result.id,
        }
        for result in search_results
    ]
    # Send in current datetime so it knows
    utc_time = datetime.now(timezone.utc).replace(microsecond=0)
    current_date_time = utc_time.astimezone(ZoneInfo("Europe/Stockholm"))
    current_date_time_str = current_date_time.strftime("%Y-%m-%dT%H:%M:%S")
    # Prepare the prompt for GPT-4o in Swedish
    instructions_prompt = f"""
    Du är en AI-assistent som är specialiserad på att hjälpa anställda inom Falkenbergs kommun med frågor kring kommunens intranet. 
    Din roll är att ge tydliga, exakta och användarvänliga svar baserade på tillgänglig information. 
    Du ska alltid försöka ge det mest relevanta svaret med hjälp av dokument du får tillgång till, men även vara öppen om det finns osäkerheter i informationen.

    Här är information som skulle kunna vara till hjälp för att hjälpa användaren kring frågan:
    Dokument:
    {similar_texts[0]['chunk']}
    URL: {similar_texts[0]['url']}
    Likhetsscore: {similar_texts[0]['score']}

    Dokument:
    {similar_texts[1]['chunk']}
    URL: {similar_texts[1]['url']}
    Likhetsscore: {similar_texts[1]['score']}
    
    Dokument:
    {similar_texts[2]['chunk']}
    URL: {similar_texts[2]['url']}
    Likhetsscore: {similar_texts[2]['score']}
    
    Dokument:
    {similar_texts[3]['chunk']}
    URL: {similar_texts[3]['url']}
    Likhetsscore: {similar_texts[3]['score']}
    
    Dokument:
    {similar_texts[4]['chunk']}
    URL: {similar_texts[4]['url']}
    Likhetsscore: {similar_texts[4]['score']}


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
    question_cost += calculate_cost(json.dumps(messages))

    if not chat_id or not user_history:
        print("Hittade inte chat_id eller user_history så skapas ny chatt", chat_id)
        chat_data = {}
        post_response = requests.post(
            chat_api_url, json=chat_data, headers=headers, params=params
        )
        if post_response.status_code != 200:
            print("Fel vid skapande av ny chatt.")
            print("Chat creation response:", post_response.json())
            chat_id = None
        else:
            print("Chat creation response:", post_response.json())
            chat_id = post_response.json().get("data", {}).get("chat_id")
            print("Skapat nytt id: ", chat_id)

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
            print("Använder: ", chat_id)
            message_data = {
                "chat_id": chat_id,
                "prompt": user_input_no_emoji,
                "response": full_response_no_emojis,
            }
            message_response = requests.post(
                message_api_url, json=message_data, headers=headers, params=params
            )

            if message_response.status_code > 299:
                print(
                    "Fel vid skickande av svaret i API:n. Hela request: ",
                    message_api_url,
                    message_data,
                    headers,
                    params,
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
                    print("Directus cost_usd uppdaterad!ID:", chat_id)
                    return jsonify({"message": "Konstnad Uppdaterad!"}), 200
                else:
                    return (
                        jsonify(
                            {
                                "error": f"Fel vid uppdatering av kostnad: {response.text}"
                            }
                        ),
                        response.status_code,
                    )
            except requests.exceptions.RequestException as e:
                return jsonify({"error": f"Nätverksfel: {str(e)}"}), 500

    return generate


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
            return jsonify({"error": "Invalid or missing API key"}), 401
        if not data or "url" not in data:
            return jsonify({"error": "URL is required"}), 400

        url = data["url"]

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
        response = remove_qdrant_data(url)

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
        return jsonify({"error": str(e)}), 500
