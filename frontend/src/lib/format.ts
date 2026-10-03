const inr = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 0,
  maximumFractionDigits: 2,
});

/** "125000" -> "₹1,25,000" (Indian digit grouping). Money arrives as strings. */
export function formatINR(value: string | number): string {
  const amount = typeof value === "number" ? value : Number(value);
  return Number.isFinite(amount) ? inr.format(amount) : String(value);
}

/** "2.0" -> "2", "1.50" -> "1.5" */
export function formatDays(value: string): string {
  const n = Number(value);
  return Number.isFinite(n) ? String(n) : value;
}

export function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]!.toUpperCase())
    .join("");
}
