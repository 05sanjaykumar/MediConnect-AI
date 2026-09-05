# backend/services/LLM.py
from config import GROQ_API_KEY, LLM_MODEL
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.services.groq.llm import GroqLLMService

from agents.prompt import build_system_prompt
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
        ),
    )


def get_llm_context(session: VoiceSession | None = None):
    """Build the conversation context with the booking tools attached.

    The session is what binds the tools to one caller — it holds who they are
    and, importantly, which doctor and slot ids have actually been offered to
    the model, so it can't book something it invented.
    """
    session = session or VoiceSession()

    context = LLMContext(
        messages=[{"role": "system", "content": build_system_prompt()}],
        tools=build_tools(session),
    )
    return LLMContextAggregatorPair(context)
