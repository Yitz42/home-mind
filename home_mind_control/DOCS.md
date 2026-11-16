# Home Mind Control Model

Runs the small house-control model you trained for Home Mind (see "A small local model for house control" in Home Mind's documentation) with llama.cpp, on the CPU. A GPU isn't needed (and Home Assistant OS can't give add-ons an NVIDIA GPU anyway).

## Setup
1. Train the model in Colab with `tools/finetune_functiongemma.ipynb` and download `home-mind-control.gguf` (about 300 MB).
2. Copy it into Home Assistant's share folder as `home_mind/home-mind-control.gguf`, with the Samba share or File editor add-on.
3. Start this add-on. The log says "Starting ..." when the model is loaded; until the file is there it waits and says so.
4. Note the **Hostname** on this add-on's Info page (it ends in `home-mind-control`).
5. In Home Mind's options, add the model and turn it on:
   ```yaml
   models:
     - name: control
       type: openai
       url: http://HOSTNAME:8081/v1
   control_model: control
   ```

The server is only reachable from inside Home Assistant (no port is opened on your network). After you retrain (new devices, renamed devices), replace the file and restart this add-on.

## Options
| option | default | what |
|---|---|---|
| `model_file` | `/share/home_mind/home-mind-control.gguf` | the model file |
| `threads` | 4 | CPU threads it may use |
| `context` | 4096 | context length in tokens |
