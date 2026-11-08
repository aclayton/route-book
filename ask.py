"""The conversational layer — M5.

One sentence in, one tool call out. Two parsers do the work, and the order
between them is the whole design:

1. **`nl.parse_request` first, always.** Deterministic Python, no network, no
   model. It handles the phrasings operators actually use, and because it is
   pure code it can be tested exhaustively. Most requests never leave the machine.
2. **The model only when the rules come up empty.** And when it runs, it is asked
   for *structure*, never for arithmetic.

## What goes to the model, and what must never

This is the part that matters, because the obvious implementation ships a
customer database to a third party.

- **Sent:** the operator's own sentence, the list of intent names, and the list
  of date *words* we already recognise.
- **Never sent:** client names, addresses, cities, phone numbers, appointment
  times, or the customer list in any form. Not as context, not as candidates,
  not to help disambiguate.

That last one is the tempting mistake. Asking the model "did you mean Bala
Construction or Bala Septic?" would resolve the ambiguity neatly and hand a
customer list to a model provider to do it. So the model returns the *string the
operator said* — "bala" — and Python resolves it against the local database
afterwards. Ambiguity is settled **locally**, where the names already are.

## The model never does arithmetic, and never learns the date

Every date arrives as an expression — "next Thursday", "in two weeks" — passed
**verbatim** to `nl.parse_when`, which resolves it in Python against the real
today. The model is never asked what day it is, and is never asked to compute a
date, because a model that is wrong about Tuesday is wrong silently.

The one job the model has: turn a sentence the rules failed on into the same
structure the rules produce. Then it is discarded.
"""

from __future__ import annotations

import json

from . import config, nl

# The intents a sentence can carry. Deliberately the same names the tools use —
# one vocabulary, so a new intent is a new tool and never a translation table.
# The intent names are nl.parse_verb's OWN words, plus the two it has no words
# for. An earlier version invented a parallel vocabulary ("day", "week",
# "list_clients") and then had to translate between them — a translation table is
# a second place to be wrong, so there is only one vocabulary.
#
#   book | recommend | list | move | cancel   — what parse_verb returns
#   add_client | export_calendar              — added to nl.py for these
INTENTS = (
    "book",
    "recommend",
    "list",
    "move",
    "cancel",
    "add_client",
    "export_calendar",
    "unknown",
)

# The instruction sent with every parse. Note what it does NOT contain: no
# customer data, no date, no hint of the current day.
_SYSTEM = """You convert one sentence from a tradesperson into JSON.

Return ONLY a JSON object with these keys:
  "intent": one of {intents}
  "client": the customer name exactly as the speaker said it, or null
  "when":   the time expression exactly as the speaker said it, verbatim, or null
  "duration": the time expression exactly as spoken, or null

Rules you must follow:
- Copy "when" and "duration" word-for-word. Never convert, normalise, resolve or
  guess a date. If they said "next Thursday", the value is "next Thursday".
- Never invent a customer name. If none was said, use null.
- Do not answer the request. Only classify it.
- If nothing fits, use "unknown".

Examples:
"bala, next Thursday, 3 hrs" ->
{{"intent":"book","client":"bala","when":"next Thursday","duration":"3 hrs"}}

"recommend the best day for bala in two weeks" ->
{{"intent":"recommend","client":"bala","when":"in two weeks","duration":null}}

"what does my week look like" ->
{{"intent":"week","client":null,"when":null,"duration":null}}"""


def _schema():
    """The structure we require back. Kept strict so a meandering reply fails
    loudly rather than being coerced into something plausible."""
    return {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": list(INTENTS)},
            "client": {"type": ["string", "null"]},
            "when": {"type": ["string", "null"]},
            "duration": {"type": ["string", "null"]},
        },
        "required": ["intent"],
    }


def model_sentence(text):
    """Build the exact string sent to the model.

    A separate function so a test can assert on it directly. If a customer name
    ever reaches the model, this function is where it happened, and the test that
    guards this is the most important one in the file.
    """
    return _SYSTEM.format(intents=", ".join(INTENTS))


def classify_with_model(text, ctx):
    """Ask the model to classify a sentence the rules could not.

    Returns a dict in the same shape `nl.parse_request` produces, or None if
    there is no model, the call fails, or the reply is unusable. Every failure
    path returns None rather than raising: a conversational layer that dies when
    the model is unavailable is worse than one that falls back.
    """
    llm = _llm_handle(ctx)
    if llm is None:
        return None

    try:
        raw = llm.complete_structured(
            prompt=text,
            system=model_sentence(text),
            schema=_schema(),
        )
    except Exception:
        # No model is a normal state, not an error condition — the plugin works
        # fully offline and this layer is an enhancement.
        return None

    data = _coerce(raw)
    if not isinstance(data, dict):
        return None

    intent = data.get("intent")
    if intent not in INTENTS or intent == "unknown":
        return None

    # The model's job ended at structure. Client text stays a string; resolving
    # it to a real customer happens locally, after this function.
    return {
        "verb": intent,
        "client_text": _clean_field(data.get("client")),
        "when_text": _clean_field(data.get("when")),
        "duration_text": _clean_field(data.get("duration")),
        "source": "model",
    }


