# Local LLM Infrastructure with Persistent Memory

A self-hosted LLM stack on a single consumer GPU, with a memory layer that gives the model
long-term recall across sessions. Clients talk to an OpenAI-compatible proxy. The proxy
retrieves relevant facts, injects them into the prompt, forwards the request to llama.cpp,
and stores new facts extracted from the exchange.

This repository holds the systemd units, the proxy service, client configuration, and the
notes from building and debugging the system.

## Highlights

- **Prompt-routing memory proxy** (FastAPI + uvicorn) in front of llama.cpp. Memory works on
  every completion, with no tool-calling dependency.
- **mem0 OSS** (`Memory`) for fact extraction and retrieval, backed by **Chroma**.
- **Split architecture:** the LLM runs on a GPU VM, while embeddings run on a separate CPU
  Docker node (Ollama, `bge-large`).
- **Fits a 32k context on an 8 GB GTX 1070** by quantizing the KV cache to q8 and limiting
  parallel slots and context checkpoints.
- **Operational hardening:** bounded search timeouts with graceful fallback, separate thread
  pools for reads and writes, serialized embedding requests, and per-agent tool restrictions.

## Architecture

```
                 +-------------------------------+
 opencode  --->  |  mem0 proxy (FastAPI/uvicorn) |  :8000   (llama VM)
                 |  search -> inject -> forward  |
                 |  relay   -> store (background)|
                 +---------------+---------------+
                                 |                    |
                  OpenAI API     |                    |  embeddings
                                 v                    v
                 +-------------------+     +-----------------------+
                 | llama.cpp server  |     | Ollama (bge-large)    |  :11434
                 | Qwen 3.5 9B, GPU  |     | Docker node, CPU      |
                 |              :8080|     +-----------------------+
                 +-------------------+
                                  \
                                   +-- Chroma (persistent, on the proxy host)
```

Clients that do not use the memory agent (for example, a direct llama.cpp provider) still
reach llama.cpp on port 8080 without memory.

