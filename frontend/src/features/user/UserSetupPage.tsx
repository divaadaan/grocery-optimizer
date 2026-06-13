import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Card, ErrorBanner } from "../../components/ui";
import { UserForm } from "./UserForm";
import { useCurrentUser } from "./UserContext";
import { useLoadUser, useRegisterUser, useUpdateUser } from "../../hooks/queries";
import type { UserCreate } from "../../api/types";

export function UserSetupPage() {
  const { user, clearUser } = useCurrentUser();

  return (
    <div className="page">
      <h1>{user ? "Your profile" : "Get started"}</h1>
      {user ? <EditProfile /> : <Onboarding />}
      {user && (
        <button type="button" className="button button-ghost" onClick={clearUser}>
          Switch user
        </button>
      )}
    </div>
  );
}

function Onboarding() {
  const navigate = useNavigate();
  const { setUser } = useCurrentUser();
  const register = useRegisterUser();
  const load = useLoadUser();
  const [loadId, setLoadId] = useState("");

  const onRegister = (data: UserCreate) =>
    register.mutate(data, {
      onSuccess: (created) => {
        setUser(created);
        navigate("/");
      },
    });

  const onLoad = (e: FormEvent) => {
    e.preventDefault();
    const id = Number(loadId);
    if (!Number.isInteger(id) || id <= 0) return;
    load.mutate(id, {
      onSuccess: (loaded) => {
        setUser(loaded);
        navigate("/");
      },
    });
  };

  return (
    <div className="grid-2">
      <Card title="Create a profile">
        {register.isError && <ErrorBanner error={register.error} />}
        <UserForm
          submitLabel="Register"
          pending={register.isPending}
          onSubmit={onRegister}
        />
      </Card>
      <Card title="Returning user?">
        <p className="muted">
          There's no login yet — load your profile by its user ID.
        </p>
        {load.isError && <ErrorBanner error={load.error} />}
        <form className="form" onSubmit={onLoad}>
          <input
            type="number"
            min={1}
            required
            value={loadId}
            onChange={(e) => setLoadId(e.target.value)}
            placeholder="User ID"
          />
          <button type="submit" className="button" disabled={load.isPending}>
            {load.isPending ? "Loading…" : "Load profile"}
          </button>
        </form>
      </Card>
    </div>
  );
}

function EditProfile() {
  const { user, setUser } = useCurrentUser();
  const update = useUpdateUser(user!.user_id);

  const onSave = (data: UserCreate) =>
    update.mutate(
      {
        postal_code: data.postal_code,
        budget: data.budget,
        household_size: data.household_size,
        dietary_restrictions: data.dietary_restrictions,
      },
      { onSuccess: setUser },
    );

  return (
    <Card title={`User #${user!.user_id}`}>
      {update.isError && <ErrorBanner error={update.error} />}
      {update.isSuccess && <div className="banner banner-success">Profile saved.</div>}
      <UserForm
        initialUser={user!}
        submitLabel="Save changes"
        pending={update.isPending}
        onSubmit={onSave}
      />
    </Card>
  );
}