def _clean_field(value):
    """Keep the model's string verbatim, but reject obvious junk.

    A model that returns the literal string "null" or an empty string is common
    enough to handle here rather than downstream.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "n/a", "unknown"}:
        return None
    return text


def _coerce(raw):
    """Accept a dict, a JSON string, or an object with a nested payload."""
    if isinstance(raw, dict):
        # Some providers wrap the structured result.
        for key in ("data", "result", "arguments", "output"):
            if key in raw and isinstance(raw[key], dict):
                return raw[key]
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return None


def _llm_handle(ctx):
    """Pull the LLM handle off the context, or None.

    Reached through getattr at every step: the plugin must install and run on a
    host with no model configured, and `ctx.llm` may legitimately be absent.
    """
    if ctx is None:
        return None
    llm = getattr(ctx, "llm", None)
    if llm is None or not hasattr(llm, "complete_structured"):
        return None
    return llm


def understand(text, today, clients, ctx=None):
    """One sentence in, one resolved intent out.

    The rules run first and win wherever they can. The model is asked only about
    the leftovers, and its answer is resolved locally exactly as a rule-based one
    would be — so both paths converge on the same structure and the rest of the
    system cannot tell which was used, except by `source`.
    """
    parsed = nl.parse_request(text, today, clients)
    parsed["source"] = "rules"

    # The rules handled it: a verb and whatever slots the sentence carried.
    if parsed["verb"] != "unknown":
        return parsed

    # No verb. Either the rules found nothing at all, or they resolved a client
    # and a date but could not tell what to do with them — which parse_request
    # already converts to a booking. So reaching here means genuinely unparsed.
    model = classify_with_model(text, ctx)
    if model is None:
        return parsed

    # Resolve the model's strings locally, using the same helpers the rules use.
    # This is the point where a name becomes an ID, and it happens on this
    # machine against this machine's database.
    if model.get("client_text"):
        matches = nl.find_named_clients(model["client_text"], clients)
        if matches:
            parsed["clients"] = [dict(c) for c in matches]

    if model.get("when_text"):
        when_text = model["when_text"]
        # A model that resolves a date instead of passing it through has done the
        # exact thing it was told not to do, and its arithmetic is unverifiable.
        # If it hands back an ISO date the operator never said, we cannot tell an
        # honest conversion from an invention — so the expression is refused and
        # the operator is asked. "Next Thursday" misheard is a question; a wrong
        # Thursday is a customer standing at a locked door.
        if _looks_resolved(when_text) and not _looks_resolved(text):
            parsed["date_distrusted"] = when_text
        else:
            when = nl.parse_when(when_text, today)
            if when is not None:
                parsed["when"] = when.isoformat()
                # Keep the operator's own words, so a misread is visible in the
                # confirmation rather than hidden behind a resolved date.
                parsed["when_text"] = when_text

    if model.get("duration_text"):
        minutes = nl.parse_duration(model["duration_text"])
        if minutes:
            parsed["duration_min"] = minutes

    parsed["verb"] = model["verb"]
    parsed["source"] = "model"
    return parsed


_ISO_RE = r"\d{4}-\d{2}-\d{2}"


def _looks_resolved(text):
    """Does this string look like a date someone already worked out?"""
    import re
    return bool(re.search(_ISO_RE, text or ""))


def missing_slots(parsed):
    """Which slots an intent needs but does not have.

    Returned as field names so the caller can ask ONE question about the most
    important gap, rather than interrogating the operator or guessing. Guessing
    is the failure mode that matters: a wrong booking is worse than a question.
    """
    verb = parsed.get("verb")
    out = []

    # A list request needs no slots: a date narrows it to a day, a client to a
    # person, and neither means the week.

    if verb in {"book", "move", "cancel"} and not parsed.get("clients"):
        out.append("client")
    if parsed.get("date_distrusted"):
        # The model supplied a date the operator never said. Ask.
        out.append("when")
    if verb in {"book"} and not parsed.get("when"):
        out.append("when")
    if verb in {"move"} and not parsed.get("when"):
        out.append("when")
    if verb in {"add_client"} and not parsed.get("client_text") and not parsed.get("clients"):
        out.append("client")

    return out


def clarify(parsed, missing):
    """The one question worth asking, phrased in the operator's own terms."""
    verb = parsed.get("verb")
    first = missing[0]
    when_text = parsed.get("when_text")

    if first == "client":
        if verb == "cancel":
            return "Which client's appointment should I cancel?"
        if verb == "move":
            return "Which client's appointment should I move?"
        return "Which client is this for?"

    if first == "when":
        if parsed.get("date_distrusted"):
            return ("I could not read the date from your sentence, so I am not going "
                    "to guess at one. What day did you mean?")
        if when_text:
            # The date was said but not understood. Echo it and admit the doubt
            # rather than defaulting to today.
            return f'I could not work out the date from "{when_text}". What day did you mean?'
        if verb == "move":
            return "What day should I move it to?"
        return "What day is this for?"

    return "I did not catch that. Could you say it again?"
