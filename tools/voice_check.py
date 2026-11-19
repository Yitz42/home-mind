"""Try Jeeves's voice on Voicebox before switching Home Assistant to it.

    python tools/voice_check.py http://192.168.1.50:17493 Jeeves

For each emotion it asks Voicebox for one sentence the way Home Mind Voice will, prints how long it took, and saves
voice_check/<emotion>.wav to listen to. Standard library only; runs on any computer that can reach Voicebox.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import wave

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind_voice"))
from bridge import DELIVERY, speakable  # noqa: E402

LINES = {
    "neutral": "Very good. The kitchen lights and fan are off.",
    "pleased": "Noted: 7 hours of sleep on Monday night. Most satisfactory.",
    "proud": "Squat, 3×5 at 195 lb: a new best. My congratulations.",
    "amused": "The wifi password is on the note in the kitchen drawer. As before, I might add.",
    "disapproving": "The furnace filter was last changed on March 3, 47 days ago. Somewhat overdue, if I may say so.",
    "concerned": "You've slept under 5 hours three nights running. Might I suggest an early night?",
    "sympathetic": "You have 3 meetings today, the first at 9:30. Do take it gently today.",
    "apologetic": "I'm afraid I have no record of the drill's whereabouts.",
    "corrected": "Quite right, my mistake. Corrected: 6 hours of sleep on Monday night.",
    "alert": "I regret to say the front door is not locked. Shall I lock it? Say yes.",
    "relieved": "Very good. The front door is locked. Thank you.",
    "warm": "All quiet for the evening: doors locked, lights low. Good night.",
}


def main(url, voice, model="tts-1"):
    os.makedirs("voice_check", exist_ok=True)
    print(f"{'emotion':<13} {'seconds':>7}  {'audio s':>7}  file")
    for emo, line in LINES.items():
        body = {"model": model, "input": speakable(line), "voice": voice, "response_format": "pcm",
                "instructions": DELIVERY[emo]}
        req = urllib.request.Request(url.rstrip("/") + "/v1/audio/speech", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        t0 = time.time()
        try:
            pcm = urllib.request.urlopen(req, timeout=120).read()
        except urllib.error.HTTPError as e:
            print(f"{emo:<13} error {e.code}: {e.read()[:200].decode(errors='replace')}")
            continue
        except OSError as e:
            sys.exit(f"Can't reach Voicebox at {url}: {e}. Is 'Allow network access' on, and the firewall open?")
        took = time.time() - t0
        path = os.path.join("voice_check", f"{emo}.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(pcm)
        print(f"{emo:<13} {took:7.2f}  {len(pcm) / 48000:7.2f}  {path}")
    print("\nThe first line includes loading the model; the rest are what Home Assistant will wait.")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    main(*sys.argv[1:4])
