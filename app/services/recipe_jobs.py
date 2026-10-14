"""
In-process job registry for recipe generation.

Recipe generation runs for minutes with local models, so the HTTP request
that starts it cannot be the request that returns the result (browser and
proxy timeouts). `POST /recipes/generate` therefore creates a job here and
returns a `job_id`; the client polls the status endpoint.

Scope of this v1, stated plainly:

* Jobs live in a module-level dict in the API process. They are lost on
  restart and are invisible to any sibling process, so this is correct only
  for a single-process deployment (one uvicorn worker). Moving to multiple
  workers or surviving restarts means moving this state out of process
  (Redis is already a dependency) — not a change to the HTTP contract.
* Work runs on a small thread pool owned by this module, not a task queue.
  `MAX_CONCURRENT_GENERATIONS` throttles it because the real bottleneck is
  the single local Ollama server; jobs beyond that wait in `pending`.
* There is no auth, so there is no ownership check on a `job_id`. The id is
  an unguessable uuid4, which is the only thing standing between two users'
  jobs. Revisit together with auth.

Progress comes from LangGraph node-completion events (see
`app.main_recipe_generation.run_recipe_generation`'s `progress_callback`):
each finished node advances the job to a label describing the work that is
now in flight, which is what makes "SousChef 2/3 cooking…" possible.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Optional
import logging
import threading
import uuid

logger = logging.getLogger(__name__)

# Job states. "pending" means accepted but not yet picked up by a worker.
PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"

TERMINAL_STATES = frozenset({COMPLETED, FAILED})

# The nominal pipeline, used for the coarse "step N of M" indicator. The
# graph has a retry loop, so a job can revisit step 5 — the index is a
# checklist position, not a monotonic percentage.
PIPELINE_STAGES = (
    "Fetching local deals for your postal code",
    "Chef Orchestrator groups ingredients for reuse",
    "Three SousChefs draft recipes in parallel",
    "Aggregating recipes and costing them against the deal index",
    "Nutritionist validates each recipe",
    "Saving your meal plan",
)

#: Stage shown once a worker picks the job up, before the first node finishes.
INITIAL_STAGE = (PIPELINE_STAGES[0], 1)

#: Stage shown while the job is still queued behind another generation.
QUEUED_STAGE = ("Queued behind another generation", 0)

# LangGraph emits an update when a node *finishes*, so each entry here is
# "what is now running", keyed by the node that just completed. Node names
# must match `app.agents.graph.create_recipe_generation_graph`.
_STAGE_AFTER_NODE: Dict[str, tuple] = {
    "initialize_chef": (PIPELINE_STAGES[1], 2),
    "plan_ingredient_groups": ("SousChef 1/3 cooking…", 3),
    "aggregate_recipes": (PIPELINE_STAGES[4], 5),
    "nutritionist_validate": ("Reviewing the Nutritionist's verdicts", 5),
    "handle_rejections": ("Preparing retries for rejected recipes", 5),
    "retry_generation": ("Nutritionist re-validates the retried recipes", 5),
    "finalize_meal_plan": (PIPELINE_STAGES[5], 6),
    "handle_failure": ("Generation failed", 6),
}

#: The fan-out node runs once per SousChef; progress counts completions.
SOUS_CHEF_NODE = "sous_chef_generate"
SOUS_CHEF_COUNT = 3

#: Terminal jobs retained before the oldest are evicted.
MAX_RETAINED_JOBS = 200

#: Concurrent generations; the local Ollama server is the real bottleneck.
MAX_CONCURRENT_GENERATIONS = 2


@dataclass
class RecipeJob:
    """A single generation run. Mutated only under the registry's lock."""

    job_id: str
    user_id: int
    num_meals: int
    created_at: datetime
    state: str = PENDING
    stage: str = QUEUED_STAGE[0]
    stage_index: int = QUEUED_STAGE[1]
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    # Completed SousChef tasks, for the "n/3 cooking…" label.
    sous_chefs_done: int = field(default=0, repr=False)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def elapsed_seconds(self) -> float:
        """Wall-clock time spent running (or waiting, while still pending)."""
        start = self.started_at or self.created_at
        end = self.finished_at or datetime.now()
        return max((end - start).total_seconds(), 0.0)


