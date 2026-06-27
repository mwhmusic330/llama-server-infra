# llama-server-infra

llama.cpp server infrastructure for a GTX 1070 (8GB) Ubuntu VM.

## Files

- **llama-server.service** — systemd unit with optimized flags for Qwen 2.5 7B
- **mcp-duckduckgo.service** — systemd unit for DuckDuckGo web search MCP
- **setup.sh** — one-shot bootstrap script for a fresh machine

## Flags

| Flag | Value | Why |
|---|---|---|
| `--ctx-size` | 16384 | Fits 8GB VRAM with Qwen 2.5 7B (~4.4GB weights) |
| `--parallel` | 2 | One slot for chat, one for background agents |
| `--cont-batching` | on | Smart scheduling across slots |
| `--flash-attn on` | Q8_0 KV cache | Halves KV cache memory usage |
| `--cache-reuse` | 256 | Reuses KV cache for similar prompts |
| `--threads` | 6 | Match 6 vCPUs for prompt processing |
| `--threads-batch` | 6 | Same for batch processing |
| `--tools all` | on | Enables built-in server tools (read_file, etc.) |
| `--ui-mcp-proxy` | on | CORS proxy for MCP connections |
| `--repeat-penalty` | 1.1 | Prevents repetition loops |
| `--frequency-penalty` | 0.01 | Discourages token frequency loops |
| `-ngl` | 99 | Offload all layers to GPU |
| `--chat-template-file` | Qwen-Qwen2.5-7B-Instruct.jinja | Qwen 2.5 tool calling format |

## Usage

```
sudo cp llama-server.service /etc/systemd/system/
sudo cp mcp-duckduckgo.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now llama-server
sudo systemctl enable --now mcp-duckduckgo
```

## Architecture

```
Web UI (port 8080) -> llama-server -> Qwen 2.5 7B
                            |
                 mcp-proxy (port 8090)
                        |
              duckduckgo-websearch (stdio)
```

## Notes

- Qwen 2.5 7B Q4_K_M uses ~4.4GB VRAM (vs ~5.5GB for Llama 3.1 8B)
- DuckDuckGo search requires no API key
- MCP servers are configured via web UI at Settings -> MCP Servers
