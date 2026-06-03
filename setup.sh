#!/usr/bin/env bash
set -euo pipefail

echo "=== Installing build dependencies ==="
sudo apt update -qq
sudo apt install -y cmake build-essential

echo "=== Cloning llama.cpp ==="
sudo mkdir -p /opt/llama.cpp
sudo chown "$USER:$USER" /opt/llama.cpp
git clone --depth 1 https://github.com/ggerganov/llama.cpp /opt/llama.cpp

echo "=== Building with CUDA support ==="
cd /opt/llama.cpp
cmake -B build -DGGML_CUDA=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j "$(nproc)"

echo "=== Installing service and template ==="
mkdir -p /opt/llama.cpp/models
cp "$HOME/llama-server-infra/llama3-tool-template.jinja" /opt/llama.cpp/
sudo cp "$HOME/llama-server-infra/llama-server.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable llama-server

echo "=== Done! Place a model GGUF in /opt/llama.cpp/models/ ==="
echo "Then run: sudo systemctl start llama-server"
