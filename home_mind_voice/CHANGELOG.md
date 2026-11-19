# Changelog

## 0.1.0
- First version: a Wyoming voice for Home Assistant that speaks through Voicebox's OpenAI-compatible endpoint
  (`/v1/audio/speech`, pcm at 24 kHz) with Home Mind's emotion as a delivery instruction, and falls back to Piper
  (or any Wyoming voice) when Voicebox is off, slow or answers with an error. Emotions come from Home Mind's
  marker (optional) or the words themselves. Announces itself to Home Assistant (Supervisor discovery). Standard
  library only.
