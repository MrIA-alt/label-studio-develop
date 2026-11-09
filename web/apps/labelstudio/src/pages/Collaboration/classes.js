import { cn } from "../../utils/bem";

// SCSS selectors are namespaced by the native frontend build.
export const campaignClass = (names) =>
  names
    .split(/\s+/)
    .filter(Boolean)
    .map((name) => cn(name).toClassName())
    .join(" ");
