"""What kind of fact a note is, and how much it matters (#38, #75), decided by rules when it is filed.

  category  safety, identity, correction, preference, device, episode or note. Each fades at its own pace: safety,
            identity and corrections never; preferences 3 times and devices 4 times more slowly than notes
            (option fade_days); episodes at the base pace.
  importance 3 for safety (an allergy, the gas or water shutoff, medication, an emergency contact), 2 for identity,
            preferences and devices, 1 otherwise. Search leans toward more important notes a little, and
            importance 3 never fades.
"""
import re

SAFETY = re.compile(r"\b(?:allerg\w*|anaphyla\w*|epi-?pen|inhaler|insulin|seizure\w*|diabet\w*|medication|meds|"
                    r"dosage|prescription|gas (?:shut-?off|valve|leak)|water (?:shut-?off|main)|main (?:valve|shutoff)|"
                    r"shut-?off valve|breaker (?:box|panel)|fuse box|carbon monoxide|smoke (?:alarm|detector)|"
                    r"fire extinguisher|first aid|poison control|emergency (?:contact|number|exit|kit)|"
                    r"blood type|pacemaker|dnr)\b", re.I)
IDENTITY = re.compile(r"\b(?:my name is|named after|was born|birthday|date of birth|lives? (?:at|in)|"
                      r"moved (?:here|to)|is my (?:wife|husband|partner|son|daughter|mother|father|mom|dad|sister|brother)|"
                      r"works? (?:at|as|for)|job is|nickname)\b", re.I)
PREFERENCE = re.compile(r"\b(?:likes?|loves?|hates?|prefers?|favou?rite|can't stand|doesn't like|don't like|"
                        r"enjoys?|rather have|is into|allergic to)\b", re.I)
DEVICE = re.compile(r"\b(?:model (?:number|no)|serial|firmware|mac address|ip address|router|modem|wifi|"
                    r"warranty|dishwasher|washer|dryer|fridge|refrigerator|furnace|boiler|thermostat|water heater|"
                    r"printer|tv|television|battery|batteries|filter)\b", re.I)

FADE_X = {"safety": None, "identity": None, "correction": None, "preference": 3, "device": 4, "episode": 1, "note": 1}
IMPORTANCE = {"safety": 3, "identity": 2, "preference": 2, "device": 2}


def categorize(text, kind="note", source=""):
    """(category, importance) for a note being filed."""
    t = text or ""
    if source == "correction":
        cat = "correction"
    elif SAFETY.search(t):
        cat = "safety"
    elif IDENTITY.search(t):
        cat = "identity"
    elif PREFERENCE.search(t):
        cat = "preference"
    elif DEVICE.search(t):
        cat = "device"
    elif kind == "episode":
        cat = "episode"
    else:
        cat = "note"
    return cat, IMPORTANCE.get(cat, 1)