The LLM VM also runs two MCP servers: DuckDuckGo search on `:8090` and the Omada network
controller on `:8091`. See [MCP servers](#mcp-servers).

## Hardware used

| Role | Machine | Notes |
|---|---|---|
| LLM inference | Ubuntu VM, GTX 1070 (8 GB), 6 vCPU | llama.cpp, Qwen 3.5 9B Q3_K_XL |
| Memory proxy | Same VM | FastAPI service, Chroma storage |
| Embeddings | Docker node, 4 vCPU, no GPU | Ollama container, `bge-large:latest` |

## Quick start

Replace `LLAMA_HOST` with the llama VM address and `DOCKER_NODE_IP` with the Docker node
address throughout.

### 1. Embeddings on the Docker node

```bash
mkdir -p ~/ollama-data
docker run -d --name ollama --restart unless-stopped \
  -p 11434:11434 \
  -e OLLAMA_KEEP_ALIVE=-1 \
  -e OLLAMA_NUM_PARALLEL=1 \
  -v ~/ollama-data:/root/.ollama \
  ollama/ollama
docker exec ollama ollama pull bge-large:latest
```

`OLLAMA_KEEP_ALIVE=-1` keeps the model resident. Without it Ollama unloads the model after
idle, and the next embedding call takes around 15 s. `OLLAMA_NUM_PARALLEL=1` serializes
requests, which avoids slowdowns when requests overlap on a small CPU.

### 2. llama.cpp on the llama VM

```bash
sudo cp llama-server/llama-server.service /etc/systemd/system/
# edit User= and Group= to your user first
sudo systemctl daemon-reload
sudo systemctl enable --now llama-server
curl -s http://127.0.0.1:8080/props | grep -o '"n_ctx":[0-9]*'   # expect 32768
```

### 3. Memory proxy on the llama VM

```bash
sudo mkdir -p /opt/mem0-proxy && sudo chown "$USER" /opt/mem0-proxy
cp proxy/proxy.py proxy/requirements.txt proxy/.env.example /opt/mem0-proxy/
cp /opt/mem0-proxy/.env.example /opt/mem0-proxy/.env     # then edit the values
cd /opt/mem0-proxy
python3 -m venv venv && venv/bin/pip install -r requirements.txt
```

Install the unit (after setting `User=` and `Group=`):

```bash
sudo cp proxy/mem0-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mem0-proxy
curl -s http://127.0.0.1:8000/health      # {"status":"ok"}
journalctl -u mem0-proxy -f
```

### 4. Point clients at the proxy

Copy `examples/opencode.json` into your opencode configuration and replace `LLAMA_HOST`.
Select the `memory` agent for memory-backed sessions. The `llama.cpp` provider still goes
straight to llama.cpp.

## MCP servers

Two MCP servers run on the llama VM as systemd services. Both give the model tools that the
base llama.cpp server does not have.

| Server | Service | Port | Transport | Purpose |
|---|---|---|---|---|
| DuckDuckGo search | `mcp-duckduckgo` | 8090 | HTTP via `mcp-proxy`, wrapping a stdio server | Web search. llama.cpp's Web UI reaches it through `--ui-mcp-proxy` |
| Omada controller | `mcp-omada` | 8091 | Streamable HTTP at `/mcp` | 82 tools for an Omada network controller: read network state and change configuration |

The llama-server unit runs with `--tools all --ui-mcp-proxy`, so the Web UI can call built-in
tools and the MCP proxy. opencode connects to the Omada server directly as a remote MCP server
(see `examples/opencode.json`). The DuckDuckGo server is run locally by opencode through
`npx` in the same example.

### Install

```bash
sudo cp mcp/mcp-duckduckgo.service /etc/systemd/system/
sudo cp mcp/mcp-omada.service /etc/systemd/system/
# Omada controller code goes in /opt/omada-mcp (built with dist/index.js).
# Credentials go in /etc/default/mcp-omada. Create it root-only:
sudo touch /etc/default/mcp-omada && sudo chmod 600 /etc/default/mcp-omada
sudo systemctl daemon-reload
sudo systemctl enable --now mcp-duckduckgo mcp-omada
ss -tlnp | grep -E ":(8090|8091)\b"
journalctl -u mcp-omada -n 10 --no-pager
```

The Omada server is a separate project. Its source is not included here. Only the systemd unit
and the env-file contract are.

### Security: the Omada server

About half of the Omada tools change the network: create or delete firewall rules, change WAN
and LAN settings, reboot devices, and change port profiles. The server has no authentication in
this setup. Keep it on localhost unless a client needs it, and firewall port 8091 to the
clients that do.

The unit in this repo binds to `127.0.0.1`. If a client on another machine needs access, restrict
the port with a firewall rule to that machine's address. Do not expose it to the whole network
without authentication.

Credentials belong in `/etc/default/mcp-omada` (mode 600, root-owned). Never put them in a
unit file or in this repository.

## Configuration

### Proxy (`proxy/.env`)

| Variable | Default | Purpose |
|---|---|---|
| `LLAMA_CPP_INTERNAL_URL` | `http://127.0.0.1:8080/v1` | llama.cpp OpenAI-compatible endpoint |
| `OLLAMA_DOCKER_NODE_URL` | `http://localhost:11434` | Ollama embedding server |
| `MEM0_EMBED_MODEL` | `bge-large:latest` | Embedding model |
| `MEM0_LLM_MODEL` | `qwen3.5-9b` | Model name sent to llama.cpp for mem0's extraction calls |
| `MEM0_USER_ID` | `default_user` | Memory scope |
| `MEM0_TOP_K` | `5` | Memories injected per request |
| `MEM0_SEARCH_TIMEOUT` | `15` | Seconds before a search is abandoned and the request is forwarded without memory |
| `MEM0_SKIP_NO_TOOLS` | `true` | Skip memory for tool-less requests (opencode title and compaction calls) |
| `MEM0_MAX_STORE_CHARS` | `4000` | Cap on text embedded per exchange |
| `MEM0_PROXY_PORT` | `8000` | Listen port |

### llama.cpp (`llama-server/llama-server.service`)

| Flag | Value | Why |
|---|---|---|
| `-m` | Qwen 3.5 9B, UD-Q3_K_XL | Quality-to-size balance for 8 GB VRAM with the context below |
| `-c` | `32768` | opencode requests reached about 24k tokens. 16k rejected them |
| `-np` | `1` | One slot. Four slots shared one KV pool and large prompts starved each other |
| `-ctk` / `-ctv` | `q8_0` | Halves KV memory, so 32k fits without moving layers to CPU. Needs flash attention |
| `--ctx-checkpoints` | `4` | Each checkpoint is about 50 MiB. The default of 32 could hold about 1.6 GB per slot |
| `-ngl` | `35` | GPU layers. Kept for generation speed |
| `--flash-attn` | `on` | Required for quantized V cache |
| `--repeat-penalty` | `1.2` | Reduces repetition loops |
| `--tools` | `all` | Enables built-in server tools for the Web UI |
| `--ui-mcp-proxy` | on | Lets the Web UI reach the MCP proxy on port 8090 |

Measured on this setup: about 22 tokens/s generation, 370-480 tokens/s prompt processing,
and 5.1 GB of 8 GB VRAM used at 32k context.

## Design decisions

| Decision | Choice | Reason |
|---|---|---|
| Memory integration | Prompt-routing proxy, not an MCP tool | Memory applies to every completion. The model does not have to decide to remember |
| Server framework | FastAPI + uvicorn | Native async and streaming. No reverse proxy needed for one upstream |
| Memory library | mem0 OSS (`Memory`) | Runs locally with no hosted service |
| Vector store | Chroma | Persistent and local, with no extra service to run |
| Embeddings | Separate CPU node | Keeps the GPU free for chat |
| Memory writes | mem0 default `add()` with LLM-extracted facts | Richer facts than raw logs |
| Context | 32k with q8 KV cache | Fits the workload on an 8 GB card |
| Reverse proxy | None | One upstream on a trusted LAN |

## Troubleshooting log

These are the real failures from building this system, with the cause and the fix.

**1. Memory search silently failing.** The proxy awaited mem0's synchronous `Memory.search()`
and `add()`. Every request raised `'dict' object can't be awaited`, which was caught and
logged, so chat kept working with no memory. *Fix:* run sync calls in a thread pool. Log
search failures at a level you actually watch.

**2. Restarted service still running old code.** A fix was deployed to disk, but the
service was not restarted, so the old process kept failing. *Fix:* after deploying,
confirm the running PID's start time is newer than the file's modification time.

**3. Internal server error in the client.** An Ollama `ConnectTimeout` during the search path
surfaced as an HTTP 500. *Fix:* bound the search with `asyncio.wait_for` and fall back to
forwarding the request without memory.

**4. Context overflow.** One chat request was 24,341 tokens against a 16,384-token context.
The memory layer was not the cause. opencode sends every tool schema on each request. *Fix:*
raise the context to 32k, and hide tools the memory agent does not need using the agent
`tools` map.

**5. Permission deny did not hide tools.** `permission` blocked tool calls but not tool
schemas, so the model still received 89 tool definitions per request. *Fix:* use the
`tools` map to remove them from the request. Verified by the proxy's `tools=N` log field.

**6. Concurrent requests exhausted the KV cache.** Four parallel slots shared one KV pool,
and large prompts failed with `failed to find free space in the KV cache`. *Fix:* `-np 1`.

**7. Model loops and verbose replies.** The memory agent dropped the strict reply rules from
the base agent, and the model produced long option lists. Compaction was also firing early
because the declared context was 16k while the server ran 32k. *Fix:* restore the rules,
cap `steps`, and align the declared context with the server.

**8. Agent acted on a reminder.** After an agent switch, the model repeated opencode's
mode-change reminder and tried to edit a file. Edit and bash were set to `ask`, which still
allowed the attempt. *Fix:* deny `edit`, `bash`, `webfetch`, and `task` for read-only agents.

**9. Memory search timeouts.** Searches shared one thread pool with background writes, and the
writes blocked on llama.cpp. *Fix:* separate pools, one writer. The 5 s limit was also too
tight for embedding latency, so it was raised to 15 s.

**10. Slow embeddings on the Docker node.** Overlapping requests on two CPU cores made each
embedding take 25-30 s. *Fix:* `OLLAMA_NUM_PARALLEL=1`, skipping memory work for tool-less
requests, and capping stored text. Moving the node from 2 to 4 vCPUs took warm embeddings
from about 1.3 s to 0.44 s.

**11. Keep-alive not applied.** After recreating a container, the environment variable was
missing, and the model unloaded after idle. *Fix:* verify with
`docker inspect ollama --format '{{.Config.Env}}'` and check `/api/ps` for `expires_at`.

## Known issues and limits

- The Omada MCP server has no authentication, and about half its tools change configuration.
  Keep it off the LAN (see the security note under MCP servers).

- The proxy's extraction step occasionally returns non-JSON from the model. mem0 logs an
  error and skips that write.
- Chroma does not support keyword search, so retrieval is semantic only.
- `cache_reuse` is disabled by llama.cpp in this configuration, so prompt reuse is limited
  to slot-level prefix caching.
- One user ID is used for all sessions.

## Roadmap

- Smaller embedding model option (`nomic-embed-text` or `bge-small`) for lower latency.
- Trim the built-in tool schemas for the memory agent to reduce prefill cost.
- Memory management: list, review, and delete stored facts.
- Route llama.ui through the proxy as well.

## Repository layout

```
proxy/
  proxy.py               FastAPI memory proxy
  requirements.txt       pinned dependencies
  .env.example           configuration template
  mem0-proxy.service     systemd unit
llama-server/
  llama-server.service   systemd unit for llama.cpp
mcp/
  mcp-duckduckgo.service systemd unit for DuckDuckGo search (mcp-proxy, port 8090)
  mcp-omada.service      systemd unit for the Omada controller MCP server (port 8091)
examples/
  opencode.json          client config: llama.cpp provider, mem0 provider, memory agent
  github-actions-deploy.yml   optional deployment workflow (not active)
```

## Model history

| Model | File | Size | Status |
|---|---|---|---|
| Llama 3.1 8B | model.gguf | 4.6 GB | Replaced. KV cache too large |
| Qwen 2.5 7B | qwen2.5-7b-q4_k_m.gguf | 4.4 GB | Replaced. Looping behavior |
| Qwen 3.5 9B | qwen3.5-9b-ud-q3_k_xl.gguf | 4.8 GB | Current |
