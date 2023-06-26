from __future__ import annotations
from abc import ABC, abstractmethod

from queue import Queue
from threading import Thread
from typing import Iterable, List, Optional

from fastchat.serve.inference import generate_stream
from fastchat.conversation import Conversation
from langchain.schema import Document
from langchain.vectorstores.base import VectorStore
from transformers import PreTrainedModel, PreTrainedTokenizerBase
from llm.qa.model_configs import ChatModelConfig
from llm.qa.prompts import ZERO_SHOT, ContextPrompt


class InferenceEngine(ABC):
    @abstractmethod
    def generate_stream(self, prompt: str, **kwargs) -> Iterable[str]:
        """
        Stream generated text as each new token is added
        """
        raise NotImplementedError()


class FastchatEngine(InferenceEngine):
    """
    Wrapper on FastChat's generate_stream that implements
    the InferenceEngine interface
    """
    def __init__(
        self,
        model: PreTrainedModel,
        tokenizer: PreTrainedTokenizerBase,
        max_length: int = 2048,
    ):
        self.model = model
        self.tokenizer = tokenizer
        self.max_length = max_length

    def generate_stream(
        self,
        prompt: str,
        **kwargs,
    ) -> Iterable[str]:
        gen_params = {
            "prompt": prompt,
            **kwargs,
        }

        output_stream = generate_stream(
            self.model,
            self.tokenizer,
            gen_params,
            self.model.device,
            context_len=self.max_length,
        )

        for outputs in output_stream:
            output_text = outputs["text"].strip()
            yield output_text


class QueuedEngine(InferenceEngine):
    """
    Run inference engines in background threads with queued requests/responses

    Enables thread-safe handling of concurrent requests to one or more engines
    """

    def __init__(self, *engines: InferenceEngine):
        self.queue: Queue[StreamRequest] = Queue()
        for engine in engines:
            self.add_watcher(engine)

    def add_watcher(self, engine: InferenceEngine) -> Thread:
        def _watch():
            while True:
                request = self.queue.get()
                for text in engine.generate_stream(request.prompt, **request.kwargs):
                    request.output.put(text)

                # None indicates to the requester that the stream is completed
                request.output.put(None)

        watch_thread = Thread(None, _watch, daemon=True)
        watch_thread.start()
        return watch_thread

    def generate_stream(
        self,
        prompt: str,
        **kwargs,
    ) -> Iterable[str]:
        request = StreamRequest(prompt, **kwargs)
        self.queue.put(request)
        while True:
            text = request.output.get()
            request.output.task_done()
            if text is not None:
                yield text
            else:
                # Stream completed
                break


class StreamRequest:
    """
    An inference request to be enqueued
    """
    def __init__(self, prompt: str, **kwargs) -> None:
        self.prompt = prompt
        self.kwargs = kwargs
        # When retrieved value is None, stream is completed
        self.output: Queue[Optional[str]] = Queue()


class QASession:
    """
    Manages session state for a question-answering conversation between a user and an AI.
    Contexts relevant to questions are retrieved from the given vector store and appended
    to the system prompt that is fed into inference.
    """
    def __init__(
        self,
        engine: InferenceEngine,
        vector_store: VectorStore,
        conv: Conversation,
        prompt: ContextPrompt = ZERO_SHOT,
        debug: bool = False,
    ):
        self.engine = engine
        self.vector_store = vector_store
        self.conv = conv
        self.prompt = prompt
        self.debug = debug
        self.results: List[Document] = []

    @classmethod
    def from_model_config(
        cls,
        model_config: ChatModelConfig,
        engine: InferenceEngine,
        vector_store: VectorStore,
        prompt: Optional[ContextPrompt] = None,
        **kwargs,
    ) -> QASession:
        conv = model_config.new_conversation()
        return cls(engine, vector_store, conv, prompt or model_config.default_prompt, **kwargs)

    def append_question(self, question: str, update_context: bool = False, **kwargs):
        self.conv.append_message(self.conv.roles[0], question)
        self.conv.append_message(self.conv.roles[1], None)
        if update_context:
            self.update_context(question, **kwargs)

    def update_context(self, question: str, top_k: int = 3, **kwargs):
        self.results = self.vector_store.similarity_search(question, top_k)
        self.set_context([r.page_content for r in self.results], **kwargs)

    def set_context(self, contexts: List[str], **kwargs):
        if "roles" in self.prompt.inputs:
            kwargs.setdefault("roles", self.conv.roles)
        self.conv.system = self.prompt.render(contexts, **kwargs)

    def conversation_stream(self, **kwargs) -> Iterable[str]:
        input_text = self.conv.get_prompt()
        params = {
            "temperature": 0.7,
            "top_p": 0.9,
            "repetition_penalty": 1.0,
            "max_new_tokens": 512,
            "echo": False,
            "stop": self.conv.stop_str,
            "stop_token_ids": self.conv.stop_token_ids,
            **kwargs,
        }
        for output_text in self.engine.generate_stream(input_text, **params):
            yield output_text

        self.conv.messages[-1][-1] = output_text.strip()
        if self.debug:
            print('**DEBUG**')
            print(input_text)
            print(output_text)

    def get_history(self) -> str:
        messages = [f'{role}: {"" if message is None else message}' for role, message in self.conv.messages]
        return "\n\n".join(messages)

    def clear(self, keep_results: bool = False):
        self.conv.messages = []
        self.conv.system = ""
        if not keep_results:
            self.results = []
