import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Card,
  EmptyState,
  ErrorBanner,
  Field,
  QueryError,
  SavingsCallout,
  Spinner,
} from "../../components/ui";
import { ChipsInput } from "../../components/ChipsInput";
import { GenerationProgress } from "./GenerationProgress";
import { RecipeCard } from "./RecipeCard";
import { PantryChecklist, TrayCarousel } from "./TrayCarousel";
import type { TrayName } from "../../api/types";
import { useCurrentUser } from "../user/UserContext";
import { useGenerateRecipes, useTrayProposal } from "../../hooks/queries";
import { formatDuration, formatMoney, humanize } from "../../utils/format";

const MEAL_TYPES = ["breakfast", "lunch", "dinner"];
const TRAY_ORDER: TrayName[] = ["anchors", "sides", "stock_up"];

export function PlannerPage() {
  const { user } = useCurrentUser();
  const generate = useGenerateRecipes();

  const [numMeals, setNumMeals] = useState(5);
  const [cuisines, setCuisines] = useState<string[]>([]);
  const [avoid, setAvoid] = useState<string[]>([]);
  const [mealTypes, setMealTypes] = useState<string[]>(["dinner"]);
  // Rejections, not keeps: everything offered starts kept, and an id the
  // server stops offering simply drops out of the kept list.
  const [rejected, setRejected] = useState<Set<number>>(() => new Set());
  const [pantryUnchecked, setPantryUnchecked] = useState<Set<number>>(() => new Set());
  const trays = useTrayProposal(user?.user_id, numMeals);

  const selection = useMemo(() => {
    const proposal = trays.data;
    if (!proposal) return null;
    const kept = (name: TrayName) =>
      proposal.trays[name].filter((t) => !rejected.has(t.deal_id)).map((t) => t.deal_id);
    return {
      anchors: kept("anchors"),
      keptDealIds: TRAY_ORDER.flatMap(kept),
      pantryIds: proposal.pantry
        .filter((p) => !pantryUnchecked.has(p.product_id))
        .map((p) => p.product_id),
    };
  }, [trays.data, rejected, pantryUnchecked]);

  if (!user) {
    return (
      <div className="page">
        <h1>Meal planner</h1>
        <EmptyState
          title="Set up a profile first"
          message="The planner needs your postal code and dietary restrictions."
          action={
            <Link className="button button-primary" to="/setup">
              Set up profile
            </Link>
          }
        />
      </div>
    );
  }

  const toggleMealType = (type: string) =>
    setMealTypes((prev) =>
      prev.includes(type) ? prev.filter((t) => t !== type) : [...prev, type],
    );

  const onGenerate = () =>
    generate.mutate({
      user_id: user.user_id,
      num_meals: numMeals,
      kept_deal_ids: selection?.keptDealIds,
      pantry_product_ids: selection?.pantryIds,
      preferences: {
        ...(cuisines.length && { cuisine_preferences: cuisines.map(humanize) }),
        ...(avoid.length && { avoid_ingredients: avoid }),
        ...(mealTypes.length && { meal_types: mealTypes }),
      },
    });

  const result = generate.data;

  return (
    <div className="page">
      <h1>Meal planner</h1>
      <p className="muted">
        What you can cook from grocery sales near {user.postal_code} · household of{" "}
        {user.household_size}
        {user.dietary_restrictions.length > 0 &&
          ` · ${user.dietary_restrictions.map(humanize).join(", ")}`}
      </p>

      <Card title="Plan settings">
        <div className="form">
          <div className="form-row">
            <Field label={`Meals to plan: ${numMeals}`}>
              <input
                type="range"
                min={1}
                max={21}
                value={numMeals}
                onChange={(e) => setNumMeals(Number(e.target.value))}
              />
            </Field>
            <Field label="Meal types">
              <div className="checkbox-row">
                {MEAL_TYPES.map((type) => (
                  <label key={type} className="toggle">
                    <input
                      type="checkbox"
                      checked={mealTypes.includes(type)}
                      onChange={() => toggleMealType(type)}
                    />
                    {humanize(type)}
                  </label>
                ))}
              </div>
            </Field>
          </div>
          <div className="form-row">
            <Field label="Cuisine preferences" hint="Optional — e.g. italian, asian">
              <ChipsInput value={cuisines} onChange={setCuisines} placeholder="Add a cuisine" />
            </Field>
            <Field label="Avoid ingredients" hint="Optional — e.g. mushrooms">
              <ChipsInput value={avoid} onChange={setAvoid} placeholder="Add an ingredient" />
            </Field>
          </div>
        </div>
      </Card>

      <Card title="1. This week's ingredients">
        <p className="muted">
          Built from this week's flyers near you, after your dietary restrictions. Click a card
          to reject it; the Chef plans only with what you keep.
        </p>
        {trays.isPending && <Spinner label="Sorting this week's deals into trays…" />}
        {trays.isError && <QueryError error={trays.error} stubTitle="Trays unavailable" />}
        {trays.data && (
          <>
            {trays.data.warnings.length > 0 && (
              <ul className="warnings">
                {trays.data.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            )}
            {TRAY_ORDER.map((name) => (
              <TrayCarousel
                key={name}
                name={name}
                items={trays.data.trays[name]}
                rejected={rejected}
                onChange={setRejected}
              />
            ))}
          </>
        )}
      </Card>

      {trays.data && (
        <Card title="2. Your pantry">
          <p className="muted">
            Staples you already have. Recipes may use these at no cost; untick anything you're
            out of.
          </p>
          <PantryChecklist
            pantry={trays.data.pantry}
            unchecked={pantryUnchecked}
            onChange={setPantryUnchecked}
          />
        </Card>
      )}

      <Card title="3. Generate">
        {selection && selection.anchors.length === 0 && (
          <p className="banner banner-warning">
            Keep at least one anchor: every dish is built around one.
          </p>
        )}
        {selection && selection.anchors.length > 0 && selection.anchors.length < numMeals && (
          <p className="muted">
            {selection.anchors.length} anchor(s) for {numMeals} meals: some anchors will serve
            more than one dish.
          </p>
        )}
        <button
          type="button"
          className="button button-primary"
          disabled={generate.isPending || !selection || selection.anchors.length === 0}
          onClick={onGenerate}
        >
          {generate.isPending ? "Generating…" : `Plan ${numMeals} meals`}
        </button>
        {selection && (
          <p className="muted small">
            Using {selection.keptDealIds.length} deals and {selection.pantryIds.length} pantry
            staples.
          </p>
        )}
      </Card>

      {generate.isPending && <GenerationProgress job={generate.job} />}
      {generate.isError && <ErrorBanner error={generate.error} />}

      {result && (
        <>
          <SavingsCallout savings={result.estimated_savings} total={result.total_cost} />
          <Card title="Plan summary">
            <div className="stats-row">
              <Stat label="Total cost" value={formatMoney(result.total_cost)} />
              <Stat label="Per meal" value={formatMoney(result.cost_per_meal)} />
              <Stat label="Generated in" value={formatDuration(result.generation_time)} />
            </div>
            {result.warnings.length > 0 && (
              <ul className="warnings">
                {result.warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            )}
          </Card>
          {result.recipes.map((recipe, i) => (
            <RecipeCard key={`${recipe.name}-${i}`} recipe={recipe} />
          ))}
        </>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat">
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
    </div>
  );
}
