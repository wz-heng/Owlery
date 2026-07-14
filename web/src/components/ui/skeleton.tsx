import { cn } from "../../lib/utils";

/** Loading placeholder — a shimmer on the parchment well.
 *
 * Use this wherever content is *known to be coming* but hasn't arrived, so
 * the layout doesn't pop. It is not for empty states: "still loading" and
 * "there is nothing here" are different claims, and showing an empty state
 * during a fetch tells the user something false.
 *
 * `prefers-reduced-motion` kills the pulse via the global rule in
 * tokens.css; the block itself stays, so the affordance survives.
 */
export function Skeleton({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("animate-pulse rounded-md bg-ink-200", className)}
      {...props}
    />
  );
}
