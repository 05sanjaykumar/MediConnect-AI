# backend/services/TTS.py
"""Kokoro text-to-speech, with speech chunked at clauses rather than sentences.

Kokoro emits nothing until a whole chunk is synthesised, and synthesis time
scales with chunk length: a 6-second sentence takes ~1.7s before the first
audio, a 1.4-second one ~0.5s. Pipecat chunks at sentence ends by default, and
the model — whatever the prompt says — will sometimes produce a 30-word
sentence. So we also cut at a comma, semicolon or dash once a chunk is long
enough, and we drop fragments that are only punctuation (the model likes to
emit ".." on its own), each of which would otherwise be a wasted TTS call.
"""

from collections.abc import AsyncIterator

from config import KOKORO_VOICE
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.utils.text.base_text_aggregator import Aggregation, AggregationType
from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator

# Cut at a clause boundary once the pending text is at least this long. Below
# this, a clause is short enough that waiting for the full sentence is fine.
CLAUSE_MIN_CHARS = 60
CLAUSE_BOUNDARIES = (",", ";", ":", "—", "–")


def _speakable(text: str) -> bool:
    """Is there anything here a voice can actually say?"""
    return any(ch.isalnum() for ch in text)


class ClauseTextAggregator(SimpleTextAggregator):
    """Sentence aggregation, plus clause cuts for long sentences."""

    async def _check_sentence_with_lookahead(self, char: str) -> Aggregation | None:
        found = await super()._check_sentence_with_lookahead(char)
        if found is not None:
            return found if _speakable(found.text) else None

        # A clause boundary followed by whitespace, with enough text queued up
        # that speaking it now beats waiting for the sentence to finish.
        if (
            char.isspace()
            and len(self._text) >= CLAUSE_MIN_CHARS
            and self._text.rstrip().endswith(CLAUSE_BOUNDARIES)
        ):
            result, self._text = self._text.rstrip(), ""
            self._needs_lookahead = False
            return Aggregation(text=result, type=AggregationType.SENTENCE)
        return None

    async def flush(self) -> Aggregation | None:
        remaining = await super().flush()
        if remaining is not None and not _speakable(remaining.text):
            return None
        return remaining


def get_tts_service():
    tts = KokoroTTSService(
        settings=KokoroTTSService.Settings(
            voice=KOKORO_VOICE,
        )
    )
    # TTSService has no constructor hook for this; it builds its aggregator in
    # __init__ and reads it from this attribute on every text frame.
    tts._text_aggregator = ClauseTextAggregator(aggregation_type=tts._text_aggregation_mode)
    return tts
