import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/** Combine Tailwind class names, merging conflicting utilities sensibly.
 *
 * The standard shadcn `cn` helper.
 * `clsx` collapses falsy values + arrays; `twMerge` resolves conflicts
 * so `cn("px-2", "px-4")` returns `"px-4"` not `"px-2 px-4"`.
 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
