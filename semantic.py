"""Optional semantic transcript search through a local Ollama embedding model."""
import hashlib
import json
import studio_json
import project_cancel
import math
import urllib.error
import urllib.request
from pathlib import Path

HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "nomic-embed-text"


def ollama_embed(texts, model):
    payload = {"model": model, "input": texts, "truncate": True}
    try:
        if project_cancel.active():
            from editorial_providers import ProviderError
            try:
                return project_cancel.ollama("/api/embed", payload, 180)["embeddings"]
            except ProviderError as exc:
                raise ValueError(str(exc)) from exc
        request = urllib.request.Request(HOST + "/api/embed", data=studio_json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=180) as response:
            vectors = json.load(response)["embeddings"]
    except (OSError, KeyError, ValueError) as exc:
        raise ValueError(f"Local semantic search unavailable. Start Ollama and run "
                         f"'ollama pull {model}', then retry. ({exc})") from exc
    return vectors


def validate_vectors(vectors, count):
    if not isinstance(vectors, list) or len(vectors) != count or not vectors:
        raise ValueError("Embedding model returned the wrong number of vectors.")
    if any(not isinstance(v, list) for v in vectors):
        raise ValueError("Embedding model returned invalid vectors.")
    size = len(vectors[0])
    if not size or any(len(v) != size or not all(isinstance(x, (float, int)) and math.isfinite(x)
                                                for x in v) or not any(v) for v in vectors):
        raise ValueError("Embedding model returned invalid vectors.")


def search(sentences, query, model=DEFAULT_MODEL, cache_dir=None, embed=None):
    if not query or not query.strip() or not sentences:
        raise ValueError("Semantic search needs a query and a non-empty transcript.")
    embed = embed or ollama_embed
    texts = [s["text"] for s in sentences]
    key = hashlib.sha256(json.dumps([model, texts], ensure_ascii=False).encode()).hexdigest()
    cache = Path(cache_dir) / "embeddings.json" if cache_dir else None
    vectors = None
    if cache and cache.exists():
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            if saved["key"] == key:
                validate_vectors(saved["vectors"], len(texts))
                vectors = saved["vectors"]
        except (ValueError, KeyError, TypeError):
            pass
    if vectors is None:
        vectors = []
        for i in range(0, len(texts), 64):
            project_cancel.check()
            batch = embed(texts[i:i + 64], model)
            validate_vectors(batch, len(texts[i:i + 64]))
            vectors.extend(batch)
        validate_vectors(vectors, len(texts))
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            studio_json.write(cache, {"key": key, "vectors": vectors})
    project_cancel.check()
    query_vector = embed([query], model)
    validate_vectors(query_vector, 1)
    q = query_vector[0]
    if len(q) != len(vectors[0]):
        raise ValueError("Query and document embeddings have different dimensions.")
    def cosine(v):
        return sum(a * b for a, b in zip(q, v)) / math.sqrt(
            sum(x * x for x in q) * sum(x * x for x in v))
    return sorted([{"index": i, "similarity": cosine(v)} for i, v in enumerate(vectors)],
                  key=lambda r: r["similarity"], reverse=True)


def candidates(sentences, query, model, cache_dir, min_len, max_len, max_clips):
    from score_clips import dedupe_candidates, discover_candidates

    matches = search(sentences, query, model, cache_dir)
    similarity = {match["index"]: round(match["similarity"], 4)
                  for match in matches[:200]}
    if not similarity:
        return []
    found, _ = discover_candidates(sentences, min_len, max_len,
                                   seed_indices=list(similarity))
    for item in found:
        project_cancel.check()
        item["similarity"] = similarity.get(item["seed_index"], 0.0)
    kept = dedupe_candidates(found)
    if max_clips is not None:
        kept = sorted(kept, key=lambda item: (
            -item.get("similarity", 0.0), -item["overall"],
            item["start"]))[:max_clips]
    return sorted(kept, key=lambda item: (item["start"], item["end"]))
