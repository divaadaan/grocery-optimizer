import { useState, type KeyboardEvent } from "react";

/**
 * Free-text tag input: Enter or comma adds a chip, click x removes.
 * Used for dietary restrictions, cuisine preferences, avoid-ingredients.
 */
export function ChipsInput({
  value,
  onChange,
  placeholder,
  suggestions = [],
}: {
  value: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  suggestions?: string[];
}) {
  const [draft, setDraft] = useState("");

  const add = (raw: string) => {
    const chip = raw.trim().toLowerCase().replace(/\s+/g, "_");
    if (chip && !value.includes(chip)) onChange([...value, chip]);
    setDraft("");
  };

  const remove = (chip: string) => onChange(value.filter((c) => c !== chip));

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      add(draft);
    } else if (e.key === "Backspace" && !draft && value.length) {
      remove(value[value.length - 1]);
    }
  };

  const available = suggestions.filter((s) => !value.includes(s));

  return (
    <div className="chips-input">
      <div className="chips-row">
        {value.map((chip) => (
          <span key={chip} className="chip">
            {chip.replace(/_/g, " ")}
            <button
              type="button"
              className="chip-remove"
              aria-label={`Remove ${chip}`}
              onClick={() => remove(chip)}
            >
              ×
            </button>
          </span>
        ))}
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          onBlur={() => draft && add(draft)}
          placeholder={value.length ? "" : placeholder}
        />
      </div>
      {available.length > 0 && (
        <div className="chips-suggestions">
          {available.map((s) => (
            <button key={s} type="button" className="chip chip-suggestion" onClick={() => add(s)}>
              + {s.replace(/_/g, " ")}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
