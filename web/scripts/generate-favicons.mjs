/**
 * Regenerates the Safari-compatible favicon fallbacks from the owl wax
 * seal SVG (public/favicon.svg — geometry sourced from src/lib/seal.ts,
 * see the comment atop that SVG). Safari does not reliably honour
 * `<link rel="icon" type="image/svg+xml">`; it falls back to probing
 * /favicon.ico and /apple-touch-icon.png, and a 404 on either leaves a
 * stale cached icon (or none) on the tab. This script renders both from
 * the single SVG source of truth so they can never drift from the mark.
 *
 * Outputs (checked into the repo, not generated at build time):
 *   public/favicon.ico          — 16/32/48 px, packed multi-size ICO
 *   public/apple-touch-icon.png — 180x180, opaque parchment background
 *                                 (touch-icon convention forbids alpha)
 *
 *   cd web && node scripts/generate-favicons.mjs
 */
import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { Resvg } from "@resvg/resvg-js";
import pngToIco from "png-to-ico";
import sharp from "sharp";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.join(__dirname, "..", "public");
const SVG_PATH = path.join(PUBLIC_DIR, "favicon.svg");

const PARCHMENT = "#FBF8F1";
const ICO_SIZES = [16, 32, 48];
const TOUCH_ICON_SIZE = 180;
// The seal fills a 32x32 viewBox edge-to-edge; touch icons read better
// with a little breathing room, so render the seal a bit smaller than
// the full canvas and let the parchment background bleed to the edge.
const TOUCH_ICON_SEAL_FRACTION = 0.86;

function renderPng(svg, { width, height, background }) {
  const resvg = new Resvg(svg, {
    fitTo: { mode: "width", value: width },
    background,
  });
  const rendered = resvg.render();
  return { buffer: rendered.asPng(), height: rendered.height, width: rendered.width };
}

async function main() {
  // Strip XML comments before handing the SVG to resvg: the source file's
  // header comment contains a literal "--" (in "--seal-rim"), which is
  // valid in an HTML/SVG comment but rejected by resvg's strict XML
  // comment parser. The comment carries no rendering-relevant content.
  const rawSvg = await readFile(SVG_PATH, "utf8");
  const svg = rawSvg.replace(/<!--[\s\S]*?-->/g, "");

  const icoPngs = ICO_SIZES.map((size) => renderPng(svg, { width: size, height: size }).buffer);
  const icoBuffer = await pngToIco(icoPngs);
  const icoOut = path.join(PUBLIC_DIR, "favicon.ico");
  await writeFile(icoOut, icoBuffer);
  console.log(`wrote ${path.relative(process.cwd(), icoOut)} (${ICO_SIZES.join("/")}px)`);

  // Composite the seal onto an opaque parchment square: render the seal
  // at a fraction of the target size, then paste it centered over a
  // solid parchment canvas so no alpha reaches the final PNG.
  const sealSize = Math.round(TOUCH_ICON_SIZE * TOUCH_ICON_SEAL_FRACTION);
  const seal = renderPng(svg, { width: sealSize, height: sealSize });
  const canvasSvg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${TOUCH_ICON_SIZE}" height="${TOUCH_ICON_SIZE}">` +
    `<rect width="${TOUCH_ICON_SIZE}" height="${TOUCH_ICON_SIZE}" fill="${PARCHMENT}"/>` +
    `<image x="${Math.round((TOUCH_ICON_SIZE - seal.width) / 2)}" y="${Math.round((TOUCH_ICON_SIZE - seal.height) / 2)}" ` +
    `width="${seal.width}" height="${seal.height}" href="data:image/png;base64,${seal.buffer.toString("base64")}"/>` +
    `</svg>`;
  const touchIcon = renderPng(canvasSvg, { width: TOUCH_ICON_SIZE, height: TOUCH_ICON_SIZE, background: PARCHMENT });
  // resvg always rasterizes to RGBA; strip the alpha channel entirely
  // (rather than merely leaving it at 255) to match the Apple touch-icon
  // convention, which expects a true opaque RGB PNG.
  const opaquePng = await sharp(touchIcon.buffer).removeAlpha().png().toBuffer();
  const touchIconOut = path.join(PUBLIC_DIR, "apple-touch-icon.png");
  await writeFile(touchIconOut, opaquePng);
  console.log(`wrote ${path.relative(process.cwd(), touchIconOut)} (${TOUCH_ICON_SIZE}x${TOUCH_ICON_SIZE}px, opaque)`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
