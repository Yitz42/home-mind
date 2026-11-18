"""The router: which model answers, by how hard the question is and how private it is.

    simple  ("where's the drill?", "what's the wifi password?")      -> a local model first (the GPU computer)
    complex ("why", compare, plan, summarise, explain; long or multi-part)  -> the cloud model first
    private (health, body, money, a personal note among the hits)   -> local first, whatever its difficulty

Only when no model is named (the Ask box, Telegram, a voice agent that leaves it to Home Mind). A computer that's
away is moved to the back as before, so "local first" quietly becomes "cloud" while the GPU laptop sleeps. If the
local model says it doesn't know although good notes were found, the next model gets one try.
"""
import re

COMPLEX = re.compile(r"\b(?:why|explain|compare|comparison|versus|vs\.?|pros and cons|plan|planning|schedule|summari[sz]e|"
                     r"summary|analy[sz]e|analysis|recommend|suggest|should i|what if|difference between|pattern|"
                     r"trend|overview|in detail|step by step|walk me through|write|draft|rewrite)\b", re.I)
PRIVATE = re.compile(r"\b(?:sleep|slept|weight|weigh|body fat|protein|calories|meal|ate|diet|workout|exercise|squat|"
                     r"bench|deadlift|run|ran|steps|readiness|hrv|heart|blood|pressure|medic\w*|doctor|symptom|pain|"
                     r"sick|ill|fog|tired|mood|anxious|stress|therap\w*|pill|dose|prescription|salary|bank|money|"
                     r"debt|password|pin|code|secret)\b", re.I)


def classify(question, hits=None, voice=False):
    """('simple' | 'complex', private, why)"""
    q = (question or "").strip()
    words = len(q.split())
    why = []
    hard = 0
    if COMPLEX.search(q):
        hard += 2
        why.append("asks for reasoning")
    if words > 25:
        hard += 1
        why.append("long")
    if len(re.findall(r"\b(?:and|also|then)\b|[;,]", q)) >= 3:
        hard += 1
        why.append("several parts")
    if voice and hard < 3:
        hard -= 1                                   # spoken: short answers; speed matters more
    private = bool(PRIVATE.search(q)) or any((h.get("scope") == "personal") or "body/" in (h.get("shelf") or "")
                                              for h in (hits or [])[:3])
    if private:
        why.append("private")
    return ("complex" if hard >= 2 else "simple"), private, why


def route(question, hits, local, cloud, voice=False):
    """The order to ask models in, and why."""
    level, private, why = classify(question, hits, voice)
    if private or level == "simple":
        order = list(local) + [m for m in cloud if m not in local]
    else:
        order = list(cloud) + [m for m in local if m not in cloud]
    return order, {"level": level, "private": private, "why": why}
