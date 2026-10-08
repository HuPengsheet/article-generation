import json
import os
import time

import requests


class LLM:
    """Provider-neutral Chat Completions interface, configured only on the server."""

    def __init__(self):
        self.base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
        self.key = os.environ.get("LLM_API_KEY", "")
        self.model = os.environ.get("LLM_MODEL", "")

    @property
    def configured(self):
        return bool(self.base and self.key and self.model)

    def complete(self, system, user, json_mode=False):
        if not self.configured:
            raise ValueError(
                "请先在 .env 配置 LLM_BASE_URL、LLM_API_KEY 和 LLM_MODEL，再重启服务。"
            )
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        for attempt in range(3):
            try:
                response = requests.post(
                    self.base + "/chat/completions",
                    json=payload,
                    headers={"Authorization": "Bearer " + self.key},
                    timeout=(15, 180),
                )
                if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                if response.status_code >= 400:
                    raise ValueError(
                        f"模型请求失败（HTTP {response.status_code}），请检查配置、额度及 JSON 模式支持。"
                    )
                choice = response.json()["choices"][0]
                if choice.get("finish_reason") in ("length", "content_filter"):
                    raise ValueError("模型输出被截断或中止，请检查模型输出上限后重试。")
                text = choice["message"]["content"]
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("模型返回了空内容。")
                return json.loads(text) if json_mode else text
            except requests.RequestException:
                if attempt == 2:
                    raise ValueError("模型连接超时或失败，请稍后重试。") from None
                time.sleep(3 * (attempt + 1))
            except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                raise ValueError("模型返回格式无效，请确认接口兼容并支持 JSON 模式。") from None


def validate_filter(result, paper):
    if not isinstance(result, dict) or result.get("id") != paper["id"]:
        raise ValueError("AI 返回的论文 ID 不匹配。")
    if result.get("decision") not in ("keep", "reject", "uncertain"):
        raise ValueError("AI 筛选 decision 无效。")
    score = result.get("relevance_score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 100:
        raise ValueError("AI 相关性分数无效。")
    for key in ("summary_zh", "reason", "limitations"):
        if not isinstance(result.get(key), str):
            raise ValueError(f"AI 缺少有效 {key}。")
    if not isinstance(result.get("topics"), list) or not all(
        isinstance(x, str) for x in result["topics"]
    ):
        raise ValueError("AI topics 无效。")
    evidence = result.get("evidence")
    if not isinstance(evidence, list) or not all(isinstance(x, str) for x in evidence):
        raise ValueError("AI evidence 无效。")
    source = " ".join((paper["title"] + " " + paper.get("abstract", "")).split()).casefold()
    if any(" ".join(quote.split()).casefold() not in source for quote in evidence):
        raise ValueError("AI 给出的证据不在标题或摘要中。")
    return result
