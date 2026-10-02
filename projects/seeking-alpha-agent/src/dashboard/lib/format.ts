export function money(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const abs = Math.abs(v);
  const s = abs >= 1_000_000 ? `$${(abs / 1_000_000).toFixed(2)}M` : abs >= 10_000 ? `$${(abs / 1000).toFixed(1)}K` : `$${abs.toLocaleString("en-US", { maximumFractionDigits: digits, minimumFractionDigits: digits })}`;
  return v < 0 ? `−${s}` : s;
}

export function signedMoney(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return (v > 0 ? "+" : v < 0 ? "−" : "") + money(Math.abs(v), 0);
}

export function r(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(digits)}R`;
}

export function pct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return `${(v * 100).toFixed(digits)} %`;
}

export function num(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return v.toFixed(digits);
}

export function et(isoTs: string | null | undefined, withDate = false): string {
  if (!isoTs) return "—";
  const d = new Date(isoTs);
  if (Number.isNaN(d.getTime())) return isoTs;
  const opts: Intl.DateTimeFormatOptions = withDate
    ? { timeZone: "America/New_York", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }
    : { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false };
  return new Intl.DateTimeFormat("en-US", opts).format(d);
}

export function todayEt(now = new Date()): string {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(now);
  const g = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  return `${g("year")}-${g("month")}-${g("day")}`;
}
