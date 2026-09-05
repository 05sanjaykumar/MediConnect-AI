# backend/services/LLM.py
from config import GROQ_API_KEY, LLM_MODEL, LLM_REASONING_EFFORT, USER_SPEECH_TIMEOUT
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.groq.llm import GroqLLMService
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from agents.prompt import build_system_prompt, greeting_for
from agents.tools import VoiceSession, build_tools


def get_llm_service():
    return GroqLLMService(
        api_key=GROQ_API_KEY,
        settings=GroqLLMService.Settings(
            model=LLM_MODEL,
            # Lower than the original 0.7: this agent reads out appointment times
            # and doctors' names, where invention is a bug rather than character.
            temperature=0.3,
            top_p=0.9,
            max_completion_tokens=1024,
            # gpt-oss models reason before answering. On a phone call that
            # reasoning is dead air. "low" keeps it short; this is the setting
            # config.py declared but the service was never actually given.
            extra={"reasoning_effort": LLM_REASONING_EFFORT},
        ),
    )


def get_llm_context(session: VoiceSession | None = None):
    """Build the conversation context with the booking tools attached.

    The session is what binds the tools to one caller — it holds who they are
    and, importantly, which doctor and slot ids have actually been offered to
    the model, so it can't book something it invented.

    Turn-taking uses a plain speech timeout rather than the default Smart Turn
    model. Smart Turn fired on "I want to see" before "neurologist" arrived and
    sent a half-sentence to the LLM. A fixed pause is duller but predictable,
    which is what a demo needs.
    """
    session = session or VoiceSession()

    context = LLMContext(
        # The prompt says who is calling — confirm a known name, ask for an
        # unknown one — so the session has to be built before the prompt is.
        messages=[{"role": "system", "content": build_system_prompt(session)}],
        tools=build_tools(session),
    )
    # The greeting is spoken by TTS the moment the call connects (routes/audio.py),
    # with no model call. Record it here as the assistant's first line so the
    # model knows it has already introduced itself and used the caller's name.
    context.add_message({"role": "assistant", "content": greeting_for(session)})
    user_params = LLMUserAggregatorParams(
        user_turn_strategies=UserTurnStrategies(
            stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=USER_SPEECH_TIMEOUT)],
        ),
    )
    return LLMContextAggregatorPair(context, user_params=user_params)
