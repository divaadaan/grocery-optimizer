import { useEffect, useState } from "react";
import { Card, Spinner } from "../../components/ui";
import { formatDuration } from "../../utils/format";

/**
 * The generate endpoint is synchronous and can take minutes with local
 * models, with no progress signal yet (async job API is on the roadmap —
 * see ROADMAP.md). Until then: show the real pipeline stages as static
 * context plus an honest elapsed timer, not a fake progress bar.
 */

const PIPELINE = [
  "Fetching local deals for your postal code",
  "Chef Orchestrator groups ingredients for reuse across meals",
  "Three SousChefs draft recipes in parallel",
  "Nutritionist validates each recipe (failed ones are retried)",
  "Costs computed against the live deal index",
];

export function GenerationProgress() {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const start = Date.now();
    const timer = setInterval(() => setElapsed((Date.now() - start) / 1000), 1000);
    return () => clearInterval(timer);
  }, []);

  return (
    <Card title="Cooking up your plan…">
      <Spinner label={`Elapsed: ${formatDuration(elapsed)} — local models can take a few minutes`} />
      <ol className="pipeline">
        {PIPELINE.map((stage) => (
          <li key={stage}>{stage}</li>
        ))}
      </ol>
    </Card>
  );
}