def stage_after_node(node: str, sous_chefs_done: int) -> Optional[tuple]:
    """
    Map a finished graph node to the (label, pipeline index) now in flight.

    `sous_chefs_done` is the number of SousChef tasks finished *including*
    this one when `node` is the fan-out node. Returns None for nodes with no
    user-visible meaning, so the caller leaves the current stage alone.
    """
    if node == SOUS_CHEF_NODE:
        if sous_chefs_done >= SOUS_CHEF_COUNT:
            # All three are in; the graph moves on to fan-in + costing.
            return (PIPELINE_STAGES[3], 4)
        return (f"SousChef {sous_chefs_done + 1}/{SOUS_CHEF_COUNT} cooking…", 3)
    return _STAGE_AFTER_NODE.get(node)


class RecipeJobRegistry:
    """Thread-safe registry of in-flight and recently finished jobs."""

    def __init__(
        self,
        max_workers: int = MAX_CONCURRENT_GENERATIONS,
        max_retained: int = MAX_RETAINED_JOBS,
    ):
        self._jobs: Dict[str, RecipeJob] = {}
        self._lock = threading.Lock()
        self._max_retained = max_retained
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="recipe-gen"
        )

    # -- lifecycle ---------------------------------------------------------

    def create(self, user_id: int, num_meals: int) -> RecipeJob:
        """Register a pending job and return it."""
        job = RecipeJob(
            job_id=uuid.uuid4().hex,
            user_id=user_id,
            num_meals=num_meals,
            created_at=datetime.now(),
        )
        with self._lock:
            self._jobs[job.job_id] = job
            self._evict_oldest_terminal()
        return job

    def submit(self, job_id: str, work: Callable[[], None]) -> None:
        """
        Hand `work` to the generation thread pool.

        `work` is responsible for calling `mark_running` / `complete` /
        `fail`; anything it raises is recorded as a job failure so a crashed
        worker can never leave a job stuck in `running`.
        """

        def _guarded() -> None:
            try:
                work()
            except BaseException as exc:  # noqa: BLE001 - last-resort net
                logger.error("Recipe job %s crashed: %s", job_id, exc, exc_info=True)
                self.fail(job_id, f"Recipe generation crashed: {exc}")

        self._executor.submit(_guarded)

    def get(self, job_id: str) -> Optional[RecipeJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def mark_running(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.is_terminal:
                return
            job.state = RUNNING
            job.started_at = datetime.now()
            job.stage, job.stage_index = INITIAL_STAGE

    def report_node(self, job_id: str, node: str) -> None:
        """Advance a running job's stage from a finished graph node."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.is_terminal:
                return
            if node == SOUS_CHEF_NODE:
                job.sous_chefs_done += 1
            stage = stage_after_node(node, job.sous_chefs_done)
            if stage is not None:
                job.stage, job.stage_index = stage

    def complete(self, job_id: str, result: Dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.is_terminal:
                return
            job.state = COMPLETED
            job.stage, job.stage_index = "Meal plan ready", len(PIPELINE_STAGES)
            job.result = result
            job.finished_at = datetime.now()

    def fail(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.is_terminal:
                return
            job.state = FAILED
            job.error = error
            job.finished_at = datetime.now()

    # -- housekeeping ------------------------------------------------------

    def _evict_oldest_terminal(self) -> None:
        """Drop the oldest finished jobs once the registry is over cap.

        Caller holds the lock. Only terminal jobs are evicted — an in-flight
        job is never dropped, so a flood of generations grows the registry
        past the cap rather than losing a running job's result.
        """
        if len(self._jobs) <= self._max_retained:
            return
        terminal = sorted(
            (j for j in self._jobs.values() if j.is_terminal),
            key=lambda j: j.finished_at or j.created_at,
        )
        for job in terminal[: len(self._jobs) - self._max_retained]:
            del self._jobs[job.job_id]


#: Process-wide registry used by the routes.
job_registry = RecipeJobRegistry()
