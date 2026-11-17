# Changelog

## 0.1.1
- No watchdog: it waits (without serving) until the model file is copied in, and Home Assistant's health check restarted it over and over meanwhile.

## 0.1.0
- First version: llama.cpp's server for the FunctionGemma model fine-tuned on your house, on the CPU, internal network only. Waits for the model file instead of failing.
