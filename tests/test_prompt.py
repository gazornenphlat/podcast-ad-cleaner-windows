"""The built-in prompt: examples and rules the AI sees."""
from podcast_cleaner import analyze


def test_prompt_has_the_three_examples():
    t = analyze.PROMPT_TEMPLATE.format(extra="", transcript="X", show="", stars="", types="t", segment_rules="")
    assert t.index("Example - an ad:") < t.index("Example - an ad with no code") < t.index("Example - not an ad:")
    assert '"sponsor": "Acme VPN", "type": "sponsor", "confidence": 0.6' in t     # medium confidence, valid JSON
    assert "medium confidence (0.5-0.7)" in t


import json

from podcast_cleaner import lmstudio


def _ollama_cfg(url, **kw):
    return {"url": url, "model": "qwen3.6:27b", "temperature": 0.1, "num_ctx": 8192, "timeout_seconds": 30, **kw}


def test_ollama_thinking_switched_off(server):
    bodies = []

    def gen(req):
        bodies.append(json.loads(req.body))
        return 200, "application/json", json.dumps({"response": '<think>hmm</think>{"ads": []}', "done": True})
    srv = server({"/api/generate": gen})
    assert analyze._ask(_ollama_cfg(srv.url), "p") == {"ads": []}          # visible thinking is removed too
    assert bodies[-1]["think"] is False
    analyze._ask(_ollama_cfg(srv.url, think=True), "p")
    assert "think" not in bodies[-1]                                      # you chose to let it think


def test_ollama_that_refuses_think_is_asked_without_it(server):
    bodies = []

    def gen(req):
        b = json.loads(req.body)
        bodies.append(b)
        if "think" in b:
            return 400, "application/json", json.dumps({"error": "unknown field think"})
        return 200, "application/json", json.dumps({"response": '{"ads": []}', "done": True})
    srv = server({"/api/generate": gen})
    cfg = _ollama_cfg(srv.url, model="old-model")
    assert analyze._ask(cfg, "p") == {"ads": []}
    assert analyze._ask(cfg, "p") == {"ads": []}
    assert ["think" in b for b in bodies] == [True, False, False]           # remembered: asked without it next time


def test_lmstudio_thinking_switched_off(server):
    bodies = []

    def chat(req):
        bodies.append(json.loads(req.body))
        return 200, "application/json", json.dumps({"choices": [{"message": {"content": '{"ads": []}'}}]})
    srv = server({"/v1/chat/completions": chat})
    lmstudio.ask(srv.url, "qwen/qwen3-14b", {}, "s", "prompt", 30)
    assert bodies[-1]["chat_template_kwargs"] == {"enable_thinking": False}
    assert bodies[-1]["messages"][1]["content"].endswith("/no_think")
    lmstudio.ask(srv.url, "google/gemma-4-12b", {}, "s", "prompt", 30)
    assert bodies[-1]["messages"][1]["content"] == "prompt"                  # /no_think is only for Qwen3
    lmstudio.ask(srv.url, "qwen/qwen3-14b", {"think": True}, "s", "prompt", 30)
    assert "chat_template_kwargs" not in bodies[-1]
