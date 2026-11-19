# Home Mind Voice

Jeeves's voice from **Voicebox** running on another computer (one with an NVIDIA GPU), with Home Mind's emotions,
and **Piper** whenever that computer is off, asleep, restarting or slow. Home Assistant sees one voice; you pick
it in your voice assistant and nothing else changes.

How each answer is spoken:
1. **The emotion:** the marker Home Mind gave that answer (with *Home Mind address* set), or read from the words
   ("I'm afraid" is apologetic, "My congratulations" is proud, "Say yes" is alert).
2. **Voicebox** speaks it in your voice profile, with the emotion as a delivery instruction ("Speak gently, with
   quiet concern, a little slower").
3. If Voicebox doesn't answer within *Wait for Voicebox*, **Piper** speaks the same words. After Voicebox is
   unreachable, Piper answers straight away until *try again after* has passed.

The wording is Home Mind's, fact-checked; Voicebox only supplies the voice. (Voicebox's own text-rewriting
"personality" isn't used.)

## 1. On the Voicebox computer
1. Install Voicebox (voicebox.sh) and open it.
2. **Models:** download **Qwen VoiceDesign 1.7B**. It's the engine that both designs a voice from a description
   and follows delivery instructions. (Cloned voices use the base Qwen model, which ignores instructions: they
   work, but without emotions.)
3. **Voices › New:** name it **Jeeves**, engine VoiceDesign, language English, and describe him, for example:
   > A middle-aged English butler with a refined Received Pronunciation accent. A calm, low, measured baritone;
   > precise, crisp diction; unhurried and composed, with a faint dry warmth.

   Generate a few lines and adjust the description until he sounds right.
4. **Settings › Server:** turn on **Allow network access** and **Keep server running when app closes**, then
   restart Voicebox.
5. **Windows:**
   - Give the computer a fixed address in your router.
   - Allow TCP port 17493 in Windows Firewall for your home network only (Private profile). Voicebox has no
     password, so it must never be reachable from outside.
   - In Power settings: when plugged in, never sleep; closing the lid does nothing.
   - On a laptop, Dell Power Manager › Battery: **Primarily AC use**, so the battery isn't held at 100%.

Check it from any computer with Python (this repo's `tools/voice_check.py`):

    python tools/voice_check.py http://<voicebox-computer>:17493 Jeeves

It times one sentence per emotion and saves them as WAV files to listen to.

## 2. In Home Assistant
1. Install the **Piper** app too (the fallback), with a British voice such as `en_GB-northern_english_male-medium`.
2. Install **Home Mind Voice** and set *Voicebox address* (e.g. `http://192.168.1.50:17493`). Optionally set
   *Fallback voice name* to the Piper voice above. Start it.
3. Home Assistant offers the new Wyoming voice under **Settings › Devices & services** (discovered). Add it. If it
   doesn't appear, add the **Wyoming Protocol** integration by hand with the host and port the app's log names.
4. **Settings › Voice assistants ›** your assistant **› Text-to-speech:** choose **Home Mind Voice**, voice
   **Jeeves**.
5. Optional, for the exact emotion Home Mind chose rather than one read from the words: set *Home Mind address*
   (Home Mind's direct API, e.g. `http://homeassistant.local:8099`) and a read token.

## Options
| Option | Default | |
|---|---|---|
| `voicebox_url` | empty | the Voicebox computer; empty = Piper only |
| `voice` | Jeeves | the Voicebox voice profile |
| `model` | tts-1 | the profile's own engine; or a model from Voicebox's `/v1/models` |
| `language` | en_GB | shown to Home Assistant |
| `emotions` | on | send the emotion as a delivery instruction |
| `timeout_seconds` | 10 | longer than this and Piper speaks |
| `retry_seconds` | 60 | after Voicebox is unreachable, Piper answers at once until this has passed |
| `speed` | 1.0 | Voicebox stretches the audio without changing pitch |
| `fallback_host` / `fallback_port` / `fallback_voice` | core-piper / 10200 / empty | the fallback Wyoming voice |
| `homemind_url` / `homemind_token` | empty | ask Home Mind for the emotion it marked |

## Emotions and their delivery
| Emotion | Delivery instruction |
|---|---|
| neutral | Speak calmly and evenly, at a measured pace. |
| pleased | Speak with quiet, understated satisfaction. |
| proud | Speak warmly, with restrained pride, as if offering congratulations. |
| amused | Speak with dry, deadpan understatement and the faintest hint of amusement. |
| disapproving | Speak with polite, restrained disapproval, a little slower. |
| concerned | Speak gently, with quiet concern, a little slower. |
| sympathetic | Speak softly and kindly, slowly and gently. |
| apologetic | Speak in a polite, apologetic tone. |
| corrected | Speak graciously and briskly, acknowledging a small mistake. |
| alert | Speak firmly and clearly, formal and brisk, with no humour. |
| relieved | Speak with calm, understated relief. |
| warm | Speak warmly and softly, relaxed, as in the evening. |

## Voices and permission
Use a designed voice (above), a Voicebox preset, your own voice, or a recording of someone who has agreed. Don't
clone an actor or anyone else without their permission.
