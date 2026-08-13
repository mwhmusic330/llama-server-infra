# llama-server-infra

llama.cpp server infrastructure for a GTX 1070 (8GB) Ubuntu VM.

## Files

- **llama-server.service** — systemd unit with optimized flags for Qwen 3.5 9B
- **mcp-duckduckgo.service** — systemd unit for DuckDuckGo web search MCP
- **setup.sh** — one-shot bootstrap script for a fresh machine

## Flags

| Flag | Value | Why |
|---|---|---|
| `-m` | qwen3.5-9b-ud-q3_k_xl.gguf | Qwen 3.5 9B Unsloth Dynamic Q3_K_XL (~4.8GB) |
| `-c` | 16384 | Context for coding agent tasks (fits 8GB VRAM) |
| `--cont-batching` | on | Smart scheduling |
| `--flash-attn on` | — | Halves KV cache memory usage |
| `--temp` | 0.7 | Balanced temperature for natural output |
| `--top-p` | 0.95 | Nucleus sampling for quality/speed |
| `--repeat-penalty` | 1.1 | Prevents repetition loops |
| `--frequency-penalty` | 0.3 | Penalizes repeated patterns |
| `--threads` | 6 | Match 6 vCPUs |
| `--threads-batch` | 6 | Same for batch processing |
| `--tools all` | on | Enables built-in server tools |
| `--ui-mcp-proxy` | on | CORS proxy for MCP connections |
| `-ngl` | 35 | Offload ~35 layers to GPU (fits 8GB VRAM) |
| `--cache-reuse` | 256 | Reuses KV cache for similar prompts |

## Usage

sudo cp llama-server.service /etc/systemd/system/
sudo cp mcp-duckduckgo.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now llama-server
sudo systemctl enable --now mcp-duckduckgo

## Architecture

Web UI (port 8080) -> llama-server -> Qwen 3.5 9B
                            |
                 mcp-proxy (port 8090)
                        |
              duckduckgo-websearch (stdio)

## Client config (opencode)

Model behavior is controlled client-side via agent prompts. Add to opencode.json:

"provider": {
  "llama.cpp": {
    "npm": "@ai-sdk/openai-compatible",
    "name": "llama-server (local)",
    "options": {
      "baseURL": "http://192.168.1.8:8080/v1"
    },
    "models": {
      "model": {
        "name": "Qwen 3.5 9B Instruct (local)",
        "tools": true,
        "limit": {
          "context": 16384,
          "output": 4096
        },
        "options": {
          "chat_template_kwargs": {
            "enable_thinking": false
          }
        }
      }
    }
  }
},
"agent": {
  "local": {
    "prompt": "You are Qwen, a concise assistant. CRITICAL RULES: 1. Never ask questions or offer options. 2. Never suggest next steps. 3. Never call tools unless explicitly instructed. 4. Answer once, then stop completely. 5. Do not say Would you like me to or any variation. 6. Your response must be a final answer, not a conversation starter."
  }
}

Launch with: opencode --agent local

## Model history

| Model | File | Size | Notes |
|---|---|---|---|
| Llama 3.1 8B | model.gguf (old) | 4.6 GB | Replaced — too large KV cache |
| Qwen 2.5 7B | qwen2.5-7b-q4_k_m.gguf | 4.4 GB | Replaced — looping behavior |
| Qwen 3.5 9B | qwen3.5-9b-ud-q3_k_xl.gguf | 4.8 GB | Current |

## Known issues

- **Build b1-63e66fd**: --ctx-size/-c does not reliably override the model's embedded n_ctx. Requesting -c 32768 only allocates ~16384 — possibly VRAM-capped or silently clamped. Workaround: use -c and verify with /v1/models. Rebuilding from latest source may resolve this.

- **No --system-prompt flag** in build b1-63e66fd. Model behavior customization must be done client-side (opencode agent.prompt, AGENTS.md, or instructions).

- **Qwen 3.5 thinking mode**: Outputs to reasoning_content field by default. Must disable via chat_template_kwargs: {enable_thinking: false} in client config to get direct content responses.

## Performance

| Metric | Value |
|---|---|
| Generation speed | ~23-25 tok/s (GTX 1070, 9B Q3_K_XL) |
| GPU VRAM usage | ~5.2 GB / 8 GB |
| Context window | 16384 tokens |
| GPU layers offloaded | 35 / ~90 |
