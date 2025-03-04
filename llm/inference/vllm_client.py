import json
import os
from typing import Dict, Iterable, List, Optional, Union
from requests import Session
from llm.inference.base import InferenceEngine
from llm.inference.transformer import check_stop_str


class VLLMClient(InferenceEngine):
    """
    Client for requesting inference from a remote VLLM server.
    """

    def __init__(self, base_url: str, headers: Optional[Dict[str, str]] = None):
        self.session = Session()
        self.session.headers.update(headers)
        self.base_url = base_url

    def generate_stream(
        self,
        prompt: str,
        max_new_tokens: int = 256,
        echo_prompt: bool = False,
        stop: Union[str, List[str]] = "",
        **kwargs,
    ) -> Iterable[str]:
        return self._request(prompt, max_new_tokens, echo_prompt, stop, stream=True, **kwargs)

    def generate(
        self,
        prompt: str,
        max_new_tokens: int = 256,
        echo_prompt: bool = False,
        stop: Union[str, List[str]] = "",
        **kwargs,
    ) -> str:
        text = ""
        for t in self._request(prompt, max_new_tokens, echo_prompt, stop, stream=False, **kwargs):
            text = t
        return text

    def _request(
        self,
        prompt: str,
        max_new_tokens: int,
        echo_prompt: bool,
        stop: Union[str, List[str]],
        stream: bool = True,
        **kwargs,
    ) -> Iterable[str]:
        if not isinstance(stop, list):
            stop = [stop]
        data = {"prompt": prompt, "stream": stream, "max_tokens": max_new_tokens, "stop": stop, **kwargs}
        url = os.path.join(self.base_url, "generate")

        response = self.session.post(url, json=data, stream=stream)
        if stream:
            for line in response.iter_lines(delimiter=b"\0"):
                if line:
                    data = json.loads(line)
                    answer = self._trim_answer(prompt, data["text"][0], echo_prompt=echo_prompt)

                    _, partial_stop = check_stop_str(answer, stop)
                    if not partial_stop:
                        # Don't yield a partially completed stop string
                        yield answer
        else:
            data = response.json()
            yield self._trim_answer(prompt, data["text"][0], echo_prompt=echo_prompt)

    def _trim_answer(self, prompt: str, answer: str, echo_prompt: bool = False) -> str:
        if not echo_prompt:
            if answer.startswith(prompt):
                return answer[len(prompt):]
        return answer
