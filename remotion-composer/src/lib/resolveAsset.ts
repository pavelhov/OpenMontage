import { staticFile } from "remotion";

const isRemoteAsset = (src: string): boolean =>
  src.startsWith("http://") ||
  src.startsWith("https://") ||
  src.startsWith("data:");

const isWindowsAbsolutePath = (src: string): boolean =>
  /^[A-Za-z]:[\\/]/.test(src);

/** Resolve public assets and absolute filesystem paths consistently. */
export function resolveAsset(src: string): string {
  if (isRemoteAsset(src)) {
    return src;
  }

  const withoutScheme = src.replace(/^file:\/\//i, "");
  const clean = /^\/[A-Za-z]:[\\/]/.test(withoutScheme)
    ? withoutScheme.slice(1)
    : withoutScheme;

  if (clean.startsWith("/") || isWindowsAbsolutePath(clean)) {
    const posix = clean.replace(/\\/g, "/");
    return posix.startsWith("/") ? `file://${posix}` : `file:///${posix}`;
  }

  // staticFile() paths are already relative to public/, and Remotion throws
  // when the prefix is included. Callers writing props by hand get this wrong
  // (remotion_caption_burn shipped "public/talking-head/..." and every render
  // failed), so strip it here rather than fail at render time.
  return staticFile(clean.replace(/^\.?\/?public\//, ""));
}
