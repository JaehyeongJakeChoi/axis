"""Claude API 도구 호출 루프. 각 에이전트 세션은 이 루프 하나로 돈다."""
import json
import os
from pathlib import Path
from typing import Callable

import anthropic

from . import config, store

# Claude Opus 5.5 단가 (USD / 1M tokens) — 비용 추정용
PRICES = {"claude-opus-5-5": (4.0, 20.0, 0.20), "claude-sonnet-5-5": (2.0, 10.0, 0.20), "claude-haiku-4-5": (1.0, 5.0, 0.10)}


class Done(Exception):
    """submit_* 도구가 성공하면 세션을 끝내기 위해 handler가 던진다."""

    def __init__(self, result):
        self.result = result


def _text(content) -> str:
    return "\n".join(b.text for b in content if getattr(b, "type", None) == "text").strip()


class LLM:
    # 도구 호출 사이의 진행 메모를 받아서 대시보드에 보여준다 (거절되면 끈다)
    use_updates = os.getenv("COIN_DESK_PROGRESS_NOTES", "1") == "1"

    def __init__(self):
        has_profile = os.getenv("ANTHROPIC_PROFILE") or (Path.home() / ".config" / "anthropic").exists()
        if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN") or has_profile):
            raise SystemExit("Claude API 인증 정보가 없습니다. `export ANTHROPIC_API_KEY=...` 후 다시 실행하세요. "
                             "(API 없이 배선만 점검하려면 --mock)")
        self.client = anthropic.Anthropic()
        self.use_fallbacks = config.USE_FALLBACKS

    def _create(self, **kw):
        while True:
            extra, betas = {}, []
            if self.use_fallbacks:
                betas.append("server-side-fallback-2026-07-01")
                extra["fallbacks"] = "default"
            if self.use_updates:
                betas.append("thinking-display-updates-2026-08-18")
                extra["thinking"] = {"type": "adaptive", "display": "updates"}
            try:
                return self.client.beta.messages.create(**kw, **extra, **({"betas": betas} if betas else {}))
            except anthropic.BadRequestError as e:
                msg = str(e).lower()
                if self.use_fallbacks and "fallback" in msg:
                    print("  (서버측 fallback 미지원 — 끄고 재시도)")
                    self.use_fallbacks = False
                elif self.use_updates and ("display" in msg or "thinking-display" in msg):
                    print("  (진행 메모 미지원 — 끄고 재시도)")
                    self.use_updates = False
                else:
                    raise

    def _log_usage(self, label: str, model: str, u):
        cin, cout, cread = PRICES.get(model, (4.0, 20.0, 0.20))
        inp = u.input_tokens or 0
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        out = u.output_tokens or 0
        usd = (inp * cin + cw * cin * 1.25 + cr * cread + out * cout) / 1e6
        store.append_jsonl("usage.jsonl", {"ts": store.now(), "label": label, "model": model, "input": inp,
                                           "cache_write": cw, "cache_read": cr, "output": out, "usd_est": round(usd, 4)})

    def run(self, *, role: str, label: str, system: str, user: str, tools: list,
            handler: Callable[[str, dict], object], max_iters: int = config.MAX_TOOL_ITERS) -> dict:
        """도구 루프. 반환: {"text": 마지막 텍스트, "submitted": submit 도구 결과 또는 None}"""
        model, effort = config.MODELS[role], config.EFFORT[role]
        messages = [{"role": "user", "content": user}]
        last_text = ""
        for i in range(max_iters):
            try:
                resp = self._create(
                    model=model, max_tokens=16000, system=system, tools=tools, messages=messages,
                    output_config={"effort": effort}, cache_control={"type": "ephemeral"})
            except anthropic.RateLimitError as e:
                return {"text": f"[rate limit] {e.message}", "submitted": None}
            except anthropic.APIStatusError as e:
                return {"text": f"[API 오류 {e.status_code}] {e.message}", "submitted": None}
            except anthropic.APIConnectionError:
                return {"text": "[네트워크 오류] Claude API에 연결할 수 없습니다.", "submitted": None}
            self._log_usage(label, model, resp.usage)
            if resp.stop_reason == "refusal":
                cat = getattr(resp.stop_details, "category", None) if resp.stop_details else None
                return {"text": f"[refusal: {cat}]", "submitted": None}
            # 히스토리는 append-only: 응답 content(thinking 블록 포함)를 그대로 되돌려준다
            messages.append({"role": "assistant", "content": resp.content})
            agent_id = label.split(":")[0]
            for b in resp.content:
                note = getattr(b, "thinking", "") if b.type == "thinking" else ""
                if note.strip():
                    store.feed("note", agent_id, "", note.strip())
            last_text = _text(resp.content) or last_text
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                return {"text": last_text, "submitted": None}
            results = []
            for tu in tool_uses:
                print(f"  [{label}] 🔧 {tu.name}")
                try:
                    out = handler(tu.name, dict(tu.input))
                    results.append({"type": "tool_result", "tool_use_id": tu.id,
                                    "content": json.dumps(out, ensure_ascii=False, default=str)})
                except Done as d:
                    return {"text": last_text, "submitted": d.result}
                except Exception as e:  # 도구 오류는 모델에게 돌려줘서 스스로 고치게 한다
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "is_error": True,
                                    "content": f"{type(e).__name__}: {e}"})
            if i == max_iters - 3:
                results.append({"type": "text", "text": "도구 호출이 2회 남았습니다. 지금 바로 최종 제출 도구를 호출하세요."})
            messages.append({"role": "user", "content": results})
        return {"text": last_text + "\n[도구 호출 한도 도달]", "submitted": None}


def usage_summary() -> dict:
    rows = store.read_jsonl("usage.jsonl")
    by = {}
    for r in rows:
        k = r["label"].split(":")[0]
        by[k] = round(by.get(k, 0) + r["usd_est"], 4)
    return {"calls": len(rows), "usd_est_total": round(sum(r["usd_est"] for r in rows), 4), "by_agent": by}
