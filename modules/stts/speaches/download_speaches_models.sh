#!/bin/bash

function download {
  docker compose exec -i speaches huggingface-cli download $1
}

download speaches-ai/Kokoro-82M-v1.0-ONNX
download Systran/faster-distil-whisper-large-v3
download Systran/faster-whisper-large-v3
download Systran/faster-whisper-medium
download Systran/faster-whisper-small
download deepdml/faster-whisper-large-v3-turbo-ct2
download ufozone/piper-de_DE-jarvis-high

