/** Owlery mark — same SVG as the favicon, inlined so we can recolor
 * with `currentColor` instead of being stuck with the navy fill.
 *
 * One path, `evenodd` fill: the eyes and the gap under the beak are holes
 * punched through the silhouette rather than shapes painted in a background
 * color, so the mark reads correctly on any surface (light, dark, or the
 * primary-tinted header). */
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
      <path
        fillRule="evenodd"
        clipRule="evenodd"
        d="M9.6 2.4a1 1 0 0 1 1.5-.9l3.3 1.9a7.7 7.7 0 0 1 3.2 0l3.3-1.9a1 1 0 0 1 1.5.9l-.3 3.9a9.2 9.2 0 0 1 2.5 6.3v6.1c0 4.2-2.9 7.8-6.9 8.9v1.5l2.4 1.4a1 1 0 0 1-.5 1.9h-6.2a1 1 0 0 1-.5-1.9l2.4-1.4v-1.5c-4-1.1-6.9-4.7-6.9-8.9v-6.1c0-2.4.9-4.7 2.5-6.3l-.3-3.9Zm2.8 7.4a4.3 4.3 0 1 0 0 8.6 4.3 4.3 0 0 0 0-8.6Zm7.2 0a4.3 4.3 0 1 0 0 8.6 4.3 4.3 0 0 0 0-8.6ZM12.4 12a2.1 2.1 0 1 1 0 4.2 2.1 2.1 0 0 1 0-4.2Zm7.2 0a2.1 2.1 0 1 1 0 4.2 2.1 2.1 0 0 1 0-4.2ZM16 18.6l1.7 2.5h-3.4l1.7-2.5Z"
      />
    </svg>
  );
}
