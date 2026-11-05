"""Spend guard for the Claude agent. The track gives about $40 of API credit, so every
call's token usage is priced and added to outputs/budget.json; a run will not start
(and stops between steps) once the limit is reached.

Prices are USD per million tokens: (input, output, cache read, cache write).
Override with AGENT_PRICE="in,out,cache_read,cache_write" for another model.
"""
import json
import os
from pathlib import Path

PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}
DEFAULT_LIMIT = float(os.environ.get("AGENT_BUDGET_USD", "35"))   # leaves a margin under $40
ROOT = Path(__file__).resolve().parents[1]


class BudgetExceeded(Exception):
    pass


class Budget:
    def __init__(self, path=None, limit=DEFAULT_LIMIT):
        self.path = Path(path or os.environ.get("AGENT_BUDGET_FILE", ROOT / "outputs" / "budget.json"))
        self.limit = limit
        self.data = {"spent_usd": 0.0, "calls": 0, "runs": 0, "input_tokens": 0, "output_tokens": 0}
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def check(self):
        if self.data["spent_usd"] >= self.limit:
            raise BudgetExceeded(f"budget used: ${self.data['spent_usd']:.2f} of ${self.limit:.2f}")

    def start_run(self):
        self.check()
        self.data["runs"] += 1
        self._save()

    @staticmethod
    def price(model):
        env = os.environ.get("AGENT_PRICE")
        if env:
            return tuple(float(v) for v in env.split(","))
        if model not in PRICES:
            raise BudgetExceeded(f"no price known for {model}; set AGENT_PRICE")
        return PRICES[model]

    def add(self, model, usage):
        p_in, p_out, p_read, p_write = self.price(model)
        get = lambda k: int(getattr(usage, k, 0) or 0)                  # noqa: E731
        cost = (get("input_tokens") * p_in + get("output_tokens") * p_out
                + get("cache_read_input_tokens") * p_read + get("cache_creation_input_tokens") * p_write) / 1e6
        d = self.data
        d["spent_usd"] = round(d["spent_usd"] + cost, 6)
        d["calls"] += 1
        d["input_tokens"] += get("input_tokens") + get("cache_read_input_tokens") + get("cache_creation_input_tokens")
        d["output_tokens"] += get("output_tokens")
        self._save()
        return cost
