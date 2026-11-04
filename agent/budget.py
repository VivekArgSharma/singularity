import os
import json

BUDGET_FILE = 'budget_tracker.json'

class RunCounter:
    def __init__(self, filepath=BUDGET_FILE, max_runs=120, warn_runs=100, alert_cost=30.0):
        self.filepath = filepath
        self.max_runs = max_runs
        self.warn_runs = warn_runs
        self.alert_cost = alert_cost
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        return {'total_runs': 0, 'total_steps': 0, 'estimated_cost_usd': 0.0}

    def _save(self):
        with open(self.filepath, 'w') as f:
            json.dump(self.data, f, indent=2)

    def check_can_run(self):
        if self.data['total_runs'] >= self.max_runs:
            raise RuntimeError(
                f"[BUDGET BLOCK] Maximum run limit reached ({self.data['total_runs']}/{self.max_runs}). "
                "Preserve remaining budget for final hidden evaluation runs."
            )
        if self.data['total_runs'] >= self.warn_runs:
            print(f"⚠️ [BUDGET WARNING] Approaching run limit: {self.data['total_runs']}/{self.max_runs} runs completed.")
        if self.data['estimated_cost_usd'] >= self.alert_cost:
            print(f"⚠️ [COST ALERT] Estimated API spend has reached ${self.data['estimated_cost_usd']:.2f}")

    def record_run(self, steps: int, cost_est: float = None):
        self.data['total_runs'] += 1
        self.data['total_steps'] += steps
        # Approx $0.009 per step with prompt caching enabled
        cost = cost_est if cost_est is not None else (steps * 0.009)
        self.data['estimated_cost_usd'] += cost
        self._save()
        return self.data

