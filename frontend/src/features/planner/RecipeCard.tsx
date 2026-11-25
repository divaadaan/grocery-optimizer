import { useState } from "react";
import { Badge, Card } from "../../components/ui";
import { formatMoney, humanize } from "../../utils/format";
import type { DietaryCompliance, RecipeInfo } from "../../api/types";

function ComplianceBadge({ compliance }: { compliance: DietaryCompliance }) {
  if (compliance.approved === false || compliance.meets_restrictions === false) {
    return <Badge tone="warning">Check diet</Badge>;
  }
  return <Badge tone="success">Meets your diet</Badge>;
}

export function RecipeCard({ recipe }: { recipe: RecipeInfo }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <Card
      className="recipe-card"
      title={
        <span className="recipe-title">
          {recipe.name}
          <span className="recipe-badges">
            {recipe.dietary_compliance && <ComplianceBadge compliance={recipe.dietary_compliance} />}
            {recipe.meal_type && <Badge tone="accent">{humanize(recipe.meal_type)}</Badge>}
            {recipe.cuisine_type && <Badge>{humanize(recipe.cuisine_type)}</Badge>}
          </span>
        </span>
      }
      actions={
        <button type="button" className="button button-ghost" onClick={() => setExpanded(!expanded)}>
          {expanded ? "Hide details" : "Show details"}
        </button>
      }
    >
      <div className="recipe-meta">
        <span>
          <strong>{formatMoney(recipe.total_cost)}</strong> at sale prices
        </span>
        <span>
          <strong>{recipe.servings}</strong> servings
        </span>
        {recipe.estimated_prep_time != null && (
          <span>
            <strong>{recipe.estimated_prep_time}</strong> min prep
          </span>
        )}
        {recipe.health_score != null && <span>Health score: {String(recipe.health_score)}</span>}
      </div>

      {expanded && (
        <div className="recipe-details">
          {recipe.dietary_compliance?.violations && recipe.dietary_compliance.violations.length > 0 && (
            <ul className="warnings">
              {recipe.dietary_compliance.violations.map((v) => (
                <li key={v}>{v}</li>
              ))}
            </ul>
          )}
          <div>
          <h4>Ingredients</h4>
          <table className="table">
            <thead>
              <tr>
                <th>Ingredient</th>
                <th>Quantity</th>
                <th className="num">Price</th>
              </tr>
            </thead>
            <tbody>
              {recipe.ingredients.map((ing, i) => (
                <tr key={i}>
                  <td>{String(ing.name ?? "—")}</td>
                  <td>
                    {[ing.quantity, ing.unit].filter((v) => v != null && v !== "").join(" ") || "—"}
                  </td>
                  <td className="num">
                    {ing.product_id != null && ing.deal_id == null ? (
                      <span className="pantry-tag">Pantry</span>
                    ) : ing.price != null ? (
                      formatMoney(ing.price)
                    ) : (
                      "—"
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>

          <div>
          <h4>Instructions</h4>
          <ol className="instructions">
            {recipe.instructions.map((step, i) => (
              <li key={i}>{step}</li>
            ))}
          </ol>
          </div>

          {recipe.nutrition_facts && Object.keys(recipe.nutrition_facts).length > 0 && (
            <div className="nutrition">
              <h4>Nutrition</h4>
              <dl className="nutrition-grid">
                {Object.entries(recipe.nutrition_facts).map(([key, value]) => (
                  <div key={key}>
                    <dt>{humanize(key)}</dt>
                    <dd>{String(value)}</dd>
                  </div>
                ))}
              </dl>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}
