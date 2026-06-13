import { useState } from "react";
import { Link } from "react-router-dom";
import { Card, EmptyState, ErrorBanner, Field } from "../../components/ui";
import { ChipsInput } from "../../components/ChipsInput";
import { GenerationProgress } from "./GenerationProgress";
import { RecipeCard } from "./RecipeCard";
import { useCurrentUser } from "../user/UserContext";
import { useGenerateRecipes } from "../../hooks/queries";
import { formatDuration, formatMoney, humanize } from "../../utils/format";

const MEAL_TYPES = ["breakfast", "lunch", "dinner"];

export function PlannerPage() {
  const { user } = useCurrentUser();
  const generate = useGenerateRecipes();

  const [numMeals, setNumMeals] = useState(5);
  const [cuisines, setCuisines] = useState<string[]>([]);
  const [avoid, setAvoid] = useState<string[]>([]);
  const [mealTypes, setMealTypes] = useState<string[]>(["dinner"]);

  if (!user) {
    return (
      <div className="page">
        <h1>Meal planner</h1>
        <EmptyState
          title="Set up a profile first"
          message="The planner needs your postal code, budget, and dietary restrictions."
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
        Plans for {user.email} · {user.postal_code} · budget {formatMoney(user.budget)} ·
        household of {user.household_size}
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
          <button
            type="button"
            className="button button-primary"
            disabled={generate.isPending}
            onClick={onGenerate}
          >
            {generate.isPending ? "Generating…" : "Generate meal plan"}
          </button>
        </div>
      </Card>

      {generate.isPending && <GenerationProgress />}
      {generate.isError && <ErrorBanner error={generate.error} />}

      {result && (
        <>
          <Card title="Plan summary">
            <div className="stats-row">
              <Stat label="Total cost" value={formatMoney(result.total_cost)} />
              <Stat label="Per meal" value={formatMoney(result.cost_per_meal)} />
              <Stat label="Estimated savings" value={formatMoney(result.estimated_savings)} />
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
