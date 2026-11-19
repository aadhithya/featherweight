"""Conversion between the messages of a client (AG-UI) and the messages of the model (LangChain)."""

from ag_ui.core import Message
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage


def to_langchain(messages: list[Message]) -> list[BaseMessage]:
    """The client's history as LangChain messages. For now only text from the user and the assistant: tool calls, tool
    results and reasoning in the history are refused (see `check_supported`)."""
    converted: list[BaseMessage] = []
    for message in messages:
        content = getattr(message, "content", None) or ""
        if not isinstance(content, str):
            raise ValueError("Only text content can be converted yet")
        if message.role == "user":
            converted.append(HumanMessage(content=content, id=message.id))
        elif message.role == "assistant":
            converted.append(AIMessage(content=content, id=message.id))
        else:
            raise ValueError(f"A {message.role} message cannot be converted yet")
    return converted


def check_supported(messages: list[Message]) -> None:
    """Raise `ValueError` for a message that `to_langchain` cannot convert yet."""
    for index, message in enumerate(messages):
        if message.role not in ("user", "assistant"):
            raise ValueError(
                f"messages[{index}] is a {message.role} message: only user and assistant messages are supported "
                "in the history for now"
            )
        if not isinstance(message.content or "", str):
            raise ValueError(f"messages[{index}] has content that is not text: only text is supported for now")
        if message.role == "assistant" and message.tool_calls:
            raise ValueError(f"messages[{index}] has tool calls: tool calls in the history are not supported yet")
