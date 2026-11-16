#!/bin/sh
# Start llama.cpp's server on the model file named in the options. If the file isn't there yet (not copied to
# /share), say so and wait for it instead of failing over and over.
OPT=/data/options.json
opt() { sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" "$OPT" | head -n 1; }
MODEL=$(opt model_file)
THREADS=$(opt threads)
CTX=$(opt context)
MODEL=${MODEL:-/share/home_mind/home-mind-control.gguf}
while [ ! -f "$MODEL" ]; do
  echo "Waiting for the model file $MODEL. Copy home-mind-control.gguf (from the Colab notebook) into the share"
  echo "folder, under home_mind/, with the Samba share or File editor add-on. Checking again in 60 seconds."
  sleep 60
done
SERVER=$(command -v llama-server || echo /app/llama-server)
echo "Starting $SERVER on $MODEL ($THREADS threads, context $CTX)"
# --jinja: use the model's own chat template, which knows how FunctionGemma reads tools and writes calls.
# One slot, so the long tool list stays cached between requests and each request only reads the new words.
exec "$SERVER" -m "$MODEL" --jinja --host 0.0.0.0 --port 8081 -c "${CTX:-4096}" -t "${THREADS:-4}" -np 1
