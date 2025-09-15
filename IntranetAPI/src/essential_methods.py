import hashlib
import uuid

import tiktoken


# UUID Gen
def generate_uuid(text):
    hash_object = hashlib.md5(text.encode())
    return str(uuid.UUID(hash_object.hexdigest()))


# Token Count/Calc
def count_tokens(texts, model="text-embedding-3-large"):
    encoding = tiktoken.encoding_for_model(model)
    if isinstance(texts, str):
        texts = [texts]
    total_tokens = 0
    for text in texts:
        tokens = encoding.encode(text)
        total_tokens += len(tokens)
    return total_tokens


def calculate_cost(texts, model="text-embedding-3-large", is_input=True):
    # Get the number of tokens in the text
    num_tokens = count_tokens(texts, model)

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
