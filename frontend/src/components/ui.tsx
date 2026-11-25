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
  tone?: "neutral" | "success" | "warning" | "danger" | "accent" | "sale";
}) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

/**
 * Savings vs. regular shelf prices. Renders nothing when savings is unknown
 * (null) or <= 0. Most flyer deals carry no regular price, so the figure
 * covers only the deals that do: it is a floor, and no percentage is shown
 * because the denominator would include lines with no known regular price.
 */
export function SavingsCallout({ savings }: { savings: Money | null | undefined }) {
  if (savings === null || savings === undefined) return null; // unknown: hide, never "$0"
  if (moneyToNumber(savings) <= 0) return null;

  return (
    <div className="savings-callout" role="status">
      <span className="savings-callout-value">Saves at least {formatMoney(savings)}</span>
      <span className="savings-callout-label">
        against regular shelf prices, counting only deals whose flyer lists one
      </span>
    </div>
  );
}

/**
 * A price set the way a flyer sets it: "$" and cents in superscript beside
 * big dollars. The accessible name stays the plain formatted amount.
 */
export function Price({ value }: { value: Money | null | undefined }) {
  if (value === null || value === undefined) return <span className="price">—</span>;
  const [dollars, cents] = moneyToNumber(value).toFixed(2).split(".");
  return (
    <span className="price" aria-label={formatMoney(value)}>
      <span className="price-cur" aria-hidden="true">$</span>
      <span aria-hidden="true">{dollars}</span>
      <span className="price-cents" aria-hidden="true">{cents}</span>
    </span>
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
