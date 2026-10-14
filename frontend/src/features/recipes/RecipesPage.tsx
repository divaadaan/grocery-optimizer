import { Link } from "react-router-dom";
import { EmptyState, QueryError, Spinner } from "../../components/ui";
import { RecipeCard } from "../planner/RecipeCard";
import { useCurrentUser } from "../user/UserContext";
import { useUserRecipes } from "../../hooks/queries";

/**
 * Recipe history: the persisted recipes served by GET /recipes/user/{id}.
 * Unlike the planner (which shows the in-memory result of a live generation),
 * this page surfaces a user's previously saved recipes across sessions.
 */
export function RecipesPage() {
  const { user } = useCurrentUser();
  const recipes = useUserRecipes(user?.user_id);

  if (!user) {
    return (
      <div className="page">
        <h1>Saved recipes</h1>
        <EmptyState
          title="Set up a profile first"
          message="Your saved recipes are tied to your profile."
          action={
            <Link className="button button-primary" to="/setup">
              Set up profile
            </Link>
          }
        />
      </div>
    );
  }

  const count = recipes.data?.length ?? 0;

  return (
    <div className="page">
      <h1>Saved recipes</h1>
      {count > 0 && (
        <p className="muted">
          {count} {count === 1 ? "recipe" : "recipes"} saved for {user.email}
        </p>
      )}
      {recipes.isPending ? (
        <Spinner label="Loading saved recipes…" />
      ) : recipes.isError ? (
        <QueryError error={recipes.error} stubTitle="Saved recipes" />
      ) : recipes.data.length === 0 ? (
        <EmptyState
          title="No saved recipes yet"
          message="Generate a meal plan and your recipes will show up here."
          action={
            <Link className="button button-primary" to="/planner">
              Plan some meals
            </Link>
          }
        />
      ) : (
        recipes.data.map((recipe) => (
          <RecipeCard key={recipe.recipe_id} recipe={recipe} />
        ))
      )}
    </div>
  );
}
