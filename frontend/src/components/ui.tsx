/**
 * Shared UI primitives. Styling lives in styles/global.css under matching
 * class names; keep these components markup-only.
 */

import type { ReactNode } from "react";
import { ApiError } from "../api/client";
import type { Money } from "../api/types";
import { formatMoney, moneyToNumber } from "../utils/format";

export function Card({
  title,
  actions,
  children,
  className = "",
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="card-header">
          {title && <h2 className="card-title">{title}</h2>}
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "success" | "warning" | "danger" | "accent";
}) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

/**
 * Foregrounds the app's core value prop — money saved vs. regular prices.
 * Renders nothing when there's nothing to celebrate (savings ≤ 0). When a
 * `total` (the cost actually paid) is given, also shows the % off regular.
 */
export function SavingsCallout({ savings, total }: { savings: Money; total?: Money }) {
  const amount = moneyToNumber(savings);
  if (amount <= 0) return null;

  const regular = amount + moneyToNumber(total);
  const pct = total !== undefined && regular > 0 ? Math.round((amount / regular) * 100) : null;

  return (
    <div className="savings-callout" role="status">
      <span className="savings-callout-icon" aria-hidden="true">
        💸
      </span>
      <div>
        <span className="savings-callout-value">You save {formatMoney(savings)}</span>
        <span className="savings-callout-label">
          {pct !== null ? `${pct}% off regular shelf prices` : "vs. regular shelf prices"}
        </span>
      </div>
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="spinner-wrap" role="status">
      <span className="spinner" aria-hidden="true" />
      {label && <span className="spinner-label">{label}</span>}
    </span>
  );
}

export function ErrorBanner({ error }: { error: unknown }) {
  const message =
    error instanceof Error ? error.message : "Something went wrong.";
  return (
    <div className="banner banner-error" role="alert">
      {message}
    </div>
  );
}

export function EmptyState({
  title,
  message,
  action,
}: {
  title: string;
  message?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <h3>{title}</h3>
      {message && <p>{message}</p>}
      {action}
    </div>
  );
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

/**
 * Renders a stub-endpoint (501) response as a friendly "coming soon" panel,
 * other errors as a banner. Usage: wraps the error branch of query screens.
 */
export function QueryError({ error, stubTitle }: { error: unknown; stubTitle: string }) {
  if (error instanceof ApiError && error.isNotImplemented) {
    return (
      <EmptyState
        title={`${stubTitle} — coming soon`}
        message={typeof error.detail === "string" ? error.detail : error.message}
      />
    );
  }
  return <ErrorBanner error={error} />;
}
