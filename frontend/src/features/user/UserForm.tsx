import { useState, type FormEvent } from "react";
import { Field, Spinner } from "../../components/ui";
import { useSupportedRestrictions } from "../../hooks/queries";
import { humanize } from "../../utils/format";
import type { UserCreate, UserResponse } from "../../api/types";

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
      <RestrictionPicker value={restrictions} onChange={setRestrictions} />
      <button type="submit" className="button button-primary" disabled={pending}>
        {pending ? "Saving…" : submitLabel}
      </button>
    </form>
  );
}

/**
 * Offers only the restrictions the backend enforces (GET
 * /users/dietary-restrictions). No free text: a label the backend cannot check
 * from product data would be refused with 422 — or, worse, look enforced when
 * it is not. A stored label that is no longer supported is shown so it can be
 * removed; saving with it still in place is refused by the server.
 */
function RestrictionPicker({
  value,
  onChange,
}: {
  value: string[];
  onChange: (next: string[]) => void;
}) {
  const supported = useSupportedRestrictions();
  const options = supported.data ?? [];
  const unsupported = supported.data ? value.filter((v) => !options.includes(v)) : [];

  const toggle = (label: string, on: boolean) =>
    onChange(on ? [...value, label] : value.filter((v) => v !== label));

  return (
    <fieldset className="field restriction-picker">
      <legend className="field-label">Dietary restrictions</legend>
      {supported.isPending && <Spinner label="Loading supported restrictions…" />}
      {supported.isError && (
        <div className="banner banner-error" role="alert">
          Could not load the supported dietary restrictions, so none can be
          changed right now. {supported.error.message}
        </div>
      )}
      {options.length > 0 && (
        <div className="restriction-options">
          {options.map((label) => (
            <label key={label} className="restriction-option">
              <input
                type="checkbox"
                checked={value.includes(label)}
                onChange={(e) => toggle(label, e.target.checked)}
              />
              {humanize(label)}
            </label>
          ))}
        </div>
      )}
      {unsupported.length > 0 && (
        <div className="banner banner-warning" role="alert">
          <p>
            Not supported, so it cannot be checked against product data. Remove
            it to save:
          </p>
          <div className="chips-row">
            {unsupported.map((label) => (
              <span key={label} className="chip chip-unsupported">
                {humanize(label)}
                <button
                  type="button"
                  className="chip-remove"
                  aria-label={`Remove ${label}`}
                  onClick={() => toggle(label, false)}
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        </div>
      )}
      <span className="field-hint">
        Only restrictions we can check from curated product data are offered.
      </span>
    </fieldset>
  );
}
