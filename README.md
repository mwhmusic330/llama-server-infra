# llama-server-infra

llama.cpp server infrastructure for a GTX 1070 (8GB) Ubuntu machine.

## Files

- **llama-server.service** — systemd unit with tuned flags for 8GB VRAM
- **llama3-tool-template.jinja** — chat template with Llama 3.1 tool call support
- **setup.sh** — one-shot bootstrap script for a fresh machine

## Flags

| Flag | Value | Why |
|---|---|---|
| `--ctx-size` | 16384 | Fits 8GB VRAM at Q4_K_M (~5.5GB weights + ~2GB KV) |
| `--parallel` | 1 | Single slot to keep KV cache under VRAM limit |
| `--repeat-penalty` | 1.1 | Prevents repetition loops |
| `--frequency-penalty` | 0.01 | Discourages token frequency loops |
| `-ngl` | 99 | Offload all layers to GPU |
| `--chat-template-file` | (custom) | Proper Llama 3.1 tool calling format |

## Usage

```bash
sudo cp llama-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now llama-server
```
