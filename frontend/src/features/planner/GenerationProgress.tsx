import { useEffect, useState } from "react";
import { Card, Spinner } from "../../components/ui";
import { formatDuration } from "../../utils/format";
import type { RecipeGenerationJobStatus } from "../../api/types";

/**
 * Generation runs as a background job; `job` carries the real LangGraph
 * stage and its position in the pipeline, both
 * server-supplied so this list cannot drift from the graph. Until the first
 * poll lands there is no job yet — fall back to the static checklist below.
 * The elapsed timer stays local so it ticks between polls.
 */

const FALLBACK_PIPELINE = [
  "Checking the ingredients you kept against this week's deals",
  "The Chef plans one dish per meal",
  "SousChefs write each dish's recipe",
  "Costing the recipes against this week's deals",
  "Checking each recipe against your dietary restrictions",
  "Saving your meal plan",
];

export function GenerationProgress({ job }: { job?: RecipeGenerationJobStatus }) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const start = Date.now();
    const timer = setInterval(() => setElapsed((Date.now() - start) / 1000), 1000);
    return () => clearInterval(timer);
  }, []);

  const stages = job?.pipeline.length ? job.pipeline : FALLBACK_PIPELINE;
  const currentStep = job?.stage_index ?? 0;
  const queued = job?.state === "pending";

  const label = queued
    ? `${job!.stage} — waiting for a generation slot`
    : job
      ? `${job.stage} (step ${Math.max(currentStep, 1)} of ${job.stage_total})`
      : "Starting generation…";

  return (
    <Card title="Cooking up your plan…">
      <Spinner label={label} />
      <p className="muted">
        Elapsed: {formatDuration(elapsed)} — local models can take a few minutes
      </p>
      <ol className="pipeline">
        {stages.map((stage, i) => {
          const step = i + 1;
          const done = step < currentStep;
          const current = step === currentStep;
          return (
            <li
              key={stage}
              className={done ? "stage-done" : current ? "stage-current" : undefined}
              aria-current={current ? "step" : undefined}
            >
              {stage}
            </li>
          );
        })}
      </ol>
    </Card>
  );
}
