import { useState, type FormEvent } from "react";
import { Field } from "../../components/ui";
import { ChipsInput } from "../../components/ChipsInput";
import type { UserCreate, UserResponse } from "../../api/types";

const COMMON_RESTRICTIONS = [
  "vegetarian",
  "vegan",
  "gluten_free",
  "dairy_free",
  "nut_free",
  "halal",
  "kosher",
  "pescatarian",
];

/**
 * Shared form for register (no initialUser) and profile edit (with one).
 * Email is immutable after registration, matching the backend UserUpdate schema.
 */
export function UserForm({
  initialUser,
  submitLabel,
  pending,
  onSubmit,
}: {
  initialUser?: UserResponse;
  submitLabel: string;
  pending: boolean;
  onSubmit: (data: UserCreate) => void;
}) {
  const [email, setEmail] = useState(initialUser?.email ?? "");
  const [postalCode, setPostalCode] = useState(initialUser?.postal_code ?? "");
  const [budget, setBudget] = useState(String(initialUser?.budget ?? 100));
  const [householdSize, setHouseholdSize] = useState(initialUser?.household_size ?? 1);
  const [restrictions, setRestrictions] = useState<string[]>(
    initialUser?.dietary_restrictions ?? [],
  );

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    onSubmit({
      email,
      postal_code: postalCode,
      budget: Number(budget),
      household_size: householdSize,
      dietary_restrictions: restrictions,
    });
  };

  return (
    <form className="form" onSubmit={handleSubmit}>
      <Field label="Email">
        <input
          type="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          disabled={!!initialUser}
          placeholder="you@example.com"
        />
      </Field>
      <Field label="Postal code" hint="Used to find grocery deals near you">
        <input
          required
          minLength={6}
          maxLength={10}
          value={postalCode}
          onChange={(e) => setPostalCode(e.target.value.toUpperCase())}
          placeholder="M5V 3A8"
        />
      </Field>
      <div className="form-row">
        <Field label="Weekly budget (CAD)">
          <input
            type="number"
            min={0}
            max={10000}
            step="0.01"
            required
            value={budget}
            onChange={(e) => setBudget(e.target.value)}
          />
        </Field>
        <Field label="Household size">
          <input
            type="number"
            min={1}
            max={20}
            required
            value={householdSize}
            onChange={(e) => setHouseholdSize(Number(e.target.value))}
          />
        </Field>
      </div>
      <Field label="Dietary restrictions" hint="Press Enter to add custom entries">
        <ChipsInput
          value={restrictions}
          onChange={setRestrictions}
          suggestions={COMMON_RESTRICTIONS}
          placeholder="e.g. vegetarian"
        />
      </Field>
      <button type="submit" className="button button-primary" disabled={pending}>
        {pending ? "Saving…" : submitLabel}
      </button>
    </form>
  );
}
