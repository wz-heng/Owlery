/** Owlery mark — the wax seal, with the owl impressed into it.
 *
 * Round 3 (`docs/plans/messenger-form.md` §3): the mark and the shape
 * grammar answer the same question — what is Owlery's geometric motif —
 * so they were chosen together. The mark is not a picture of an owl that
 * happens to sit in a circle; it is *the seal*, the same disc the app
 * stamps on every message bubble, card and dialog. Seeing it in the
 * header and then seeing it on every letter is the entire point.
 *
 * The geometry is shared, not redrawn: `lib/seal.ts` is the single source
 * for the rim and the face, and `--seal-rim` in tokens.css carries the
 * same path to the CSS-only dots. The previous full-body owl silhouette
 * is gone — at 16px its legs, tail and ear tufts turned to mush, which is
 * exactly the size the favicon and the sidebar need.
 *
 * One path, `evenodd` fill: the eyes and beak are holes punched through
 * the wax rather than shapes painted in a background colour, so the mark
 * reads correctly on any surface (parchment, brass, ink).
 */
import { SEAL_MARK_PATH } from "../lib/seal";

export function OwleryLogo({
  size = 18,
  className,
}: {
  size?: number;
  className?: string;
}) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 32 32"
      width={size}
      height={size}
      fill="currentColor"
      className={className}
      aria-hidden
    >
      <path fillRule="evenodd" clipRule="evenodd" d={SEAL_MARK_PATH} />
    </svg>
  );
}
