import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from time import sleep
from typing import Iterator, Tuple

import numpy as np
import openai
from oaklib.utilities.apikey_manager import get_apikey_value

logger = logging.getLogger(__name__)
NUM_RETRIES = 3



@dataclass
class OpenAIClient:
    #max_tokens: int = field(default_factory=lambda: 3000)
    model: str = field(default_factory=lambda: "gpt-3.5-turbo")
    cache_db_path: str = None
    api_key: str = None

    def __post_init__(self):
        if not self.api_key:
            self.api_key = get_apikey_value("openai")
        openai.api_key = self.api_key

    def complete(self, prompt, max_tokens=3000, **kwargs) -> str:
        engine = self.model
        logging.info(f"Complete: engine={engine}, prompt={prompt[0:100]}...")
        cur = self.db_connection()
        res = cur.execute("SELECT payload FROM cache WHERE prompt=? AND engine=?", (prompt, engine))
        payload = res.fetchone()
        if payload:
            prompt_peek = str(prompt)[0:80].replace("\n", "\\n")
            logger.info(f"Using cached payload for prompt: {prompt_peek}...")
            return payload[0]
        response = None
        i = 0
        while not response:
            i += 1
            logging.debug(f"Calling OpenAI API (attempt {i})...")
            try:
                if self._must_use_chat_api():
                    response = openai.ChatCompletion.create(
                        model=engine,
                        messages=[
                            {"role": "user",
                             "content": prompt,
                             },
                        ],
                        max_tokens=max_tokens,
                        **kwargs,
                    )
                else:
                    response = openai.Completion.create(
                                            engine = engine,
                                            prompt = prompt,
                                            max_tokens = max_tokens,
                    )
                break
            except Exception as e:
                logger.error(f"OpenAI API connection error: {e}")
                if i >= NUM_RETRIES:
                    raise e
                sleep_time = 4**i
                logger.info(f"Retrying {i} of {NUM_RETRIES} after {sleep_time} seconds...")
                sleep(sleep_time)

        if self._must_use_chat_api():
            payload = response['choices'][0]['message']['content']
        else:
            payload = response['choices'][0]['text']
        logger.info(f"Storing payload of len: {len(payload)}")
        cur.execute(
            "INSERT INTO cache (prompt, engine, payload) VALUES (?, ?, ?)",
            (prompt, engine, payload),
        )
        cur.connection.commit()
        return payload

    def db_connection(self):
        if not self.cache_db_path:
            self.cache_db_path = ".openai_cache.db"
        logger.info(f"Caching OpenAI responses to {Path(self.cache_db_path).absolute()}")
        create = not Path(self.cache_db_path).exists()
        con = sqlite3.connect(self.cache_db_path)
        cur = con.cursor()
        if create:
            cur.execute("CREATE TABLE cache (prompt, engine, payload)")
        return cur

    def cached_completions(
        self, search_term: str = None, engine: str = None
    ) -> Iterator[Tuple[str, str, str]]:
        if search_term:
            search_term = search_term.lower()
        cur = self.db_connection()
        res = cur.execute("SELECT engine, prompt, payload FROM cache")
        for row in res:
            if (
                search_term
                and search_term not in row[1].lower()
                and search_term not in row[2].lower()
            ):
                continue
            if engine and engine != row[0]:
                continue
            yield row

    def _must_use_chat_api(self) -> bool:
        """
        Returns True if the model requires the chat API, False otherwise.
        """
        if self.model.startswith("text-davinci"):
            return False
        return True

    def embeddings(self, text: str, model: str = None):
        if model is None:
            model = "text-embedding-ada-002"
        cur = self.db_connection()
        try:
            logger.info("creating embeddings cache")
            cur.execute("CREATE TABLE embeddings_cache (text, engine, vector_as_string)")
        except sqlite3.OperationalError:
            logger.info("Embeddings cache table already exists")
            pass
        res = cur.execute("SELECT vector_as_string FROM embeddings_cache WHERE text=? AND engine=?", (text, model))
        payload = res.fetchone()
        if payload:
            logger.info(f"Using cached embeddings for {model} {text[0:80]}...")
            return eval(payload[0])
        logger.info(f"querying OpenAI for {model} {text[0:80]}...")
        response = openai.Embedding.create(
            model=model,
            input=text,
        )
        v = response.data[0]["embedding"]
        logger.info(f"Storing embeddings of len: {len(v)}")
        cur.execute(
            "INSERT INTO embeddings_cache (text, engine, vector_as_string) VALUES (?, ?, ?)",
            (text, model, str(v)),
        )
        cur.connection.commit()
        return v

    def similarity(self, text1: str, text2: str, **kwargs):
        a1 = self.embeddings(text1, **kwargs)
        a2 = self.embeddings(text2, **kwargs)
        return np.dot(a1, a2) / (np.linalg.norm(a1) * np.linalg.norm(a2))



