"""mem0 memory proxy for llama.cpp.

OpenAI-compatible /v1/chat/completions front end:
  client -> search mem0 -> inject memories -> llama.cpp -> relay -> store exchange in mem0
"""

import asyncio
import functools
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from mem0 import Memory

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("mem0-proxy")

LLAMA_CPP_URL = os.getenv("LLAMA_CPP_INTERNAL_URL", "http://127.0.0.1:8080/v1").rstrip("/")
OLLAMA_URL = os.getenv("OLLAMA_DOCKER_NODE_URL", "http://localhost:11434")
EMBED_MODEL = os.getenv("MEM0_EMBED_MODEL", "bge-large:latest")
LLM_MODEL = os.getenv("MEM0_LLM_MODEL", "qwen3.5-9b")
USER_ID = os.getenv("MEM0_USER_ID", "opencode_user")
TOP_K = int(os.getenv("MEM0_TOP_K", "5"))
SEARCH_TIMEOUT_S = float(os.getenv("MEM0_SEARCH_TIMEOUT", "5"))
# Requests without tools are opencode's title/summary/compaction calls, not chat turns.
SKIP_NO_TOOLS = os.getenv("MEM0_SKIP_NO_TOOLS", "true").lower() == "true"
MAX_STORE_CHARS = int(os.getenv("MEM0_MAX_STORE_CHARS", "4000"))
CHROMA_PATH = os.getenv("MEM0_CHROMA_PATH", str(BASE_DIR / "mem0_storage"))

# mem0 OSS: LLM = llama.cpp (OpenAI-compatible), embedder = Ollama container, store = Chroma
config = {
    "llm": {
        "provider": "openai",
        "config": {
            "model": LLM_MODEL,
            "api_key": "local",  # llama.cpp runs without auth; any non-empty string
            "openai_base_url": LLAMA_CPP_URL,
            "temperature": 0.1,
            "max_tokens": 1024,
        },
    },
    "embedder": {
        "provider": "ollama",
        "config": {
            "model": EMBED_MODEL,
            "ollama_base_url": OLLAMA_URL,
        },
    },
    "vector_store": {
        "provider": "chroma",
        "config": {
            "collection_name": "vm_agent_memory",
            "path": CHROMA_PATH,
        },
    },
}
memory = Memory.from_config(config)

http = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0))
background_tasks: set[asyncio.Task] = set()

# Separate pools so slow background writes (which wait on llama.cpp) cannot starve searches.
search_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mem0-search")
add_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mem0-add")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await http.aclose()


app = FastAPI(title="mem0-proxy", lifespan=lifespan)


def inject_memories(messages: list, facts: str) -> None:
    block = "\n\n[Persistent User Profile/Memories]:\n" + facts
    system_msg = next((m for m in messages if m.get("role") == "system"), None)
    if system_msg is not None and isinstance(system_msg.get("content"), str):
        system_msg["content"] += block
    else:
        messages.insert(
            0,
            {
                "role": "system",
                "content": "Use the following user facts to tailor your responses:" + block,
            },
        )


def parse_sse_text(raw: bytes) -> str:
    """Concatenate assistant content deltas from an SSE chat-completion stream."""
    parts = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload in ("", "[DONE]"):
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        for choice in obj.get("choices", []):
            content = (choice.get("delta") or {}).get("content")
            if content:
                parts.append(content)
    return "".join(parts)


async def store_exchange(user_text: str, assistant_text: str) -> None:
    if not user_text or not assistant_text:
        return
    # Cap what is embedded and extracted; long replies cost many embedding calls on the 2-vCPU node.
    user_text = user_text[:MAX_STORE_CHARS]
    assistant_text = assistant_text[:MAX_STORE_CHARS]
    try:
        # Single worker: adds queue here instead of occupying the search thread pool.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            add_pool,
            functools.partial(
                memory.add,
                [
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": assistant_text},
                ],
                user_id=USER_ID,
            ),
        )
    except Exception:
        log.exception("mem0 add failed")


def schedule_store(user_text: str, assistant_text: str) -> None:
    # Fire-and-forget so extraction never delays the response to the client.
    task = asyncio.create_task(store_exchange(user_text, assistant_text))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models():
    resp = await http.get(f"{LLAMA_CPP_URL}/models")
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    messages = body.get("messages") or []
    if not messages:
        return JSONResponse(status_code=400, content={"error": "No messages provided"})

    user_text = next(
        (
            m.get("content")
            for m in reversed(messages)
            if m.get("role") == "user" and isinstance(m.get("content"), str)
        ),
        "",
    )

    # Diagnostics: size of the incoming request (tool schemas are often the bulk)
    log.info(
        "request: messages=%d tools=%d chars=%d",
        len(messages),
        len(body.get("tools") or []),
        len(json.dumps(body)),
    )

    # Tool-less requests (title/summary/compaction) skip memory entirely: no search, no store.
    if SKIP_NO_TOOLS and not body.get("tools"):
        user_text = ""

    # 1. Retrieve relevant memories (failure falls back to a plain pass-through)
    if user_text:
        try:
            # Memory is the synchronous mem0 client; run it off the event loop.
            # Bounded wait: if Ollama (embedder) or Chroma stalls, forward without memories.
            loop = asyncio.get_running_loop()
            t0 = time.monotonic()
            found = await asyncio.wait_for(
                loop.run_in_executor(
                    search_pool,
                    functools.partial(
                        memory.search,
                        query=user_text,
                        filters={"user_id": USER_ID},
                        top_k=TOP_K,
                    ),
                ),
                timeout=SEARCH_TIMEOUT_S,
            )
            log.info(
                "mem0 search ok in %.2fs (%d results)",
                time.monotonic() - t0,
                len(found.get("results", [])),
            )
            facts = "\n".join(
                f"- {m['memory']}" for m in found.get("results", []) if m.get("memory")
            )
            if facts:
                inject_memories(messages, facts)
        except asyncio.TimeoutError:
            log.warning("mem0 search timed out after %ss; forwarding without memories", SEARCH_TIMEOUT_S)
        except Exception:
            log.exception("mem0 search failed; forwarding without memories")

    upstream_url = f"{LLAMA_CPP_URL}/chat/completions"

    # 2. Streaming: relay SSE bytes, collect text, store exchange when the stream ends
    if body.get("stream"):
        upstream = await http.send(http.build_request("POST", upstream_url, json=body), stream=True)
        content_type = upstream.headers.get("content-type", "text/event-stream")
        if upstream.status_code >= 400:
            err = await upstream.aread()
            await upstream.aclose()
            return Response(content=err, status_code=upstream.status_code, media_type=content_type)

        async def relay():
            collected = bytearray()
            try:
                async for chunk in upstream.aiter_bytes():
                    collected.extend(chunk)
                    yield chunk
            finally:
                await upstream.aclose()
                schedule_store(user_text, parse_sse_text(bytes(collected)))

        return StreamingResponse(relay(), status_code=upstream.status_code, media_type=content_type)

    # 3. Non-streaming: forward, store, return upstream status/body unchanged
    resp = await http.post(upstream_url, json=body)
    if resp.status_code < 400:
        try:
            assistant_text = resp.json()["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, ValueError):
            assistant_text = ""
        schedule_store(user_text, assistant_text)

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("MEM0_PROXY_PORT", "8000")))
