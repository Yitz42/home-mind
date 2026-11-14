# Home Mind

A brain for small models, as a Home Assistant app: memory with versions and shelves, connections to local
models (Ollama, OpenAI-compatible), Claude (Home Assistant AI Task or the API), an MCP endpoint for Claude,
knowledge packs that update, and random tests that measure how well it adapts to new information and finds what
it has organized.

## Install

Settings › Apps › App store › ⋮ › Repositories, add `https://github.com/Yitz42/home-mind`, then install
**Home Mind**. See [DOCS](home_mind/DOCS.md).

## Layout

- `home_mind/` the app (add-on): `rootfs/app/homemind` is the Python service, `rootfs/app/bridge` the
  Home Assistant integration it installs
- `packs/` knowledge packs and the update manifest
- `tests/` tests that run without Home Assistant
