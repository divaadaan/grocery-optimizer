import { useState } from "react";
import { Badge, Card } from "../../components/ui";
import { formatMoney, humanize } from "../../utils/format";
import type { RecipeInfo } from "../../api/types";

export function RecipeCard({ recipe }: { recipe: RecipeInfo }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <Card
      className="recipe-card"
      title={
        <span className="recipe-title">
          {recipe.name}
          {recipe.meal_type && <Badge tone="accent">{humanize(recipe.meal_type)}</Badge>}
          {recipe.cuisine_type && <Badge>{humanize(recipe.cuisine_type)}</Badge>}
        </span>
      }
      actions={
        <button type="button" className="button button-ghost" onClick={() => setExpanded(!expanded)}>
          {expanded ? "Hide details" : "Show details"}
        </button>
      }
    >
      <div className="recipe-meta">
        <span>{formatMoney(recipe.total_cost)}</span>
        <span>{recipe.servings} servings</span>
        {recipe.estimated_prep_time != null && <span>{recipe.estimated_prep_time} min prep</span>}
        {recipe.health_score != null && <span>Health score: {String(recipe.health_score)}</span>}
      </div>

      {expanded && (
        <div className="recipe-details">
          <h4>Ingredients</h4>
          <table className="table">
            <thead>
              <tr>
                <th>Ingredient</th>
                <th>Quantity</th>
                <th>Price</th>
              </tr>
            </thead>
            <tbody>
              {recipe.ingredients.map((ing, i) => (
                <tr key={i}>
                  <td>{String(ing.name ?? "—")}</td>
                  <td>
                    {[ing.quantity, ing.unit].filter((v) => v != null && v !== "").join(" ") || "—"}
                  </td>
                  <td>{ing.price != null ? formatMoney(ing.price) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h4>Instructions</h4>
          <ol className="instructions">
            {recipe.instructions.map((step, i) => (
              <li key={i}>{step}</li>
            ))}
          </ol>

          {recipe.nutrition_facts && Object.keys(recipe.nutrition_facts).length > 0 && (
            <>
              <h4>Nutrition</h4>
              <dl className="nutrition-grid">
                {Object.entries(recipe.nutrition_facts).map(([key, value]) => (
                  <div key={key}>
                    <dt>{humanize(key)}</dt>
                    <dd>{String(value)}</dd>
                  </div>
                ))}
              </dl>
            </>
          )}
        </div>
      )}
    </Card>
  );
}
